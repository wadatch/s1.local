"""センサー一覧。

守るべき性質:
  - センサーを増やしても設定変更なしに一覧へ出ること
  - 古い値・値なしを、現在の室温と読み違えないこと
"""

import httpx
import pytest
import respx

from app.sensors import Sensor, assign, fetch, group_by_home, parse

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
# TYPE switchbot_device_excluded gauge
switchbot_device_excluded{device_id="Y",device_name="南棟和室温度計"} 1.0
# TYPE switchbot_device_known gauge
switchbot_device_known{device_id="A",device_name="リビングの温度計"} 1.0
switchbot_device_known{device_id="B",device_name="外の温度計"} 1.0
switchbot_device_known{device_id="C",device_name="寝室のハブ3"} 1.0
switchbot_device_known{device_id="Y",device_name="南棟和室温度計"} 1.0
switchbot_device_known{device_id="Z",device_name="寝室温度計"} 1.0
switchbot_device_known{device_id="W",device_name="ON に戻したばかり"} 1.0
"""


def by_name(sensors: list[Sensor]) -> dict[str, Sensor]:
    return {s.name: s for s in sensors}


def test_全センサーを設定なしで拾う():
    """services.yml に書かなくても一覧に出ること。センサーを増やしたら
    そのまま増えるのが、この一覧ページの存在理由。"""
    sensors = by_name(parse(METRICS_TEXT))
    assert set(sensors) == {
        "リビングの温度計", "外の温度計", "寝室のハブ3", "寝室温度計",
        "南棟和室温度計", "ON に戻したばかり",
    }


def test_値がまだ無いセンサーも一覧に出る():
    """ON に戻した直後は次の取得まで値が入らない。その間だけ行ごと
    消えてしまうと、画面から存在を見失う。"""
    sensor = by_name(parse(METRICS_TEXT))["ON に戻したばかり"]
    assert sensor.enabled is True
    assert sensor.offline is False
    assert sensor.temperature is None
    assert sensor.pending is True


def test_値のあるセンサーは取得待ちではない():
    assert by_name(parse(METRICS_TEXT))["外の温度計"].pending is False


def test_止めたセンサーは取得待ちではない():
    assert by_name(parse(METRICS_TEXT))["南棟和室温度計"].pending is False


def test_値なしのセンサーは取得待ちではない():
    """電池切れと、単に取得前なのとは別物として扱う。"""
    assert by_name(parse(METRICS_TEXT))["寝室温度計"].pending is False


def test_取得を止めたセンサーも一覧に出る():
    """出さないと画面から ON に戻せなくなる。"""
    sensor = by_name(parse(METRICS_TEXT))["南棟和室温度計"]
    assert sensor.excluded is True
    assert sensor.enabled is False
    assert sensor.temperature is None


def test_取得を止めたセンサーは末尾に置く():
    assert parse(METRICS_TEXT)[-1].name == "南棟和室温度計"


def test_取得中のセンサーは有効扱い():
    assert by_name(parse(METRICS_TEXT))["外の温度計"].enabled is True


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


def test_値なしのセンサーは値のあるものより後ろ():
    names = [s.name for s in parse(METRICS_TEXT)]
    assert names.index("寝室温度計") > names.index("寝室のハブ3")


def test_値なしのセンサーに温度は入らない():
    """0℃ や古い値を現在の室温として出さないこと。"""
    sensor = by_name(parse(METRICS_TEXT))["寝室温度計"]
    assert sensor.offline is True
    assert sensor.temperature is None


def test_給電デバイスは電池を持たない():
    sensor = by_name(parse(METRICS_TEXT))["寝室のハブ3"]
    assert sensor.battery is None
    assert sensor.battery_level == "none"


# --- 不快指数 --------------------------------------------------------------

def test_不快指数を計算できる():
    """DI = 0.81T + 0.01H(0.99T - 14.3) + 46.3"""
    s = Sensor("x", "x", "x", temperature=28.0, humidity=60.0)
    expected = 0.81 * 28 + 0.01 * 60 * (0.99 * 28 - 14.3) + 46.3
    assert s.discomfort == pytest.approx(expected)
    # 28℃ / 60% は「暑くて汗が出る」の一歩手前
    assert s.discomfort == pytest.approx(77.0, abs=0.1)


def test_同じ温度でも湿度で不快指数が変わる():
    """温度だけでは体感が分からない、というのがこの指標を出す理由。"""
    dry = Sensor("x", "x", "x", temperature=28.0, humidity=40.0).discomfort
    humid = Sensor("x", "x", "x", temperature=28.0, humidity=80.0).discomfort
    assert humid > dry + 4


@pytest.mark.parametrize("temperature, humidity", [(None, 50.0), (25.0, None), (None, None)])
def test_片方でも欠けていれば不快指数は出さない(temperature, humidity):
    s = Sensor("x", "x", "x", temperature=temperature, humidity=humidity)
    assert s.discomfort is None
    assert s.discomfort_level == "unknown"
    assert s.discomfort_text == "—"


@pytest.mark.parametrize(
    "temperature, humidity, expected",
    [
        (0.0, 50.0, "cold"),        # DI 約 46
        (13.0, 50.0, "cool"),       # DI 約 58
        (20.0, 50.0, "comfort"),    # DI 約 65
        (27.0, 60.0, "warm"),       # DI 約 76
        (30.0, 70.0, "hot"),        # DI 約 82
        (35.0, 80.0, "severe"),     # DI 約 90
    ],
)
def test_不快指数の段階(temperature, humidity, expected):
    s = Sensor("x", "x", "x", temperature=temperature, humidity=humidity)
    assert s.discomfort_level == expected


def test_不快指数の言い換え():
    """数字だけでは何を意味するのか分からないため。"""
    s = Sensor("x", "x", "x", temperature=20.0, humidity=50.0)
    assert s.discomfort_text == "快適"


# --- 熱中症リスク ----------------------------------------------------------

def test_暑さ指数を推定できる():
    """WBGT ≒ 0.735T + 0.0374H + 0.00292TH − 4.064（小野・登内 2014）"""
    s = Sensor("x", "x", "x", temperature=30.0, humidity=60.0)
    expected = 0.735 * 30 + 0.0374 * 60 + 0.00292 * 30 * 60 - 4.064
    assert s.wbgt == pytest.approx(expected)


def test_同じ温度でも湿度で熱中症リスクが変わる():
    """温度だけで熱中症を語れないことが、この指標を出す理由。"""
    dry = Sensor("x", "x", "x", temperature=31.0, humidity=30.0)
    humid = Sensor("x", "x", "x", temperature=31.0, humidity=80.0)
    # 同じ 31℃ でも、湿度 30% なら「注意」、80% なら「厳重警戒」
    assert dry.heat_level == "caution"
    assert humid.heat_level == "severe"


@pytest.mark.parametrize("temperature, humidity", [(None, 50.0), (33.0, None), (None, None)])
def test_片方でも欠けていれば熱中症リスクは出さない(temperature, humidity):
    """片方だけで熱中症リスクを名乗ると、外し方が危険な側に出る。"""
    s = Sensor("x", "x", "x", temperature=temperature, humidity=humidity)
    assert s.wbgt is None
    assert s.heat_level == "unknown"
    assert s.heat_icon == "none"


@pytest.mark.parametrize(
    "temperature, humidity, expected",
    [
        (20.0, 50.0, "safe"),       # WBGT 約 15
        (28.0, 50.0, "caution"),    # WBGT 約 23
        (30.0, 70.0, "warn"),       # WBGT 約 27
        (33.0, 70.0, "severe"),     # WBGT 約 30
        (35.0, 70.0, "danger"),     # WBGT 約 31
    ],
)
def test_熱中症リスクの段階(temperature, humidity, expected):
    """区切りは日本生気象学会「日常生活における熱中症予防指針」に合わせる。
    独自基準にすると、外で見聞きする「厳重警戒」と画面が食い違う。"""
    s = Sensor("x", "x", "x", temperature=temperature, humidity=humidity)
    assert s.heat_level == expected


def test_熱中症リスクは段階ごとに形の違うアイコンになる():
    """色だけで区別すると、色を見分けにくい人に何も伝わらない。"""
    levels = ["safe", "caution", "warn", "severe", "danger"]
    icons = {
        Sensor("x", "x", "x", temperature=t, humidity=h).heat_icon
        for t, h in [(20.0, 50.0), (28.0, 50.0), (30.0, 70.0), (33.0, 70.0), (35.0, 70.0)]
    }
    assert len(icons) == len(levels), "段階ごとに別の形であること"
    assert "none" not in icons


def test_熱中症リスクは言葉でも読める():
    """アイコンだけでは意味が伝わらないので、吹き出しと読み上げに文字を残す。"""
    s = Sensor("x", "x", "x", temperature=35.0, humidity=70.0)
    assert "危険" in s.heat_text
    assert "目安" in s.heat_text, "測定値ではなく推定であることを断る"


def test_熱中症の判定がsensors_jsと一致している():
    """自動更新は sensors.js が同じ判定をやり直す。片方だけ直すと、
    30 秒ごとの描き直しの前後でアイコンが変わってしまう。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app/static/sensors.js").read_text(
        encoding="utf-8"
    )

    # 係数（Sensor.wbgt と同じ式であること）
    for coefficient in ["0.735", "0.0374", "0.00292", "4.064"]:
        assert coefficient in js, f"WBGT の係数 {coefficient} が JS 側に無い"

    # 段階の区切り
    for boundary, level in [("21", "safe"), ("25", "caution"), ("28", "warn"), ("31", "severe")]:
        assert f"< {boundary}) return \"{level}\"" in js, f"{level} の区切りが JS 側と違う"


