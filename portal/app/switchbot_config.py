"""`config/switchbot.yml` の読み書き。

このファイルには 3 つの情報が入っている。

- `homes`   … ホームの表示順
- `devices` … デバイスの所属（ホーム・ルーム）
- `exclude` … 取得を止めているデバイス

**`exclude` だけがポータルの画面から書き換わる。** そのため書き戻しでは
必ず文書全体を読み直し、`homes` と `devices` をそのまま持ち越す
（ここを取りこぼすと、トグルを 1 回押しただけで所属の対応づけが全部消える）。

SwitchBot API v1.1 はホームもルームも返さないので、この対応づけは
設定として持つしかない。

exporter は 15 秒ごとにこのファイルを読み直すので、書き換えるだけで反映される。
ポータルから exporter を叩く必要はない。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

HEADER = """\
# ===========================================================================
# SwitchBot exporter の設定。
#
# ★ exclude の部分はポータルの /sensors 画面から更新される。
#   手で書いたコメントは書き戻しの際に失われるので注意。
#
# 照合は id で行う。name は人間が読むためのもので、照合には使わない
# （SwitchBot アプリで名前を変えても対応づけが外れないようにするため）。
#
# SwitchBot API v1.1 はホームもルームも返さないため、homes と devices の
# 対応づけはここで持つしかない。
# ===========================================================================
"""

DEFAULT_REASON = "画面から取得を停止"
UNASSIGNED = "未分類"


class ConfigWriteError(Exception):
    pass


def _path() -> Path:
    return Path(os.environ.get("SWITCHBOT_CONFIG", "/app/config/switchbot.yml"))


def load_document() -> dict[str, Any]:
    """設定全体を読む。壊れていても画面は開けること。"""
    try:
        data = yaml.safe_load(_path().read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def load_homes() -> list[str]:
    """ホームの表示順。設定に書かれた順がそのまま表示順になる。"""
    homes = load_document().get("homes") or []
    return [str(h) for h in homes if h]


def load_device_meta() -> dict[str, dict[str, str]]:
    """デバイスの所属。id -> {home, area, room}。

    設定に書いた順を保つ。エリアの表示順を「そのホームで最初に出てきた順」で
    決めているため、ここで並びが崩れると画面の並びも崩れる。
    """
    devices = load_document().get("devices") or {}
    if not isinstance(devices, dict):
        return {}

    meta: dict[str, dict[str, str]] = {}
    for device_id, info in devices.items():
        if isinstance(info, dict):
            meta[str(device_id)] = {
                "home": str(info.get("home") or UNASSIGNED),
                "area": str(info.get("area") or ""),
                "room": str(info.get("room") or ""),
            }
    return meta


def load_area_order() -> dict[str, list[str]]:
    """ホームごとのエリアの表示順。設定に最初に出てきた順。

    順番を別に書かせると、デバイスを足すたびに 2 か所直すことになり
    片方を忘れる。書いた順がそのまま表示順になるようにしている。
    """
    order: dict[str, list[str]] = {}
    for info in load_device_meta().values():
        areas = order.setdefault(info["home"], [])
        if info["area"] and info["area"] not in areas:
            areas.append(info["area"])
    return order


def load_excluded() -> dict[str, dict[str, str]]:
    """取得を止めているデバイス。id -> {name, reason}。"""
    excluded: dict[str, dict[str, str]] = {}
    for entry in load_document().get("exclude") or []:
        if isinstance(entry, dict) and entry.get("id"):
            excluded[str(entry["id"])] = {
                "name": str(entry.get("name") or entry["id"]),
                "reason": str(entry.get("reason") or DEFAULT_REASON),
            }
    return excluded


def _inherit_ownership(source: Path, target: Path) -> None:
    """元のファイルの所有者とパーミッションを引き継ぐ。

    ポータルのコンテナは root で動くので、これをやらないと書き戻した
    ファイルが root 所有 0600 になる。すると **開発機からの rsync 配備が
    失敗し、ホスト上で手編集もできなくなる**。実際に一度そうなった。
    """
    try:
        stat = source.stat()
    except FileNotFoundError:
        target.chmod(0o644)
        return

    target.chmod(stat.st_mode & 0o7777)
    try:
        os.chown(target, stat.st_uid, stat.st_gid)
    except (PermissionError, AttributeError):
        # root でなければ所有者は変えられない。その場合は元から
        # 自分の持ち物なので、そのままで問題ない。
        pass


def _write(excluded: dict[str, dict[str, str]]) -> None:
    """`exclude` を書き戻す。homes と devices はそのまま持ち越す。

    同じディレクトリに一時ファイルを作ってから置き換える。書き込み途中の
    ファイルを exporter が読むと、除外が一時的に空になって全センサーへの
    問い合わせが走ってしまうため。
    """
    path = _path()

    document = load_document()
    document["exclude"] = [
        {"id": device_id, "name": info["name"], "reason": info["reason"]}
        for device_id, info in sorted(excluded.items(), key=lambda kv: kv[1]["name"])
    ]

    # homes → devices → exclude の順で書く。読んだときに分かりやすいように。
    ordered = {}
    for key in ("homes", "devices", "exclude"):
        if key in document:
            ordered[key] = document[key]
    for key, value in document.items():
        if key not in ordered:
            ordered[key] = value

    text = HEADER + "\n" + yaml.safe_dump(
        ordered, allow_unicode=True, sort_keys=False, default_flow_style=False
    )

    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".switchbot-", delete=False
        ) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        _inherit_ownership(path, temporary)
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigWriteError(f"設定を保存できません: {exc}") from exc


def set_enabled(device_id: str, device_name: str, enabled: bool) -> None:
    """センサー 1 台の取得を切り替える。"""
    set_many_enabled({device_id: device_name}, enabled)


def set_many_enabled(devices: dict[str, str], enabled: bool) -> None:
    """複数のセンサーをまとめて切り替える。ホーム単位のトグルで使う。

    1 台ずつ書き戻すとホームの台数だけファイル差し替えが走り、その間に
    exporter が読むと中途半端な状態が反映される。まとめて 1 回で書く。
    """
    excluded = load_excluded()

    for device_id, device_name in devices.items():
        if enabled:
            excluded.pop(device_id, None)
        else:
            excluded[device_id] = {
                "name": device_name or device_id,
                # 既に止めていたなら、そのときの理由を残す
                "reason": excluded.get(device_id, {}).get("reason", DEFAULT_REASON),
            }

    _write(excluded)
