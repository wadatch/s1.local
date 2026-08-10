"""回線速度 exporter。

守るべき性質:
  - 有線と無線を同じサーバに当てること（差が回線由来だと言えるようにする）
  - 同時に測らないこと（帯域を奪い合って両方低く出る）
  - スクレイプでは測らないこと（ページを開くたびに回線を占有してしまう）
"""

import json
import subprocess

import pytest

from exporter import (
    Measurement,
    SpeedCollector,
    SpeedtestError,
    State,
    build_command,
    measure_all,
    parse_links,
    parse_result,
    run_speedtest,
)


def ookla_json(download_bytes=8_000_000, upload_bytes=1_000_000,
               latency=12.5, jitter=1.5, server_id=7139, server_name="Tsukuba"):
    return json.dumps({
        "type": "result",
        "ping": {"latency": latency, "jitter": jitter},
        "download": {"bandwidth": download_bytes},
        "upload": {"bandwidth": upload_bytes},
        "server": {"id": server_id, "name": server_name},
    })


def fake_runner(stdout="", returncode=0, stderr="", record=None):
    def run(command, **kwargs):
        if record is not None:
            record.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


# --- 経路の指定 ------------------------------------------------------------

def test_経路の指定を読める():
    assert parse_links("wired:eno1,wifi:wlp3s0") == [
        ("wired", "eno1"), ("wifi", "wlp3s0")
    ]


def test_書いた順を保つ():
    """最初のものが基準のサーバを決めるので、順番に意味がある。"""
    assert parse_links("wifi:wlp3s0,wired:eno1")[0] == ("wifi", "wlp3s0")


def test_壊れた指定は飛ばす():
    assert parse_links("wired:eno1, ,こわれてる,wifi:wlp3s0") == [
        ("wired", "eno1"), ("wifi", "wlp3s0")
    ]


def test_空なら空を返す():
    assert parse_links("") == []


# --- コマンドの組み立て ----------------------------------------------------

def test_インターフェースを指定する():
    """これが無いと、無線を測ったつもりで有線を測ってしまう。"""
    assert "--interface=wlp3s0" in build_command("wlp3s0", None, 150)


def test_サーバを指定できる():
    assert "--server-id=7139" in build_command("eno1", 7139, 150)


def test_サーバ未指定なら付けない():
    assert not any(c.startswith("--server-id") for c in build_command("eno1", None, 150))


def test_ライセンス同意を付ける():
    """付けないと対話待ちで止まる。"""
    command = build_command("eno1", None, 150)
    assert "--accept-license" in command and "--accept-gdpr" in command


# --- 結果の読み取り --------------------------------------------------------

def test_バイト毎秒をビット毎秒に直す():
    """Ookla の bandwidth はバイト毎秒。8 倍を忘れると 8 分の 1 の値が出る。"""
    values = parse_result(json.loads(ookla_json(download_bytes=8_264_500)))
    assert values["download_bps"] == pytest.approx(66_116_000)


def test_必要な項目を取り出せる():
    values = parse_result(json.loads(ookla_json()))
    assert values["upload_bps"] == pytest.approx(8_000_000)
    assert values["ping_ms"] == 12.5
    assert values["jitter_ms"] == 1.5
    assert values["server_id"] == 7139
    assert values["server_name"] == "Tsukuba"


def test_項目が欠けていても落ちない():
    values = parse_result({"ping": {}, "download": {}, "upload": {}, "server": {}})
    assert values["download_bps"] is None
    assert values["ping_ms"] is None


def test_測定できる():
    values = run_speedtest("eno1", runner=fake_runner(stdout=ookla_json()))
    assert values["server_id"] == 7139


def test_失敗は例外にする():
    with pytest.raises(SpeedtestError):
        run_speedtest("eno1", runner=fake_runner(returncode=2, stderr="なにか失敗"))


def test_ログ形式の応答も失敗として扱う():
    """CLI は失敗しても終了コード 0 で log を返すことがある。"""
    body = json.dumps({"type": "log", "message": "Cannot open socket"})
    with pytest.raises(SpeedtestError, match="Cannot open socket"):
        run_speedtest("eno1", runner=fake_runner(stdout=body))


def test_JSON_でなければ失敗():
    with pytest.raises(SpeedtestError):
        run_speedtest("eno1", runner=fake_runner(stdout="<html>"))


def test_時間内に終わらなければ失敗():
    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 150)
    with pytest.raises(SpeedtestError, match="時間内"):
        run_speedtest("eno1", runner=timeout_runner)


# --- 全経路の測定 ----------------------------------------------------------