@pytest.mark.parametrize(
    "temperature, expected",
    [
        (-5, "cold"), (10, "cold"),
        (10.1, "cool"), (18, "cool"),
        (18.1, "comfort"), (26, "comfort"),
        (26.1, "warm"), (30, "warm"),
        (30.1, "hot"), (38, "hot"),
        (None, "unknown"),
    ],
)
def test_温度の段階(temperature, expected):
    """数字を読まずに、暑い部屋・寒い部屋が目に入ること。"""
    assert Sensor("x", "x", "x", temperature=temperature).temperature_level == expected


@pytest.mark.parametrize(
    "humidity, expected",
    [
        (0, "dry"), (30, "dry"),
        (31, "dryish"), (40, "dryish"),
        (41, "comfort"), (60, "comfort"),
        (61, "humidish"), (70, "humidish"),
        (71, "humid"), (95, "humid"),
        (None, "unknown"),
    ],
)
def test_湿度の段階(humidity, expected):
    """乾燥側と多湿側で色の向きが変わるので、段階も両側に分ける。"""
    assert Sensor("x", "x", "x", humidity=humidity).humidity_level == expected


@pytest.mark.parametrize(
    "battery, expected",
    [(100, "ok"), (31, "ok"), (30, "warn"), (11, "warn"), (10, "crit"), (0, "crit")],
)
def test_電池の段階(battery, expected):
    assert Sensor("x", "x", "x", battery=battery).battery_level == expected


