"""SwitchBot クラウド API を Prometheus テキスト形式で公開する。

## なぜ exporter を挟むのか

SwitchBot API には **1 日 10,000 回**の呼び出し上限がある。ポータルの
トップページから直接叩くと、開くたび・タブの数だけ呼び出しが増え、
センサーが数個あるだけで上限に達する。

そこで取得と公開を分ける。バックグラウンドで低頻度（既定 5 分）に取得して
メモリに持ち、`/metrics` はそのキャッシュを返すだけにする。
これで **API の呼び出し回数はポータルの閲覧回数と無関係**になる。

センサー 5 個・5 分間隔なら 1 日 1,440 回。上限に対して十分な余裕がある。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

API_BASE = "https://api.switch-bot.com"

# 温度を報告するデバイス種別。SwitchBot は種別名を増やしていくので、
# ここに載っていなくても temperature を持っていれば拾う方針にしている
# （この一覧はあくまでログの補助）。
KNOWN_METER_TYPES = {
    "Meter", "MeterPlus", "MeterPro", "MeterPro(CO2)",
    "WoIOSensor", "Hub 2", "Hub2",
}

log = logging.getLogger("switchbot-exporter")


def sign_headers(token: str, secret: str, now_ms: int, nonce: str) -> dict[str, str]:
    """API v1.1 の署名ヘッダを組み立てる。

    署名は HMAC-SHA256(secret, token + t + nonce) を base64 にしたもの。
    t はミリ秒。サーバ側の時刻とずれると弾かれるので、s1 では chrony が
    時刻を合わせていることが前提になる。
    """
    payload = f"{token}{now_ms}{nonce}"
    digest = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    return {
        "Authorization": token,
        "sign": base64.b64encode(digest).decode("utf-8"),
        "nonce": nonce,
        "t": str(now_ms),
        "Content-Type": "application/json; charset=utf8",
    }


class SwitchBotError(Exception):
    pass


class SwitchBotClient:
    def __init__(self, token: str, secret: str, timeout: float = 10.0) -> None:
        self._token = token
        self._secret = secret
        self._client = httpx.Client(base_url=API_BASE, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return sign_headers(
            self._token, self._secret, int(time.time() * 1000), str(uuid.uuid4())
        )

    def _get(self, path: str) -> dict:
        response = self._client.get(path, headers=self._headers())
        if response.status_code != 200:
            raise SwitchBotError(f"HTTP {response.status_code}")

        payload = response.json()
        code = payload.get("statusCode")
        if code != 100:
            # 161: デバイスがオフライン、171: ハブがオフライン、
            # 190: 内部エラー。401 相当は statusCode ではなく HTTP で来る。
            raise SwitchBotError(f"statusCode {code}: {payload.get('message', '')}")
        return payload.get("body") or {}

    def devices(self) -> list[dict]:
        return self._get("/v1.1/devices").get("deviceList") or []

    def status(self, device_id: str) -> dict:
        return self._get(f"/v1.1/devices/{device_id}/status")

    def close(self) -> None:
        self._client.close()


@dataclass
class Reading:
    device_id: str
    device_name: str
    device_type: str
    temperature: float | None = None
    humidity: float | None = None
    battery: float | None = None
    # 取得に成功した時刻（monotonic ではなく壁時計。経過時間の表示に使う）
    fetched_at: float = 0.0


@dataclass
class State:
    """ポーラーとスクレイプの間で共有する状態。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    readings: dict[str, Reading] = field(default_factory=dict)
    # 応答はするが中身が空のセンサー（電池切れ・圏外）。device_id -> device_name
    offline: dict[str, str] = field(default_factory=dict)
    last_success: float = 0.0
    api_errors: int = 0
    up: bool = False


def looks_offline(status: dict) -> bool:
    """温度・湿度・電池がすべて 0 なら、値を持っていないとみなす。

    電池切れや圏外のセンサーに対し、SwitchBot API はエラーではなく
    すべて 0 の状態を返してくる。これをそのまま通すと **0℃ が現在の室温として
    表示される**。実測でも 6 台がこの状態だった。

    真冬の屋外が 0.0℃ になることはあるが、そのとき湿度と電池まで同時に
    ちょうど 0 になることはない。3 つ揃ってのみ「値なし」と判断する。
    """
    return (
        _as_float(status.get("temperature")) == 0
        and _as_float(status.get("humidity")) == 0
        and _as_float(status.get("battery")) == 0
    )


