"""回線速度を有線・無線それぞれで測って Prometheus 形式で公開する。

## なぜ自前で測るのか

network-analyzer にも speedtest はあるが、**有線しか測っていない**
（Docker のブリッジ上で動くので、ホストの既定経路＝有線から出ていく）。
無線側の速度を知るには、無線インターフェースにバインドして測る必要がある。

s1 は有線 `eno1` と無線 `wlp3s0` の両方につながっているので、
`speedtest --interface=...` で測り分けられる。

## なぜ同じサーバに当てるのか

有線と無線を別々のサーバに対して測ると、差が「回線の違い」なのか
「相手サーバの違い」なのか分からなくなる。最初に測った回で選ばれた
サーバを覚えて、残りもそこへ当てる。

## なぜ低頻度なのか

1 回の測定で数百 MB を流す。頻繁に測ると回線を占有し、通信量も食う。
知りたいのは「今の回線はどのくらい出るのか」であって秒単位の変化ではないので、
既定は 6 時間ごとにしてある。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.core import GaugeMetricFamily

log = logging.getLogger("speed-exporter")


@dataclass
class Measurement:
    link: str
    interface: str
    download_bps: float | None = None
    upload_bps: float | None = None
    ping_ms: float | None = None
    jitter_ms: float | None = None
    server_id: int | None = None
    server_name: str = ""
    measured_at: float = 0.0
    ok: bool = False
    detail: str = ""


@dataclass
class State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    results: dict[str, Measurement] = field(default_factory=dict)
    runs: int = 0
    failures: int = 0


class SpeedtestError(Exception):
    pass


def parse_links(raw: str) -> list[tuple[str, str]]:
    """"wired:eno1,wifi:wlp3s0" を [(link, interface), ...] にする。

    順番は書いた順のまま。最初のものが基準のサーバを決める。
    """
    links: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, interface = chunk.partition(":")
        if not name or not interface:
            log.warning("経路の指定を読めません（name:interface の形で書く）: %s", chunk)
            continue
        links.append((name.strip(), interface.strip()))
    return links


def build_command(interface: str, server_id: int | None, timeout: int) -> list[str]:
    command = [
        "speedtest",
        "--format=json",
        "--progress=no",
        "--accept-license",
        "--accept-gdpr",
        f"--interface={interface}",
    ]
    if server_id is not None:
        command.append(f"--server-id={server_id}")
    return command


def parse_result(payload: dict) -> dict:
    """Ookla の JSON から必要なところだけ取り出す。

    bandwidth は **バイト毎秒**なので 8 倍してビット毎秒にする。
    ここを間違えると 8 分の 1 の値が出る。
    """
    download = payload.get("download") or {}
    upload = payload.get("upload") or {}
    ping = payload.get("ping") or {}
    server = payload.get("server") or {}

    return {
        "download_bps": float(download["bandwidth"]) * 8 if "bandwidth" in download else None,
        "upload_bps": float(upload["bandwidth"]) * 8 if "bandwidth" in upload else None,
        "ping_ms": float(ping["latency"]) if "latency" in ping else None,
        "jitter_ms": float(ping["jitter"]) if "jitter" in ping else None,
        "server_id": int(server["id"]) if "id" in server else None,
        "server_name": str(server.get("name") or ""),
    }


def run_speedtest(
    interface: str, server_id: int | None = None, timeout: int = 150,
    runner=subprocess.run,
) -> dict:
    """1 回測る。失敗は SpeedtestError にして上げる。"""
    command = build_command(interface, server_id, timeout)
    try:
        completed = runner(
            command, capture_output=True, timeout=timeout, check=False, text=True
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeedtestError(f"時間内に終わりませんでした（{timeout} 秒）") from exc
    except OSError as exc:
        raise SpeedtestError(f"実行できません: {exc}") from exc

    if completed.returncode != 0:
        # CLI は失敗も JSON で返すことがあるので、読めるなら message を使う
        message = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = message[-1] if message else f"終了コード {completed.returncode}"
        raise SpeedtestError(detail[:200])

    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise SpeedtestError("結果を JSON として読めません") from exc

    if payload.get("type") == "log":
        raise SpeedtestError(str(payload.get("message", "測定できませんでした"))[:200])

    return parse_result(payload)


def measure_all(
    state: State, links: list[tuple[str, str]], timeout: int = 150, runner=subprocess.run
) -> None:
    """全経路を順番に測る。

    **同時に測らない。** 同時に流すと互いに帯域を奪い合って、どちらの数字も
    本来より低く出る。
    """
    server_id: int | None = None

    for link, interface in links:
        try:
            try:
                values = run_speedtest(interface, server_id, timeout, runner)
            except SpeedtestError as exc:
                if server_id is None:
                    raise
                # 固定したサーバに届かないことがある。経路が違えば届く先も
                # 違うので、無線からは有線で選ばれたサーバに繋がらない、が起きる。
                # 比べやすさより「値が出ること」を優先して選び直させる。
                log.warning(
                    "%s (%s): サーバ %s に届かないので選び直します（%s）",
                    link, interface, server_id, exc,
                )
                values = run_speedtest(interface, None, timeout, runner)
        except SpeedtestError as exc:
            with state.lock:
                state.failures += 1
                previous = state.results.get(link)
                # 前回の値は残す。ポータル側は経過時間で古さを判断できる。
                state.results[link] = Measurement(
                    link=link,
                    interface=interface,
                    download_bps=previous.download_bps if previous else None,
                    upload_bps=previous.upload_bps if previous else None,
                    ping_ms=previous.ping_ms if previous else None,
                    jitter_ms=previous.jitter_ms if previous else None,
                    server_id=previous.server_id if previous else None,
                    server_name=previous.server_name if previous else "",
                    measured_at=previous.measured_at if previous else 0.0,
                    ok=False,
                    detail=str(exc),
                )
            log.error("%s (%s) を測れません: %s", link, interface, exc)
            continue

        # 最初に測れた回のサーバを、残りの経路でも使う。
        # そうしないと差が回線の違いなのかサーバの違いなのか分からない。
        if server_id is None:
            server_id = values["server_id"]

        with state.lock:
            state.results[link] = Measurement(
                link=link, interface=interface, measured_at=time.time(), ok=True,
                **values,
            )
        log.info(
            "%s (%s): 下り %.1f Mbps / 上り %.1f Mbps / Ping %.1f ms（%s）",
            link, interface,
            (values["download_bps"] or 0) / 1e6,
            (values["upload_bps"] or 0) / 1e6,
            values["ping_ms"] or 0,
            values["server_name"],
        )

    with state.lock:
        state.runs += 1


class SpeedCollector:
    """測った結果を返すだけ。スクレイプでは測らない。

    スクレイプのたびに測ると、ページを開くたびに回線を占有してしまう。
    """

    def __init__(self, state: State) -> None:
        self._state = state

    def collect(self):
        with self._state.lock:
            results = list(self._state.results.values())
            runs, failures = self._state.runs, self._state.failures

        labels = ["link", "interface"]
        download = GaugeMetricFamily(
            "home_speed_download_bits_per_second", "下り速度", labels=labels)
        upload = GaugeMetricFamily(
            "home_speed_upload_bits_per_second", "上り速度", labels=labels)
        ping = GaugeMetricFamily(
            "home_speed_ping_milliseconds", "Ping（往復遅延）", labels=labels)
        jitter = GaugeMetricFamily(
            "home_speed_jitter_milliseconds", "Ping のばらつき", labels=labels)
        age = GaugeMetricFamily(
            "home_speed_age_seconds",
            "測ってからの経過秒数。古い値を今の速度と誤読しないための指標",
            labels=labels)
        up = GaugeMetricFamily(
            "home_speed_up", "直近の測定が成功したか（1 / 0）", labels=labels)
        server = GaugeMetricFamily(
            "home_speed_server_id", "測定に使ったサーバ", labels=labels)

        now = time.time()
        for result in results:
            values = [result.link, result.interface]
            if result.download_bps is not None:
                download.add_metric(values, result.download_bps)
            if result.upload_bps is not None:
                upload.add_metric(values, result.upload_bps)
            if result.ping_ms is not None:
                ping.add_metric(values, result.ping_ms)
            if result.jitter_ms is not None:
                jitter.add_metric(values, result.jitter_ms)
            if result.measured_at:
                age.add_metric(values, max(0.0, now - result.measured_at))
            if result.server_id is not None:
                server.add_metric(values, float(result.server_id))
            up.add_metric(values, 1.0 if result.ok else 0.0)

        yield from (download, upload, ping, jitter, age, up, server)

        runs_metric = GaugeMetricFamily("home_speed_runs_total", "測定した回数")
        runs_metric.add_metric([], float(runs))
        yield runs_metric

        failures_metric = GaugeMetricFamily(
            "home_speed_failures_total", "測定に失敗した回数")
        failures_metric.add_metric([], float(failures))
        yield failures_metric


def run_loop(state: State, links: list[tuple[str, str]], interval: float, timeout: int) -> None:
    while True:
        try:
            measure_all(state, links, timeout)
        except Exception as exc:  # noqa: BLE001 - 測定の失敗で止まらない
            log.error("測定で予期しない失敗: %s", exc)
        time.sleep(interval)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/metrics":
                body = generate_latest(REGISTRY)
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *args) -> None:
            pass

    return Handler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    links = parse_links(os.environ.get("SPEED_LINKS", "wired:eno1,wifi:wlp3s0"))
    if not links:
        raise SystemExit(
            "SPEED_LINKS が空です。'wired:eno1,wifi:wlp3s0' の形で指定してください。"
        )

    interval = float(os.environ.get("SPEED_INTERVAL_SECONDS", "1800"))
    timeout = int(os.environ.get("SPEED_TIMEOUT_SECONDS", "150"))
    port = int(os.environ.get("SPEED_PORT", "9800"))

    state = State()
    REGISTRY.register(SpeedCollector(state))

    threading.Thread(
        target=run_loop, args=(state, links, interval, timeout), daemon=True
    ).start()

    log.info(
        "待ち受け開始 :%d（測定間隔 %.0f 秒 / 経路 %s）",
        port, interval, ", ".join(f"{n}={i}" for n, i in links),
    )
    ThreadingHTTPServer(("", port), make_handler(state)).serve_forever()


if __name__ == "__main__":
    main()
