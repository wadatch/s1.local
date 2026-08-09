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

    @property
    def battery_level(self) -> str:
        """電池残量の段階。切れる前に気づけるようにする。"""
        if self.battery is None:
            return "none"  # 給電されているデバイス（ハブなど）
        if self.battery <= 10:
            return "crit"
        if self.battery <= 30:
            return "warn"
        return "ok"

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
                sensor = get(sample.labels)
                sensor.offline = True

    # 暑い順。異常に気づきやすいのと、屋外と室内が自然に分かれるため。
    # 値の無いものは末尾へ。
    return sorted(
        sensors.values(),
        key=lambda s: (s.temperature is None, -(s.temperature or 0), s.name),
    )


async def fetch(client: httpx.AsyncClient, endpoint: str) -> tuple[list[Sensor], str]:
    """(センサー一覧, エラーメッセージ) を返す。例外は投げない。"""
    try:
        response = await client.get(f"{endpoint.rstrip('/')}/metrics")
    except httpx.HTTPError as exc:
        return [], f"exporter に接続できません: {type(exc).__name__}"

    if response.status_code != 200:
        return [], f"exporter が HTTP {response.status_code} を返しました"

    try:
        return parse(response.text), ""
    except Exception as exc:  # noqa: BLE001 - パース失敗でページを落とさない
        return [], f"応答を解釈できません: {type(exc).__name__}"
