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
from pathlib import Path

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
    full_path = SCRIPT_DIR / config_path
    if not full_path.exists():
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
        coupon.get("title", ""),
        coupon.get("discount", ""),
        coupon.get("area", ""),
        coupon.get("type", ""),
        coupon.get("target", ""),
        detail.get("booking_period", ""),
        detail.get("stay_period", ""),
        detail.get("discount", ""),
        " ".join(detail.get("coupon_codes", [])),
        " ".join(detail.get("passwords", [])),
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
# コア処理
# ============================================================

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
    date_str = datetime.now().strftime("%Y-%m-%d")
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
    aff_config = load_affiliate_config(page_config["affiliate_config"])

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
        emoji = {"updated": "✅", "dry_run": "📝", "blocked": "🚨", "error": "❌"}.get(status, "?")
        target = r.get("target_slug") or r.get("target") or "-"
        print(f"  {emoji} {r['ota'].upper()} ({r['slug']}): {status} / target={target}")
        if r.get("reason"):
            print(f"     理由: {r['reason']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 致命的エラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
