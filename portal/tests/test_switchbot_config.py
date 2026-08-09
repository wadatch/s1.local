"""センサーの取得 ON/OFF。

守るべき性質:
  - 切り替えが exporter の読む形式で保存されること
  - 書き込み途中のファイルを exporter に読ませないこと
    （除外が一時的に空になると、全センサーへの問い合わせが走る）
"""

import pytest
import yaml

from app import switchbot_config


@pytest.fixture(autouse=True)
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "switchbot.yml"
    monkeypatch.setenv("SWITCHBOT_CONFIG", str(path))
    return path


def read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


FULL_CONFIG = """\
homes:
  - 南棟
  - 北棟
  - 苗場
devices:
  AAA: {home: 南棟, area: 外部}
  BBB: {home: 南棟, area: 2階, room: 寝室}
  CCC: {home: 北棟, area: 1階, room: リビング}
exclude:
  - id: CCC
    name: 壊れたやつ
    reason: 電池切れ
"""


def test_ファイルが無ければ除外なし():
    assert switchbot_config.load_excluded() == {}


# --- ホームとルーム --------------------------------------------------------

def test_ホームの並び順を読める(config_path):
    config_path.write_text(FULL_CONFIG, encoding="utf-8")
    assert switchbot_config.load_homes() == ["南棟", "北棟", "苗場"]


def test_デバイスの所属を読める(config_path):
    config_path.write_text(FULL_CONFIG, encoding="utf-8")
    assert switchbot_config.load_device_meta() == {
        "AAA": {"home": "南棟", "area": "外部", "room": ""},
        "BBB": {"home": "南棟", "area": "2階", "room": "寝室"},
        "CCC": {"home": "北棟", "area": "1階", "room": "リビング"},
    }


def test_エリアの順は設定に書いた順(config_path):
    """順番を別に書かせると、デバイスを足すたびに 2 か所直すことになる。"""
    config_path.write_text(FULL_CONFIG, encoding="utf-8")
    assert switchbot_config.load_area_order() == {
        "南棟": ["外部", "2階"],
        "北棟": ["1階"],
    }


def test_トグルしてもホームと所属が消えない(config_path):
    """ここを取りこぼすと、トグルを 1 回押しただけで
    全台ぶんの対応づけが消える。"""
    config_path.write_text(FULL_CONFIG, encoding="utf-8")

    switchbot_config.set_enabled("AAA", "ベランダ", False)

    assert switchbot_config.load_homes() == ["南棟", "北棟", "苗場"]
    assert switchbot_config.load_device_meta()["BBB"] == {
        "home": "南棟", "area": "2階", "room": "寝室"
    }
    assert switchbot_config.load_area_order()["南棟"] == ["外部", "2階"]
    assert set(switchbot_config.load_excluded()) == {"AAA", "CCC"}


def test_まとめて切り替えられる(config_path):
    config_path.write_text(FULL_CONFIG, encoding="utf-8")

    switchbot_config.set_many_enabled({"AAA": "一台目", "BBB": "二台目"}, False)
    assert set(switchbot_config.load_excluded()) == {"AAA", "BBB", "CCC"}

    switchbot_config.set_many_enabled({"AAA": "一台目", "BBB": "二台目"}, True)
    assert set(switchbot_config.load_excluded()) == {"CCC"}


def test_まとめて切り替えは書き戻しが一回だけ(config_path, monkeypatch):
    """1 台ずつ書くと、その間に exporter が中途半端な状態を読む。"""
    config_path.write_text(FULL_CONFIG, encoding="utf-8")

    calls = []
    real_replace = switchbot_config.os.replace
    monkeypatch.setattr(
        switchbot_config.os, "replace",
        lambda src, dst: (calls.append(1), real_replace(src, dst))[1],
    )

    switchbot_config.set_many_enabled(
        {"AAA": "1", "BBB": "2", "DDD": "3", "EEE": "4"}, False
    )
    assert len(calls) == 1


