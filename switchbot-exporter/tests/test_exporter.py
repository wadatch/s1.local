"""SwitchBot exporter。

守るべき性質:
  - 署名が仕様どおりであること（間違うと 401 で全滅する）
  - 1 台の失敗が他のセンサーを巻き込まないこと
  - スクレイプ時に API を呼ばないこと（呼ぶと回数制限に達する）
"""

import base64
import hashlib
import hmac

import httpx
import pytest
import respx

from exporter import (
    Reading,
    State,
    SwitchBotClient,
    SwitchBotCollector,
    SwitchBotError,
    load_excluded,
    looks_offline,
    poll_once,
    sign_headers,
)

TOKEN = "test-token"
SECRET = "test-secret"


# --- 署名 -----------------------------------------------------------------

def test_署名は_HMAC_SHA256_を_base64_にしたもの():
    headers = sign_headers(TOKEN, SECRET, 1700000000000, "nonce-1")

    expected = base64.b64encode(
        hmac.new(
            SECRET.encode(), f"{TOKEN}1700000000000nonce-1".encode(), hashlib.sha256
        ).digest()
    ).decode()

    assert headers["sign"] == expected
    assert headers["Authorization"] == TOKEN
    assert headers["nonce"] == "nonce-1"
    assert headers["t"] == "1700000000000"


def test_nonce_が変われば署名も変わる():
    a = sign_headers(TOKEN, SECRET, 1700000000000, "a")["sign"]
    b = sign_headers(TOKEN, SECRET, 1700000000000, "b")["sign"]
    assert a != b


# --- API 応答の扱い --------------------------------------------------------

DEVICES_BODY = {
    "statusCode": 100,
    "body": {
        "deviceList": [
            {"deviceId": "AAA", "deviceName": "リビング", "deviceType": "MeterPlus"},
            {"deviceId": "BBB", "deviceName": "寝室", "deviceType": "Meter"},
            {"deviceId": "CCC", "deviceName": "玄関の照明", "deviceType": "Plug"},
        ],
        "infraredRemoteList": [],
    },
}


def status_body(temperature=None, humidity=None, battery=None):
    body = {"deviceId": "x"}
    if temperature is not None:
        body["temperature"] = temperature
    if humidity is not None:
        body["humidity"] = humidity
    if battery is not None:
        body["battery"] = battery
    return {"statusCode": 100, "body": body}


@respx.mock
def test_温度を持つデバイスだけを取り込む():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(200, json=DEVICES_BODY)
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/AAA/status").mock(
        return_value=httpx.Response(200, json=status_body(24.5, 55, 92))
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/BBB/status").mock(
        return_value=httpx.Response(200, json=status_body(21.0, 48, 88))
    )
    # プラグは温度を持たない
    respx.get("https://api.switch-bot.com/v1.1/devices/CCC/status").mock(
        return_value=httpx.Response(200, json={"statusCode": 100, "body": {"power": "on"}})
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state)

    assert set(state.readings) == {"AAA", "BBB"}, "温度を持つものだけが対象"
    assert state.readings["AAA"].device_name == "リビング"
    assert state.readings["AAA"].temperature == 24.5
    assert state.readings["AAA"].humidity == 55
    assert state.readings["AAA"].battery == 92
    assert state.up is True


@respx.mock
def test_一台がオフラインでも他のセンサーは取り込む():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(200, json=DEVICES_BODY)
    )
    # 161 = デバイスがオフライン
    respx.get("https://api.switch-bot.com/v1.1/devices/AAA/status").mock(
        return_value=httpx.Response(200, json={"statusCode": 161, "message": "offline"})
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/BBB/status").mock(
        return_value=httpx.Response(200, json=status_body(21.0, 48, 88))
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/CCC/status").mock(
        return_value=httpx.Response(200, json={"statusCode": 100, "body": {}})
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state)

    assert set(state.readings) == {"BBB"}, "落ちている 1 台が他を巻き込まないこと"
    assert state.api_errors == 1


@respx.mock
def test_通信エラーも例外として上がる():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(httpx.HTTPError):
        poll_once(SwitchBotClient(TOKEN, SECRET), State())


@respx.mock
def test_認証エラーを検出する():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(SwitchBotError, match="401"):
        poll_once(SwitchBotClient(TOKEN, SECRET), State())