@pytest.mark.parametrize(
    "battery, expected",
    [
        (100, "full"), (71, "full"),
        (70, "medium"), (31, "medium"),
        (30, "low"), (11, "low"),
        (10, "warning"), (0, "warning"),
        (None, "charging"),
    ],
)
def test_電池のアイコン(battery, expected):
    """色は 3 段階だが、それだと 100% と 40% が同じ見た目になる。
    減ってきていることに気づけるよう、アイコンは 4 段階に分ける。"""
    assert Sensor("x", "x", "x", battery=battery).battery_icon == expected


@pytest.mark.parametrize(
    "battery, expected",
    [(100, "電池 100%"), (60.4, "電池 60%"), (None, "給電")],
)
def test_電池の読み上げ文言(battery, expected):
    """アイコンだけでは値が分からないので、文言も持つ。"""
    assert Sensor("x", "x", "x", battery=battery).battery_text == expected


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


# --- ホームとルーム --------------------------------------------------------
#
# SwitchBot API はホームもルームも返さないので、対応づけは設定で持つ。

META = {
    "A": {"home": "南棟", "area": "1階", "room": "リビング"},
    "B": {"home": "南棟", "area": "外部", "room": ""},
    "C": {"home": "北棟", "area": "2階", "room": "寝室"},
    "Y": {"home": "苗場", "area": "トイレ", "room": ""},
    # "Z"（寝室温度計）は対応づけなし
}

AREA_ORDER = {"南棟": ["外部", "1階"], "北棟": ["2階"], "苗場": ["トイレ"]}


def test_設定からホームとエリアとルームが入る():
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    by = by_name(sensors)
    assert by["リビングの温度計"].home == "南棟"
    assert by["リビングの温度計"].area == "1階"
    assert by["リビングの温度計"].room == "リビング"
    assert by["寝室のハブ3"].home == "北棟"
    assert by["寝室のハブ3"].area == "2階"


