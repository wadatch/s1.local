"""メトリクス取得と表示整形。

守るべき性質は「監視系が落ちていてもポータルは開ける」こと。
取得失敗は例外ではなく None として扱われ、カードが『—』になるだけであること。
"""

import httpx
import pytest
import respx

from app.metrics import (
    MetricResult,
    _extract_node_fields,
    _cpu_samples,
    _select_sample,
    fetch,
    fetch_all,
    format_value,
)
from app.registry import Metric


@pytest.fixture
def client():
    return httpx.AsyncClient(timeout=3.0)


def prom_metric(query="up", **kwargs):
    return Metric(
        label="テスト",
        source="prometheus",
        endpoint="http://prom:9090",
        query=query,
        **kwargs,
    )


def prom_response(value):
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [1700000000, str(value)]}],
            },
        },
    )


# --- Prometheus ----------------------------------------------------------

@respx.mock
async def test_prometheus_の値を取得できる(client):
    respx.get("http://prom:9090/api/v1/query").mock(return_value=prom_response("0.042"))
    result = await fetch(client, prom_metric(format="percent"))
    assert result.value == pytest.approx(0.042)
    assert result.detail == ""


@respx.mock
async def test_系列が空なら値なし扱い(client):
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"result": []}}
        )
    )
    result = await fetch(client, prom_metric())
    assert result.value is None
    assert "系列がありません" in result.detail


@respx.mock
async def test_prometheus_が落ちていても例外にならない(client):
    respx.get("http://prom:9090/api/v1/query").mock(
        side_effect=httpx.ConnectError("refused")
    )
    result = await fetch(client, prom_metric())
    assert result.value is None
    assert "問い合わせ失敗" in result.detail


@respx.mock
async def test_クエリエラーを検出する(client):
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            400, json={"status": "error", "error": "parse error"}
        )
    )
    result = await fetch(client, prom_metric())
    assert result.value is None


@respx.mock
async def test_JSON_でない応答でも落ちない(client):
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(200, text="<html>")
    )
    result = await fetch(client, prom_metric())
    assert result.value is None


@respx.mock
async def test_一部が失敗しても全件返る(client):
    respx.get("http://prom:9090/api/v1/query").mock(
        side_effect=[prom_response("1"), httpx.ConnectError("refused")]
    )
    results = await fetch_all(client, [prom_metric(), prom_metric()])
    assert len(results) == 2
    assert results[0].value == 1.0
    assert results[1].value is None


# --- node_exporter -------------------------------------------------------

NODE_TEXT = """\
# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 800
node_cpu_seconds_total{cpu="0",mode="user"} 150
node_cpu_seconds_total{cpu="0",mode="system"} 50
# HELP node_memory_MemTotal_bytes Memory information.
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 1.6e+10
# TYPE node_memory_MemAvailable_bytes gauge
node_memory_MemAvailable_bytes 1.2e+10
# TYPE node_filesystem_size_bytes gauge
node_filesystem_size_bytes{mountpoint="/"} 1000
# TYPE node_filesystem_avail_bytes gauge
node_filesystem_avail_bytes{mountpoint="/"} 250
# TYPE node_hwmon_temp_celsius gauge
node_hwmon_temp_celsius{chip="a",sensor="temp1"} 41
node_hwmon_temp_celsius{chip="a",sensor="temp2"} 57
# TYPE node_boot_time_seconds gauge
node_boot_time_seconds 1.0e+09
"""


def test_メモリとディスクの使用率を算出できる():
    _cpu_samples.clear()
    fields = _extract_node_fields(NODE_TEXT, "test-mem")
    assert fields["memory_usage_ratio"] == pytest.approx(0.25)
    assert fields["rootfs_usage_ratio"] == pytest.approx(0.75)


def test_スペックを取り出せる():
    """何が載っているかも見たい。CPU の数は系列の数から数える。"""
    _cpu_samples.clear()
    fields = _extract_node_fields(NODE_TEXT, "test-spec")
    assert fields["cpu_cores"] == 1, "NODE_TEXT には cpu=0 しかない"
    assert fields["memory_total_bytes"] == pytest.approx(1.6e10)
    assert fields["rootfs_total_bytes"] == 1000


def test_CPU_の数は重複を数えない():
    _cpu_samples.clear()
    text = NODE_TEXT.replace(
        'node_cpu_seconds_total{cpu="0",mode="idle"} 800',
        'node_cpu_seconds_total{cpu="0",mode="idle"} 800\n'
        'node_cpu_seconds_total{cpu="1",mode="idle"} 800\n'
        'node_cpu_seconds_total{cpu="1",mode="user"} 100',
    )
    assert _extract_node_fields(text, "test-cores")["cpu_cores"] == 2


def test_温度は最大値を採る():
    """平均だと熱くなっている箇所が埋もれるため。"""
    _cpu_samples.clear()
    fields = _extract_node_fields(NODE_TEXT, "test-temp")
    assert fields["cpu_temperature_celsius"] == 57


