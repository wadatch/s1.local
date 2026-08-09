"""温湿度の履歴。

## なぜポータル側に置くのか

exporter は現在値しか持たない（SwitchBot API の回数制限に収めるための
キャッシュであって、時系列の保管庫ではない）。グラフには履歴が要る。

記録役をポータルに置いたのは、**書き手を 1 つに保つ**ため。exporter が書いて
ポータルが読む形にすると 2 つのコンテナが同じ DB ファイルを触ることになる。
ポータルだけが読み書きするなら、その心配が無くなる。

## 重複の扱い

exporter は 10 分ごとにしか値を更新しないので、ポータルが 2 分ごとに読むと
同じ値を何度も拾う。exporter が公開している「取得からの経過秒数」から
**その値が測られた時刻**を復元し、それを主キーにして重複を捨てる。
何度読んでも 1 サンプルは 1 行にしかならない。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import sensors as sensors_mod

log = logging.getLogger("history")

# サンプル時刻をこの秒数に丸めてから主キーにする。
# exporter を読むたびに経過秒数は少しずつ変わるので、丸めないと
# 同じ値が別サンプルとして何行も入る。
BUCKET_SECONDS = 60


@dataclass
class Point:
    timestamp: float
    temperature: float | None
    humidity: float | None


def _database_path() -> Path:
    return Path(os.environ.get("HISTORY_DB", "/data/history.db"))


def connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    # 読み書きが同時に来ても待たされないようにする。
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def init(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS readings (
            device_id   TEXT    NOT NULL,
            bucket      INTEGER NOT NULL,   -- サンプル時刻（秒・丸め済み）
            temperature REAL,
            humidity    REAL,
            battery     REAL,
            PRIMARY KEY (device_id, bucket)
        );
        CREATE INDEX IF NOT EXISTS readings_bucket ON readings (bucket);
        """
    )
    connection.commit()


def record(connection: sqlite3.Connection, found: list[sensors_mod.Sensor],
           now: float | None = None) -> int:
    """センサーの現在値を記録する。返り値は新しく入った行数。

    同じサンプルを何度渡しても行は増えない（主キーで弾かれる）。
    """
    now = time.time() if now is None else now
    rows = []

    for sensor in found:
        if sensor.temperature is None and sensor.humidity is None:
            continue
        # 「いつ測られた値か」を経過秒数から復元する。読んだ時刻ではない。
        measured_at = now - (sensor.age_seconds or 0)
        bucket = int(measured_at // BUCKET_SECONDS) * BUCKET_SECONDS
        rows.append(
            (sensor.device_id, bucket, sensor.temperature, sensor.humidity,
             sensor.battery)
        )

    if not rows:
        return 0

    with closing(connection.cursor()) as cursor:
        cursor.executemany(
            "INSERT OR IGNORE INTO readings "
            "(device_id, bucket, temperature, humidity, battery) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        inserted = cursor.rowcount
    connection.commit()
    return max(0, inserted)


def prune(connection: sqlite3.Connection, keep_days: float,
          now: float | None = None) -> int:
    """古い行を消す。返り値は消した行数。"""
    now = time.time() if now is None else now
    cutoff = int(now - keep_days * 86400)
    with closing(connection.cursor()) as cursor:
        cursor.execute("DELETE FROM readings WHERE bucket < ?", (cutoff,))
        deleted = cursor.rowcount
    connection.commit()
    return max(0, deleted)


def query(
    connection: sqlite3.Connection,
    device_ids: list[str],
    hours: float,
    now: float | None = None,
) -> dict[str, list[Point]]:
    """指定したセンサーの履歴を返す。device_id -> 時系列。"""
    if not device_ids:
        return {}

    now = time.time() if now is None else now
    since = int(now - hours * 3600)

    placeholders = ",".join("?" * len(device_ids))
    rows = connection.execute(
        f"SELECT device_id, bucket, temperature, humidity FROM readings "
        f"WHERE device_id IN ({placeholders}) AND bucket >= ? "
        f"ORDER BY bucket",
        (*device_ids, since),
    ).fetchall()

    series: dict[str, list[Point]] = {device_id: [] for device_id in device_ids}
    for device_id, bucket, temperature, humidity in rows:
        series[device_id].append(Point(float(bucket), temperature, humidity))
    return series


def span(connection: sqlite3.Connection) -> tuple[float | None, float | None]:
    """記録がある期間。グラフに「まだ溜まっていない」と出すために使う。"""
    row = connection.execute(
        "SELECT MIN(bucket), MAX(bucket) FROM readings"
    ).fetchone()
    if not row or row[0] is None:
        return None, None
    return float(row[0]), float(row[1])


async def run_recorder(
    client: httpx.AsyncClient,
    exporter_url: str,
    interval: float,
    keep_days: float,
) -> None:
    """定期的に exporter を読んで記録する。

    exporter の更新間隔（既定 10 分）より短い間隔で読む。取りこぼしを
    防ぐためで、重複はサンプル時刻の丸めで落ちる。
    """
    connection = connect()
    init(connection)
    last_prune = 0.0

    while True:
        try:
            found, error = await sensors_mod.fetch(client, exporter_url)
            if error:
                log.warning("履歴を記録できません: %s", error)
            elif found:
                inserted = record(connection, found)
                if inserted:
                    log.info("履歴に %d 件を追加しました", inserted)

            now = time.time()
            if now - last_prune > 6 * 3600:
                deleted = prune(connection, keep_days)
                if deleted:
                    log.info("古い履歴 %d 件を削除しました", deleted)
                last_prune = now
        except sqlite3.Error as exc:
            log.error("履歴の書き込みに失敗しました: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 記録の失敗でポータルを落とさない
            log.error("履歴の記録で予期しない失敗: %s", exc)

        await asyncio.sleep(interval)
