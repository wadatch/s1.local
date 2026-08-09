"""死活チェック。

守るべき性質は「例外を投げないこと」と「並列であること」。
どちらもトップページが開けなくなる事故に直結する。
"""

import asyncio

import httpx
import pytest
import respx

from app.health import STATUS_DOWN, STATUS_UNKNOWN, STATUS_UP, check, check_all


@pytest.fixture
def client():
    return httpx.AsyncClient(timeout=3.0)


@respx.mock
async def test_200_は_UP(client):
    respx.get("http://svc/health").mock(return_value=httpx.Response(200))
    result = await check(client, {"type": "http", "url": "http://svc/health"})
    assert result.status == STATUS_UP
    assert result.latency_ms is not None


@respx.mock
async def test_302_も_UP(client):
    """Grafana はログインページへ 302 を返す。生きている証拠として扱う。"""
    respx.get("http://svc/").mock(return_value=httpx.Response(302))
    result = await check(client, {"type": "http", "url": "http://svc/"})
    assert result.status == STATUS_UP


@respx.mock
async def test_500_は_DOWN(client):
    respx.get("http://svc/health").mock(return_value=httpx.Response(500))
    result = await check(client, {"type": "http", "url": "http://svc/health"})
    assert result.status == STATUS_DOWN
    assert "500" in result.detail


@respx.mock
async def test_接続不能は例外にせず_DOWN(client):
    respx.get("http://svc/health").mock(side_effect=httpx.ConnectError("refused"))
    result = await check(client, {"type": "http", "url": "http://svc/health"})
    assert result.status == STATUS_DOWN
    assert "接続できません" in result.detail


@respx.mock
async def test_タイムアウトは例外にせず_DOWN(client):
    respx.get("http://svc/health").mock(side_effect=httpx.ConnectTimeout("timeout"))
    result = await check(client, {"type": "http", "url": "http://svc/health"})
    assert result.status == STATUS_DOWN
    assert result.detail == "タイムアウト"


async def test_always_up_は常に_UP(client):
    result = await check(client, {"type": "always_up"})
    assert result.status == STATUS_UP


async def test_未知の方式は不明を返す(client):
    result = await check(client, {"type": "telepathy"})
    assert result.status == STATUS_UNKNOWN


async def test_tcp_到達可能なら_UP(client):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        result = await check(
            client, {"type": "tcp", "host": "127.0.0.1", "port": port}
        )
    assert result.status == STATUS_UP


async def test_tcp_閉じたポートは_DOWN(client):
    # 一度開いて即座に閉じ、確実に誰も listen していないポートを得る
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    result = await check(client, {"type": "tcp", "host": "127.0.0.1", "port": port})
    assert result.status == STATUS_DOWN


@respx.mock
async def test_遅いサービスが他を待たせない(client):
    """直列だとサービスが増えるほどページが重くなる。並列であることを担保する。"""

    async def slow(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200)

    for i in range(5):
        respx.get(f"http://svc{i}/health").mock(side_effect=slow)

    healths = [{"type": "http", "url": f"http://svc{i}/health"} for i in range(5)]

    loop = asyncio.get_running_loop()
    started = loop.time()
    results = await check_all(client, healths)
    elapsed = loop.time() - started

    assert all(r.status == STATUS_UP for r in results)
    # 直列なら 1.5 秒かかる。並列なら 0.3 秒強で終わる。
    assert elapsed < 1.0, f"並列に実行されていない（{elapsed:.2f}秒）"


@respx.mock
async def test_一部が落ちていても全件返る(client):
    respx.get("http://ok/health").mock(return_value=httpx.Response(200))
    respx.get("http://ng/health").mock(side_effect=httpx.ConnectError("refused"))

    results = await check_all(
        client,
        [
            {"type": "http", "url": "http://ok/health"},
            {"type": "http", "url": "http://ng/health"},
            {"type": "always_up"},
        ],
    )
    assert [r.status for r in results] == [STATUS_UP, STATUS_DOWN, STATUS_UP]
