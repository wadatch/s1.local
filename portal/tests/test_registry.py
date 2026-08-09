"""services.yml の読み込みと検証。

このプロジェクトの中心は「設定ファイルに 1 行足すだけでサービスが増える」こと。
したがって最も守るべき性質は、**壊れたエントリが他のサービスを巻き込まないこと**。
"""

import textwrap

import pytest
import yaml

from app.registry import ConfigError, RegistryCache, load, parse


def parse_yaml(text: str):
    return parse(yaml.safe_load(textwrap.dedent(text)))


def test_最小構成のサービスを読める():
    registry = parse_yaml(
        """
        services:
          - id: foo
            name: フー
        """
    )
    assert len(registry.services) == 1
    service = registry.services[0]
    assert service.id == "foo"
    assert service.name == "フー"
    assert service.category == "other"
    assert service.health == {"type": "always_up"}
    assert service.errors == []


def test_メトリクスと閾値を読める():
    registry = parse_yaml(
        """
        services:
          - id: foo
            name: フー
            category: monitoring
            health:
              type: http
              url: http://example/health
            metrics:
              - label: ロス率
                source: prometheus
                endpoint: http://prom:9090
                query: 'up'
                format: percent
                thresholds: {warn: 0.01, crit: 0.05}
        """
    )
    metric = registry.services[0].metrics[0]
    assert metric.source == "prometheus"
    assert metric.query == "up"
    assert metric.format == "percent"
    assert metric.thresholds == {"warn": 0.01, "crit": 0.05}


def test_node_exporter_のフィールドを読める():
    registry = parse_yaml(
        """
        services:
          - id: host
            name: ホスト
            metrics:
              - label: CPU
                source: node_exporter
                endpoint: http://node:9100
                field: cpu_usage_ratio
                format: percent
        """
    )
    assert registry.services[0].metrics[0].field_name == "cpu_usage_ratio"


def test_prometheus_text_のラベルを読める():
    registry = parse_yaml(
        """
        services:
          - id: sensors
            name: 温湿度
            metrics:
              - label: リビング 温度
                source: prometheus_text
                endpoint: http://switchbot-exporter:9110
                metric: switchbot_temperature_celsius
                labels: {device_name: リビング}
                format: celsius
        """
    )
    metric = registry.services[0].metrics[0]
    assert metric.metric_name == "switchbot_temperature_celsius"
    assert metric.labels == {"device_name": "リビング"}


def test_prometheus_text_に_metric_が無いとエラー():
    registry = parse_yaml(
        """
        services:
          - id: sensors
            name: 温湿度
            metrics:
              - label: 温度
                source: prometheus_text
                endpoint: http://sb:9110
        """
    )
    assert any("metric が必要" in e for e in registry.services[0].errors)


def test_カテゴリの既定値を上書きできる():
    registry = parse_yaml(
        """
        categories:
          tools: わが家のツール
        services:
          - id: foo
            name: フー
        """
    )
    assert registry.categories["tools"] == "わが家のツール"
    # 定義しなかったものは既定値が残る
    assert registry.categories["monitoring"] == "監視"


# --- 壊れた設定の閉じ込め ------------------------------------------------

def test_壊れたエントリは他のサービスを巻き込まない():
    registry = parse_yaml(
        """
        services:
          - id: broken
            name: 壊れている
            metrics:
              - label: だめなやつ
                source: そんなソースはない
          - id: healthy
            name: 無事なほう
        """
    )
    broken, healthy = registry.services
    assert broken.errors, "不正な source がエラーとして記録されること"
    assert broken.metrics == [], "不正なメトリクスは捨てられること"
    assert healthy.errors == [], "隣のサービスは影響を受けないこと"


@pytest.mark.parametrize(
    "metric_yaml, expected_fragment",
    [
        ("source: prometheus\n                endpoint: http://p", "query が必要"),
        ("source: node_exporter\n                endpoint: http://p", "field が必要"),
        ("source: prometheus\n                query: up", "endpoint が必要"),
    ],
)
def test_メトリクスの必須項目が欠けているとエラーになる(metric_yaml, expected_fragment):
    registry = parse_yaml(
        f"""
        services:
          - id: foo
            name: フー
            metrics:
              - label: なにか
                {metric_yaml}
        """
    )
    assert any(expected_fragment in e for e in registry.services[0].errors)


def test_不正な_health_type_は常時稼働に落とす():
    registry = parse_yaml(
        """
        services:
          - id: foo
            name: フー
            health:
              type: telepathy
        """
    )
    service = registry.services[0]
    assert service.errors
    # 死活チェックが不正でも、サービス自体は一覧に残ること
    assert service.health == {"type": "always_up"}


def test_http_チェックに_url_が無いとエラー():
    registry = parse_yaml(
        """
        services:
          - id: foo
            name: フー
            health:
              type: http
        """
    )
    assert any("url が必要" in e for e in registry.services[0].errors)


def test_id_の重複を検出する():
    registry = parse_yaml(
        """
        services:
          - id: dup
            name: 一つ目
          - id: dup
            name: 二つ目
        """
    )
    assert any("重複" in e for e in registry.services[1].errors)


def test_services_キーが無ければ設定エラー():
    with pytest.raises(ConfigError, match="services"):
        parse_yaml("categories: {}")


def test_トップレベルがリストなら設定エラー():
    with pytest.raises(ConfigError):
        parse_yaml("- foo\n- bar")


# --- 再読み込み ----------------------------------------------------------

def test_ファイルを書き換えると再読み込みされる(tmp_path):
    path = tmp_path / "services.yml"
    path.write_text("services:\n  - id: a\n    name: A\n", encoding="utf-8")

    cache = RegistryCache(path)
    registry, error = cache.get()
    assert error is None
    assert [s.id for s in registry.services] == ["a"]

    # mtime が変わったことを確実にする
    path.write_text(
        "services:\n  - id: a\n    name: A\n  - id: b\n    name: B\n", encoding="utf-8"
    )
    import os
    os.utime(path, (0, 0))

    registry, error = cache.get()
    assert error is None
    assert [s.id for s in registry.services] == ["a", "b"], \
        "コンテナを再起動せずにサービスが増えること"


def test_壊れたファイルに書き換わったらエラーを返す(tmp_path):
    import os

    path = tmp_path / "services.yml"
    path.write_text("services:\n  - id: a\n    name: A\n", encoding="utf-8")
    cache = RegistryCache(path)
    assert cache.get()[1] is None

    path.write_text("services: [", encoding="utf-8")
    os.utime(path, (0, 0))

    registry, error = cache.get()
    assert registry is None
    assert error, "壊れたことが分かる状態になること（古い内容を出し続けない）"


def test_ファイルが無ければエラーを返す(tmp_path):
    registry, error = RegistryCache(tmp_path / "ない.yml").get()
    assert registry is None
    assert "読めません" in error


def test_同梱の_services_yml_が検証を通る():
    """リポジトリに入っている実物の設定が壊れていないこと。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "config" / "services.yml"
    if not path.exists():
        pytest.skip("config/services.yml がイメージに入っていない")

    registry = load(path)
    for service in registry.services:
        assert service.errors == [], f"{service.id}: {service.errors}"
