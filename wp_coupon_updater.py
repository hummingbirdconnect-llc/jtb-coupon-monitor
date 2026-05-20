#!/usr/bin/env python3
"""
WordPress クーポンページ自動更新スクリプト
==========================================
WP REST APIで現在のページを取得→Gutenberg HTMLを動的解析→
テーブルのtbodyだけ最新クーポンで差替え→下書き保存。

公開記事は直接上書きしない。公開中の記事は `<slug>-coupon-update`
という下書きコピーを作成・更新し、元記事は publish のまま維持する。
元記事自体が draft の場合だけ、その下書き本文を更新する。

Usage:
    python wp_coupon_updater.py --site yakushimafan
    python wp_coupon_updater.py --site yakushimafan --page his-coupon --dry-run
    python wp_coupon_updater.py --site yakushimafan --page his-coupon --rollback
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from html import escape as h
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import requests

from gutenberg_parser import (
    count_table_rows,
    extract_existing_affiliate_links,
    parse_page_sections,
)
from section_matcher import match_sections_to_coupons
from table_renderer import render_table_body

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config" / "wp_sites.json"
BACKUP_DIR = SCRIPT_DIR / "backups"
RESULT_FILE = SCRIPT_DIR / "wp_update_result.json"

REVIEW_DIFF_START = "<!-- coupon-review-diff-start -->"
REVIEW_DIFF_END = "<!-- coupon-review-diff-end -->"
REVIEW_ADD_CONTENT_START = "<!-- coupon-review-add-content-start -->"
REVIEW_ADD_CONTENT_END = "<!-- coupon-review-add-content-end -->"


# ============================================================
# 設定
# ============================================================

def load_sites_config() -> dict:
    """wp_sites.json 全体を読み込む。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_env_value(env_names: str | list[str]) -> str:
    """設定に書かれた環境変数名から値を取得する。配列なら先に見つかった値を使う。"""
    names = env_names if isinstance(env_names, list) else [env_names]
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value.strip()
    return ""


def load_site_config(site_id: str) -> dict:
    """wp_sites.json からサイト設定を読み込む。"""
    config = load_sites_config()
    site = config["sites"].get(site_id)
    if not site:
        print(f"❌ サイト '{site_id}' が設定に見つかりません")
        sys.exit(1)

    wp_url = resolve_env_value(site["wp_url_env"]).rstrip("/")
    wp_user = resolve_env_value(site["wp_user_env"])
    wp_app_password = resolve_env_value(site["wp_app_password_env"]).replace(" ", "")

    return {
        "site_id": site_id,
        "wp_url": wp_url,
        "wp_user": wp_user,
        "wp_app_password": wp_app_password,
        "pages": site["pages"],
        "raw": site,
    }


def validate_site_config(site_config: dict) -> None:
    """WordPress接続に必要な設定があるか確認する。"""
    missing = [
        key for key in ("wp_url", "wp_user", "wp_app_password")
        if not site_config.get(key)
    ]
    if missing:
        print(f"❌ WordPress認証情報が未設定です: {', '.join(missing)}")
        print("   config/wp_sites.json の *_env に対応する GitHub Secrets / 環境変数を設定してください")
        sys.exit(1)


def public_page_options() -> dict:
    """ダッシュボードに埋め込んでよいページ選択肢だけを返す。"""
    config = load_sites_config()
    sites: dict[str, dict] = {}
    for site_id, site in config.get("sites", {}).items():
        pages = []
        for page in site.get("pages", []):
            pages.append({
                "ota": page.get("ota", ""),
                "slug": page.get("slug", ""),
                "label": page.get("label") or f"{site_id} / {page.get('slug', '')}",
                "url": page.get("url", ""),
                "update_enabled": bool(page.get("update_enabled", True)),
                "note": page.get("note", ""),
            })
        sites[site_id] = {"pages": pages}
    return {"sites": sites}


def find_page_config(site_config: dict, slug: str) -> dict | None:
    for page in site_config["pages"]:
        if page.get("slug") == slug:
            return page
    return None


# ============================================================
# WP REST API
# ============================================================

def wp_auth(site_config: dict) -> tuple[str, str]:
    return site_config["wp_user"], site_config["wp_app_password"]


def wp_api_url(site_config: dict, endpoint: str) -> str:
    return f"{site_config['wp_url']}/wp-json/wp/v2/{endpoint.lstrip('/')}"


def fetch_wp_post(site_config: dict, slug: str) -> dict:
    """WP REST API でページを取得（context=edit で raw HTML）。"""
    url = wp_api_url(site_config, "posts")
    params = {
        "slug": slug,
        "status": "publish,draft,pending,private",
        "context": "edit",
        "per_page": 10,
    }

    resp = requests.get(url, params=params, auth=wp_auth(site_config), timeout=30)
    resp.raise_for_status()

    posts = resp.json()
    if not posts:
        raise ValueError(f"ページが見つかりません: slug={slug}")
    for post in posts:
        if post.get("slug") == slug:
            return post
    return posts[0]


def wp_post_title(post: dict) -> str:
    title = post.get("title", {})
    if isinstance(title, dict):
        return title.get("raw") or re.sub(r"<[^>]+>", "", title.get("rendered", ""))
    return str(title)


def wp_post_content(post: dict) -> str:
    content = post.get("content", {})
    if isinstance(content, dict):
        return content.get("raw") or content.get("rendered", "")
    return str(content)


def update_wp_post_content(
    site_config: dict,
    post_id: int,
    new_content: str,
    *,
    title: str | None = None,
) -> dict:
    """WP REST API で既存下書きの本文を更新する。statusは変更しない。"""
    url = wp_api_url(site_config, f"posts/{post_id}")

    payload = {"content": new_content}
    if title:
        payload["title"] = title
    resp = requests.post(url, json=payload, auth=wp_auth(site_config), timeout=30)
    resp.raise_for_status()
    return resp.json()


def create_wp_draft(site_config: dict, title: str, slug: str, content: str) -> dict:
    """WP REST API でクーポン更新確認用の下書きを作成する。"""
    url = wp_api_url(site_config, "posts")
    payload = {
        "title": title,
        "slug": slug,
        "content": content,
        "status": "draft",
    }
    resp = requests.post(url, json=payload, auth=wp_auth(site_config), timeout=30)
    resp.raise_for_status()
    return resp.json()


def save_coupon_update_draft(
    site_config: dict,
    source_post: dict,
    source_slug: str,
    new_content: str,
) -> dict:
    """公開記事は分岐下書きへ、下書き記事は同じ下書きへ保存する。"""
    source_status = source_post.get("status", "")
    source_id = source_post["id"]

    if source_status == "draft":
        result = update_wp_post_content(site_config, source_id, new_content)
        return {
            "target": "source_draft",
            "target_post_id": result["id"],
            "target_slug": result.get("slug", source_slug),
            "target_status": result.get("status", "draft"),
            "message": "元記事が下書きのため同じ下書きを更新",
        }

    branch_slug = f"{source_slug}-coupon-update"
    branch_title = f"{wp_post_title(source_post)}【クーポン更新案】"
    try:
        branch_post = fetch_wp_post(site_config, branch_slug)
    except ValueError:
        branch_post = None

    if branch_post:
        branch_status = branch_post.get("status", "")
        if branch_status != "draft":
            return {
                "target": "blocked",
                "reason": (
                    f"分岐先 {branch_slug} が draft ではありません"
                    f"（status={branch_status}）。安全のため停止しました"
                ),
            }
        result = update_wp_post_content(
            site_config,
            branch_post["id"],
            new_content,
            title=branch_title,
        )
        return {
            "target": "branch_draft_updated",
            "target_post_id": result["id"],
            "target_slug": result.get("slug", branch_slug),
            "target_status": result.get("status", "draft"),
            "message": f"公開元は変更せず、既存の分岐下書き {branch_slug} を更新",
        }

    result = create_wp_draft(site_config, branch_title, branch_slug, new_content)
    return {
        "target": "branch_draft_created",
        "target_post_id": result["id"],
        "target_slug": result.get("slug", branch_slug),
        "target_status": result.get("status", "draft"),
        "message": f"公開元は変更せず、分岐下書き {branch_slug} を作成",
    }


def rollback_from_backup(site_config: dict, slug: str):
    """最新バックアップから復元。"""
    pattern = str(BACKUP_DIR / f"{slug}_*.html")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"❌ バックアップが見つかりません: {slug}")
        sys.exit(1)
    latest = files[-1]
    print(f"🔄 バックアップから復元: {latest}")

    content = Path(latest).read_text(encoding="utf-8")
    post = fetch_wp_post(site_config, slug)
    if post.get("status") != "draft":
        print(f"❌ rollback対象がdraftではありません（status={post.get('status')}）。直接更新はしません")
        sys.exit(1)
    update_wp_post_content(site_config, post["id"], content)
    print(f"✅ 復元完了（下書き保存）: post_id={post['id']}")


# ============================================================
# クーポンデータ読み込み
# ============================================================

