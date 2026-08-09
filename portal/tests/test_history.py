"""温湿度の履歴。

守るべき性質:
  - 同じサンプルを何度読んでも 1 行にしかならないこと
    （exporter は 10 分ごとにしか更新しないのに、ポータルは 2 分ごとに読む）
  - 記録できなくてもポータルが落ちないこと
"""

import sqlite3

import pytest

from app import history
from app.sensors import Sensor


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_DB", str(tmp_path / "history.db"))
    conn = history.connect()
    history.init(conn)
    yield conn
    conn.close()


def sensor(device_id="A", temperature=24.5, humidity=55.0, age=0.0, battery=90.0):
    return Sensor(
        device_id=device_id,
        name=f"センサー{device_id}",
        device_type="Meter",
        temperature=temperature,
        humidity=humidity,
        battery=battery,
        age_seconds=age,
    )


NOW = 1_800_000_000.0


def test_記録して読み出せる(connection):
    history.record(connection, [sensor()], now=NOW)

    series = history.query(connection, ["A"], hours=24, now=NOW)
    assert len(series["A"]) == 1
    assert series["A"][0].temperature == 24.5
    assert series["A"][0].humidity == 55.0


def test_同じサンプルは何度読んでも一行(connection):
    """exporter は 10 分ごとにしか値を更新しないのに、ポータルは 2 分ごとに
    読む。ここで弾かないと同じ値が 5 行入る。"""
    # 2 分おきに 5 回読む。経過秒数はそのぶん増えていく＝同じサンプル
    for i in range(5):
        history.record(
            connection, [sensor(age=i * 120.0)], now=NOW + i * 120
        )

    series = history.query(connection, ["A"], hours=24, now=NOW + 600)
    assert len(series["A"]) == 1


def test_新しい値が来れば行が増える(connection):
    history.record(connection, [sensor(temperature=24.5, age=0)], now=NOW)
    # 10 分後に exporter が値を更新した（経過秒数が 0 に戻る）
    history.record(connection, [sensor(temperature=25.5, age=0)], now=NOW + 600)

    series = history.query(connection, ["A"], hours=24, now=NOW + 600)
    assert [p.temperature for p in series["A"]] == [24.5, 25.5]


def test_時刻は読んだ時ではなく測られた時(connection):
    """経過秒数を引かないと、グラフ上で実際より新しい位置に点が立つ。"""
    history.record(connection, [sensor(age=600.0)], now=NOW)

    point = history.query(connection, ["A"], hours=24, now=NOW)["A"][0]
    assert NOW - 660 <= point.timestamp <= NOW - 540


def test_複数のセンサーを分けて返す(connection):
    history.record(connection, [sensor("A", 24.5), sensor("B", 21.0)], now=NOW)

    series = history.query(connection, ["A", "B"], hours=24, now=NOW)
    assert series["A"][0].temperature == 24.5
    assert series["B"][0].temperature == 21.0


def test_選んでいないセンサーは返さない(connection):
    history.record(connection, [sensor("A"), sensor("B")], now=NOW)
    assert set(history.query(connection, ["A"], hours=24, now=NOW)) == {"A"}


def test_記録の無いセンサーは空の系列(connection):
    """キーごと無いと、画面側で「選んだのに出てこない」になる。"""
    assert history.query(connection, ["知らない子"], hours=24, now=NOW) == {
        "知らない子": []
    }


def test_期間外は返さない(connection):
    history.record(connection, [sensor(age=0)], now=NOW - 86400 * 3)
    history.record(connection, [sensor(age=0)], now=NOW)

    assert len(history.query(connection, ["A"], hours=24, now=NOW)["A"]) == 1


def test_値の無いセンサーは記録しない(connection):
    """取得待ちや値なしのセンサーを 0 として記録するとグラフが歪む。"""
    history.record(
        connection,
        [Sensor("A", "取得待ち", "Meter", temperature=None, humidity=None)],
        now=NOW,
    )
    assert history.query(connection, ["A"], hours=24, now=NOW)["A"] == []


def test_湿度だけでも記録する(connection):
    history.record(
        connection,
        [Sensor("A", "湿度だけ", "Meter", temperature=None, humidity=60.0)],
        now=NOW,
    )
    point = history.query(connection, ["A"], hours=24, now=NOW)["A"][0]
    assert point.temperature is None
    assert point.humidity == 60.0


def test_古い記録を消せる(connection):
    history.record(connection, [sensor(age=0)], now=NOW - 86400 * 100)
    history.record(connection, [sensor(age=0)], now=NOW)

    deleted = history.prune(connection, keep_days=90, now=NOW)

    assert deleted == 1
    assert len(history.query(connection, ["A"], hours=24 * 365, now=NOW)["A"]) == 1


def test_記録期間が分かる(connection):
    history.record(connection, [sensor(age=0)], now=NOW - 3600)
    history.record(connection, [sensor(age=0)], now=NOW)

    first, last = history.span(connection)
    assert first <= NOW - 3600 + history.BUCKET_SECONDS
    assert last >= NOW - history.BUCKET_SECONDS


def test_記録が無ければ期間は空(connection):
    assert history.span(connection) == (None, None)


def test_センサーが空でも落ちない(connection):
    assert history.record(connection, [], now=NOW) == 0


def test_同じ書き込みを繰り返しても壊れない(connection):
    for _ in range(3):
        history.record(connection, [sensor(age=0)], now=NOW)
    assert len(history.query(connection, ["A"], hours=24, now=NOW)["A"]) == 1


def test_テーブルは何度作ってもよい(connection):
    """起動のたびに init を呼ぶので、既存データを壊さないこと。"""
    history.record(connection, [sensor(age=0)], now=NOW)
    history.init(connection)
    assert len(history.query(connection, ["A"], hours=24, now=NOW)["A"]) == 1


def test_読めない_DB_でも例外の型が分かる(tmp_path, monkeypatch):
    """呼び出し側が sqlite3.Error で捕まえられること。"""
    path = tmp_path / "history.db"
    path.write_text("これは SQLite ではない", encoding="utf-8")
    monkeypatch.setenv("HISTORY_DB", str(path))

    # PRAGMA の時点で弾かれるので connect からまとめて包む
    with pytest.raises(sqlite3.DatabaseError):
        conn = history.connect()
        history.init(conn)