def load_excluded(path: str) -> dict[str, str]:
    """取得対象から外すデバイスを読む。返り値は id -> 名前。

    照合は id で行う。SwitchBot アプリ側で名前を変えても除外が外れないように
    するため、name は参考情報として持つだけにしている。
    """
    try:
        import yaml

        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - 設定ミスで取得全体を止めない
        log.error("除外リストを読めません（全デバイスを対象にします）: %s", exc)
        return {}

    excluded: dict[str, str] = {}
    for entry in data.get("exclude") or []:
        if isinstance(entry, dict) and entry.get("id"):
            excluded[str(entry["id"])] = str(entry.get("name") or entry["id"])
    return excluded


def poll_once(
    client: SwitchBotClient, state: State, excluded: dict[str, str] | None = None
) -> None:
    """デバイス一覧と各センサーの状態を取り込む。

    1 台の失敗で全体を落とさない。オフラインのセンサーがあっても
    他のセンサーの値は出し続ける。
    """
    excluded = excluded or {}
    devices = client.devices()

    for device in devices:
        device_id = device.get("deviceId")
        if not device_id:
            continue

        if device_id in excluded:
            # 状態の問い合わせ自体を行わない。API の消費を減らすため。
            continue

        try:
            status = client.status(device_id)
        except (SwitchBotError, httpx.HTTPError) as exc:
            with state.lock:
                state.api_errors += 1
            log.warning("状態を取得できません %s (%s): %s",
                        device.get("deviceName"), device_id, exc)
            continue

        if "temperature" not in status:
            # 温度を持たないデバイス（プラグ・カーテンなど）は対象外。
            continue

        name = device.get("deviceName") or device_id

        if looks_offline(status):
            with state.lock:
                state.offline[device_id] = name
                # 過去の値も消す。古い値を現在値として出し続けないため。
                state.readings.pop(device_id, None)
            log.warning("値を持っていません（電池切れ・圏外の可能性）: %s", name)
            continue

        reading = Reading(
            device_id=device_id,
            device_name=name,
            device_type=device.get("deviceType") or "",
            temperature=_as_float(status.get("temperature")),
            humidity=_as_float(status.get("humidity")),
            battery=_as_float(status.get("battery")),
            fetched_at=time.time(),
        )
        with state.lock:
            state.readings[device_id] = reading
            state.offline.pop(device_id, None)

    with state.lock:
        state.last_success = time.time()
        state.up = True


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class SwitchBotCollector:
    """キャッシュ済みの値を Prometheus 形式で返す。

    ここでは API を呼ばない。スクレイプのたびに API を叩くと
    回数制限に達するため、この分離は意図的なもの。
    """

    def __init__(self, state: State) -> None:
        self._state = state

    def collect(self):
        with self._state.lock:
            readings = list(self._state.readings.values())
            offline = dict(self._state.offline)
            up = self._state.up
            api_errors = self._state.api_errors

        labels = ["device_id", "device_name", "device_type"]

        temperature = GaugeMetricFamily(
            "switchbot_temperature_celsius", "センサーの温度（摂氏）", labels=labels)
        humidity = GaugeMetricFamily(
            "switchbot_humidity_percent", "センサーの湿度（％）", labels=labels)
        battery = GaugeMetricFamily(
            "switchbot_battery_percent", "センサーの電池残量（％）", labels=labels)
        age = GaugeMetricFamily(
            "switchbot_reading_age_seconds",
            "その値を取得してからの経過秒数。古い値を現在値と誤読しないための指標",
            labels=labels)

        now = time.time()
        for reading in readings:
            values = [reading.device_id, reading.device_name, reading.device_type]
            if reading.temperature is not None:
                temperature.add_metric(values, reading.temperature)
            if reading.humidity is not None:
                humidity.add_metric(values, reading.humidity)
            if reading.battery is not None:
                battery.add_metric(values, reading.battery)
            age.add_metric(values, max(0.0, now - reading.fetched_at))

        yield temperature
        yield humidity
        yield battery
        yield age

        # 値を持たないセンサーは温度の系列に出さない代わりに、ここで見えるようにする。
        # 黙って消すと「センサーが減ったこと」に気づけない。
        offline_metric = GaugeMetricFamily(
            "switchbot_device_offline",
            "応答はするが値を持っていないセンサー（電池切れ・圏外）",
            labels=["device_id", "device_name"])
        for device_id, name in offline.items():
            offline_metric.add_metric([device_id, name], 1.0)
        yield offline_metric

        offline_count = GaugeMetricFamily(
            "switchbot_offline_device_count", "値を持っていないセンサーの台数")
        offline_count.add_metric([], float(len(offline)))
        yield offline_count

        sensor_count = GaugeMetricFamily(
            "switchbot_sensor_count", "値を取得できているセンサーの台数")
        sensor_count.add_metric([], float(len(readings)))
        yield sensor_count

        up_metric = GaugeMetricFamily(
            "switchbot_up", "直近のポーリングが成功したか（1 / 0）")
        up_metric.add_metric([], 1.0 if up else 0.0)
        yield up_metric

        errors = GaugeMetricFamily(
            "switchbot_api_errors_total", "API 呼び出しに失敗した累計回数")
        errors.add_metric([], float(api_errors))
        yield errors