def load_latest_coupons(data_dir: str) -> list[dict]:
    """最新のクーポンJSONを読み込む。"""
    data_path = SCRIPT_DIR / data_dir
    pattern = str(data_path / "coupons_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"クーポンデータなし: {data_path}")
    latest = files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  📊 クーポン読み込み: {Path(latest).name} ({len(data)}件)")
    return data


def load_affiliate_config(config_path: str) -> dict:
    """アフィリエイト設定を読み込む。"""
    if not config_path:
        return {}
    full_path = SCRIPT_DIR / config_path
    if not full_path.exists() or not full_path.is_file():
        print(f"  ⚠️ アフィリエイト設定なし: {config_path}")
        return {}
    with open(full_path, "r", encoding="utf-8") as f:
        return json.load(f)


def coupon_search_text(coupon: dict) -> str:
    """ページ別フィルタに使う検索対象テキストを作る。"""
    detail = coupon.get("detail_data") or {}
    parts = [
        coupon.get("id", ""),
        coupon.get("category", ""),
        coupon.get("provider", ""),
        coupon.get("provider_label", ""),
        coupon.get("title", ""),
        coupon.get("discount", ""),
        coupon.get("area", ""),
        coupon.get("type", ""),
        coupon.get("product_type", ""),
        coupon.get("target", ""),
        coupon.get("source_url", ""),
        coupon.get("placement_hint", ""),
        detail.get("booking_period", ""),
        detail.get("stay_period", ""),
        detail.get("discount", ""),
        coupon.get("booking_period", ""),
        coupon.get("stay_period", ""),
        coupon.get("travel_period", ""),
        " ".join(detail.get("coupon_codes", [])),
        " ".join(detail.get("passwords", [])),
        " ".join(coupon.get("conditions", []) or []),
        " ".join(detail.get("conditions", [])),
        " ".join(detail.get("notes", [])),
    ]
    for item in coupon.get("coupon_codes", []):
        if isinstance(item, dict):
            parts.extend([
                item.get("code", ""),
                item.get("condition", ""),
                item.get("discount", ""),
            ])
    return " ".join(str(p) for p in parts if p).lower()


def filter_coupons_for_page(coupons: list[dict], page_config: dict) -> tuple[list[dict], dict]:
    """include_keywords / exclude_keywords に従ってページ対象のクーポンだけに絞る。"""
    include = [kw.lower() for kw in page_config.get("include_keywords", []) if kw]
    exclude = [kw.lower() for kw in page_config.get("exclude_keywords", []) if kw]
    filtered = []
    excluded_by_include = 0
    excluded_by_exclude = 0

    for coupon in coupons:
        text = coupon_search_text(coupon)
        if include and not any(kw in text for kw in include):
            excluded_by_include += 1
            continue
        if exclude and any(kw in text for kw in exclude):
            excluded_by_exclude += 1
            continue
        filtered.append(coupon)

    return filtered, {
        "input": len(coupons),
        "output": len(filtered),
        "include_keywords": include,
        "exclude_keywords": exclude,
        "excluded_by_include": excluded_by_include,
        "excluded_by_exclude": excluded_by_exclude,
    }


# ============================================================
# 安全チェック
# ============================================================

def normalize_outside_tbody(html: str) -> str:
    """tbody以外が変わっていないか確認するため、tbodyだけをプレースホルダ化する。"""
    return re.sub(r"<tbody>.*?</tbody>", "<tbody>__COUPON_ROWS__</tbody>", html, flags=re.DOTALL)


def count_affiliate_markers(html: str) -> int:
    """アフィリエイトURLや計測タグらしきURLの数を数える。"""
    patterns = [
        r"https?://t\.afi-b\.com/",
        r"https?://ck\.jp\.ap\.valuecommerce\.com/",
        r"https?://ad\.jp\.ap\.valuecommerce\.com/",
        r"https?://px\.a8\.net/",
        r"https?://www\.a8\.net/",
        r"https?://hb\.afl\.rakuten\.co\.jp/",
    ]
    return sum(len(re.findall(pattern, html)) for pattern in patterns)


def safety_check(old_html: str, new_html: str, replacements_count: int) -> dict:
    """更新前後のHTMLを比較し、異常がないかチェック。"""
    if replacements_count == 0:
        return {"passed": False, "reason": "差し替え対象のクーポンテーブルが見つかりません"}

    old_rows = count_table_rows(old_html)
    new_rows = count_table_rows(new_html)

    if new_rows == 0 and old_rows > 0:
        return {"passed": False, "reason": f"テーブル行が0件に（旧: {old_rows}件）"}

    if old_rows > 0 and new_rows < old_rows * 0.5:
        return {
            "passed": False,
            "reason": f"テーブル行が50%以上減少（{old_rows}→{new_rows}）",
        }

    old_affiliate_markers = count_affiliate_markers(old_html)
    new_affiliate_markers = count_affiliate_markers(new_html)
    if old_affiliate_markers > 0 and new_affiliate_markers == 0:
        return {
            "passed": False,
            "reason": "アフィリエイトURLまたは計測タグが消失しました",
        }
    if old_affiliate_markers > 0 and new_affiliate_markers < old_affiliate_markers * 0.5:
        return {
            "passed": False,
            "reason": (
                "アフィリエイトURLまたは計測タグが50%以上減少"
                f"（{old_affiliate_markers}→{new_affiliate_markers}）"
            ),
        }

    # Gutenbergブロックコメントの対応チェック
    opens = len(re.findall(r'<!-- wp:table', new_html))
    closes = len(re.findall(r'<!-- /wp:table -->', new_html))
    if opens != closes:
        return {
            "passed": False,
            "reason": f"wp:table の開始/終了が不一致（開始:{opens}, 終了:{closes}）",
        }

    old_table_blocks = len(re.findall(r'<!-- wp:table', old_html))
    new_table_blocks = len(re.findall(r'<!-- wp:table', new_html))
    if old_table_blocks != new_table_blocks:
        return {
            "passed": False,
            "reason": f"wp:table 数が変化（旧:{old_table_blocks}, 新:{new_table_blocks}）",
        }

    old_headings = re.findall(r"<h([1-6])[^>]*>.*?</h\1>", old_html, flags=re.DOTALL)
    new_headings = re.findall(r"<h([1-6])[^>]*>.*?</h\1>", new_html, flags=re.DOTALL)
    if old_headings != new_headings:
        return {"passed": False, "reason": "見出し構造が変化しました"}

    if normalize_outside_tbody(old_html) != normalize_outside_tbody(new_html):
        return {
            "passed": False,
            "reason": "tbody以外のHTMLが変化しました。クーポン表限定更新ではないため停止しました",
        }

    return {
        "passed": True,
        "old_rows": old_rows,
        "new_rows": new_rows,
        "replacements": replacements_count,
    }


# ============================================================
# 差分レビュー用HTML
# ============================================================

def has_review_markup(html: str) -> bool:
    """差分レビュー用の赤/青マークが入っているか判定する。"""
    return REVIEW_DIFF_START in html and REVIEW_DIFF_END in html


def finalize_review_markup(html: str) -> tuple[str, int]:
    """
    赤/青の差分レビューを確定し、青側の本文だけを残す。

    赤側は削除予定、青側は追加予定として保存しているため、
    確定時は diff ブロック全体を青側の中身に置換する。
    """
    pattern = re.compile(
        re.escape(REVIEW_DIFF_START)
        + r".*?"
        + re.escape(REVIEW_ADD_CONTENT_START)
        + r"(.*?)"
        + re.escape(REVIEW_ADD_CONTENT_END)
        + r".*?"
        + re.escape(REVIEW_DIFF_END),
        re.DOTALL,
    )

    replacements = 0

    def replace(match: re.Match) -> str:
        nonlocal replacements
        replacements += 1
        return clean_review_visual_markup(match.group(1).strip())

    return pattern.sub(replace, html), replacements


def clean_review_visual_markup(html: str) -> str:
    """差分確認用の色・ラベルだけを外す。"""
    html = re.sub(
        r'<span class="coupon-review-label"[^>]*>.*?</span><br\s*/?>',
        "",
        html,
        flags=re.DOTALL,
    )

    def clean_li(match: re.Match) -> str:
        tag = match.group(0)
        if 'data-coupon-review="' not in tag:
            return tag
        tag = re.sub(r'\sdata-coupon-review="[^"]*"', "", tag)
        tag = re.sub(r'\sstyle="[^"]*"', "", tag)

        def clean_class(class_match: re.Match) -> str:
            classes = [
                part for part in class_match.group(1).split()
                if part not in ("coupon-review-added-item", "coupon-review-deleted-item")
            ]
            return f' class="{" ".join(classes)}"' if classes else ""

        return re.sub(r'\sclass="([^"]*)"', clean_class, tag)

    return re.sub(r"<li\b[^>]*>", clean_li, html, flags=re.DOTALL)


def extract_deleted_review_content(red_part: str) -> str:
    """既存レビューの赤側から、元HTMLだけを取り出す。"""
    red_only = red_part.split('<!-- wp:group {"className":"coupon-review-added"} -->', 1)[0]
    group_match = re.search(
        r'<!-- /wp:paragraph -->\s*(.*?)\s*</div>\s*<!-- /wp:group -->\s*$',
        red_only,
        flags=re.DOTALL,
    )
    if group_match:
        return group_match.group(1).strip()
    return clean_review_visual_markup(red_only.strip())


def restore_review_deleted_baseline(html: str) -> tuple[str, int]:
    """
    レビュー状態のHTMLを、赤側（削除予定）を基準に戻す。

    既存のまるごと差分から1件単位差分へ作り直すときに使う。
    """
    pattern = re.compile(
        re.escape(REVIEW_DIFF_START)
        + r"(.*?)"
        + re.escape(REVIEW_ADD_CONTENT_START)
        + r".*?"
        + re.escape(REVIEW_ADD_CONTENT_END)
        + r".*?"
        + re.escape(REVIEW_DIFF_END),
        re.DOTALL,
    )
    replacements = 0

    def replace(match: re.Match) -> str:
        nonlocal replacements
        replacements += 1
        return extract_deleted_review_content(match.group(1))

    return pattern.sub(replace, html), replacements


def build_review_diff_block(old_html: str, new_html: str, label: str) -> str:
    """削除予定を赤、追加予定を青で表示する確認用ブロックを作る。"""
    return (
        f"{REVIEW_DIFF_START}\n"
        '<!-- wp:group {"className":"coupon-review-deleted"} -->\n'
        '<div class="wp-block-group coupon-review-deleted" '
        'style="border:2px solid #dc2626;border-left-width:8px;'
        'padding:16px;margin:20px 0;background:#fff1f2;color:#b91c1c;">'
        '<!-- wp:paragraph -->\n'
        f'<p><strong>削除予定（赤）: {h(label)}</strong></p>\n'
        '<!-- /wp:paragraph -->\n'
        f"{old_html}\n"
        "</div>\n"
        "<!-- /wp:group -->\n\n"
        '<!-- wp:group {"className":"coupon-review-added"} -->\n'
        '<div class="wp-block-group coupon-review-added" '
        'style="border:2px solid #2563eb;border-left-width:8px;'
        'padding:16px;margin:20px 0;background:#eff6ff;color:#1d4ed8;">'
        '<!-- wp:paragraph -->\n'
        f'<p><strong>追加予定（青）: {h(label)}</strong></p>\n'
        '<!-- /wp:paragraph -->\n'
        f"{REVIEW_ADD_CONTENT_START}\n"
        f"{new_html}\n"
        f"{REVIEW_ADD_CONTENT_END}\n"
        "</div>\n"
        "<!-- /wp:group -->\n"
        f"{REVIEW_DIFF_END}"
    )


def tint_review_list_item(raw_html: str, label: str, tone: str) -> str:
    """リスト項目1件をレビュー用に赤または青で表示する。"""
    if tone == "red":
        class_name = "coupon-review-deleted-item"
        style = "border:2px solid #dc2626;border-left-width:8px;padding:12px;margin:12px 0;background:#fff1f2;color:#b91c1c;"
    else:
        class_name = "coupon-review-added-item"
        style = "border:2px solid #2563eb;border-left-width:8px;padding:12px;margin:12px 0;background:#eff6ff;color:#1d4ed8;"

    def replace_li(match: re.Match) -> str:
        tag = match.group(0)
        if "class=" in tag:
            tag = re.sub(r'class="([^"]*)"', f'class="\\1 {class_name}"', tag, count=1)
        else:
            tag = tag[:-1] + f' class="{class_name}">'
        tag = tag[:-1] + f' data-coupon-review="{tone}" style="{style}">'
        return (
            tag
            + f'<span class="coupon-review-label" style="font-weight:700;">{h(label)}</span><br>'
        )

    return re.sub(r"<li\b[^>]*>", replace_li, raw_html, count=1, flags=re.DOTALL)


def build_review_list_item_diff(old_html: str, new_html: str, label: str) -> str:
    """クーポン1件単位の赤/青差分を作る。"""
    parts = [REVIEW_DIFF_START]
    if old_html:
        parts.append(tint_review_list_item(old_html, f"削除予定（赤）: {label}", "red"))
    parts.append(REVIEW_ADD_CONTENT_START)
    if new_html:
        parts.append(tint_review_list_item(new_html, f"追加予定（青）: {label}", "blue"))
    parts.append(REVIEW_ADD_CONTENT_END)
    parts.append(REVIEW_DIFF_END)
    return "\n".join(parts)


def apply_review_replacements(html: str, replacements: list[dict]) -> str:
    """置換位置がずれないよう、後方から差分ブロックへ置換する。"""
    new_html = html
    for item in sorted(replacements, key=lambda r: r["start"], reverse=True):
        old_html = new_html[item["start"]:item["end"]]
        if item.get("already_reviewed"):
            review_block = item["new_html"]
        else:
            review_block = build_review_diff_block(old_html, item["new_html"], item["label"])
        new_html = new_html[:item["start"]] + review_block + new_html[item["end"]:]
    return new_html


def heading_blocks(html: str) -> list[dict]:
    """Gutenberg見出しブロックを位置情報付きで返す。"""
    pattern = re.compile(
        r'<!-- wp:heading\s*(\{[^}]*\})?\s*-->\s*'
        r'<h(\d)([^>]*)>(.*?)</h\d>\s*'
        r'<!-- /wp:heading -->',
        re.DOTALL,
    )
    blocks = []
    for match in pattern.finditer(html):
        blocks.append({
            "start": match.start(),
            "end": match.end(),
            "level": int(match.group(2)),
            "attrs": match.group(3) or "",
            "text": re.sub(r"<[^>]+>", "", match.group(4)).strip(),
            "raw_html": match.group(0),
        })
    return blocks


def find_coupon_list_section(html: str) -> dict | None:
    """
    テーブルではないクーポン一覧セクションを探す。

    現在のWelltrip記事では、H3「配布中の主な...クーポン一覧」から次のH2までが
    更新対象のクーポン情報ブロックになっている。
    """
    headings = heading_blocks(html)
    if not headings:
        return None

    candidate = None
    for block in headings:
        text = block["text"]
        attrs = block.get("attrs", "")
        if 'id="available' in attrs and "クーポン" in text:
            candidate = block
            break
        if 'id="coupon' in attrs and "クーポン" in text:
            candidate = block
            break
        if block["level"] in (2, 3) and "クーポン早見表" in text:
            candidate = block
            break
        if block["level"] in (2, 3) and "配布中" in text and "クーポン" in text:
            candidate = block
            break
        if block["level"] in (2, 3) and "クーポン一覧" in text:
            candidate = block
            break

    if not candidate:
        return None

    end = len(html)
    for block in headings:
        if block["start"] <= candidate["start"]:
            continue
        if block["level"] <= candidate["level"]:
            end = block["start"]
            break

    section_html = html[candidate["start"]:end]
    first_non_coupon = len(section_html)
    for marker in ("<!-- wp:loos/cap-block", "<!-- wp:group"):
        marker_pos = section_html.find(marker)
        if marker_pos != -1:
            first_non_coupon = min(first_non_coupon, marker_pos)
    last_list_end = None
    for match in re.finditer(r"<!-- /wp:list -->", section_html[:first_non_coupon]):
        last_list_end = match.end()
    if last_list_end:
        end = candidate["start"] + last_list_end

    return {
        "start": candidate["start"],
        "end": end,
        "heading": candidate,
        "raw_html": html[candidate["start"]:end],
    }


def extract_valuecommerce_template(html: str) -> dict:
    """既存HTMLからValueCommerceのsid/pidを抽出し、新規リンク生成に使う。"""
    match = re.search(
        r"//ck\.jp\.ap\.valuecommerce\.com/servlet/referral\?sid=([^&\"']+)&amp;pid=([^&\"']+)",
        html,
    )
    if not match:
        return {}
    return {"sid": match.group(1), "pid": match.group(2)}


def valuecommerce_link(detail_url: str, template: dict) -> tuple[str, str]:
    """JTB詳細URLを既存ValueCommerce形式のリンクにする。"""
    if not detail_url or not template:
        return "", ""
    target = detail_url
    if "utm_source=" not in target:
        separator = "&" if "?" in target else "?"
        target = f"{target}{separator}utm_source=vcdom&utm_medium=affiliate"
    encoded = quote(target, safe="")
    sid = template["sid"]
    pid = template["pid"]
    url = f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&amp;pid={pid}&amp;vc_url={encoded}"
    pixel = f"//ad.jp.ap.valuecommerce.com/servlet/gifbanner?sid={sid}&amp;pid={pid}"
    return url, pixel


def normalize_url_key(url: str) -> str:
    """URL照合用に、ValueCommerce経由URLや計測パラメータの差を吸収する。"""
    if not url:
        return ""
    url = html_unescape(str(url)).strip()
    if url.startswith("//"):
        url = "https:" + url

    parsed = urlparse(url)
    if "valuecommerce.com" in parsed.netloc:
        query = parse_qs(parsed.query)
        vc_url = query.get("vc_url", [""])[0]
        if vc_url:
            url = unquote(vc_url)
            if url.startswith("//"):
                url = "https:" + url
            parsed = urlparse(url)

    query = parse_qs(parsed.query, keep_blank_values=True)
    noisy = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "yclid", "fbclid"}
    kept_query = []
    for key, values in query.items():
        if key in noisy:
            continue
        for value in values:
            kept_query.append((key, value))
    query_string = "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
        for key, value in kept_query
    )

    path = re.sub(r"/+$", "", parsed.path or "")
    normalized = urlunparse((
        parsed.scheme.lower() or "https",
        parsed.netloc.lower(),
        path,
        "",
        query_string,
        "",
    ))
    return normalized


