"""温湿度センサーの一覧。

センサーは 32 台あり、トップページのカードに全部並べると
「一目で分かる」というポータルの目的が崩れる。トップには代表的な数点だけを出し、
全台はこの一覧ページで見る。

services.yml には代表センサーだけを登録すればよく、ここは exporter が
公開している系列を**そのまま全部**拾う。センサーを増やしても設定変更は要らない。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from prometheus_client.parser import text_string_to_metric_families


@dataclass
class Sensor:
    device_id: str
    name: str
    device_type: str
    temperature: float | None = None
    humidity: float | None = None
    battery: float | None = None
    age_seconds: float | None = None
    offline: bool = False
    # 取得を止めているセンサー。値は無いが、画面から ON に戻せるよう一覧に出す。
    excluded: bool = False
    # 所属。API からは取れないので設定で対応づける。
    # home（棟）→ area（階・外部など）→ room（部屋）の 3 階層。
    home: str = ""
    area: str = ""
    room: str = ""

    @property
    def enabled(self) -> bool:
        return not self.excluded

    @property
    def pending(self) -> bool:
        """取得中だがまだ値が入っていない状態。

        ON に戻した直後に起きる。次の取得（最大 10 分後）で値が入る。
        """
        return self.enabled and not self.offline and self.temperature is None

    @property
    def discomfort(self) -> float | None:
        """不快指数（THI）。温度と湿度の両方が要る。

            DI = 0.81T + 0.01H(0.99T - 14.3) + 46.3

        温度だけでは体感が分からない。同じ 28℃ でも湿度 40% と 80% では
        まるで違うので、両方をまとめた 1 つの数字を並べておく。
        """
        if self.temperature is None or self.humidity is None:
            return None
        t, h = self.temperature, self.humidity
        return 0.81 * t + 0.01 * h * (0.99 * t - 14.3) + 46.3

    @property
    def discomfort_level(self) -> str:
        """不快指数の段階。色は温度と同じ向き（青い＝寒い、赤い＝暑い）。"""
        value = self.discomfort
        if value is None:
            return "unknown"
        if value < 55:
            return "cold"
        if value < 60:
            return "cool"
        if value < 75:
            return "comfort"
        if value < 80:
            return "warm"
        if value < 85:
            return "hot"
        return "severe"

    @property
    def discomfort_text(self) -> str:
        """段階の言い換え。数字だけでは何を意味するのか分からないため。"""
        return {
            "cold": "寒い",
            "cool": "肌寒い",
            "comfort": "快適",
            "warm": "やや暑い",
            "hot": "暑くて汗が出る",
            "severe": "暑くてたまらない",
            "unknown": "—",
        }[self.discomfort_level]

    @property
    def temperature_level(self) -> str:
        """温度の段階。色を決めるのに使う。

        一覧を眺めたときに、暑い部屋と寒い部屋が数字を読まずに目に入ること。
        境目は日本の住宅で「快適」とされる 18〜26℃ を中心に置いている。
        """
        if self.temperature is None:
            return "unknown"
        if self.temperature <= 10:
            return "cold"
        if self.temperature <= 18:
            return "cool"
        if self.temperature <= 26:
            return "comfort"
        if self.temperature <= 30:
            return "warm"
        return "hot"

    @property
    def humidity_level(self) -> str:
        """湿度の段階。40〜60% を快適とみなす。

        乾燥側と多湿側で色の向きを変えているので、
        どちらに振れているかが色だけで分かる。
        """
        if self.humidity is None:
            return "unknown"
        if self.humidity <= 30:
            return "dry"
        if self.humidity <= 40:
            return "dryish"
        if self.humidity <= 60:
            return "comfort"
        if self.humidity <= 70:
            return "humidish"
        return "humid"

    @property
    def battery_level(self) -> str:
        """電池残量の段階。色を決めるのに使う。切れる前に気づけるように。"""
        if self.battery is None:
            return "none"  # 給電されているデバイス（ハブなど）
        if self.battery <= 10:
            return "crit"
        if self.battery <= 30:
            return "warn"
        return "ok"

    @property
    def battery_icon(self) -> str:
        """電池のアイコン。色より細かく、残りの目減りが形で分かるようにする。

        色は 3 段階（ok / warn / crit）だが、それだと 100% と 40% が
        同じ見た目になる。減ってきていることに気づけるよう、
        アイコンは 4 段階に分けている。
        """
        if self.battery is None:
            return "charging"   # 給電されているデバイス
        if self.battery <= 10:
            return "warning"
        if self.battery <= 30:
            return "low"
        if self.battery <= 70:
            return "medium"
        return "full"

    @property
    def battery_text(self) -> str:
        """読み上げと吹き出しに使う文言。アイコンだけでは値が分からないため。"""
        if self.battery is None:
            return "給電"
        return f"電池 {self.battery:.0f}%"

    @property
    def freshness(self) -> str:
        """値の新しさ。古い値を現在値と読み違えないための段階。

        取得間隔は既定 10 分なので、30 分を超えていれば取得が
        止まっていると考えてよい。
        """
        if self.age_seconds is None:
            return "unknown"
        if self.age_seconds > 3600:
            return "crit"
        if self.age_seconds > 1800:
            return "warn"
        return "ok"


def parse(text: str) -> list[Sensor]:
    """exporter の /metrics 本文からセンサー一覧を組み立てる。"""
    sensors: dict[str, Sensor] = {}

    def get(labels: dict[str, str]) -> Sensor:
        device_id = labels.get("device_id", "")
        if device_id not in sensors:
            sensors[device_id] = Sensor(
                device_id=device_id,
                name=labels.get("device_name") or device_id,
                device_type=labels.get("device_type", ""),
            )
        return sensors[device_id]

    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == "switchbot_temperature_celsius":
                get(sample.labels).temperature = sample.value
            elif sample.name == "switchbot_humidity_percent":
                get(sample.labels).humidity = sample.value
            elif sample.name == "switchbot_battery_percent":
                get(sample.labels).battery = sample.value
            elif sample.name == "switchbot_reading_age_seconds":
                get(sample.labels).age_seconds = sample.value
            elif sample.name == "switchbot_device_offline":
                get(sample.labels).offline = True
            elif sample.name == "switchbot_device_excluded":
                get(sample.labels).excluded = True
            elif sample.name == "switchbot_device_known":
                # 値がまだ無いセンサーも一覧に出すため、存在だけ登録する。
                get(sample.labels)

    # 暑い順。異常に気づきやすいのと、屋外と室内が自然に分かれるため。
    # 値の無いものと取得を止めているものは末尾へ。
    return sorted(
        sensors.values(),
        key=lambda s: (s.excluded, s.temperature is None, -(s.temperature or 0), s.name),
    )


@dataclass
class HomeGroup:
    """1 つのホームに属するセンサーのまとまり。"""

    name: str
    sensors: list[Sensor]

    @property
    def active(self) -> list[Sensor]:
        return [s for s in self.sensors if s.enabled and not s.offline]

    @property
    def stopped(self) -> list[Sensor]:
        return [s for s in self.sensors if not s.enabled]

    @property
    def all_stopped(self) -> bool:
        """このホーム全体が停止しているか。トグルの向きを決める。"""
        return bool(self.sensors) and all(not s.enabled for s in self.sensors)


def assign(sensors: list[Sensor], meta: dict[str, dict[str, str]]) -> None:
    """設定の対応づけをセンサーに反映する。"""
    for sensor in sensors:
        info = meta.get(sensor.device_id)
        if info:
            sensor.home = info.get("home", "")
            sensor.area = info.get("area", "")
            sensor.room = info.get("room", "")


def group_by_home(
    sensors: list[Sensor],
    homes: list[str],
    area_order: dict[str, list[str]] | None = None,
    unassigned_label: str = "未分類",
) -> list[HomeGroup]:
    """設定に書かれたホームの順で並べる。

    ホームの中はエリア順（設定に書いた順）、同じエリアの中は暑い順。

    設定に無いホームと、対応づけの無いセンサーは末尾にまとめる。
    センサーを増やしたときに、設定を直すまで見えなくなるのを防ぐため。
    """
    area_order = area_order or {}
    buckets: dict[str, list[Sensor]] = {home: [] for home in homes}

    for sensor in sensors:
        key = sensor.home or unassigned_label
        buckets.setdefault(key, []).append(sensor)

    def sort_key(home: str):
        order = area_order.get(home, [])

        def key(sensor: Sensor):
            try:
                area_index = order.index(sensor.area)
            except ValueError:
                # 設定に無いエリアは末尾へ
                area_index = len(order)
            return (
                area_index,
                sensor.area,
                sensor.room,
                sensor.temperature is None,
                -(sensor.temperature or 0),
                sensor.name,
            )

        return key

    known = list(homes)
    others = sorted(k for k in buckets if k not in known)
    return [
        HomeGroup(name, sorted(buckets[name], key=sort_key(name)))
        for name in known + others
        if buckets[name]
    ]


async def fetch(
    client: httpx.AsyncClient,
    endpoint: str,
    meta: dict[str, dict[str, str]] | None = None,
) -> tuple[list[Sensor], str]:
    """(センサー一覧, エラーメッセージ) を返す。例外は投げない。"""
    try:
        response = await client.get(f"{endpoint.rstrip('/')}/metrics")
    except httpx.HTTPError as exc:
        return [], f"exporter に接続できません: {type(exc).__name__}"

    if response.status_code != 200:
        return [], f"exporter が HTTP {response.status_code} を返しました"

    try:
        found = parse(response.text)
    except Exception as exc:  # noqa: BLE001 - パース失敗でページを落とさない
        return [], f"応答を解釈できません: {type(exc).__name__}"

    if meta:
        assign(found, meta)
    return found, ""
