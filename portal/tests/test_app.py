"""エンドポイントの振る舞い。

守るべき性質:
  - 監視系が全滅していてもトップページは 200 を返すこと
  - services.yml を書き換えたら、再起動なしに反映されること
"""

import os
import textwrap

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """services.yml を差し替えたアプリを組み立てる。"""

    def _make(yaml_text: str):
        path = tmp_path / "services.yml"
        path.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
        monkeypatch.setenv("SERVICES_FILE", str(path))
        # 設定を変えたテストごとに読み直させたいのでキャッシュを無効化する
        monkeypatch.setenv("STATUS_CACHE_SECONDS", "0")
        # 履歴の記録は裏で走り続けるので、ここでは止めておく
        monkeypatch.setenv("HISTORY_ENABLED", "0")

        import importlib
        from app import main as main_module

        importlib.reload(main_module)
        return main_module, path

    return _make


BASIC_YAML = """
    services:
      - id: svc
        name: サービス
        category: monitoring
        url: /somewhere
        health:
          type: http
          url: http://svc/health
        metrics:
          - label: ロス率
            source: prometheus
            endpoint: http://prom:9090
            query: 'up'
            format: percent
            thresholds: {warn: 0.01, crit: 0.05}
    """


@respx.mock
def test_トップページに値が出る(make_app):
    respx.get("http://svc/health").mock(return_value=httpx.Response(200))
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success",
                  "data": {"result": [{"metric": {}, "value": [0, "0.02"]}]}},
        )
    )

    main_module, _ = make_app(BASIC_YAML)
    with TestClient(main_module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "サービス" in body
    assert "2.0 %" in body
    assert "level-warn" in body, "閾値どおりの色になること"
    assert "監視" in body, "カテゴリ名で束ねられること"


@respx.mock
def test_監視系が全滅してもトップページは開ける(make_app):
    """ポータルは障害時にこそ開ける必要がある。"""
    respx.get("http://svc/health").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("http://prom:9090/api/v1/query").mock(
        side_effect=httpx.ConnectError("refused")
    )

    main_module, _ = make_app(BASIC_YAML)
    with TestClient(main_module.app) as client:
        response = client.get("/")
        status = client.get("/api/status").json()

    assert response.status_code == 200
    assert "DOWN" in response.text
    assert "—" in response.text, "取れなかった値はダッシュで出ること"
    assert status["services"][0]["health"]["status"] == "down"
    assert status["services"][0]["metrics"][0]["value"] is None


@respx.mock
def test_api_status_の形(make_app):
    respx.get("http://svc/health").mock(return_value=httpx.Response(200))
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success",
                  "data": {"result": [{"metric": {}, "value": [0, "0.5"]}]}},
        )
    )

    main_module, _ = make_app(BASIC_YAML)
    with TestClient(main_module.app) as client:
        payload = client.get("/api/status").json()

    assert payload["error"] is None
    assert payload["updated_at"] > 0
    service = payload["services"][0]
    assert service["id"] == "svc"
    assert service["url"] == "/somewhere"
    assert service["health"]["status"] == "up"
    assert service["metrics"][0] == {
        "label": "ロス率",
        "row": "",
        "column": "",
        "value": 0.5,
        "display": "50.0 %",
        "level": "crit",
        "detail": "",
    }
    assert service["layout"] == "list"
    assert service["matrix"] is None


@respx.mock
def test_サービスの追加が再起動なしで反映される(make_app):
    """このポータルの設計の中心。壊れたら真っ先に気づけるようにしておく。"""
    respx.get("http://svc/health").mock(return_value=httpx.Response(200))
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"result": []}}
        )
    )

    main_module, path = make_app(BASIC_YAML)
    with TestClient(main_module.app) as client:
        assert len(client.get("/api/status").json()["services"]) == 1

        # プロセスはそのままに設定ファイルだけを書き換える
        path.write_text(
            textwrap.dedent(BASIC_YAML)
            + "  - id: added\n"
            + "    name: あとから足したもの\n"
            + "    category: tools\n",
            encoding="utf-8",
        )
        os.utime(path, (0, 0))

        payload = client.get("/api/status").json()
        body = client.get("/").text

    assert payload["error"] is None
    assert [s["id"] for s in payload["services"]] == ["svc", "added"]
    assert "あとから足したもの" in body
    assert "自作ツール" in body, "新しいカテゴリの見出しも出ること"


def test_設定が壊れていてもトップページは_200_を返す(make_app):
    main_module, _ = make_app("services: [\n")
    with TestClient(main_module.app) as client:
        response = client.get("/")
        payload = client.get("/api/status").json()

    assert response.status_code == 200
    assert "設定ファイルを読み込めません" in response.text
    assert payload["error"]
    assert payload["services"] == []


