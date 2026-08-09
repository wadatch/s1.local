"""s1.local ホームポータルの本体。"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import health as health_mod
from . import metrics as metrics_mod
from .registry import RegistryCache, Service

BASE_DIR = Path(__file__).resolve().parent
SERVICES_FILE = os.environ.get("SERVICES_FILE", "/app/config/services.yml")
CACHE_SECONDS = float(os.environ.get("STATUS_CACHE_SECONDS", "10"))
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT_SECONDS", "3"))

registry_cache = RegistryCache(SERVICES_FILE)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class StatusCache:
    """/api/status の結果を短時間だけ保持する。

    トップページを複数の端末やタブで開いても、裏側の Prometheus と
    node_exporter への問い合わせが人数分に増えないようにするためのもの。
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._value: dict[str, Any] | None = None
        self._fetched_at = 0.0

    async def get(self, producer) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            if self._value is None or now - self._fetched_at >= self._ttl:
                self._value = await producer()
                self._fetched_at = now
            return self._value


status_cache = StatusCache(CACHE_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 接続を使い回す。毎回張り直すと、サービスが増えたときに
    # トップページの表示が目に見えて遅くなる。
    app.state.client = httpx.AsyncClient(timeout=HEALTH_TIMEOUT)
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="s1.local ホームポータル", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _service_payload(
    service: Service,
    health_result: health_mod.HealthResult,
    metric_results: list[metrics_mod.MetricResult],
) -> dict[str, Any]:
    return {
        "id": service.id,
        "name": service.name,
        "description": service.description,
        "url": service.url,
        "category": service.category,
        "errors": service.errors,
        "health": {
            "status": health_result.status,
            "detail": health_result.detail,
            "latency_ms": (
                round(health_result.latency_ms, 1)
                if health_result.latency_ms is not None
                else None
            ),
        },
        "metrics": [
            {
                "label": m.label,
                "value": m.value,
                "display": metrics_mod.format_value(m.value, m.format),
                "level": m.level,
                "detail": m.detail,
            }
            for m in metric_results
        ],
    }


async def _build_status() -> dict[str, Any]:
    registry, error = registry_cache.get()
    if registry is None:
        return {
            "error": error,
            "services": [],
            "categories": {},
            "updated_at": time.time(),
        }

    client: httpx.AsyncClient = app.state.client

    # 死活チェックとメトリクス取得を、サービスをまたいで一斉に走らせる。
    health_task = health_mod.check_all(
        client, [s.health for s in registry.services], HEALTH_TIMEOUT
    )
    metric_tasks = [metrics_mod.fetch_all(client, s.metrics) for s in registry.services]
    health_results, *metric_results = await asyncio.gather(health_task, *metric_tasks)

    services = [
        _service_payload(service, health_result, metric_result)
        for service, health_result, metric_result in zip(
            registry.services, health_results, metric_results
        )
    ]

    return {
        "error": None,
        "services": services,
        "categories": registry.categories,
        "updated_at": time.time(),
    }


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(await status_cache.get(_build_status))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    status = await status_cache.get(_build_status)

    # カテゴリごとにまとめる。categories の定義順を表示順にする。
    by_category: list[tuple[str, list[dict[str, Any]]]] = []
    categories: dict[str, str] = status["categories"]
    for key, label in categories.items():
        members = [s for s in status["services"] if s["category"] == key]
        if members:
            by_category.append((label, members))

    known = set(categories)
    orphans = [s for s in status["services"] if s["category"] not in known]
    if orphans:
        by_category.append(("その他", orphans))

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"status": status, "by_category": by_category},
    )
