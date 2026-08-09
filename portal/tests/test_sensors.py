"""センサー一覧。

守るべき性質:
  - センサーを増やしても設定変更なしに一覧へ出ること
  - 古い値・値なしを、現在の室温と読み違えないこと
"""

import httpx
import pytest
import respx

from app.sensors import Sensor, fetch, parse

METRICS_TEXT = """\
# TYPE switchbot_temperature_celsius gauge
switchbot_temperature_celsius{device_id="A",device_name="リビングの温度計",device_type="MeterPro"} 27.8
switchbot_temperature_celsius{device_id="B",device_name="外の温度計",device_type="WoIOSensor"} 31.2
switchbot_temperature_celsius{device_id="C",device_name="寝室のハブ3",device_type="Hub 3"} 24.0
# TYPE switchbot_humidity_percent gauge
switchbot_humidity_percent{device_id="A",device_name="リビングの温度計",device_type="MeterPro"} 63
switchbot_humidity_percent{device_id="B",device_name="外の温度計",device_type="WoIOSensor"} 86
switchbot_humidity_percent{device_id="C",device_name="寝室のハブ3",device_type="Hub 3"} 50
# TYPE switchbot_battery_percent gauge
switchbot_battery_percent{device_id="A",device_name="リビングの温度計",device_type="MeterPro"} 100
switchbot_battery_percent{device_id="B",device_name="外の温度計",device_type="WoIOSensor"} 8
# TYPE switchbot_reading_age_seconds gauge
switchbot_reading_age_seconds{device_id="A",device_name="リビングの温度計",device_type="MeterPro"} 120
switchbot_reading_age_seconds{device_id="B",device_name="外の温度計",device_type="WoIOSensor"} 300
switchbot_reading_age_seconds{device_id="C",device_name="寝室のハブ3",device_type="Hub 3"} 60
# TYPE switchbot_device_offline gauge
switchbot_device_offline{device_id="Z",device_name="寝室温度計"} 1.0
"""


def by_name(sensors: list[Sensor]) -> dict[str, Sensor]:
    return {s.name: s for s in sensors}


def test_全センサーを設定なしで拾う():
    """services.yml に書かなくても一覧に出ること。センサーを増やしたら
    そのまま増えるのが、この一覧ページの存在理由。"""
    sensors = by_name(parse(METRICS_TEXT))
    assert set(sensors) == {"リビングの温度計", "外の温度計", "寝室のハブ3", "寝室温度計"}


def test_温度湿度電池をまとめて持つ():
    sensor = by_name(parse(METRICS_TEXT))["リビングの温度計"]
    assert sensor.temperature == 27.8
    assert sensor.humidity == 63
    assert sensor.battery == 100
    assert sensor.age_seconds == 120
    assert sensor.device_type == "MeterPro"
    assert sensor.offline is False


def test_暑い順に並ぶ():
    names = [s.name for s in parse(METRICS_TEXT)]
    assert names[:3] == ["外の温度計", "リビングの温度計", "寝室のハブ3"]


def test_値なしのセンサーは末尾に置く():
    assert parse(METRICS_TEXT)[-1].name == "寝室温度計"


def test_値なしのセンサーに温度は入らない():
    """0℃ や古い値を現在の室温として出さないこと。"""
    sensor = by_name(parse(METRICS_TEXT))["寝室温度計"]
    assert sensor.offline is True
    assert sensor.temperature is None


def test_給電デバイスは電池を持たない():
    sensor = by_name(parse(METRICS_TEXT))["寝室のハブ3"]
    assert sensor.battery is None
    assert sensor.battery_level == "none"


@pytest.mark.parametrize(
    "battery, expected",
    [(100, "ok"), (31, "ok"), (30, "warn"), (11, "warn"), (10, "crit"), (0, "crit")],
)
def test_電池の段階(battery, expected):
    assert Sensor("x", "x", "x", battery=battery).battery_level == expected


@pytest.mark.parametrize(
    "age, expected",
    [(0, "ok"), (1800, "ok"), (1801, "warn"), (3600, "warn"), (3601, "crit"),
     (None, "unknown")],
)
def test_値の新しさの段階(age, expected):
    """取得が止まったことに気づけること。"""
    assert Sensor("x", "x", "x", age_seconds=age).freshness == expected


def test_電池が少ないセンサーが分かる():
    assert by_name(parse(METRICS_TEXT))["外の温度計"].battery_level == "crit"


# --- 取得 -----------------------------------------------------------------

@respx.mock
async def test_exporter_から取得できる():
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(200, text=METRICS_TEXT)
    )
    async with httpx.AsyncClient() as client:
        sensors, error = await fetch(client, "http://sb:9110")
    assert error == ""
    assert len(sensors) == 4


@respx.mock
async def test_exporter_が落ちていても例外にならない():
    respx.get("http://sb:9110/metrics").mock(side_effect=httpx.ConnectError("x"))
    async with httpx.AsyncClient() as client:
        sensors, error = await fetch(client, "http://sb:9110")
    assert sensors == []
    assert "接続できません" in error


@respx.mock
async def test_壊れた応答でも例外にならない():
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(500, text="oops")
    )
    async with httpx.AsyncClient() as client:
        sensors, error = await fetch(client, "http://sb:9110")
    assert sensors == []
    assert "500" in error
