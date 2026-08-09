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
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.core import GaugeMetricFamily

API_BASE = "https://api.switch-bot.com"

# 手で取得を要求されたときの歯止め。
#
# 取得 1 回でセンサー台数 + 1 回ぶん API を使う。定期取得（10 分ごと）で
# 1 日 2,900 回ほど使っているので、上限 10,000 回に対する余りは 7,000 回ほど。
# それを使い切らないよう、間隔と 1 日の回数の両方で抑える。
MANUAL_MIN_GAP_SECONDS = 60
MANUAL_DAILY_BUDGET = 150

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
    # 取得対象から外しているセンサー。device_id -> device_name
    # ポータルの画面から ON に戻せるよう、これも公開する。
    excluded: dict[str, str] = field(default_factory=dict)
    # 一度でも見かけた温度センサー全部。device_id -> device_name
    #
    # ON に戻した直後のセンサーは、次の取得（最大 10 分後）まで値を持たない。
    # これが無いと**その間だけ一覧から行ごと消える**ため、画面から
    # 存在を見失う。値の有無とは別に「居ること」を覚えておく。
    known: dict[str, str] = field(default_factory=dict)
    last_success: float = 0.0
    api_errors: int = 0
    up: bool = False
    # 取得が 1 回終わるたびに増える。手動要求が「取り終わったか」を
    # 待つのに使う。時刻で待つと、取得が速いときに取りこぼす。
    poll_seq: int = 0
    # 手動要求の歯止め用
    last_poll_at: float = 0.0
    manual_used: int = 0
    manual_window_start: float = 0.0


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
            # 名前はデバイス一覧から取れるので、除外中でも画面に出せる。
            name = device.get("deviceName") or device_id
            with state.lock:
                state.excluded[device_id] = name
                state.known[device_id] = name
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
        with state.lock:
            state.known[device_id] = name

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
            state.excluded.pop(device_id, None)

    with state.lock:
        state.last_success = time.time()
        state.last_poll_at = state.last_success
        state.poll_seq += 1
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
            excluded = dict(self._state.excluded)
            known = dict(self._state.known)
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

        # 一度でも見かけたセンサー全部。値がまだ無いものも画面に出せるようにする。
        # ON に戻した直後の 10 分間、行ごと消えてしまうのを防ぐためのもの。
        known_metric = GaugeMetricFamily(
            "switchbot_device_known",
            "一度でも見かけた温度センサー（値の有無によらない）",
            labels=["device_id", "device_name"])
        for device_id, name in known.items():
            known_metric.add_metric([device_id, name], 1.0)
        yield known_metric

        # 取得を止めているセンサー。画面から ON に戻せるようにするため、
        # 値が無くても存在は公開しておく。
        excluded_metric = GaugeMetricFamily(
            "switchbot_device_excluded",
            "取得対象から外しているセンサー",
            labels=["device_id", "device_name"])
        for device_id, name in excluded.items():
            excluded_metric.add_metric([device_id, name], 1.0)
        yield excluded_metric

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


def apply_exclusions(state: State, excluded: dict[str, str]) -> None:
    """除外リストを今の状態に反映する。

    OFF にしたセンサーの値をその場で消す。次の取得（既定 10 分後）まで
    値が残っていると、画面で OFF にしたのに温度が出たままになり、
    切り替えが効いていないように見えるため。
    """
    with state.lock:
        for device_id, name in excluded.items():
            if device_id in state.readings:
                state.excluded[device_id] = state.readings[device_id].device_name
                del state.readings[device_id]
            elif device_id not in state.excluded:
                state.excluded[device_id] = name
            state.offline.pop(device_id, None)

        # ON に戻されたものは除外一覧から消す。値は次の取得で入る。
        for device_id in list(state.excluded):
            if device_id not in excluded:
                del state.excluded[device_id]