def test_OFF_にすると除外に加わる(config_path):
    switchbot_config.set_enabled("AAA", "リビング", False)

    data = read(config_path)
    assert data["exclude"] == [
        {"id": "AAA", "name": "リビング", "reason": "画面から取得を停止"}
    ]


def test_ON_に戻すと除外から消える(config_path):
    switchbot_config.set_enabled("AAA", "リビング", False)
    switchbot_config.set_enabled("AAA", "リビング", True)

    assert read(config_path)["exclude"] == []
    assert switchbot_config.load_excluded() == {}


def test_他のセンサーを巻き込まない(config_path):
    switchbot_config.set_enabled("AAA", "リビング", False)
    switchbot_config.set_enabled("BBB", "寝室", False)
    switchbot_config.set_enabled("AAA", "リビング", True)

    assert set(switchbot_config.load_excluded()) == {"BBB"}


def test_手で書いた理由は書き戻しでも残る(config_path):
    """なぜ止めたかは残す価値がある。"""
    config_path.write_text(
        "exclude:\n"
        "  - id: AAA\n"
        "    name: リビング\n"
        "    reason: 電池切れ。交換したら戻す\n",
        encoding="utf-8",
    )

    switchbot_config.set_enabled("BBB", "寝室", False)

    excluded = switchbot_config.load_excluded()
    assert excluded["AAA"]["reason"] == "電池切れ。交換したら戻す"


def test_保存した内容を_exporter_が読める(config_path):
    """ポータルが書き、exporter が読む。形式が合っていること。"""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    switchbot_config.set_enabled("AAA", "リビング", False)

    exporter_path = (
        Path(__file__).resolve().parents[2] / "switchbot-exporter" / "exporter.py"
    )
    if not exporter_path.exists():
        pytest.skip("exporter.py がイメージに入っていない")

    spec = spec_from_file_location("exporter", exporter_path)
    exporter = module_from_spec(spec)
    spec.loader.exec_module(exporter)

    assert exporter.load_excluded(str(config_path)) == {"AAA": "リビング"}


def test_書き込みは差し替えで行う(config_path, monkeypatch):
    """書き込み途中のファイルを exporter に読ませないこと。
    除外が一時的に空になると、全センサーへの問い合わせが走ってしまう。"""
    switchbot_config.set_enabled("AAA", "リビング", False)

    seen = {}
    real_replace = switchbot_config.os.replace

    def spy(src, dst):
        # 差し替え前の時点で、元のファイルはまだ完全な内容のままであること
        seen["before"] = read(config_path)
        return real_replace(src, dst)

    monkeypatch.setattr(switchbot_config.os, "replace", spy)
    switchbot_config.set_enabled("BBB", "寝室", False)

    assert seen["before"]["exclude"] == [
        {"id": "AAA", "name": "リビング", "reason": "画面から取得を停止"}
    ]


def test_書き戻してもパーミッションが変わらない(config_path):
    """ポータルは root で動くので、これを守らないと書き戻したファイルが
    root 所有 0600 になり、rsync 配備も手編集もできなくなる。"""
    config_path.write_text("exclude: []\n", encoding="utf-8")
    config_path.chmod(0o644)

    switchbot_config.set_enabled("AAA", "リビング", False)

    assert config_path.stat().st_mode & 0o777 == 0o644


def test_ファイルが無い場合は誰でも読める権限で作る(config_path):
    assert not config_path.exists()
    switchbot_config.set_enabled("AAA", "リビング", False)
    assert config_path.stat().st_mode & 0o777 == 0o644


def test_壊れたファイルでも画面は開ける(config_path):
    config_path.write_text("exclude: [\n", encoding="utf-8")
    assert switchbot_config.load_excluded() == {}


def test_名前が無くても_id_で保存できる(config_path):
    switchbot_config.set_enabled("AAA", "", False)
    assert read(config_path)["exclude"][0]["name"] == "AAA"
