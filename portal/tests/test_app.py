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
        "value": 0.5,
        "display": "50.0 %",
        "level": "crit",
        "detail": "",
    }


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