@respx.mock
def test_電池を報告しないデバイスでも温度は取れる():
    """Hub 2 など電源給電のデバイスは battery を返さない。"""
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(
            200,
            json={"statusCode": 100, "body": {"deviceList": [
                {"deviceId": "HUB", "deviceName": "ハブ", "deviceType": "Hub 2"}]}},
        )
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/HUB/status").mock(
        return_value=httpx.Response(200, json=status_body(23.0, 50))
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state)

    assert state.readings["HUB"].temperature == 23.0
    assert state.readings["HUB"].battery is None


# --- 除外リスト ------------------------------------------------------------

def test_除外リストを読める(tmp_path):
    path = tmp_path / "switchbot.yml"
    path.write_text(
        "exclude:\n"
        "  - id: AAA\n"
        "    name: リビング\n"
        "    reason: 電池切れ\n",
        encoding="utf-8",
    )
    assert load_excluded(str(path)) == {"AAA": "リビング"}


def test_除外リストが無ければ全デバイスが対象(tmp_path):
    assert load_excluded(str(tmp_path / "ない.yml")) == {}


def test_壊れた除外リストでも取得は止めない(tmp_path):
    """設定ミスで全センサーが見えなくなるほうが困る。"""
    path = tmp_path / "switchbot.yml"
    path.write_text("exclude: [\n", encoding="utf-8")
    assert load_excluded(str(path)) == {}


def test_id_の無いエントリは無視する(tmp_path):
    path = tmp_path / "switchbot.yml"
    path.write_text("exclude:\n  - name: 名前だけ\n  - id: BBB\n", encoding="utf-8")
    assert load_excluded(str(path)) == {"BBB": "BBB"}


@respx.mock
def test_除外したデバイスは状態を問い合わせない():
    """問い合わせ自体を行わないこと。API の消費を減らすのが目的なので、
    取得してから捨てるのでは意味がない。"""
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(200, json=DEVICES_BODY)
    )
    excluded_route = respx.get(
        "https://api.switch-bot.com/v1.1/devices/AAA/status"
    ).mock(return_value=httpx.Response(200, json=status_body(24.5, 55, 92)))
    respx.get("https://api.switch-bot.com/v1.1/devices/BBB/status").mock(
        return_value=httpx.Response(200, json=status_body(21.0, 48, 88))
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/CCC/status").mock(
        return_value=httpx.Response(200, json={"statusCode": 100, "body": {}})
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state, {"AAA": "リビング"})

    assert excluded_route.call_count == 0, "除外したデバイスに問い合わせている"
    assert set(state.readings) == {"BBB"}
    assert state.offline == {}, "除外は「不調」ではないので計上しないこと"


# --- 値を持たないセンサーの扱い --------------------------------------------
#
# 電池切れ・圏外のセンサーに対し、SwitchBot API はエラーではなく
# すべて 0 の状態を返す。実測で 32 台中 6 台がこの状態だった。
# これを通すと 0℃ が現在の室温として表示される。

def test_全部ゼロなら値なしとみなす():
    assert looks_offline({"temperature": 0, "humidity": 0, "battery": 0})


def test_真冬の屋外_0度は値ありとみなす():
    """0.0℃ はあり得る。湿度と電池まで同時に 0 になることはない。"""
    assert not looks_offline({"temperature": 0, "humidity": 62, "battery": 60})


def test_電池が切れかけていても値があれば通す():
    assert not looks_offline({"temperature": 21.5, "humidity": 50, "battery": 0})


def test_電池を持たないハブは対象外():
    """Hub は battery を返さない。誤って値なし扱いにしないこと。"""
    assert not looks_offline({"temperature": 23.0, "humidity": 50})


@respx.mock
def test_値を持たないセンサーは温度の系列に出さない():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(200, json=DEVICES_BODY)
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/AAA/status").mock(
        return_value=httpx.Response(200, json=status_body(0, 0, 0))
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/BBB/status").mock(
        return_value=httpx.Response(200, json=status_body(21.0, 48, 88))
    )
    respx.get("https://api.switch-bot.com/v1.1/devices/CCC/status").mock(
        return_value=httpx.Response(200, json={"statusCode": 100, "body": {}})
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state)

    assert set(state.readings) == {"BBB"}, "0℃ を現在値として出さないこと"
    assert state.offline == {"AAA": "リビング"}

    families = collect_samples(state)
    names = {s.labels["device_name"] for s in families["switchbot_temperature_celsius"]}
    assert names == {"寝室"}
    assert families["switchbot_offline_device_count"][0].value == 1.0
    assert families["switchbot_sensor_count"][0].value == 1.0
    # 黙って消えるのではなく、オフラインとして見えること
    assert families["switchbot_device_offline"][0].labels["device_name"] == "リビング"