def watch_config(state: State, config_path: str, interval: float = 15.0) -> None:
    """設定ファイルを短い間隔で見て、除外の変更を即座に反映する。

    取得そのものは API の回数制限があるため低頻度だが、
    ON/OFF の切り替えは画面操作なので、待たされると壊れて見える。
    ファイルを読むだけなので頻繁に見ても負荷にならない。
    """
    while True:
        apply_exclusions(state, load_excluded(config_path))
        time.sleep(interval)


def request_manual_poll(
    state: State, wake: threading.Event, now: float | None = None
) -> tuple[bool, str]:
    """画面から「今すぐ取って」と言われたときの受け口。

    受け付けたら True。断るときは理由を返す。断るのは 2 つの場合だけ。

    - 取ったばかり（60 秒以内）… 値は 10 分ごとにしか変わらないので、
      連打しても API を使うだけで得るものがない
    - 1 日の回数を使い切った … 定期取得のぶんを食いつぶさないため
    """
    now = time.time() if now is None else now

    with state.lock:
        if now - state.manual_window_start >= 86400:
            state.manual_window_start = now
            state.manual_used = 0

        if state.last_poll_at and now - state.last_poll_at < MANUAL_MIN_GAP_SECONDS:
            remaining = int(MANUAL_MIN_GAP_SECONDS - (now - state.last_poll_at)) + 1
            return False, f"取得したばかりです（あと {remaining} 秒）"

        if state.manual_used >= MANUAL_DAILY_BUDGET:
            return False, "手動での取得が 1 日の上限に達しました"

        state.manual_used += 1

    wake.set()
    return True, ""


def wait_for_poll(state: State, since: int, timeout: float = 25.0) -> bool:
    """取得が 1 回終わるまで待つ。終わったら True。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state.lock:
            if state.poll_seq != since:
                return True
        time.sleep(0.1)
    return False


def run_poller(
    client: SwitchBotClient,
    state: State,
    interval: float,
    config_path: str,
    wake: threading.Event,
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
                # 失敗も「取得を試みた時刻」として扱う。そうしないと
                # 失敗している間、手動要求が歯止めなしに通ってしまう。
                state.last_poll_at = time.time()
                state.poll_seq += 1
            # 直前の値は捨てない。ポータル側は経過秒数で古さを判断できる。
            log.error("ポーリングに失敗しました: %s", exc)

        # 手で要求されたら待たずに次へ進む。
        wake.wait(interval)
        wake.clear()


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


def make_handler(state: State, wake: threading.Event):
    """/metrics に加えて POST /refresh を受ける。

    prometheus_client の start_http_server は /metrics しか出せないので、
    自前で立てている。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の作法
            if self.path.split("?")[0] == "/metrics":
                self._send(200, generate_latest(REGISTRY), CONTENT_TYPE_LATEST)
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/refresh":
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return

            with state.lock:
                before = state.poll_seq

            accepted, reason = request_manual_poll(state, wake)
            if not accepted:
                # 断っただけで異常ではない。値は今あるものがそのまま使える。
                self._send(
                    429,
                    json.dumps({"triggered": False, "reason": reason},
                               ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return

            completed = wait_for_poll(state, before)
            self._send(
                200,
                json.dumps({"triggered": True, "completed": completed},
                           ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def log_message(self, *args) -> None:
            # 既定では 1 リクエストごとに標準エラーへ出る。うるさいので黙らせる。
            pass

    return Handler


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
    wake = threading.Event()
    REGISTRY.register(SwitchBotCollector(state))

    threading.Thread(
        target=run_poller,
        args=(client, state, interval, config_path, wake),
        daemon=True,
    ).start()

    # 取得とは別に、ON/OFF の切り替えだけを短い間隔で拾う。
    threading.Thread(
        target=watch_config, args=(state, config_path), daemon=True
    ).start()

    log.info("待ち受け開始 :%d（取得間隔 %.0f 秒）", port, interval)
    ThreadingHTTPServer(("", port), make_handler(state, wake)).serve_forever()


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