def test_ルームは空でもよい():
    """エリア直下にセンサーが付いている場合。"""
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    assert by_name(sensors)["外の温度計"].area == "外部"
    assert by_name(sensors)["外の温度計"].room == ""


def test_対応づけの無いセンサーはホームが空のまま():
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    assert by_name(sensors)["寝室温度計"].home == ""


def test_設定に書いた順でホームが並ぶ():
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    groups = group_by_home(sensors, ["南棟", "北棟", "苗場"], AREA_ORDER)
    assert [g.name for g in groups[:3]] == ["南棟", "北棟", "苗場"]


def test_ホームの中は設定に書いたエリア順():
    """暑い順より先にエリア順。外部（涼しい）を先に書いたらそれが上に来ること。"""
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    group = group_by_home(sensors, ["南棟"], AREA_ORDER)[0]
    assert [s.area for s in group.sensors] == ["外部", "1階"]


def test_設定に無いエリアは末尾():
    sensors = [
        Sensor("A", "知らないエリア", "Meter", temperature=30.0, home="南棟", area="物置"),
        Sensor("B", "1階のやつ", "Meter", temperature=20.0, home="南棟", area="1階"),
    ]
    group = group_by_home(sensors, ["南棟"], {"南棟": ["1階"]})[0]
    assert [s.name for s in group.sensors] == ["1階のやつ", "知らないエリア"]


def test_同じエリアの中は暑い順():
    sensors = [
        Sensor("A", "ぬるい", "Meter", temperature=20.0, home="南棟", area="1階"),
        Sensor("B", "あつい", "Meter", temperature=30.0, home="南棟", area="1階"),
    ]
    group = group_by_home(sensors, ["南棟"], {"南棟": ["1階"]})[0]
    assert [s.name for s in group.sensors] == ["あつい", "ぬるい"]


def test_対応づけの無いセンサーは未分類として末尾に出る():
    """設定を直すまで見えなくなるのを防ぐ。センサーは増えるもの。"""
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    groups = group_by_home(sensors, ["南棟", "北棟", "苗場"], AREA_ORDER)

    assert groups[-1].name == "未分類"
    assert {s.name for s in groups[-1].sensors} == {"寝室温度計", "ON に戻したばかり"}


def test_センサーの居ないホームは出さない():
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    groups = group_by_home(sensors, ["南棟", "北棟", "苗場", "誰も居ない家"], AREA_ORDER)
    assert "誰も居ない家" not in [g.name for g in groups]


def test_設定に無いホームも末尾に出る():
    sensors = parse(METRICS_TEXT)
    assign(sensors, {"A": {"home": "別荘", "area": "居間", "room": ""}})
    groups = group_by_home(sensors, ["南棟"])
    assert "別荘" in [g.name for g in groups]


def test_ホームごとの取得中の台数():
    sensors = parse(METRICS_TEXT)
    assign(sensors, META)
    groups = {g.name: g for g in group_by_home(sensors, ["南棟", "北棟", "苗場"], AREA_ORDER)}

    # 南棟には A（リビングの温度計）と B（外の温度計）。どちらも値が入っている
    assert len(groups["南棟"].active) == 2
    assert groups["南棟"].all_stopped is False


def test_全台停止しているホームが分かる():
    """トグルの向き（まとめて ON か OFF か）を決めるのに使う。"""
    sensors = [
        Sensor("A", "止めたやつ", "Meter", excluded=True, home="苗場"),
        Sensor("B", "これも", "Meter", excluded=True, home="苗場"),
    ]
    group = group_by_home(sensors, ["苗場"])[0]
    assert group.all_stopped is True
    assert {s.name for s in group.stopped} == {"止めたやつ", "これも"}


def test_一台でも動いていればまとめて_OFF_の向き():
    sensors = [
        Sensor("A", "止めたやつ", "Meter", excluded=True, home="苗場"),
        Sensor("B", "動いてる", "Meter", temperature=20.0, home="苗場"),
    ]
    assert group_by_home(sensors, ["苗場"])[0].all_stopped is False


# --- 取得 -----------------------------------------------------------------

@respx.mock
async def test_exporter_から取得できる():
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(200, text=METRICS_TEXT)
    )
    async with httpx.AsyncClient() as client:
        sensors, error = await fetch(client, "http://sb:9110")
    assert error == ""
    assert len(sensors) == 6


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