def run_poller(
    client: SwitchBotClient, state: State, interval: float, config_path: str
) -> None:
    while True:
        # 毎周期で読み直す。センサーを外したときにコンテナの再起動が
        # 要らないようにするため（ポータルの services.yml と同じ方針）。
        excluded = load_excluded(config_path)
        try:
            poll_once(client, state, excluded)
            log.info(
                "取得しました（センサー %d 台 / 除外 %d 台）",
                len(state.readings), len(excluded),
            )
        except (SwitchBotError, httpx.HTTPError) as exc:
            with state.lock:
                state.api_errors += 1
                state.up = False
            # 直前の値は捨てない。ポータル側は経過秒数で古さを判断できる。
            log.error("ポーリングに失敗しました: %s", exc)
        time.sleep(interval)


def _credentials() -> tuple[str, str]:
    token = os.environ.get("SWITCHBOT_TOKEN", "").strip()
    secret = os.environ.get("SWITCHBOT_SECRET", "").strip()
    if not token or not secret:
        raise SystemExit(
            "SWITCHBOT_TOKEN と SWITCHBOT_SECRET が必要です。"
            ".env に設定してください（SwitchBot アプリ → プロフィール → "
            "設定 → アプリバージョンを 10 回連打 → 開発者向けオプション で発行）。"
        )
    return token, secret


def list_devices() -> None:
    """`make switchbot-devices` から呼ぶ。services.yml に書く材料を出す。

    デバイス名とセンサーの現在値を並べる。これを見ながら
    config/services.yml にカードを登録する。
    """
    client = SwitchBotClient(*_credentials())
    excluded = load_excluded(
        os.environ.get("SWITCHBOT_CONFIG", "/app/config/switchbot.yml")
    )
    try:
        devices = client.devices()
    except (SwitchBotError, httpx.HTTPError) as exc:
        raise SystemExit(f"デバイス一覧を取得できません: {exc}")

    print(f"{'デバイス名':<20} {'種別':<16} {'deviceId':<16} 現在値")
    print("-" * 78)
    found = 0
    for device in devices:
        device_id = device.get("deviceId", "")
        name = device.get("deviceName", "")
        device_type = device.get("deviceType", "")

        if device_id in excluded:
            print(f"{name:<20} {device_type:<16} {device_id:<16} （除外中）")
            continue

        try:
            status = client.status(device_id)
        except (SwitchBotError, httpx.HTTPError) as exc:
            print(f"{name:<20} {device_type:<16} {device_id:<16} 取得できません: {exc}")
            continue

        if "temperature" not in status:
            continue

        found += 1
        print(
            f"{name:<20} {device_type:<16} {device_id:<16} "
            f"{status.get('temperature')}℃ / {status.get('humidity')}% / "
            f"電池 {status.get('battery')}%"
        )

    print("-" * 78)
    print(f"温度を報告するデバイス: {found} 台")
    if found:
        print("\nconfig/services.yml には device_name ラベルでこう書く:")
        print("  labels: {device_name: <上のデバイス名>}")
    client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    token, secret = _credentials()

    interval = float(os.environ.get("SWITCHBOT_POLL_SECONDS", "600"))
    port = int(os.environ.get("SWITCHBOT_PORT", "9110"))
    config_path = os.environ.get("SWITCHBOT_CONFIG", "/app/config/switchbot.yml")

    state = State()
    client = SwitchBotClient(token, secret)
    REGISTRY.register(SwitchBotCollector(state))

    threading.Thread(
        target=run_poller,
        args=(client, state, interval, config_path),
        daemon=True,
    ).start()

    log.info("待ち受け開始 :%d（取得間隔 %.0f 秒）", port, interval)
    start_http_server(port)
    threading.Event().wait()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SwitchBot exporter")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="登録済みデバイスと現在値を一覧表示して終了する",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
    else:
        main()
