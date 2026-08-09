"""config/services.yml の読み込みと検証。

このモジュールの責務は「設定ファイルの中身を信用できる形にしてから渡す」こと。

設定ミスは 1 サービス単位で閉じ込める。1 つのエントリの書き間違いで
ポータル全体が 500 を返すと、障害調査の入口としての価値が失われるため。
壊れたエントリはエラーを抱えたまま返し、そのカードだけをエラー表示にする。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_SOURCES = {"prometheus", "prometheus_text", "node_exporter", "none"}
VALID_HEALTH_TYPES = {"http", "tcp", "always_up"}
VALID_FORMATS = {
    "percent",
    "percent100",
    "seconds_ms",
    "bytes",
    "count",
    "celsius",
    "duration",
    "raw",
}

DEFAULT_CATEGORIES = {
    "home": "家の状態",
    "monitoring": "監視",
    "infra": "サーバ基盤",
    "tools": "自作ツール",
    "other": "その他",
}


class ConfigError(Exception):
    """services.yml 全体が読めない・形が違うときに投げる。"""


@dataclass
class Metric:
    label: str
    source: str
    endpoint: str | None = None
    query: str | None = None
    field_name: str | None = None
    # source=prometheus_text 用。metric 名と、系列を絞り込むラベル。
    metric_name: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    format: str = "raw"
    thresholds: dict[str, float] = field(default_factory=dict)
    # layout=matrix のカードで、この値をどのマスに置くか。
    row: str = ""
    column: str = ""


@dataclass
class Service:
    id: str
    name: str
    description: str = ""
    url: str | None = None
    category: str = "other"
    health: dict[str, Any] = field(default_factory=lambda: {"type": "always_up"})
    metrics: list[Metric] = field(default_factory=list)
    # "list"（既定）か "matrix"。matrix なら値を行×列の表に並べる。
    layout: str = "list"
    matrix_rows: list[str] = field(default_factory=list)
    matrix_columns: list[str] = field(default_factory=list)
    # 検証に失敗した理由。空でなければカードをエラー表示にする。
    errors: list[str] = field(default_factory=list)


@dataclass
class Registry:
    services: list[Service]
    categories: dict[str, str]


def _validate_metric(raw: Any, index: int, errors: list[str]) -> Metric | None:
    if not isinstance(raw, dict):
        errors.append(f"metrics[{index}]: マッピングではありません")
        return None

    label = raw.get("label")
    if not label:
        errors.append(f"metrics[{index}]: label が必要です")
        return None

    source = raw.get("source", "none")
    if source not in VALID_SOURCES:
        errors.append(
            f"metrics[{index}] ({label}): source '{source}' は不正です "
            f"（{'/'.join(sorted(VALID_SOURCES))} のいずれか）"
        )
        return None

    fmt = raw.get("format", "raw")
    if fmt not in VALID_FORMATS:
        errors.append(
            f"metrics[{index}] ({label}): format '{fmt}' は不正です "
            f"（{'/'.join(sorted(VALID_FORMATS))} のいずれか）"
        )
        fmt = "raw"

    if source == "prometheus" and not raw.get("query"):
        errors.append(f"metrics[{index}] ({label}): source=prometheus には query が必要です")
        return None
    if source == "node_exporter" and not raw.get("field"):
        errors.append(f"metrics[{index}] ({label}): source=node_exporter には field が必要です")
        return None
    if source == "prometheus_text" and not raw.get("metric"):
        errors.append(
            f"metrics[{index}] ({label}): source=prometheus_text には metric が必要です"
        )
        return None
    if source != "none" and not raw.get("endpoint"):
        errors.append(f"metrics[{index}] ({label}): endpoint が必要です")
        return None

    labels = raw.get("labels") or {}
    if not isinstance(labels, dict):
        errors.append(f"metrics[{index}] ({label}): labels はマッピングである必要があります")
        labels = {}

    thresholds = raw.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        errors.append(f"metrics[{index}] ({label}): thresholds はマッピングである必要があります")
        thresholds = {}

    return Metric(
        label=str(label),
        row=str(raw.get("row") or ""),
        column=str(raw.get("column") or ""),
        source=source,
        endpoint=raw.get("endpoint"),
        query=raw.get("query"),
        field_name=raw.get("field"),
        metric_name=raw.get("metric"),
        labels={str(k): str(v) for k, v in labels.items()},
        format=fmt,
        thresholds={k: float(v) for k, v in thresholds.items() if k in ("warn", "crit")},
    )


def _validate_service(raw: Any, index: int) -> Service:
    errors: list[str] = []

    if not isinstance(raw, dict):
        return Service(id=f"invalid-{index}", name=f"(不正なエントリ {index})",
                       errors=["マッピングではありません"])

    service_id = raw.get("id") or f"unnamed-{index}"
    name = raw.get("name") or service_id

    health = raw.get("health") or {"type": "always_up"}
    if not isinstance(health, dict):
        errors.append("health はマッピングである必要があります")
        health = {"type": "always_up"}
    elif health.get("type", "always_up") not in VALID_HEALTH_TYPES:
        errors.append(
            f"health.type '{health.get('type')}' は不正です "
            f"（{'/'.join(sorted(VALID_HEALTH_TYPES))} のいずれか）"
        )
        health = {"type": "always_up"}
    else:
        health = {**health, "type": health.get("type", "always_up")}
        if health["type"] == "http" and not health.get("url"):
            errors.append("health.type=http には url が必要です")
            health = {"type": "always_up"}
        if health["type"] == "tcp" and not (health.get("host") and health.get("port")):
            errors.append("health.type=tcp には host と port が必要です")
            health = {"type": "always_up"}

    metrics: list[Metric] = []
    raw_metrics = raw.get("metrics") or []
    if not isinstance(raw_metrics, list):
        errors.append("metrics はリストである必要があります")
        raw_metrics = []
    for i, raw_metric in enumerate(raw_metrics):
        metric = _validate_metric(raw_metric, i, errors)
        if metric is not None:
            metrics.append(metric)

    layout = str(raw.get("layout") or "list")
    matrix_rows: list[str] = []
    matrix_columns: list[str] = []
    if layout not in ("list", "matrix"):
        errors.append(f"layout '{layout}' は不正です（list / matrix のいずれか）")
        layout = "list"
    elif layout == "matrix":
        matrix = raw.get("matrix") or {}
        if not isinstance(matrix, dict):
            errors.append("matrix はマッピングである必要があります")
            layout = "list"
        else:
            matrix_rows = [str(r) for r in (matrix.get("rows") or [])]
            matrix_columns = [str(c) for c in (matrix.get("columns") or [])]
            if not matrix_rows or not matrix_columns:
                errors.append("layout=matrix には matrix.rows と matrix.columns が必要です")
                layout = "list"

    return Service(
        id=str(service_id),
        name=str(name),
        layout=layout,
        matrix_rows=matrix_rows,
        matrix_columns=matrix_columns,
        description=str(raw.get("description") or ""),
        url=raw.get("url"),
        category=str(raw.get("category") or "other"),
        health=health,
        metrics=metrics,
        errors=errors,
    )


def parse(data: Any) -> Registry:
    """YAML をパース済みの Python オブジェクトから Registry を作る。"""
    if not isinstance(data, dict):
        raise ConfigError("トップレベルがマッピングではありません")

    raw_services = data.get("services")
    if raw_services is None:
        raise ConfigError("services キーがありません")
    if not isinstance(raw_services, list):
        raise ConfigError("services はリストである必要があります")

    services = [_validate_service(raw, i) for i, raw in enumerate(raw_services)]

    seen: set[str] = set()
    for service in services:
        if service.id in seen:
            service.errors.append(f"id '{service.id}' が重複しています")
        seen.add(service.id)

    categories = {**DEFAULT_CATEGORIES}
    raw_categories = data.get("categories")
    if isinstance(raw_categories, dict):
        categories.update({str(k): str(v) for k, v in raw_categories.items()})

    return Registry(services=services, categories=categories)


def load(path: str | Path) -> Registry:
    """ファイルから読み込む。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML として読めません: {exc}") from exc
    return parse(data)