@respx.mock
def test_値を持たなくなったら過去の値も消す():
    """古い値を現在値として出し続けないこと。"""
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(
            200,
            json={"statusCode": 100, "body": {"deviceList": [
                {"deviceId": "AAA", "deviceName": "リビング", "deviceType": "Meter"}]}},
        )
    )
    route = respx.get("https://api.switch-bot.com/v1.1/devices/AAA/status")

    state = State()
    route.mock(return_value=httpx.Response(200, json=status_body(24.5, 55, 92)))
    poll_once(SwitchBotClient(TOKEN, SECRET), state)
    assert state.readings["AAA"].temperature == 24.5

    route.mock(return_value=httpx.Response(200, json=status_body(0, 0, 0)))
    poll_once(SwitchBotClient(TOKEN, SECRET), state)
    assert state.readings == {}
    assert "AAA" in state.offline


@respx.mock
def test_復帰したらオフライン一覧から消える():
    respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(
            200,
            json={"statusCode": 100, "body": {"deviceList": [
                {"deviceId": "AAA", "deviceName": "リビング", "deviceType": "Meter"}]}},
        )
    )
    route = respx.get("https://api.switch-bot.com/v1.1/devices/AAA/status")

    state = State()
    route.mock(return_value=httpx.Response(200, json=status_body(0, 0, 0)))
    poll_once(SwitchBotClient(TOKEN, SECRET), state)
    assert state.offline

    route.mock(return_value=httpx.Response(200, json=status_body(22.0, 50, 80)))
    poll_once(SwitchBotClient(TOKEN, SECRET), state)
    assert state.offline == {}
    assert state.readings["AAA"].temperature == 22.0


# --- Prometheus 形式での公開 ----------------------------------------------

def collect_samples(state: State) -> dict[str, list]:
    families = {f.name: f.samples for f in SwitchBotCollector(state).collect()}
    return families


def test_値を_Prometheus_形式で出せる():
    state = State()
    state.readings["AAA"] = Reading(
        device_id="AAA", device_name="リビング", device_type="MeterPlus",
        temperature=24.5, humidity=55.0, battery=92.0, fetched_at=1000.0,
    )

    families = collect_samples(state)

    temp = families["switchbot_temperature_celsius"][0]
    assert temp.value == 24.5
    assert temp.labels == {
        "device_id": "AAA", "device_name": "リビング", "device_type": "MeterPlus"
    }
    assert families["switchbot_humidity_percent"][0].value == 55.0
    assert families["switchbot_battery_percent"][0].value == 92.0


def test_経過秒数はスクレイプ時点で計算される():
    """古い値を現在値と誤読しないための指標なので、常に今との差でなければならない。"""
    import time

    state = State()
    state.readings["AAA"] = Reading(
        device_id="AAA", device_name="リビング", device_type="Meter",
        temperature=20.0, fetched_at=time.time() - 120,
    )

    age = collect_samples(state)["switchbot_reading_age_seconds"][0].value
    assert 115 < age < 125


def test_値の無い項目は系列に出さない():
    """0 として出すと『電池 0%』のように誤読される。"""
    state = State()
    state.readings["AAA"] = Reading(
        device_id="AAA", device_name="リビング", device_type="Hub 2",
        temperature=23.0, humidity=50.0, battery=None, fetched_at=1000.0,
    )

    families = collect_samples(state)
    assert families["switchbot_battery_percent"] == []
    assert len(families["switchbot_temperature_celsius"]) == 1


@respx.mock
def test_スクレイプでは_API_を呼ばない():
    """呼ぶと閲覧回数だけ API 消費が増え、1 日 10,000 回の上限に達する。"""
    route = respx.get("https://api.switch-bot.com/v1.1/devices").mock(
        return_value=httpx.Response(200, json=DEVICES_BODY)
    )
    respx.get(url__regex=r".*/status").mock(
        return_value=httpx.Response(200, json=status_body(20.0, 40, 50))
    )

    state = State()
    poll_once(SwitchBotClient(TOKEN, SECRET), state)
    calls_after_poll = route.call_count

    for _ in range(10):
        list(SwitchBotCollector(state).collect())

    assert route.call_count == calls_after_poll, "スクレイプで API が呼ばれている"


def test_一度も取得できていなければ_up_は_0():
    families = collect_samples(State())
    assert families["switchbot_up"][0].value == 0.0