@respx.mock
def test_設定ミスのあるサービスだけがエラー表示になる(make_app):
    respx.get("http://ok/health").mock(return_value=httpx.Response(200))

    main_module, _ = make_app(
        """
        services:
          - id: broken
            name: 設定を間違えたやつ
            metrics:
              - label: だめ
                source: 存在しないソース
          - id: fine
            name: 無事なやつ
            health:
              type: http
              url: http://ok/health
        """
    )
    with TestClient(main_module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "config-errors" in response.text
    assert "無事なやつ" in response.text, "隣のサービスは通常表示のままであること"


MATRIX_YAML = """
    services:
      - id: sensors
        name: 温湿度
        category: home
        layout: matrix
        matrix:
          columns: [屋外, 屋内]
          rows: [南棟, 北棟]
        metrics:
          - label: 南棟 屋外
            row: 南棟
            column: 屋外
            source: prometheus
            endpoint: http://prom:9090
            query: 'outside'
            format: celsius
          - label: 南棟 屋内
            row: 南棟
            column: 屋内
            source: prometheus
            endpoint: http://prom:9090
            query: 'inside'
            format: celsius
          - label: センサー台数
            source: prometheus
            endpoint: http://prom:9090
            query: 'count'
            format: count
    """


@respx.mock
def test_行列レイアウトが表になる(make_app):
    def value(number):
        return httpx.Response(200, json={"status": "success", "data": {
            "result": [{"metric": {}, "value": [0, number]}]}})

    # クエリごとに固定で返す。順番に依存させると、ページと API を
    # 続けて叩いたときに枯れてしまう。
    respx.get("http://prom:9090/api/v1/query", params={"query": "outside"}).mock(
        return_value=value("31.5"))
    respx.get("http://prom:9090/api/v1/query", params={"query": "inside"}).mock(
        return_value=value("26.0"))
    respx.get("http://prom:9090/api/v1/query", params={"query": "count"}).mock(
        return_value=value("19"))

    main_module, _ = make_app(MATRIX_YAML)
    with TestClient(main_module.app) as client:
        payload = client.get("/api/status").json()
        body = client.get("/").text

    matrix = payload["services"][0]["matrix"]
    assert matrix["columns"] == ["屋外", "屋内"]
    assert [r["label"] for r in matrix["rows"]] == ["南棟", "北棟"]

    south = matrix["rows"][0]["cells"]
    assert south[0]["display"] == "31.5 °C"
    assert south[1]["display"] == "26.0 °C"

    # 北棟のマスは設定していないので埋まらない
    assert matrix["rows"][1]["cells"] == [None, None]

    assert "<table class=\"matrix\">" in body
    assert "31.5 °C" in body
    assert "—" in body, "埋まらないマスはダッシュで出ること"


@respx.mock
def test_表に載らない値も消えずに出る(make_app):
    """設定の書き間違いにも、補助的な値にも気づけるようにするため。"""
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {
            "result": [{"metric": {}, "value": [0, "19"]}]}})
    )

    main_module, _ = make_app(MATRIX_YAML)
    with TestClient(main_module.app) as client:
        payload = client.get("/api/status").json()
        body = client.get("/").text

    extras = payload["services"][0]["matrix"]["extras"]
    assert [e["label"] for e in extras] == ["センサー台数"]
    assert "センサー台数" in body


@respx.mock
def test_センサー一覧の行に自動更新用の目印が付く(make_app, monkeypatch):
    """行の中身だけを差し替えるために、device_id と状態が要る。
    状態が変わったときだけページを読み込み直す判定にも使う。"""
    monkeypatch.setenv("SWITCHBOT_EXPORTER_URL", "http://sb:9110")
    respx.get("http://sb:9110/metrics").mock(
        return_value=httpx.Response(200, text=(
            '# TYPE switchbot_temperature_celsius gauge\n'
            'switchbot_temperature_celsius'
            '{device_id="A",device_name="リビング",device_type="Meter"} 24.5\n'
            '# TYPE switchbot_device_known gauge\n'
            'switchbot_device_known{device_id="A",device_name="リビング"} 1.0\n'
            'switchbot_device_known{device_id="B",device_name="止めたやつ"} 1.0\n'
            '# TYPE switchbot_device_excluded gauge\n'
            'switchbot_device_excluded{device_id="B",device_name="止めたやつ"} 1.0\n'
        ))
    )

    main_module, _ = make_app("services: []\n")
    assert main_module.SWITCHBOT_EXPORTER_URL == "http://sb:9110"

    with TestClient(main_module.app) as client:
        body = client.get("/sensors").text

    assert 'data-device-id="A"' in body
    assert 'data-state="active"' in body
    assert 'data-state="stopped"' in body
    assert 'cell-temperature' in body, "差し替える先のセルに目印が要る"
    assert '/static/sensors.js' in body
    assert '/static/refresh-ui.js' in body
    assert 'data-refresh-button' in body, "手で更新する手段が要る"


def test_グラフページが自動更新のスクリプトを読む(make_app):
    main_module, _ = make_app("services: []\n")
    with TestClient(main_module.app) as client:
        body = client.get("/graphs").text
    assert "/static/graphs.js" in body
    assert "/static/refresh-ui.js" in body
    assert "data-refresh-button" in body


def test_healthz(make_app):
    main_module, _ = make_app(BASIC_YAML)
    with TestClient(main_module.app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_サービスが空でも案内を出す(make_app):
    main_module, _ = make_app("services: []\n")
    with TestClient(main_module.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "まだ登録されていません" in response.text
