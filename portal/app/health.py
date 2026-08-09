"""サービスの死活チェック。

全サービスを並列にチェックする。1 つ応答の遅いサービスが
トップページ全体を待たせないこと。これが直列だと、サービスが増えるほど
ポータルが重くなり、増やしにくくなってしまう。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"


@dataclass
class HealthResult:
    status: str
    detail: str = ""
    latency_ms: float | None = None


async def _check_http(client: httpx.AsyncClient, url: str) -> HealthResult:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        # リダイレクトは追わない。302 を返せている時点で生きている。
        response = await client.get(url, follow_redirects=False)
    except httpx.TimeoutException:
        return HealthResult(STATUS_DOWN, "タイムアウト")
    except httpx.HTTPError as exc:
        return HealthResult(STATUS_DOWN, f"接続できません: {type(exc).__name__}")

    latency_ms = (loop.time() - started) * 1000
    if 200 <= response.status_code < 400:
        return HealthResult(STATUS_UP, f"HTTP {response.status_code}", latency_ms)
    return HealthResult(STATUS_DOWN, f"HTTP {response.status_code}", latency_ms)


async def _check_tcp(host: str, port: int, timeout: float) -> HealthResult:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return HealthResult(STATUS_DOWN, "タイムアウト")
    except OSError as exc:
        return HealthResult(STATUS_DOWN, f"接続できません: {exc.strerror or exc}")

    latency_ms = (loop.time() - started) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return HealthResult(STATUS_UP, f"{host}:{port} 到達", latency_ms)


async def check(
    client: httpx.AsyncClient, health: dict[str, Any], timeout: float = 3.0
) -> HealthResult:
    """1 サービス分のチェック。例外は投げず、必ず HealthResult を返す。"""
    kind = health.get("type", "always_up")

    if kind == "always_up":
        return HealthResult(STATUS_UP, "常時稼働とみなす")
    if kind == "http":
        return await _check_http(client, health["url"])
    if kind == "tcp":
        return await _check_tcp(health["host"], int(health["port"]), timeout)

    return HealthResult(STATUS_UNKNOWN, f"未知のチェック方式: {kind}")


async def check_all(
    client: httpx.AsyncClient, healths: list[dict[str, Any]], timeout: float = 3.0
) -> list[HealthResult]:
    """全サービスを並列にチェックする。"""
    results = await asyncio.gather(
        *(check(client, h, timeout) for h in healths), return_exceptions=True
    )
    return [
        r if isinstance(r, HealthResult)
        else HealthResult(STATUS_UNKNOWN, f"チェックに失敗: {r}")
        for r in results
    ]