def test_有線と無線を同じサーバに当てる():
    """別々のサーバに当てると、差が回線の違いなのかサーバの違いなのか
    分からなくなる。"""
    commands = []
    state = State()

    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")],
                runner=fake_runner(stdout=ookla_json(server_id=7139), record=commands))

    assert not any(c.startswith("--server-id") for c in commands[0]), \
        "1 回目はサーバを選ばせる"
    assert "--server-id=7139" in commands[1], "2 回目は同じサーバに当てる"


def test_固定したサーバに届かなければ選び直す():
    """経路が違えば届く先も違う。無線から有線で選ばれたサーバに繋がらない、
    が実際に起きた。比べやすさより値が出ることを優先する。"""
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        pinned = any(c.startswith("--server-id") for c in command)
        if "--interface=wlp3s0" in command and pinned:
            return subprocess.CompletedProcess(command, 2, "", "Cannot open socket")
        return subprocess.CompletedProcess(command, 0, ookla_json(server_id=7139), "")

    state = State()
    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")], runner=runner)

    assert state.results["wifi"].ok is True, "選び直して測れること"
    assert state.failures == 0, "選び直して測れたなら失敗として数えない"
    assert len(commands) == 3, "無線は固定ありで 1 回、固定なしで 1 回"
    assert not any(c.startswith("--server-id") for c in commands[2])


def test_選び直しても駄目なら失敗として残す():
    def runner(command, **kwargs):
        if "--interface=wlp3s0" in command:
            return subprocess.CompletedProcess(command, 2, "", "無線が落ちている")
        return subprocess.CompletedProcess(command, 0, ookla_json(), "")

    state = State()
    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")], runner=runner)

    assert state.results["wifi"].ok is False
    assert state.failures == 1


def test_順番に測る():
    """同時に測ると帯域を奪い合って、どちらの数字も本来より低く出る。"""
    order = []

    def runner(command, **kwargs):
        interface = next(c for c in command if c.startswith("--interface="))
        order.append(interface)
        return subprocess.CompletedProcess(command, 0, ookla_json(), "")

    measure_all(State(), [("wired", "eno1"), ("wifi", "wlp3s0")], runner=runner)
    assert order == ["--interface=eno1", "--interface=wlp3s0"]


def test_両方の結果を持つ():
    state = State()
    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")],
                runner=fake_runner(stdout=ookla_json()))
    assert set(state.results) == {"wired", "wifi"}
    assert all(r.ok for r in state.results.values())


def test_片方が失敗しても他方は残る():
    def runner(command, **kwargs):
        if "--interface=wlp3s0" in command:
            return subprocess.CompletedProcess(command, 2, "", "無線が落ちている")
        return subprocess.CompletedProcess(command, 0, ookla_json(), "")

    state = State()
    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")], runner=runner)

    assert state.results["wired"].ok is True
    assert state.results["wifi"].ok is False
    assert state.failures == 1


def test_失敗しても前回の値は捨てない():
    """回線が一時的に不調なだけのことがある。経過時間で古さは分かる。"""
    state = State()
    measure_all(state, [("wifi", "wlp3s0")], runner=fake_runner(stdout=ookla_json()))
    before = state.results["wifi"].download_bps

    measure_all(state, [("wifi", "wlp3s0")], runner=fake_runner(returncode=2))

    assert state.results["wifi"].download_bps == before
    assert state.results["wifi"].ok is False


# --- 公開 ------------------------------------------------------------------

def collect(state):
    return {f.name: f.samples for f in SpeedCollector(state).collect()}


def test_ラベル付きで公開する():
    state = State()
    measure_all(state, [("wired", "eno1"), ("wifi", "wlp3s0")],
                runner=fake_runner(stdout=ookla_json()))

    families = collect(state)
    labels = {tuple(sorted(s.labels.items()))
              for s in families["home_speed_download_bits_per_second"]}
    assert labels == {
        (("interface", "eno1"), ("link", "wired")),
        (("interface", "wlp3s0"), ("link", "wifi")),
    }


def test_経過秒数を出す():
    """古い値を今の速度と誤読しないため。"""
    state = State()
    measure_all(state, [("wired", "eno1")], runner=fake_runner(stdout=ookla_json()))
    age = collect(state)["home_speed_age_seconds"][0].value
    assert 0 <= age < 5


def test_一度も測れていなければ経過秒数を出さない():
    state = State()
    state.results["wired"] = Measurement(link="wired", interface="eno1")
    assert collect(state)["home_speed_age_seconds"] == []


def test_スクレイプでは測らない():
    """スクレイプのたびに測ると、ページを開くたびに回線を占有してしまう。"""
    commands = []
    state = State()
    measure_all(state, [("wired", "eno1")],
                runner=fake_runner(stdout=ookla_json(), record=commands))
    count = len(commands)

    for _ in range(5):
        collect(state)

    assert len(commands) == count, "スクレイプで測定が走っている"