def test_CPU使用率は初回では出せず二回目から出る():
    """node_cpu_seconds_total は累積値なので、差分が要る。"""
    _cpu_samples.clear()
    first = _extract_node_fields(NODE_TEXT, "test-cpu")
    assert "cpu_usage_ratio" not in first

    # idle が 40、user が 60 進んだ = 使用率 60%
    second_text = NODE_TEXT.replace(
        'mode="idle"} 800', 'mode="idle"} 840'
    ).replace('mode="user"} 150', 'mode="user"} 210')
    second = _extract_node_fields(second_text, "test-cpu")
    assert second["cpu_usage_ratio"] == pytest.approx(0.6)


def test_温度センサーが無い環境でも落ちない():
    _cpu_samples.clear()
    text = "\n".join(
        line for line in NODE_TEXT.splitlines() if "hwmon" not in line
    )
    fields = _extract_node_fields(text, "test-notemp")
    assert "cpu_temperature_celsius" not in fields
    assert "memory_usage_ratio" in fields


def test_マウントポイントが_host_でも拾える():
    """--path.rootfs の扱いは環境で揺れるので両方を見る。"""
    _cpu_samples.clear()
    text = NODE_TEXT.replace('mountpoint="/"', 'mountpoint="/host"')
    fields = _extract_node_fields(text, "test-host-mount")
    assert fields["rootfs_usage_ratio"] == pytest.approx(0.75)


@respx.mock
async def test_node_exporter_から値を取得できる(client):
    _cpu_samples.clear()
    respx.get("http://node:9100/metrics").mock(
        return_value=httpx.Response(200, text=NODE_TEXT)
    )
    metric = Metric(
        label="メモリ",
        source="node_exporter",
        endpoint="http://node:9100",
        field_name="memory_usage_ratio",
        format="percent",
    )
    result = await fetch(client, metric)
    assert result.value == pytest.approx(0.25)


@respx.mock
async def test_初回のCPU使用率は計測中と伝える(client):
    _cpu_samples.clear()
    respx.get("http://node:9100/metrics").mock(
        return_value=httpx.Response(200, text=NODE_TEXT)
    )
    metric = Metric(
        label="CPU",
        source="node_exporter",
        endpoint="http://node:9100",
        field_name="cpu_usage_ratio",
        format="percent",
    )
    result = await fetch(client, metric)
    assert result.value is None
    assert result.detail == "計測中"


@respx.mock
async def test_node_exporter_が落ちていても例外にならない(client):
    respx.get("http://node:9100/metrics").mock(side_effect=httpx.ConnectError("x"))
    metric = Metric(
        label="CPU",
        source="node_exporter",
        endpoint="http://node:9100",
        field_name="cpu_usage_ratio",
    )
    result = await fetch(client, metric)
    assert result.value is None
    assert "取得失敗" in result.detail


# --- prometheus_text（任意の /metrics）------------------------------------

SWITCHBOT_TEXT = """\
# HELP switchbot_temperature_celsius センサーの温度
# TYPE switchbot_temperature_celsius gauge
switchbot_temperature_celsius{device_id="AAA",device_name="リビング",device_type="MeterPlus"} 24.5
switchbot_temperature_celsius{device_id="BBB",device_name="寝室",device_type="Meter"} 21.0
# TYPE switchbot_battery_percent gauge
switchbot_battery_percent{device_id="AAA",device_name="リビング",device_type="MeterPlus"} 92
# TYPE switchbot_up gauge
switchbot_up 1.0
"""


def test_ラベルで系列を選べる():
    assert _select_sample(
        SWITCHBOT_TEXT, "switchbot_temperature_celsius", {"device_name": "寝室"}
    ) == 21.0


def test_ラベルは部分一致でよい():
    """センサーを増やしても services.yml 側は device_name だけ書けば済むこと。"""
    assert _select_sample(
        SWITCHBOT_TEXT, "switchbot_temperature_celsius", {"device_id": "AAA"}
    ) == 24.5


def test_ラベル指定なしなら最初の系列():
    assert _select_sample(SWITCHBOT_TEXT, "switchbot_up", {}) == 1.0


def test_該当しないラベルなら値なし():
    assert _select_sample(
        SWITCHBOT_TEXT, "switchbot_temperature_celsius", {"device_name": "書斎"}
    ) is None


def test_同名の別メトリクスと取り違えない():
    assert _select_sample(
        SWITCHBOT_TEXT, "switchbot_battery_percent", {"device_name": "リビング"}
    ) == 92.0


INFO_TEXT = """\
# TYPE node_os_info gauge
node_os_info{id="ubuntu",name="Ubuntu",pretty_name="Ubuntu 26.04 LTS",version="26.04"} 1
# TYPE node_uname_info gauge
node_uname_info{machine="x86_64",release="7.0.0-29-generic",sysname="Linux"} 1
"""


