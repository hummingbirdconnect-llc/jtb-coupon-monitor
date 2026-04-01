#!/usr/bin/env python3
"""
WordPress クーポンページ自動更新スクリプト
==========================================
WP REST APIで現在のページを取得→Gutenberg HTMLを動的解析→
テーブルのtbodyだけ最新クーポンで差替え→下書き保存。

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
# WP REST API
# ============================================================

def load_site_config(site_id: str) -> dict:
    """wp_sites.json からサイト設定を読み込む。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    site = config["sites"].get(site_id)
    if not site:
        print(f"❌ サイト '{site_id}' が設定に見つかりません")
        sys.exit(1)

    # 環境変数を解決
    return {
        "wp_url": os.environ.get("YF_WP_URL", "").rstrip("/"),
        "wp_user": os.environ.get("YF_WP_USER", ""),
        "wp_app_password": os.environ.get("YF_WP_APP_PASSWORD", "").replace(" ", ""),
        "pages": site["pages"],
    }


def fetch_wp_post(site_config: dict, slug: str) -> dict:
    """WP REST API でページを取得（context=edit で raw HTML）。"""
    url = f"{site_config['wp_url']}/wp-json/wp/v2/posts"
    params = {"slug": slug, "status": "publish,draft", "context": "edit"}
    auth = (site_config["wp_user"], site_config["wp_app_password"])

    resp = requests.get(url, params=params, auth=auth, timeout=30)
    resp.raise_for_status()

    posts = resp.json()
    if not posts:
        raise ValueError(f"ページが見つかりません: slug={slug}")
    return posts[0]


def update_wp_draft(site_config: dict, post_id: int, new_content: str) -> dict:
    """WP REST API で下書き更新。"""
    url = f"{site_config['wp_url']}/wp-json/wp/v2/posts/{post_id}"
    auth = (site_config["wp_user"], site_config["wp_app_password"])

    payload = {"content": new_content, "status": "draft"}
    resp = requests.post(url, json=payload, auth=auth, timeout=30)
    resp.raise_for_status()
    return resp.json()


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
    update_wp_draft(site_config, post["id"], content)
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


# ============================================================
# 安全チェック
# ============================================================

def safety_check(old_html: str, new_html: str) -> dict:
    """更新前後のHTMLを比較し、異常がないかチェック。"""
    old_rows = count_table_rows(old_html)
    new_rows = count_table_rows(new_html)

    if new_rows == 0 and old_rows > 0:
        return {"passed": False, "reason": f"テーブル行が0件に（旧: {old_rows}件）"}

    if old_rows > 0 and new_rows < old_rows * 0.5:
        return {
            "passed": False,
            "reason": f"テーブル行が50%以上減少（{old_rows}→{new_rows}）",
        }

    # Gutenbergブロックコメントの対応チェック
    opens = len(re.findall(r'<!-- wp:table', new_html))
    closes = len(re.findall(r'<!-- /wp:table -->', new_html))
    if opens != closes:
        return {
            "passed": False,
            "reason": f"wp:table の開始/終了が不一致（開始:{opens}, 終了:{closes}）",
        }

    return {"passed": True, "old_rows": old_rows, "new_rows": new_rows}


# ============================================================
# コア処理
# ============================================================

def update_page(site_config: dict, page_config: dict, dry_run: bool = False) -> dict:
    """1ページ分のクーポンテーブルを更新する。"""
    slug = page_config["slug"]
    ota = page_config["ota"]
    print(f"\n{'='*50}")
    print(f"📄 {ota.upper()} — {slug}")
    print(f"{'='*50}")

    # 1. WPから現在のページを取得
    print("  1. WPからページ取得中...")
    post = fetch_wp_post(site_config, slug)
    current_html = post["content"]["raw"]
    post_id = post["id"]
    print(f"     post_id={post_id}, HTML長={len(current_html)}文字")

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
    check = safety_check(current_html, new_html)
    if not check["passed"]:
        print(f"  🚨 安全チェック失敗: {check['reason']}")
        return {
            "slug": slug,
            "ota": ota,
            "status": "blocked",
            "reason": check["reason"],
        }
    print(f"     ✅ OK (行数: {check.get('old_rows',0)}→{check.get('new_rows',0)})")

    # 9. 下書き更新 or dry-run
    if dry_run:
        output_path = SCRIPT_DIR / "html_output" / f"{slug}_draft.html"
        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(new_html, encoding="utf-8")
        print(f"  9. [DRY-RUN] 出力: {output_path}")
    else:
        print("  9. WP下書き更新中...")
        update_wp_draft(site_config, post_id, new_html)
        print(f"     ✅ 下書き保存完了: post_id={post_id}")

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
        "post_id": post_id,
        "rows": check.get("new_rows", 0),
        "tables_updated": len(replacements),
        "unmatched_coupons": len(unmatched),
        "link_issues": link_issues,
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
    print(f"日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    site_config = load_site_config(args.site)

    if not site_config["wp_url"]:
        print("❌ 環境変数 YF_WP_URL が未設定です")
        sys.exit(1)

    if args.rollback:
        if not args.page:
            print("❌ --rollback には --page が必要です")
            sys.exit(1)
        rollback_from_backup(site_config, args.page)
        return

    results = []
    for page_config in site_config["pages"]:
        if args.page and page_config["slug"] != args.page:
            continue
        try:
            result = update_page(site_config, page_config, dry_run=args.dry_run)
            results.append(result)
        except Exception as e:
            print(f"\n❌ エラー ({page_config['slug']}): {e}")
            results.append({
                "slug": page_config["slug"],
                "ota": page_config["ota"],
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
        print(f"  {emoji} {r['ota'].upper()} ({r['slug']}): {status}")
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