def extract_href_urls(html: str) -> list[str]:
    """HTMLからhref属性を抜き出す。"""
    return [
        html_unescape(match.group(1))
        for match in re.finditer(r'href=["\']([^"\']+)["\']', html or "", flags=re.IGNORECASE)
    ]


JALPACK_URL_PATTERNS = [
    ("jalpack-domestic-birthday", ("domtour/birthday-cpn",)),
    ("jalpack-overseas-birthday", ("intltour/birthday-cpn",)),
    ("jalpack-domestic-timesale", ("domtour/jaldp/time_sale",)),
    ("jalpack-overseas-timesale", ("intltour/jaldp/timesale",)),
    ("jalpack-lsp-star", ("domtour/lsp-coupon",)),
    ("jalpack-hayakime", ("domtour/hayakime-cpn", "tours/guide/readme/discount")),
    ("jalpack-jalcard", ("jalcard", "jalcard_cpn")),
]


def jalpack_id_from_url(url: str) -> str:
    """JAL公式URLから共通JSON側のJALパックIDを推定する。"""
    key = normalize_url_key(url).lower()
    for coupon_id, needles in JALPACK_URL_PATTERNS:
        if any(needle in key for needle in needles):
            return coupon_id
    return ""


def normalize_title_key(text: str) -> str:
    """タイトル照合用に装飾語と空白を落とす。"""
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = html_unescape(text)
    text = re.sub(r"JALパッククーポン|クーポン|割引|特典|公式|確認する|→", "", text)
    return re.sub(r"\s+", "", text).lower()


def jalpack_id_from_title(text: str) -> str:
    """既存記事の表示タイトルからJALパックIDを推定する。"""
    key = normalize_title_key(text)
    if "海外" in key and ("タイムセール" in key or "公開コード" in key):
        return "jalpack-overseas-timesale"
    if "国内" in key and "タイムセール" in key:
        return "jalpack-domestic-timesale"
    if "海外" in key and "バースデー" in key:
        return "jalpack-overseas-birthday"
    if "国内" in key and "バースデー" in key:
        return "jalpack-domestic-birthday"
    if "早決" in key:
        return "jalpack-hayakime"
    if "star" in key or "lsp" in key:
        return "jalpack-lsp-star"
    if "jalカード" in key or "カード会員" in key:
        return "jalpack-jalcard"
    return ""


def coupon_url_index(coupons: list[dict] | None) -> dict[str, str]:
    """最新クーポンのURLからIDへ引ける辞書を作る。"""
    index: dict[str, str] = {}
    for coupon in coupons or []:
        coupon_id = str(coupon.get("id", ""))
        if not coupon_id:
            continue
        for key in ("detail_url", "source_url"):
            url_key = normalize_url_key(coupon.get(key, ""))
            if url_key:
                index[url_key] = coupon_id
    return index


def coupon_list_bucket(coupon: dict) -> str:
    """JTB国内クーポンを既存記事に近い見出し単位へゆるく分類する。"""
    text = " ".join(str(coupon.get(key, "")) for key in ("title", "category", "type", "area"))
    if any(keyword in text for keyword in ("初回", "新規", "会員")):
        return "全国・共通で使いやすいクーポン"
    if any(keyword in text for keyword in ("航空", "JAL", "ANA", "JR", "新幹線", "往復")):
        return "航空機・JR利用の国内ツアークーポン"
    if any(keyword in text for keyword in ("ホテル", "宿泊", "旅館", "施設", "ブランド")):
        return "ホテルブランド・テーマ施設のクーポン"
    return "その他の国内クーポン"


REGION_GROUPS = {
    "east": {
        "coupon": {
            "北海道", "東北", "青森", "岩手", "宮城", "秋田", "山形", "福島",
            "関東", "東京", "神奈川", "千葉", "埼玉", "茨城", "栃木", "群馬",
            "甲信越", "山梨", "長野", "新潟", "伊豆", "静岡",
        },
        "bucket": {"北海道", "東北", "関東", "甲信越", "伊豆", "東日本"},
    },
    "west": {
        "coupon": {
            "関西", "近畿", "大阪", "京都", "兵庫", "奈良", "滋賀", "和歌山",
            "中四国", "中国", "四国", "鳥取", "島根", "岡山", "広島", "山口",
            "徳島", "香川", "愛媛", "高知", "九州", "福岡", "佐賀", "長崎",
            "熊本", "大分", "宮崎", "鹿児島", "沖縄",
        },
        "bucket": {"関西", "近畿", "中四国", "中国", "四国", "九州", "沖縄", "西日本"},
    },
}


PRODUCT_KEYWORDS = {
    "transport": {"航空", "JAL", "ANA", "JR", "新幹線", "鉄道", "往復", "交通"},
    "lodging": {"宿泊", "ホテル", "旅館", "施設", "温泉", "リゾート", "ブランド"},
    "common": {"全国", "共通", "初回", "新規", "会員", "全商品", "全方面"},
}


