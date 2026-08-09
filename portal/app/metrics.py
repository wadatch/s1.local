"""数値の取得。source ごとにアダプタを持つ。

取得失敗で例外を投げない。1 つのメトリクスが取れないことは
ポータルにとって「そのカードが『—』になる」以上の意味を持たない。
監視系が落ちているときこそポータルは開けなければならない。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families

from .registry import Metric


@dataclass
class MetricResult:
    label: str
    value: float | None
    format: str
    thresholds: dict[str, float]
    detail: str = ""

    @property
    def level(self) -> str:
        """カードの色を決める段階。ok / warn / crit / unknown。"""
        if self.value is None:
            return "unknown"
        crit = self.thresholds.get("crit")
        warn = self.thresholds.get("warn")
        if crit is not None and self.value >= crit:
            return "crit"
        if warn is not None and self.value >= warn:
            return "warn"
        return "ok"


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------

async def _query_prometheus(
    client: httpx.AsyncClient, endpoint: str, query: str
) -> tuple[float | None, str]:
    try:
        response = await client.get(
            f"{endpoint.rstrip('/')}/api/v1/query", params={"query": query}
        )
    except httpx.HTTPError as exc:
        return None, f"問い合わせ失敗: {type(exc).__name__}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return None, "JSON として読めません"

    if payload.get("status") != "success":
        return None, str(payload.get("error", "クエリが失敗しました"))

    result = payload.get("data", {}).get("result") or []
    if not result:
        # クエリは通ったが系列が無い状態。取得失敗とは意味が違うので
        # そう分かる文言にしておく。
        return None, "該当する系列がありません"

    try:
        return float(result[0]["value"][1]), ""
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "値を解釈できません"


# --------------------------------------------------------------------------
# node_exporter
# --------------------------------------------------------------------------

# CPU 使用率は 1 回のスクレイプでは出せない（node_cpu_seconds_total は累積値）。
# 前回のスクレイプを覚えておき、その差分から算出する。
_cpu_samples: dict[str, tuple[float, float, float]] = {}


def _extract_node_fields(text: str, endpoint: str) -> dict[str, float]:
    """node_exporter の /metrics 本文から、ポータルが出す値を組み立てる。"""
    cpu_idle = 0.0
    cpu_total = 0.0
    mem_total: float | None = None
    mem_available: float | None = None
    fs_size: dict[str, float] = {}
    fs_avail: dict[str, float] = {}
    temps: list[float] = []
    boot_time: float | None = None

    for family in text_string_to_metric_families(text):
        if family.name == "node_cpu_seconds":
            for sample in family.samples:
                cpu_total += sample.value
                if sample.labels.get("mode") == "idle":
                    cpu_idle += sample.value
        elif family.name == "node_memory_MemTotal_bytes":
            for sample in family.samples:
                mem_total = sample.value
        elif family.name == "node_memory_MemAvailable_bytes":
            for sample in family.samples:
                mem_available = sample.value
        elif family.name == "node_filesystem_size_bytes":
            for sample in family.samples:
                fs_size[sample.labels.get("mountpoint", "")] = sample.value
        elif family.name == "node_filesystem_avail_bytes":
            for sample in family.samples:
                fs_avail[sample.labels.get("mountpoint", "")] = sample.value
        elif family.name == "node_hwmon_temp_celsius":
            temps.extend(s.value for s in family.samples)
        elif family.name == "node_boot_time_seconds":
            for sample in family.samples:
                boot_time = sample.value

    fields: dict[str, float] = {}

    now = time.monotonic()
    previous = _cpu_samples.get(endpoint)
    _cpu_samples[endpoint] = (now, cpu_idle, cpu_total)
    if previous is not None:
        _, prev_idle, prev_total = previous
        delta_total = cpu_total - prev_total
        delta_idle = cpu_idle - prev_idle
        if delta_total > 0:
            fields["cpu_usage_ratio"] = max(0.0, min(1.0, 1 - delta_idle / delta_total))

    if mem_total and mem_available is not None:
        fields["memory_usage_ratio"] = 1 - mem_available / mem_total

    # --path.rootfs=/host を付けているとマウントポイントは "/" として出るが、
    # 環境によっては "/host" のまま出ることがあるので両方見る。
    for mountpoint in ("/", "/host"):
        if fs_size.get(mountpoint) and mountpoint in fs_avail:
            fields["rootfs_usage_ratio"] = 1 - fs_avail[mountpoint] / fs_size[mountpoint]
            break

    if temps:
        # 一番熱いセンサーを見る。平均だと熱くなっている箇所が埋もれる。
        fields["cpu_temperature_celsius"] = max(temps)

    if boot_time:
        fields["uptime_seconds"] = max(0.0, time.time() - boot_time)

    return fields


# --------------------------------------------------------------------------
# 任意の /metrics（Prometheus テキスト形式）
# --------------------------------------------------------------------------

def _select_sample(text: str, metric: str, labels: dict[str, str]) -> float | None:
    """metric 名と、指定したラベルをすべて含む系列を 1 つ選んで値を返す。

    ラベルは部分一致でよい（指定したものが全て一致すればよく、
    系列が余分なラベルを持っていても構わない）。センサーを増やしても
    services.yml 側で device_name だけ書けば済むようにするため。
    """
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name != metric:
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


async def _query_prometheus_text(
    client: httpx.AsyncClient, endpoint: str, metric: str, labels: dict[str, str]
) -> tuple[float | None, str]:
    try:
        response = await client.get(f"{endpoint.rstrip('/')}/metrics")
    except httpx.HTTPError as exc:
        return None, f"取得失敗: {type(exc).__name__}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"

    try:
        value = _select_sample(response.text, metric, labels)
    except Exception as exc:  # noqa: BLE001 - パース失敗でページを落とさない
        return None, f"パースできません: {type(exc).__name__}"

    if value is None:
        return None, "該当する系列がありません"
    return value, ""


async def _query_node_exporter(
    client: httpx.AsyncClient, endpoint: str, field_name: str
) -> tuple[float | None, str]:
    try:
        response = await client.get(f"{endpoint.rstrip('/')}/metrics")
    except httpx.HTTPError as exc:
        return None, f"取得失敗: {type(exc).__name__}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"

    try:
        fields = _extract_node_fields(response.text, endpoint)
    except Exception as exc:  # noqa: BLE001 - パース失敗でページを落とさない
        return None, f"パースできません: {type(exc).__name__}"

    if field_name not in fields:
        if field_name == "cpu_usage_ratio":
            # 初回スクレイプでは差分が取れない。次回には出る。
            return None, "計測中"
        return None, "この環境では取得できません"
    return fields[field_name], ""


# --------------------------------------------------------------------------

async def fetch(client: httpx.AsyncClient, metric: Metric) -> MetricResult:
    """メトリクス 1 件を取得する。例外は投げない。"""
    value: float | None = None
    detail = ""

    if metric.source == "prometheus":
        value, detail = await _query_prometheus(client, metric.endpoint, metric.query)
    elif metric.source == "prometheus_text":
        value, detail = await _query_prometheus_text(
            client, metric.endpoint, metric.metric_name, metric.labels
        )
    elif metric.source == "node_exporter":
        value, detail = await _query_node_exporter(
            client, metric.endpoint, metric.field_name
        )

    return MetricResult(
        label=metric.label,
        value=value,
        format=metric.format,
        thresholds=metric.thresholds,
        detail=detail,
    )


async def fetch_all(
    client: httpx.AsyncClient, metrics: list[Metric]
) -> list[MetricResult]:
    """複数のメトリクスを並列に取得する。"""
    results = await asyncio.gather(
        *(fetch(client, m) for m in metrics), return_exceptions=True
    )
    output: list[MetricResult] = []
    for metric, result in zip(metrics, results):
        if isinstance(result, MetricResult):
            output.append(result)
        else:
            output.append(
                MetricResult(
                    label=metric.label,
                    value=None,
                    format=metric.format,
                    thresholds=metric.thresholds,
                    detail=f"取得に失敗: {result}",
                )
            )
    return output


# --------------------------------------------------------------------------
# 表示用の整形
# --------------------------------------------------------------------------

def format_value(value: float | None, fmt: str) -> str:
    """カードに出す文字列にする。取れていなければ '—'。"""
    if value is None:
        return "—"

    # percent は 0〜1 の比率を受け取る（ロス率など）。
    # percent100 は既に 0〜100 で来る値（湿度・電池残量など）。
    if fmt == "percent":
        return f"{value * 100:.1f} %"
    if fmt == "percent100":
        return f"{value:.0f} %"
    if fmt == "seconds_ms":
        return f"{value * 1000:.1f} ms"
    if fmt == "celsius":
        return f"{value:.1f} °C"
    if fmt == "count":
        return f"{value:.0f}"
    if fmt == "bytes":
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(size) < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
            size /= 1024
    if fmt == "duration":
        seconds = int(value)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        if days:
            return f"{days} 日 {hours} 時間"
        if hours:
            return f"{hours} 時間 {minutes} 分"
        return f"{minutes} 分"

    return f"{value:g}"
