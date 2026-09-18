#!/usr/bin/env python3
"""会社別のASP計測リンク（アフィリエイト用の計測付きリンク）設定の読み込み。

ダッシュボードでは「ASP計測リンク」と呼ぶ。すでにアフィリエイト化済みのリンクなので、
記事作成時にこの列や詳細URLをさらにアフィリエイト化しない。

設定ファイルの置き場（優先順）:
1. ``config/affiliate_links/<provider_id>.json``（全社共通の新しい置き場）
2. ``config/<provider_id>_affiliate_links.json``（HIS / JTB / KNT の従来の置き場）

ファイルの形は従来と同じ ``category_links`` + ``keyword_overrides``。
ASP（afb・バリューコマース・A8 など）の違いはURL文字列だけで吸収し、``pixel`` は任意。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LINKS_DIR = ROOT / "config" / "affiliate_links"
TEMPLATE_PATH = LINKS_DIR / "_template.json"


def config_path(provider_id: str) -> Path | None:
    """設定ファイルの実在パスを返す。無ければ None。"""
    candidates = [
        LINKS_DIR / f"{provider_id}.json",
        ROOT / "config" / f"{provider_id}_affiliate_links.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def load_affiliate_config(provider_id: str) -> dict[str, Any]:
    path = config_path(provider_id)
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def has_links(config: dict[str, Any]) -> bool:
    """URLが1つでも入っているか（枠だけの空ファイルは False）。"""
    for link in (config.get("category_links") or {}).values():
        if isinstance(link, dict) and link.get("url"):
            return True
    for override in config.get("keyword_overrides") or []:
        if isinstance(override, dict) and override.get("url"):
            return True
    return False


def affiliate_setup_status(provider_id: str) -> str:
    """ダッシュボード表示用: 設定済み / 枠のみ / なし。"""
    path = config_path(provider_id)
    if not path:
        return "なし"
    return "設定済み" if has_links(load_affiliate_config(provider_id)) else "枠のみ（URL未設定）"


def resolve_affiliate_url(coupon: dict[str, Any], config: dict[str, Any]) -> str:
    """クーポン1件に対応するアフィリエイトURLを返す。設定が無ければ空文字。"""
    if not has_links(config):
        return ""
    from table_renderer import _get_affiliate_link

    url, _pixel = _get_affiliate_link(coupon, config)
    return url


def ensure_skeletons(provider_ids: list[str]) -> list[Path]:
    """枠だけの設定ファイルを作る（既存ファイルは触らない）。"""
    LINKS_DIR.mkdir(parents=True, exist_ok=True)
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8")) if TEMPLATE_PATH.exists() else {}
    created = []
    for provider_id in provider_ids:
        path = LINKS_DIR / f"{provider_id}.json"
        if path.exists() or config_path(provider_id):
            continue
        payload = dict(template)
        payload["_provider_id"] = provider_id
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append(path)
    return created


if __name__ == "__main__":
    import sys

    ids = sys.argv[1:]
    if not ids:
        registry = json.loads((ROOT / "config" / "provider_registry.json").read_text(encoding="utf-8"))
        ids = [provider["id"] for provider in registry["providers"]]
    for path in ensure_skeletons(ids):
        print(f"created: {path.relative_to(ROOT)}")