class RegistryCache:
    """更新時刻を見て、変わったときだけ読み直す。

    サービスの追加をコンテナの再起動なしに反映させるためのもの。
    設定ファイルの編集がそのまま画面に出ることが、このポータルの
    「追加のしやすさ」を実際に支えている。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._registry: Registry | None = None
        self._error: str | None = None

    def get(self) -> tuple[Registry | None, str | None]:
        """(Registry, エラーメッセージ) を返す。読めていれば後者は None。"""
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError as exc:
                self._registry, self._error = None, f"設定ファイルを読めません: {exc}"
                return self._registry, self._error

            if mtime != self._mtime or (self._registry is None and self._error is None):
                try:
                    self._registry = load(self._path)
                    self._error = None
                except (ConfigError, OSError) as exc:
                    # 直前まで読めていた内容は捨てる。古い内容を出し続けると
                    # 編集が反映されていないのか壊れているのか区別できない。
                    self._registry = None
                    self._error = str(exc)
                self._mtime = mtime

            return self._registry, self._error


def _main() -> int:
    """make check から呼ぶ検証用のエントリポイント。"""
    import argparse

    parser = argparse.ArgumentParser(description="services.yml を検証する")
    parser.add_argument("--validate", metavar="PATH", required=True)
    args = parser.parse_args()

    try:
        registry = load(args.validate)
    except (ConfigError, OSError) as exc:
        print(f"NG: {exc}")
        return 1

    broken = [s for s in registry.services if s.errors]
    for service in broken:
        for message in service.errors:
            print(f"NG: {service.id}: {message}")

    if broken:
        return 1

    print(f"OK: services.yml — {len(registry.services)} サービス, "
          f"{sum(len(s.metrics) for s in registry.services)} メトリクス")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