def coupon_placement_text(coupon: dict) -> str:
    """配置先判定に使うクーポン側テキストをまとめる。"""
    detail = coupon.get("detail_data") or {}
    parts = [
        coupon.get("id", ""),
        coupon.get("title", ""),
        coupon.get("category", ""),
        coupon.get("area", ""),
        coupon.get("type", ""),
        coupon.get("product_type", ""),
        coupon.get("target", ""),
        coupon.get("discount", ""),
        coupon.get("source_url", ""),
        coupon.get("placement_hint", ""),
        coupon.get("booking_period", ""),
        coupon.get("stay_period", ""),
        coupon.get("travel_period", ""),
        detail.get("booking_period", ""),
        detail.get("stay_period", ""),
        detail.get("discount", ""),
        " ".join(str(item) for item in coupon.get("conditions", []) or []),
        " ".join(str(item) for item in detail.get("conditions", []) or []),
        " ".join(str(item) for item in detail.get("notes", []) or []),
    ]
    return " ".join(str(part) for part in parts if part)


def normalize_bucket_text(text: str) -> str:
    """配置先名の比較用に括弧・装飾・空白を落とす。"""
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = html_unescape(text)
    text = re.sub(r"[＜＞<>「」『』【】\[\]（）()]", "", text)
    return re.sub(r"\s+", "", text).lower()


def keyword_hits(text: str, keywords: set[str]) -> set[str]:
    return {keyword for keyword in keywords if keyword and keyword in text}


def coupon_region_groups(text: str) -> set[str]:
    return {
        group
        for group, keywords in REGION_GROUPS.items()
        if keyword_hits(text, keywords["coupon"])
    }


def bucket_region_groups(bucket: str) -> set[str]:
    return {
        group
        for group, keywords in REGION_GROUPS.items()
        if keyword_hits(bucket, keywords["bucket"] | keywords["coupon"])
    }


def coupon_product_groups(text: str) -> set[str]:
    return {
        group
        for group, keywords in PRODUCT_KEYWORDS.items()
        if keyword_hits(text, keywords)
    }


def bucket_product_groups(bucket: str) -> set[str]:
    return {
        group
        for group, keywords in PRODUCT_KEYWORDS.items()
        if keyword_hits(bucket, keywords)
    }


def choose_coupon_bucket(coupon: dict, bucket_names: list[str]) -> str:
    """
    新規クーポンを入れる既存見出しを選ぶ。

    固定バケット名ではなく、記事内に実在するH4見出しとの近さで選ぶ。
    これにより、新規クーポンが黄色背景リストの外へ出るのを防ぐ。
    """
    if not bucket_names:
        return coupon_list_bucket(coupon)

    text = coupon_placement_text(coupon)
    coupon_regions = coupon_region_groups(text)
    coupon_products = coupon_product_groups(text)
    generic_bucket = coupon_list_bucket(coupon)
    placement_hint = normalize_bucket_text(coupon.get("placement_hint", ""))
    category = normalize_bucket_text(coupon.get("category", ""))

    best_bucket = bucket_names[-1]
    best_score = -1
    for bucket in bucket_names:
        score = 0
        bucket_key = normalize_bucket_text(bucket)
        if placement_hint and (placement_hint in bucket_key or bucket_key in placement_hint):
            score += 80
        if category and category in bucket_key:
            score += 35
        if bucket == generic_bucket:
            if "common" in coupon_products or "transport" in coupon_products or not coupon_regions:
                score += 30
            else:
                score += 2

        direct_hits = keyword_hits(text, set(re.findall(r"[\w一-龥ぁ-んァ-ン]+", bucket)))
        score += len(direct_hits) * 6

        region_overlap = coupon_regions & bucket_region_groups(bucket)
        score += len(region_overlap) * 18

        product_overlap = coupon_products & bucket_product_groups(bucket)
        score += len(product_overlap) * 14

        if "common" in coupon_products and any(word in bucket for word in ("全国", "共通", "全商品")):
            score += 20
        if coupon_regions and any(word in bucket for word in ("全国", "共通")):
            score -= 10
        if "transport" in coupon_products and "lodging" in bucket_product_groups(bucket):
            score -= 8
        if "lodging" in coupon_products and "transport" in bucket_product_groups(bucket):
            score -= 8

        if score > best_score:
            best_score = score
            best_bucket = bucket
    return best_bucket