def test_ラベルの中身を取り出せる():
    """OS 名やカーネル版数は、値ではなくラベルに入っている。"""
    from app.metrics import _select_label
    assert _select_label(INFO_TEXT, "node_os_info", {}, "pretty_name") == "Ubuntu 26.04 LTS"
    assert _select_label(INFO_TEXT, "node_uname_info", {}, "release") == "7.0.0-29-generic"
    assert _select_label(INFO_TEXT, "node_uname_info", {}, "machine") == "x86_64"


def test_無いラベルなら空を返す():
    from app.metrics import _select_label
    assert _select_label(INFO_TEXT, "node_os_info", {}, "そんなラベルはない") is None


@respx.mock
async def test_文字の値を取得できる(client):
    respx.get("http://node:9100/metrics").mock(
        return_value=httpx.Response(200, text=INFO_TEXT)
    )
    metric = Metric(
        label="OS",
        source="prometheus_text",
        endpoint="http://node:9100",
        metric_name="node_os_info",
        value_from="pretty_name",
    )
    result = await fetch(client, metric)
    assert result.text == "Ubuntu 26.04 LTS"
    assert result.value is None
    assert result.level == "info", "文字は良し悪しの話ではないので色を付けない"


@respx.mock
async def test_文字の値が無ければ系列なし扱い(client):
    respx.get("http://node:9100/metrics").mock(
        return_value=httpx.Response(200, text=INFO_TEXT)
    )
    metric = Metric(
        label="OS",
        source="prometheus_text",
        endpoint="http://node:9100",
        metric_name="node_os_info",
        value_from="そんなラベルはない",
    )
    result = await fetch(client, metric)
    assert result.text is None
    assert "系列がありません" in result.detail


@respx.mock
async def test_prometheus_text_から値を取得できる(client):
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(200, text=SWITCHBOT_TEXT)
    )
    metric = Metric(
        label="リビング 温度",
        source="prometheus_text",
        endpoint="http://sb:9110",
        metric_name="switchbot_temperature_celsius",
        labels={"device_name": "リビング"},
        format="celsius",
    )
    result = await fetch(client, metric)
    assert result.value == 24.5
    assert result.detail == ""


@respx.mock
async def test_センサーが消えていたら値なし扱い(client):
    """電池切れなどで系列が消えたとき、古い値を出し続けないこと。"""
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(200, text=SWITCHBOT_TEXT)
    )
    metric = Metric(
        label="書斎 温度",
        source="prometheus_text",
        endpoint="http://sb:9110",
        metric_name="switchbot_temperature_celsius",
        labels={"device_name": "書斎"},
        format="celsius",
    )
    result = await fetch(client, metric)
    assert result.value is None
    assert "系列がありません" in result.detail


@respx.mock
async def test_exporter_が落ちていても例外にならない(client):
    respx.get("http://sb:9110/metrics").mock(side_effect=httpx.ConnectError("x"))
    metric = Metric(
        label="リビング 温度",
        source="prometheus_text",
        endpoint="http://sb:9110",
        metric_name="switchbot_temperature_celsius",
    )
    result = await fetch(client, metric)
    assert result.value is None
    assert "取得失敗" in result.detail


# --- 段階（カードの色）---------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [(0.0, "ok"), (0.005, "ok"), (0.01, "warn"), (0.03, "warn"),
     (0.05, "crit"), (0.9, "crit"), (None, "unknown")],
)
def test_閾値から段階が決まる(value, expected):
    result = MetricResult(
        label="ロス率",
        value=value,
        format="percent",
        thresholds={"warn": 0.01, "crit": 0.05},
    )
    assert result.level == expected


def test_閾値が無ければ常に_ok():
    assert MetricResult("x", 99999.0, "count", {}).level == "ok"


# --- 表示整形 ------------------------------------------------------------

@pytest.mark.parametrize(
    "value, fmt, expected",
    [
        (None, "percent", "—"),
        (None, "raw", "—"),
        (0.0423, "percent", "4.2 %"),
        (0.0185, "seconds_ms", "18.5 ms"),
        (57.4, "celsius", "57.4 °C"),
        (24.5, "celsius", "24.5 °C"),
        (55.0, "percent100", "55 %"),
        (556_686_024, "mbps", "557 Mbps"),
        (66_116_000, "mbps", "66 Mbps"),
        (12.53, "milliseconds", "12.5 ms"),
        (0.55, "percent", "55.0 %"),
        (3.0, "count", "3"),
        (0.0, "count", "0"),
        (512, "bytes", "512 B"),
        (2048, "bytes", "2.0 KiB"),
        (1610612736, "bytes", "1.5 GiB"),
        (90, "duration", "1 分"),
        (7200, "duration", "2 時間 0 分"),
        (267840, "duration", "3 日 2 時間"),
        (1.5, "raw", "1.5"),
    ],
)
def test_表示用の整形(value, fmt, expected):
    assert format_value(value, fmt) == expected