def shorten(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def coupon_detail_lines(coupon: dict, ota: str) -> list[str]:
    """リスト型クーポンカードの詳細行を作る。"""
    detail = coupon.get("detail_data") or {}
    lines = []
    if coupon.get("discount"):
        lines.append(f"<strong>割引額</strong>: {h(coupon['discount'])}")
    booking_period = usable_coupon_value(coupon.get("booking_period") or detail.get("booking_period", ""))
    if booking_period:
        lines.append(f"<strong>予約期間</strong>: {h(booking_period)}")
    stay_period = usable_coupon_value(coupon.get("stay_period") or coupon.get("travel_period") or detail.get("stay_period", ""))
    if stay_period:
        stay_label = "宿泊・出発期間" if ota in ("jtb", "knt") else "対象期間"
        lines.append(f"<strong>{stay_label}</strong>: {h(stay_period)}")
    target_parts = [coupon.get("area", ""), coupon.get("product_type", "") or coupon.get("type", "")]
    target = " / ".join(str(part) for part in target_parts if part)
    if target:
        lines.append(f"<strong>対象</strong>: {h(target)}")
    if "store_available" in coupon:
        store = "店舗利用可" if coupon.get("store_available") else "Web中心"
        lines.append(f"<strong>利用場所</strong>: {h(store)}")
    for code in detail.get("coupon_codes", []) or []:
        lines.append(f"<strong>クーポンコード</strong>: {h(code)}")
    for password in detail.get("passwords", []) or []:
        lines.append(f"<strong>パスワード</strong>: {h(password)}")
    notes = detail.get("notes", []) or detail.get("conditions", []) or []
    if notes:
        lines.append(f"<strong>条件メモ</strong>: {h(shorten(notes[0]))}")
    return lines


def render_coupon_title_html(coupon: dict, vc_template: dict) -> str:
    """クーポン名部分のリンクHTMLを作る。"""
    title = coupon.get("title", "")
    aff_url, pixel_url = valuecommerce_link(coupon.get("detail_url", ""), vc_template)
    if aff_url:
        pixel = f'<img src="{pixel_url}" height="1" width="0" border="0">' if pixel_url else ""
        return f'<a href="{aff_url}" rel="nofollow">{pixel}{h(title)}</a>'
    if coupon.get("detail_url"):
        return f'<a href="{h(coupon["detail_url"])}" rel="nofollow">{h(title)}</a>'
    return h(title)


def render_coupon_list_item(coupon: dict, ota: str, vc_template: dict) -> str:
    """Gutenbergのlist-itemとしてクーポン1件を描画する。"""
    title_html = render_coupon_title_html(coupon, vc_template)

    detail_items = "\n".join(
        "<!-- wp:list-item -->\n"
        f"<li>{line}</li>\n"
        "<!-- /wp:list-item -->"
        for line in coupon_detail_lines(coupon, ota)
    )
    return (
        "<!-- wp:list-item -->\n"
        f"<li><strong>{title_html}</strong><!-- wp:list -->\n"
        f'<ul class="wp-block-list">{detail_items}</ul>\n'
        "<!-- /wp:list --></li>\n"
        "<!-- /wp:list-item -->"
    )


def label_from_detail_line(line: str) -> str:
    """coupon_detail_linesで生成した行から項目名を取り出す。"""
    match = re.search(r"<strong>(.*?)</strong>", line)
    if not match:
        return ""
    return re.sub(r"<[^>]+>", "", match.group(1)).strip()


def detail_list_item_html(line: str) -> str:
    """詳細行1つをGutenberg list-itemにする。"""
    return "<!-- wp:list-item -->\n" f"<li>{line}</li>\n" "<!-- /wp:list-item -->"


def render_coupon_list_item_like_existing(
    coupon: dict,
    ota: str,
    vc_template: dict,
    template_html: str,
) -> str:
    """
    既存のクーポン1件HTMLをひな型にして、新規クーポン項目を作る。

    既存デザインのclass、入れ子リスト、余白、SWELL装飾を残し、
    タイトルと詳細値だけを最新クーポンに差し替える。
    """
    if not template_html:
        return render_coupon_list_item(coupon, ota, vc_template)

    if "<!-- wp:list -->" not in template_html and "<br" in template_html:
        title_html = render_coupon_title_html(coupon, vc_template)
        html = replace_first_anchor_html(template_html, title_html)
        html = update_flat_coupon_item(html, coupon)
        return html

    title_html = render_coupon_title_html(coupon, vc_template)
    html = re.sub(
        r"(<li\b[^>]*>\s*<strong>)(.*?)(</strong>)",
        lambda match: f"{match.group(1)}{title_html}{match.group(3)}",
        template_html,
        count=1,
        flags=re.DOTALL,
    )

    existing_labels: set[str] = set()
    used_labels: set[str] = set()

    detail_block_pattern = re.compile(
        r"(<!-- wp:list-item -->\s*<li><strong>([^<]+)</strong>\s*[:：]\s*)"
        r".*?"
        r"(</li>\s*<!-- /wp:list-item -->)",
        re.DOTALL,
    )

    def replace_detail(match: re.Match) -> str:
        label = match.group(2).strip()
        existing_labels.add(label)
        new_value = coupon_existing_label_value(coupon, label, "")
        if new_value is None or new_value == "":
            return ""
        used_labels.add(label)
        return f"{match.group(1)}{h(new_value)}{match.group(3)}"

    html = detail_block_pattern.sub(replace_detail, html)

    missing_lines = []
    for line in coupon_detail_lines(coupon, ota):
        label = label_from_detail_line(line)
        if label and label not in existing_labels and label not in used_labels:
            missing_lines.append(detail_list_item_html(line))

    if missing_lines:
        insert = "\n".join(missing_lines)
        ul_close = html.rfind("</ul>")
        if ul_close != -1:
            html = html[:ul_close] + insert + "\n" + html[ul_close:]
        else:
            fallback = render_coupon_list_item(coupon, ota, vc_template)
            nested_match = re.search(r"<!-- wp:list -->.*<!-- /wp:list -->", fallback, flags=re.DOTALL)
            if nested_match:
                html = re.sub(r"(</strong>)", r"\1" + nested_match.group(0), html, count=1)

    return html


def extract_coupon_id_from_html(html: str, coupons: list[dict] | None = None) -> str:
    """クーポン詳細URLからクーポンIDを取り出す。"""
    patterns = [
        r"coupon/detail/([^/\"'?#&]+)/page\.asp",
        r"coupon%2Fdetail%2F([^%\"'&#]+)%2Fpage\.asp",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    url_index = coupon_url_index(coupons)
    for url in extract_href_urls(html):
        url_key = normalize_url_key(url)
        if url_key and url_key in url_index:
            return url_index[url_key]
        jalpack_id = jalpack_id_from_url(url)
        if jalpack_id:
            return jalpack_id

    return jalpack_id_from_title(html)


def find_matching_wp_comment_block_end(html: str, start: int, block_name: str) -> int | None:
    """Gutenbergコメントブロックの対応する終了位置を返す。"""
    token_pattern = re.compile(
        rf"<!-- wp:{re.escape(block_name)}(?=\s|-->)[\s\S]*?-->|<!-- /wp:{re.escape(block_name)} -->"
    )
    depth = 0
    for token in token_pattern.finditer(html, start):
        if token.group(0).startswith(f"<!-- wp:{block_name}"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return token.end()
    return None


def find_enclosing_wp_comment_block(html: str, position: int, block_name: str) -> dict | None:
    """指定位置を内側に含むGutenbergコメントブロックを返す。"""
    start_pattern = re.compile(rf"<!-- wp:{re.escape(block_name)}(?=\s|-->)[\s\S]*?-->")
    best = None
    for match in start_pattern.finditer(html):
        if match.start() > position:
            break
        end = find_matching_wp_comment_block_end(html, match.start(), block_name)
        if end is None or not (match.start() <= position < end):
            continue
        if best is None or match.start() > best["start"]:
            best = {
                "start": match.start(),
                "end": end,
                "opening": html[match.start(): min(end, match.start() + 900)],
            }
    return best


def block_has_coupon_frame_background(block: dict | None) -> bool:
    """黄色背景のクーポン枠かどうかを判定する。"""
    if not block:
        return False
    opening = block.get("opening", "")
    return "swl-pale-04" in opening or "has-swl-pale-04-background-color" in opening


def is_coupon_design_list(list_block: dict | None) -> bool:
    """既存記事の黄色背景クーポン枠かどうかを判定する。"""
    return block_has_coupon_frame_background(list_block)


def is_coupon_frame_position(section_html: str, position: int) -> bool:
    """リスト自体または親グループが黄色背景ならクーポン枠内とみなす。"""
    list_block = find_enclosing_wp_comment_block(section_html, position, "list")
    group_block = find_enclosing_wp_comment_block(section_html, position, "group")
    return block_has_coupon_frame_background(list_block) or block_has_coupon_frame_background(group_block)


def coupon_item_title(raw_html: str) -> str:
    """チェック結果に表示するクーポン名を取り出す。"""
    link_match = re.search(r"<a\b[^>]*>(.*?)</a>", raw_html, flags=re.DOTALL | re.IGNORECASE)
    if link_match:
        text = link_match.group(1)
    else:
        text = raw_html
    text = re.sub(r"<[^>]+>", "", text)
    return shorten(text, 90)


def validate_coupon_frame_integrity(
    html: str,
    *,
    phase: str,
    require_no_review_markup: bool = False,
) -> dict:
    """
    クーポン詳細リンクを持つリスト項目が、既存のクーポン枠内に残っているか確認する。

    review時は、赤/青差分を確定した後のHTMLをシミュレーションしてからこの関数に通す。
    これにより、赤側の旧位置は許容しつつ、確定後に残る白背景クーポンだけを検出する。
    """
    section = find_coupon_list_section(html)
    if not section:
        return {
            "ok": True,
            "phase": phase,
            "skipped": True,
            "reason": "クーポン一覧リスト型セクションではないため、枠内チェックをスキップ",
            "coupon_items": 0,
            "out_of_frame": [],
            "review_markup_left": False,
        }

    items = extract_coupon_list_items(section["raw_html"])
    out_of_frame = [
        {
            "id": item["id"],
            "title": coupon_item_title(item["raw_html"]),
            "bucket": item.get("bucket") or "",
        }
        for item in items
        if not item.get("list_is_coupon_frame")
    ]
    review_markup_left = (
        REVIEW_DIFF_START in html
        or REVIEW_DIFF_END in html
        or "coupon-review-added-item" in html
        or "coupon-review-deleted-item" in html
    )
    ok = not out_of_frame and (not require_no_review_markup or not review_markup_left)
    return {
        "ok": ok,
        "phase": phase,
        "skipped": False,
        "coupon_items": len(items),
        "out_of_frame": out_of_frame,
        "review_markup_left": review_markup_left,
    }


def layout_check_reason(check: dict) -> str:
    """最終チェック失敗理由を短くまとめる。"""
    parts = []
    out_of_frame = check.get("out_of_frame") or []
    if out_of_frame:
        sample = " / ".join(f"{item['id']}:{item['title']}" for item in out_of_frame[:3])
        parts.append(f"クーポン枠外に{len(out_of_frame)}件残っています（例: {sample}）")
    if check.get("review_markup_left"):
        parts.append("差分レビュー用の赤/青マークが残っています")
    return "。".join(parts) or "最終チェックに失敗しました"


def h4_contexts(section_html: str) -> list[dict]:
    """セクション内のクーポン小見出し位置とテキストを返す。"""
    pattern = re.compile(
        r'<!-- wp:heading\s*\{"level":4[^>]*-->\s*'
        r'<h4[^>]*>(.*?)</h4>\s*'
        r'<!-- /wp:heading -->',
        re.DOTALL,
    )
    blocks = [
        {
            "start": match.start(),
            "end": match.end(),
            "text": re.sub(r"<[^>]+>", "", match.group(1)).strip(),
        }
        for match in pattern.finditer(section_html)
    ]
    section_title_pattern = re.compile(
        r'<!-- wp:paragraph\b[^>]*-->\s*'
        r'<p\b[^>]*class="[^"]*is-style-section_ttl[^"]*"[^>]*>(.*?)</p>\s*'
        r'<!-- /wp:paragraph -->',
        re.DOTALL,
    )
    for match in section_title_pattern.finditer(section_html):
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        text = html_unescape(text)
        if text:
            blocks.append({
                "start": match.start(),
                "end": match.end(),
                "text": text,
            })
    return sorted(blocks, key=lambda item: item["start"])


def bucket_for_position(contexts: list[dict], position: int) -> str:
    """指定位置の直前にあるH4をバケット名として返す。"""
    current = ""
    for context in contexts:
        if context["start"] <= position:
            current = context["text"]
        else:
            break
    return current


def extract_coupon_list_items(section_html: str, coupons: list[dict] | None = None) -> list[dict]:
    """リスト型セクションからクーポン1件ごとのHTMLを抽出する。"""
    contexts = h4_contexts(section_html)
    start_pattern = re.compile(r"<!-- wp:list-item -->\s*<li\b", re.IGNORECASE)
    items = []
    cursor = 0
    for match in start_pattern.finditer(section_html):
        if match.start() < cursor:
            continue
        end = find_matching_wp_comment_block_end(section_html, match.start(), "list-item")
        if end is None:
            continue
        raw_html = section_html[match.start():end]
        coupon_id = extract_coupon_id_from_html(raw_html, coupons)
        if not coupon_id:
            continue
        items.append({
            "start": match.start(),
            "end": end,
            "id": coupon_id,
            "raw_html": raw_html,
            "bucket": bucket_for_position(contexts, match.start()),
            "list_is_coupon_frame": is_coupon_frame_position(section_html, match.start()),
        })
        cursor = end
    return items


def find_bucket_insert_positions(section_html: str) -> dict[str, int]:
    """新規クーポンを入れるため、各H4直後の外側リスト末尾位置を返す。"""
    contexts = h4_contexts(section_html)
    positions: dict[str, int] = {}
    for index, context in enumerate(contexts):
        next_start = contexts[index + 1]["start"] if index + 1 < len(contexts) else len(section_html)
        list_start = section_html.find("<!-- wp:list", context["end"], next_start)
        if list_start == -1:
            continue
        list_end = find_matching_wp_comment_block_end(section_html, list_start, "list")
        if list_end is None:
            continue
        closing_comment = section_html.rfind("<!-- /wp:list -->", list_start, list_end)
        if closing_comment == -1:
            continue
        ul_close = section_html.rfind("</ul>", list_start, closing_comment)
        if ul_close != -1:
            positions[context["text"]] = ul_close
    return positions


def normalize_compare_text(text: str) -> str:
    """値比較用に曜日・空白・波ダッシュ差を吸収する。"""
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = re.sub(r"\([月火水木金土日祝]\)", "", text)
    text = text.replace("～", "〜").replace(" ", "")
    return text.strip()


def format_period_like_existing(new_value: str, old_value: str) -> str:
    """既存の期間表記に近い形で新しい期間を返す。"""
    if not new_value:
        return ""
    dates = re.findall(r"\d{4}/\d{1,2}/\d{1,2}", new_value)
    if len(dates) >= 2 and ("〜" in old_value or "～" in old_value):
        sep = "～" if "～" in old_value else "〜"
        return f"{dates[0]}{sep}{dates[1]}"
    return new_value


def usable_coupon_value(value: str) -> str:
    """公式HTML抽出が途中で切れたような値は記事へ上書きしない。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if text in {"・", "-", "ー", "※", "*"}:
        return ""
    if len(text) <= 4 and not re.search(r"\d", text):
        return ""
    if text.endswith(("のご", " /", "＆")):
        return ""
    return text


def coupon_existing_label_value(coupon: dict, label: str, old_value: str) -> str | None:
    """既存ラベルに合わせて、最新クーポン値を返す。"""
    detail = coupon.get("detail_data") or {}
    if label == "割引額":
        return coupon.get("discount", "")
    if label in ("対象商品", "対象"):
        target_parts = [coupon.get("area", ""), coupon.get("product_type", "") or coupon.get("type", "")]
        return " / ".join(str(part) for part in target_parts if part)
    if label == "エリア":
        return coupon.get("area", "")
    if label == "タイプ":
        return coupon.get("product_type", "") or coupon.get("type", "")
    if label in ("予約対象期間", "予約期間"):
        return format_period_like_existing(
            usable_coupon_value(coupon.get("booking_period", "") or detail.get("booking_period", "")),
            old_value,
        )
    if label in ("宿泊/出発対象期間", "宿泊・出発期間", "対象期間"):
        return format_period_like_existing(
            usable_coupon_value(
                coupon.get("stay_period", "") or coupon.get("travel_period", "") or detail.get("stay_period", "")
            ),
            old_value,
        )
    if label in ("店舗利用", "利用場所") and "store_available" in coupon:
        if label == "店舗利用":
            return "可" if coupon.get("store_available") else "不可"
        return "店舗利用可" if coupon.get("store_available") else "Web中心"
    if label == "クーポンコード":
        return ", ".join(str(code) for code in detail.get("coupon_codes", []) if code)
    if label == "パスワード":
        return ", ".join(str(password) for password in detail.get("passwords", []) if password)
    if label == "条件メモ":
        notes = detail.get("notes", []) or detail.get("conditions", []) or coupon.get("conditions", []) or []
        return shorten(notes[0]) if notes else ""
    return None


def render_coupon_title_text(coupon: dict) -> str:
    """既存リストのリンク文字列に合わせたクーポン名を作る。"""
    title = str(coupon.get("title", "")).strip()
    if not title:
        return ""
    if "JALパッククーポン" in title:
        return title
    if coupon.get("provider") == "jalpack" or coupon.get("provider_label") == "JALパック":
        discount = str(coupon.get("discount", "")).strip()
        suffix = f" {discount}" if discount and normalize_compare_text(discount) not in normalize_compare_text(title) else ""
        return f"{title}{suffix} JALパッククーポン→"
    return title


def replace_first_anchor_html(raw_html: str, replacement_html: str) -> str:
    """最初のリンクHTML全体を差し替える。"""
    if not replacement_html:
        return raw_html
    return re.sub(
        r"<a\b[^>]*>.*?</a>",
        replacement_html,
        raw_html,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )


def replace_first_anchor_text(raw_html: str, new_text: str) -> str:
    """最初のリンクのhrefやimgタグを残し、表示テキストだけ差し替える。"""
    if not new_text:
        return raw_html

    def replace(match: re.Match) -> str:
        inner = match.group(2)
        images = "".join(re.findall(r"<img\b[^>]*>", inner, flags=re.DOTALL | re.IGNORECASE))
        return f"{match.group(1)}{images}{h(new_text)}{match.group(3)}"

    return re.sub(
        r"(<a\b[^>]*>)(.*?)(</a>)",
        replace,
        raw_html,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )


def update_flat_coupon_item(raw_html: str, coupon: dict) -> str:
    """JALパックのようなbr区切りリスト項目を既存形式のまま更新する。"""
    html = replace_first_anchor_text(raw_html, render_coupon_title_text(coupon))
    changed_labels = 0
    labels = ("割引額", "対象商品", "対象", "予約対象期間", "予約期間", "宿泊/出発対象期間", "宿泊・出発期間", "対象期間", "条件メモ")
    for label in labels:
        new_value = coupon_existing_label_value(coupon, label, "")
        if new_value is None or new_value == "":
            continue
        pattern = re.compile(
            rf"({re.escape(label)}\s*[:：]\s*)(.*?)(?=<br\s*/?>|</li>)",
            re.DOTALL,
        )

        def replace_value(match: re.Match) -> str:
            nonlocal changed_labels
            old_value = match.group(2).strip()
            if normalize_compare_text(old_value) == normalize_compare_text(new_value):
                return match.group(0)
            changed_labels += 1
            return f"{match.group(1)}{h(new_value)}"

        html = pattern.sub(replace_value, html, count=1)

    return html


def update_existing_coupon_item(raw_html: str, coupon: dict) -> str:
    """
    既存のクーポン1件HTMLをできるだけ保ったまま、値だけ更新する。

    見出し、項目名、リンク装飾、並びは残し、割引額・期間・コード等だけを差し替える。
    """
    pattern = re.compile(
        r"(<li><strong>([^<]+)</strong>\s*[:：]\s*)(.*?)(</li>)",
        re.DOTALL,
    )

    def replace(match: re.Match) -> str:
        label = match.group(2).strip()
        old_value = match.group(3).strip()
        new_value = coupon_existing_label_value(coupon, label, old_value)
        if new_value is None or new_value == "":
            return match.group(0)
        if normalize_compare_text(old_value) == normalize_compare_text(new_value):
            return match.group(0)
        return f"{match.group(1)}{h(new_value)}{match.group(4)}"

    updated = pattern.sub(replace, raw_html)
    if normalize_item_html(updated) == normalize_item_html(raw_html):
        updated = update_flat_coupon_item(raw_html, coupon)
    else:
        updated = replace_first_anchor_text(updated, render_coupon_title_text(coupon))
    return updated


def normalize_item_html(html: str) -> str:
    """変更有無の判定用にHTMLの空白差を吸収する。"""
    html = re.sub(r"\s+", " ", html)
    html = re.sub(r">\s+<", "><", html)
    return html.strip()


def build_item_level_list_section(
    section_html: str,
    coupons: list[dict],
    page_config: dict,
    source_html: str,
) -> tuple[str, dict]:
    """既存リストをクーポン1件単位で更新し、必要な箇所だけ赤/青差分にする。"""
    ota = page_config["ota"]
    vc_template = extract_valuecommerce_template(source_html)
    active_coupons = [coupon for coupon in coupons if coupon.get("stock_status") == "配布中"]
    latest_by_id = {str(coupon.get("id", "")): coupon for coupon in active_coupons if coupon.get("id")}
    old_items = extract_coupon_list_items(section_html, coupons)
    templates_by_bucket: dict[str, str] = {}
    for item in old_items:
        if item.get("bucket") and item["bucket"] not in templates_by_bucket:
            templates_by_bucket[item["bucket"]] = item["raw_html"]
    default_template = old_items[0]["raw_html"] if old_items else ""
    used_ids = set()
    replacements = []
    new_by_bucket: dict[str, list[str]] = {}
    changed = 0
    unchanged = 0
    removed = 0
    moved = 0

    insert_positions = find_bucket_insert_positions(section_html)
    available_buckets = [bucket for bucket in templates_by_bucket if bucket in insert_positions]
    if not available_buckets:
        available_buckets = list(insert_positions)
    default_insert = max(insert_positions.values()) if insert_positions else len(section_html)

    for item in old_items:
        coupon = latest_by_id.get(item["id"])
        if not coupon:
            removed += 1
            replacements.append({
                "start": item["start"],
                "end": item["end"],
                "html": build_review_list_item_diff(
                    item["raw_html"],
                    "",
                    item["id"],
                ),
            })
            continue

        used_ids.add(item["id"])
        updated_item = update_existing_coupon_item(item["raw_html"], coupon)
        desired_bucket = choose_coupon_bucket(coupon, available_buckets)
        needs_move = (
            bool(desired_bucket)
            and desired_bucket in insert_positions
            and (item.get("bucket") != desired_bucket or not item.get("list_is_coupon_frame"))
        )
        if needs_move:
            moved += 1
            replacements.append({
                "start": item["start"],
                "end": item["end"],
                "html": build_review_list_item_diff(
                    item["raw_html"],
                    "",
                    item["id"],
                ),
            })
            template_html = templates_by_bucket.get(desired_bucket, default_template)
            if item.get("list_is_coupon_frame") and normalize_item_html(updated_item) != normalize_item_html(item["raw_html"]):
                moved_item = updated_item
                changed += 1
            else:
                moved_item = render_coupon_list_item_like_existing(coupon, ota, vc_template, template_html)
            new_by_bucket.setdefault(desired_bucket, []).append(
                build_review_list_item_diff("", moved_item, item["id"])
            )
            continue

        if normalize_item_html(updated_item) == normalize_item_html(item["raw_html"]):
            unchanged += 1
            continue
        changed += 1
        replacements.append({
            "start": item["start"],
            "end": item["end"],
            "html": build_review_list_item_diff(
                item["raw_html"],
                updated_item,
                item["id"],
            ),
        })

    new_coupons = [coupon for coupon in active_coupons if str(coupon.get("id", "")) not in used_ids]
    for coupon in new_coupons:
        bucket = choose_coupon_bucket(coupon, available_buckets)
        template_html = templates_by_bucket.get(bucket, default_template)
        item_html = render_coupon_list_item_like_existing(coupon, ota, vc_template, template_html)
        new_by_bucket.setdefault(bucket, []).append(
            build_review_list_item_diff("", item_html, str(coupon.get("id", "")))
        )

    for bucket, snippets in new_by_bucket.items():
        insert_at = insert_positions.get(bucket, default_insert)
        replacements.append({
            "start": insert_at,
            "end": insert_at,
            "html": "\n" + "\n".join(snippets) + "\n",
        })

    new_section = section_html
    for item in sorted(replacements, key=lambda r: r["start"], reverse=True):
        new_section = new_section[:item["start"]] + item["html"] + new_section[item["end"]:]

    summary = {
        "old_items": len(old_items),
        "active_coupons": len(active_coupons),
        "unchanged_items": unchanged,
        "changed_items": changed,
        "removed_items": removed,
        "moved_items": moved,
        "added_items": len(new_coupons),
        "review_blocks": changed + removed + len(new_coupons) + (moved * 2),
    }
    return new_section, summary


def render_coupon_list_section(coupons: list[dict], page_config: dict, source_html: str) -> str:
    """既存のクーポン一覧セクションをリスト形式で再生成する。"""
    ota = page_config["ota"]
    active = [coupon for coupon in coupons if coupon.get("stock_status") == "配布中"]
    vc_template = extract_valuecommerce_template(source_html)
    label = page_config.get("label", "")
    if "国内" in label:
        section_title = "配布中の主なJTB国内クーポン一覧"
    elif "海外" in label:
        section_title = "配布中の主なJTB海外旅行クーポン一覧"
    elif "新幹線" in label:
        section_title = "配布中の主なJTB新幹線クーポン一覧"
    elif ota == "jtb":
        section_title = "配布中の主なJTBクーポン一覧"
    else:
        section_title = "配布中の主なクーポン一覧"
    buckets: dict[str, list[dict]] = {}
    for coupon in active:
        buckets.setdefault(coupon_list_bucket(coupon), []).append(coupon)

    order = [
        "全国・共通で使いやすいクーポン",
        "航空機・JR利用の国内ツアークーポン",
        "ホテルブランド・テーマ施設のクーポン",
        "その他の国内クーポン",
    ]
    parts = [
        '<!-- wp:heading {"level":3} -->',
        f'<h3 class="wp-block-heading" id="available-domestic-coupons">{h(section_title)}</h3>',
        '<!-- /wp:heading -->',
        "",
        "<!-- wp:paragraph -->",
        "<p>最新の取得データをもとに、配布中のクーポンを目的別に整理しています。利用前に公式詳細ページで対象商品、予約期間、クーポンコード、パスワードを確認してください。</p>",
        "<!-- /wp:paragraph -->",
    ]

    for bucket in order:
        items = buckets.get(bucket, [])
        if not items:
            continue
        parts.extend([
            "",
            '<!-- wp:heading {"level":4} -->',
            f'<h4 class="wp-block-heading">{h(bucket)}</h4>',
            '<!-- /wp:heading -->',
            "",
            '<!-- wp:list {"className":"is-style-num_circle -list-under-dashed","backgroundColor":"swl-pale-04"} -->',
            '<ul class="wp-block-list is-style-num_circle -list-under-dashed has-swl-pale-04-background-color has-background">',
            "\n".join(render_coupon_list_item(coupon, ota, vc_template) for coupon in items),
            "</ul>",
            "<!-- /wp:list -->",
        ])
    return "\n".join(parts)


def build_review_replacements(
    current_html: str,
    page_config: dict,
    coupons: list[dict],
    aff_config: dict,
) -> tuple[list[dict], dict]:
    """テーブルまたはリスト型セクションのレビュー置換案を作る。"""
    ota = page_config["ota"]
    sections = parse_page_sections(current_html)
    tables = [s for s in sections if s["type"] == "table"]
    headings = [s for s in sections if s["type"] == "heading"]
    summary = {
        "h2": sum(1 for h2 in headings if h2["level"] == 2),
        "h3": sum(1 for h3 in headings if h3["level"] == 3),
        "tables": len(tables),
        "replacement_type": "",
        "unmatched_coupons": 0,
        "active_coupons": len([coupon for coupon in coupons if coupon.get("stock_status") == "配布中"]),
    }

    if tables and page_config.get("table_format") != "yellow_list":
        matched_tables, unmatched = match_sections_to_coupons(sections, coupons, ota)
        replacements = []
        for table_block in matched_tables:
            matched = table_block.get("matched_coupons", [])
            if not matched:
                continue
            tbody_start = table_block.get("tbody_start")
            tbody_end = table_block.get("tbody_end")
            if tbody_start is None or tbody_end is None:
                continue
            block_start = table_block["start"]
            block_end = table_block["end"]
            old_block = current_html[block_start:block_end]
            local_start = tbody_start - block_start
            local_end = tbody_end - block_start
            new_tbody = render_table_body(matched, aff_config, ota)
            new_block = old_block[:local_start] + new_tbody + old_block[local_end:]
            replacements.append({
                "start": block_start,
                "end": block_end,
                "new_html": new_block,
                "label": table_block.get("parent_h3") or table_block.get("parent_h2") or "クーポン表",
            })
        summary["replacement_type"] = "table"
        summary["unmatched_coupons"] = len(unmatched)
        return replacements, summary

    list_section = find_coupon_list_section(current_html)
    if not list_section:
        summary["replacement_type"] = "not_found"
        return [], summary

    new_section, item_summary = build_item_level_list_section(
        list_section["raw_html"],
        coupons,
        page_config,
        current_html,
    )
    summary["replacement_type"] = "list_section"
    summary.update(item_summary)
    summary["framework_change_note"] = (
        "既存のクーポン一覧リストを1件単位で照合しました。"
        "変更なしのクーポンHTMLはできるだけそのまま残しています。"
        "確定前にWordPress下書きで表示を確認してください。"
    )
    if item_summary["review_blocks"] == 0:
        return [], summary
    return [
        {
            "start": list_section["start"],
            "end": list_section["end"],
            "new_html": new_section,
            "label": list_section["heading"]["text"],
            "already_reviewed": True,
        }
    ], summary


# ============================================================
# コア処理
# ============================================================

def review_page(site_config: dict, page_config: dict, dry_run: bool = False) -> dict:
    """色付き差分の確認用下書きを作成する。"""
    slug = page_config["slug"]
    ota = page_config["ota"]
    label = page_config.get("label", slug)
    print(f"\n{'='*50}")
    print(f"📄 {label} ({ota.upper()} — {slug})")
    print(f"{'='*50}")

    print("  1. WPからページ取得中...")
    post = fetch_wp_post(site_config, slug)
    current_html = wp_post_content(post)
    post_id = post["id"]
    source_status = post.get("status", "")
    print(f"     post_id={post_id}, status={source_status}, HTML長={len(current_html)}文字")

    BACKUP_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{slug}_{date_str}.html"
    backup_path.write_text(current_html, encoding="utf-8")
    print(f"  2. バックアップ: {backup_path.name}")

    base_html = current_html
    if has_review_markup(current_html):
        base_html, restored_existing = restore_review_deleted_baseline(current_html)
        print(f"  3. 既存レビュー差分を赤側の元HTMLへ戻して再作成: {restored_existing}件")
    else:
        print("  3. 既存レビュー差分: なし")

    print("  4. クーポンデータ読み込み中...")
    coupons = load_latest_coupons(page_config["data_dir"])
    coupons, filter_summary = filter_coupons_for_page(coupons, page_config)
    if filter_summary["include_keywords"] or filter_summary["exclude_keywords"]:
        print(
            "     ページ別フィルタ: "
            f"{filter_summary['input']}件→{filter_summary['output']}件 "
            f"(include={filter_summary['include_keywords']}, "
            f"exclude={filter_summary['exclude_keywords']})"
        )
    aff_config = load_affiliate_config(page_config.get("affiliate_config", ""))

    print("  5. 差分レビュー案を作成中...")
    replacements, review_summary = build_review_replacements(
        base_html,
        page_config,
        coupons,
        aff_config,
    )
    print(
        "     解析結果: "
        f"H2={review_summary['h2']}個, "
        f"H3={review_summary['h3']}個, "
        f"table={review_summary['tables']}個, "
        f"type={review_summary['replacement_type'] or '-'}"
    )
    if not replacements:
        if review_summary.get("replacement_type") != "not_found" and review_summary.get("review_blocks") == 0:
            print("  ✅ 最新データとの差分はありません")
            return {
                "slug": slug,
                "ota": ota,
                "site_id": site_config["site_id"],
                "label": label,
                "status": "no_change",
                "reason": "最新データとの差分はありません",
                "source_post_id": post_id,
                "source_status": source_status,
                "review_summary": review_summary,
                "filter_summary": filter_summary,
            }
        reason = (
            "クーポン情報ブロックを特定できません。"
            "既存フレームワークの変更が必要なため、自動更新を停止しました"
        )
        print(f"  🚨 {reason}")
        return {
            "slug": slug,
            "ota": ota,
            "site_id": site_config["site_id"],
            "label": label,
            "status": "blocked",
            "reason": reason,
            "source_post_id": post_id,
            "source_status": source_status,
            "review_summary": review_summary,
            "filter_summary": filter_summary,
        }

    review_html = apply_review_replacements(base_html, replacements)
    print(f"     差分ブロック: {review_summary.get('review_blocks', len(replacements))}件")
    if review_summary.get("old_items") is not None:
        print(
            "     クーポン単位: "
            f"既存{review_summary.get('old_items', 0)}件 / "
            f"変更なし{review_summary.get('unchanged_items', 0)}件 / "
            f"変更{review_summary.get('changed_items', 0)}件 / "
            f"移動{review_summary.get('moved_items', 0)}件 / "
            f"追加{review_summary.get('added_items', 0)}件 / "
            f"終了{review_summary.get('removed_items', 0)}件"
        )
    if review_summary.get("framework_change_note"):
        print(f"     ⚠️ {review_summary['framework_change_note']}")

    print("  6. 最終チェック中...")
    finalized_preview_html, preview_finalize_count = finalize_review_markup(review_html)
    layout_check = validate_coupon_frame_integrity(
        finalized_preview_html,
        phase="review_final_preview",
        require_no_review_markup=True,
    )
    if not layout_check["ok"]:
        reason = layout_check_reason(layout_check)
        print(f"  🚨 最終チェックNG: {reason}")
        return {
            "slug": slug,
            "ota": ota,
            "site_id": site_config["site_id"],
            "label": label,
            "status": "blocked",
            "reason": reason,
            "source_post_id": post_id,
            "source_status": source_status,
            "review_summary": review_summary,
            "filter_summary": filter_summary,
            "layout_check": layout_check,
        }
    print(
        "     ✅ "
        f"確定後プレビューOK / クーポン{layout_check.get('coupon_items', 0)}件 / "
        f"枠外0件 / 差分ブロック{preview_finalize_count}件"
    )

    if dry_run:
        output_path = SCRIPT_DIR / "html_output" / f"{slug}_review.html"
        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(review_html, encoding="utf-8")
        print(f"  7. [DRY-RUN] 出力: {output_path}")
        save_result = {
            "target": "dry_run",
            "target_post_id": None,
            "target_slug": slug,
            "target_status": None,
            "message": "DRY-RUNのためWordPressは更新していません",
        }
    else:
        print("  7. 差分レビュー下書き保存中...")
        save_result = save_coupon_update_draft(site_config, post, slug, review_html)
        if save_result.get("target") == "blocked":
            print(f"     🚨 {save_result['reason']}")
            return {
                "slug": slug,
                "ota": ota,
                "site_id": site_config["site_id"],
                "label": label,
                "status": "blocked",
                "reason": save_result["reason"],
                "source_post_id": post_id,
                "source_status": source_status,
                "review_summary": review_summary,
                "filter_summary": filter_summary,
            }
        print(f"     ✅ {save_result['message']}")

    return {
        "slug": slug,
        "ota": ota,
        "site_id": site_config["site_id"],
        "label": label,
        "status": "dry_run" if dry_run else "review_ready",
        "source_post_id": post_id,
        "source_status": source_status,
        "target": save_result.get("target"),
        "target_post_id": save_result.get("target_post_id"),
        "target_slug": save_result.get("target_slug"),
        "target_status": save_result.get("target_status"),
        "target_message": save_result.get("message"),
        "review_blocks": review_summary.get("review_blocks", len(replacements)),
        "review_type": review_summary.get("replacement_type"),
        "framework_change_note": review_summary.get("framework_change_note", ""),
        "unmatched_coupons": review_summary.get("unmatched_coupons", 0),
        "active_coupons": review_summary.get("active_coupons", 0),
        "old_items": review_summary.get("old_items"),
        "unchanged_items": review_summary.get("unchanged_items"),
        "changed_items": review_summary.get("changed_items"),
        "moved_items": review_summary.get("moved_items"),
        "added_items": review_summary.get("added_items"),
        "removed_items": review_summary.get("removed_items"),
        "filter_summary": filter_summary,
        "layout_check": layout_check,
    }


def fetch_coupon_update_target_post(site_config: dict, slug: str) -> tuple[dict, str]:
    """確定対象の下書きを取得する。公開元は直接更新しない。"""
    source_post = fetch_wp_post(site_config, slug)
    if source_post.get("status") == "draft":
        return source_post, "source_draft"

    branch_slug = f"{slug}-coupon-update"
    branch_post = fetch_wp_post(site_config, branch_slug)
    if branch_post.get("status") != "draft":
        raise ValueError(
            f"確定対象 {branch_slug} がdraftではありません（status={branch_post.get('status')}）"
        )
    return branch_post, "branch_draft"


def finalize_page(site_config: dict, page_config: dict, dry_run: bool = False) -> dict:
    """ユーザー確認後、赤/青の差分色を外して通常下書きへ戻す。"""
    slug = page_config["slug"]
    ota = page_config["ota"]
    label = page_config.get("label", slug)
    print(f"\n{'='*50}")
    print(f"📄 {label} ({ota.upper()} — {slug})")
    print(f"{'='*50}")

    print("  1. 確定対象の下書き取得中...")
    target_post, target_type = fetch_coupon_update_target_post(site_config, slug)
    current_html = wp_post_content(target_post)
    target_status = target_post.get("status", "")
    print(
        f"     post_id={target_post['id']}, status={target_status}, "
        f"target={target_type}, HTML長={len(current_html)}文字"
    )

    BACKUP_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{target_post.get('slug', slug)}_before_finalize_{date_str}.html"
    backup_path.write_text(current_html, encoding="utf-8")
    print(f"  2. バックアップ: {backup_path.name}")

    print("  3. 差分色を外して青側だけ残します...")
    finalized_html, replacements = finalize_review_markup(current_html)
    if replacements == 0:
        reason = "差分レビューの赤/青マークが見つかりません。先に差分プレビュー下書きを作成してください"
        print(f"  🚨 {reason}")
        return {
            "slug": slug,
            "ota": ota,
            "site_id": site_config["site_id"],
            "label": label,
            "status": "blocked",
            "reason": reason,
            "target": target_type,
            "target_post_id": target_post["id"],
            "target_slug": target_post.get("slug", slug),
            "target_status": target_status,
        }

    print("  4. 最終チェック中...")
    layout_check = validate_coupon_frame_integrity(
        finalized_html,
        phase="finalize",
        require_no_review_markup=True,
    )
    if not layout_check["ok"]:
        reason = layout_check_reason(layout_check)
        print(f"  🚨 最終チェックNG: {reason}")
        return {
            "slug": slug,
            "ota": ota,
            "site_id": site_config["site_id"],
            "label": label,
            "status": "blocked",
            "reason": reason,
            "target": target_type,
            "target_post_id": target_post["id"],
            "target_slug": target_post.get("slug", slug),
            "target_status": target_status,
            "review_blocks_finalized": replacements,
            "layout_check": layout_check,
        }
    print(
        "     ✅ "
        f"クーポン{layout_check.get('coupon_items', 0)}件 / 枠外0件 / 赤青マークなし"
    )

    if dry_run:
        output_path = SCRIPT_DIR / "html_output" / f"{slug}_finalized.html"
        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(finalized_html, encoding="utf-8")
        print(f"  5. [DRY-RUN] 出力: {output_path}")
        status = "dry_run"
        message = "DRY-RUNのためWordPressは更新していません"
    else:
        print("  5. WordPress下書きを確定版HTMLで更新中...")
        result = update_wp_post_content(site_config, target_post["id"], finalized_html)
        status = "finalized"
        message = "差分色を外し、青側の内容だけを下書き本文に残しました"
        target_status = result.get("status", target_status)
        print(f"     ✅ {message}")

    return {
        "slug": slug,
        "ota": ota,
        "site_id": site_config["site_id"],
        "label": label,
        "status": status,
        "target": target_type,
        "target_post_id": target_post["id"],
        "target_slug": target_post.get("slug", slug),
        "target_status": target_status,
        "target_message": message,
        "review_blocks_finalized": replacements,
        "layout_check": layout_check,
    }


def update_page(site_config: dict, page_config: dict, dry_run: bool = False) -> dict:
    """1ページ分のクーポンテーブルを更新する。"""
    slug = page_config["slug"]
    ota = page_config["ota"]
    label = page_config.get("label", slug)
    print(f"\n{'='*50}")
    print(f"📄 {label} ({ota.upper()} — {slug})")
    print(f"{'='*50}")

    # 1. WPから現在のページを取得
    print("  1. WPからページ取得中...")
    post = fetch_wp_post(site_config, slug)
    current_html = wp_post_content(post)
    post_id = post["id"]
    source_status = post.get("status", "")
    print(f"     post_id={post_id}, status={source_status}, HTML長={len(current_html)}文字")

    # 2. バックアップ保存
    BACKUP_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{slug}_{date_str}.html"
    backup_path.write_text(current_html, encoding="utf-8")
    print(f"  2. バックアップ: {backup_path.name}")

    # 3. ページ構造を解析
    print("  3. ページ構造を解析中...")
    sections = parse_page_sections(current_html)
    tables = [s for s in sections if s["type"] == "table"]
    headings = [s for s in sections if s["type"] == "heading"]
    print(f"     H2: {sum(1 for h in headings if h['level']==2)}個, "
          f"H3: {sum(1 for h in headings if h['level']==3)}個, "
          f"テーブル: {len(tables)}個")

    # 4. 既存afbリンクを抽出（流用用）
    existing_afb = extract_existing_affiliate_links(current_html)
    print(f"  4. 既存afbリンク: {sum(len(v) for v in existing_afb.values())}個")

    # 5. 最新クーポンJSONを読み込み
    print("  5. クーポンデータ読み込み中...")
    coupons = load_latest_coupons(page_config["data_dir"])
    coupons, filter_summary = filter_coupons_for_page(coupons, page_config)
    if filter_summary["include_keywords"] or filter_summary["exclude_keywords"]:
        print(
            "     ページ別フィルタ: "
            f"{filter_summary['input']}件→{filter_summary['output']}件 "
            f"(include={filter_summary['include_keywords']}, "
            f"exclude={filter_summary['exclude_keywords']})"
        )
    aff_config = load_affiliate_config(page_config.get("affiliate_config", ""))

    # 6. セクション×クーポンのマッチング
    print("  6. セクション×クーポン マッチング中...")
    matched_tables, unmatched = match_sections_to_coupons(sections, coupons, ota)
    for t in matched_tables:
        h3 = t.get("parent_h3", "(なし)")
        n = len(t.get("matched_coupons", []))
        print(f"     {h3}: {n}件マッチ")
    if unmatched:
        print(f"     ⚠️ 未マッチ: {len(unmatched)}件")

    # 7. テーブル差替え（tbodyだけ入れ替え）
    print("  7. テーブルHTML再生成中...")
    new_html = current_html
    # 後ろから差替え（位置がずれないように）
    replacements = []
    for table_block in matched_tables:
        matched = table_block.get("matched_coupons", [])
        if not matched:
            continue
        tbody_start = table_block.get("tbody_start")
        tbody_end = table_block.get("tbody_end")
        if tbody_start is None or tbody_end is None:
            continue
        new_tbody = render_table_body(matched, aff_config, ota)
        replacements.append((tbody_start, tbody_end, new_tbody))

    # 位置が後ろのものから差替え
    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_tbody in replacements:
        new_html = new_html[:start] + new_tbody + new_html[end:]

    # 8. 安全チェック
    print("  8. 安全チェック中...")
    check = safety_check(current_html, new_html, len(replacements))
    if not check["passed"]:
        print(f"  🚨 安全チェック失敗: {check['reason']}")
        return {
            "slug": slug,
            "ota": ota,
            "status": "blocked",
            "reason": check["reason"],
            "source_post_id": post_id,
            "source_status": source_status,
        }
    print(f"     ✅ OK (行数: {check.get('old_rows',0)}→{check.get('new_rows',0)})")

    # 9. 下書き更新 or dry-run
    if dry_run:
        output_path = SCRIPT_DIR / "html_output" / f"{slug}_draft.html"
        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(new_html, encoding="utf-8")
        print(f"  9. [DRY-RUN] 出力: {output_path}")
        save_result = {
            "target": "dry_run",
            "target_post_id": None,
            "target_slug": slug,
            "target_status": None,
            "message": "DRY-RUNのためWordPressは更新していません",
        }
    else:
        print("  9. WP下書き保存中...")
        save_result = save_coupon_update_draft(site_config, post, slug, new_html)
        if save_result.get("target") == "blocked":
            print(f"     🚨 {save_result['reason']}")
            return {
                "slug": slug,
                "ota": ota,
                "status": "blocked",
                "reason": save_result["reason"],
                "source_post_id": post_id,
                "source_status": source_status,
            }
        print(f"     ✅ {save_result['message']}")

    # afbリンク検証
    link_issues = []
    for coupon in unmatched:
        link_issues.append({
            "type": "unmatched",
            "title": coupon.get("title", ""),
            "category": coupon.get("category", ""),
        })

    return {
        "slug": slug,
        "ota": ota,
        "status": "dry_run" if dry_run else "updated",
        "site_id": site_config["site_id"],
        "label": label,
        "source_post_id": post_id,
        "source_status": source_status,
        "target": save_result.get("target"),
        "target_post_id": save_result.get("target_post_id"),
        "target_slug": save_result.get("target_slug"),
        "target_status": save_result.get("target_status"),
        "target_message": save_result.get("message"),
        "rows": check.get("new_rows", 0),
        "old_rows": check.get("old_rows", 0),
        "tables_updated": len(replacements),
        "unmatched_coupons": len(unmatched),
        "link_issues": link_issues,
        "filter_summary": filter_summary,
    }


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="WPクーポンページ自動更新")
    parser.add_argument("--site", default="yakushimafan", help="サイトID")
    parser.add_argument("--page", default=None, help="特定ページのslugのみ更新")
    parser.add_argument(
        "--mode",
        choices=["update", "review", "finalize"],
        default="update",
        help="update=従来更新, review=赤/青差分下書き, finalize=差分色を外して確定",
    )
    parser.add_argument("--dry-run", action="store_true", help="HTMLファイル出力のみ")
    parser.add_argument("--rollback", action="store_true", help="直前バックアップから復元")
    args = parser.parse_args()

    print("=" * 50)
    print("WordPress クーポンページ自動更新")
    print(f"サイト: {args.site}")
    if args.page:
        print(f"対象ページ: {args.page}")
    if args.dry_run:
        print("モード: DRY-RUN（WordPressは更新しません）")
    print(f"実行モード: {args.mode}")
    print(f"日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    site_config = load_site_config(args.site)
    validate_site_config(site_config)

    if args.rollback:
        if not args.page:
            print("❌ --rollback には --page が必要です")
            sys.exit(1)
        rollback_from_backup(site_config, args.page)
        return

    if args.page:
        selected = find_page_config(site_config, args.page)
        if not selected:
            valid = ", ".join(page["slug"] for page in site_config["pages"])
            print(f"❌ site={args.site} に page={args.page} はありません")
            print(f"   利用可能: {valid}")
            sys.exit(1)
        page_configs = [selected]
    else:
        page_configs = site_config["pages"]

    results = []
    for page_config in page_configs:
        try:
            if args.mode == "review":
                result = review_page(site_config, page_config, dry_run=args.dry_run)
            elif args.mode == "finalize":
                result = finalize_page(site_config, page_config, dry_run=args.dry_run)
            else:
                result = update_page(site_config, page_config, dry_run=args.dry_run)
            results.append(result)
        except Exception as e:
            print(f"\n❌ エラー ({page_config['slug']}): {e}")
            results.append({
                "slug": page_config["slug"],
                "ota": page_config["ota"],
                "site_id": args.site,
                "status": "error",
                "reason": str(e),
            })

    # 結果をJSON出力（通知スクリプト用）
    RESULT_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n📋 結果: {RESULT_FILE}")

    # サマリー
    print("\n" + "=" * 50)
    print("サマリー")
    print("=" * 50)
    for r in results:
        status = r.get("status", "?")
        emoji = {
            "updated": "✅",
            "review_ready": "🔎",
            "finalized": "✅",
            "dry_run": "📝",
            "blocked": "🚨",
            "error": "❌",
        }.get(status, "?")
        target = r.get("target_slug") or r.get("target") or "-"
        print(f"  {emoji} {r['ota'].upper()} ({r['slug']}): {status} / target={target}")
        if r.get("reason"):
            print(f"     理由: {r['reason']}")
        if r.get("framework_change_note"):
            print(f"     確認: {r['framework_change_note']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 致命的エラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
