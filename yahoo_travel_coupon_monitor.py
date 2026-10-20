#!/usr/bin/env python3
"""
Yahoo!トラベル クーポン監視スクリプト
================================================================
Yahoo!トラベル公式の「クーポン一覧」（宿限定の割引クーポン）と自治体クーポン
特集を取得し、クーポン監視ダッシュボード用の共通JSONへ変換する。

宿泊予約の仕組みは一休と共通で、同じクーポンが一休.com にも載ることがある。
同じ管理IDが一休側の最新データにもあれば detail_data.shared_with に "ikkyu" を
記す（出所の考え方は ikyu_platform_coupons.py の冒頭）。

使い方:
  python3 yahoo_travel_coupon_monitor.py --dry-run
  python3 yahoo_travel_coupon_monitor.py
  python3 yahoo_travel_coupon_monitor.py --init

ログインが要る範囲（Yahoo! JAPAN ID）は読まない。クーポンの獲得・残枚数・
ランダム表示のクーポンは取れない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from coupon_validator import validate_coupons
from ikyu_platform_coupons import (
    JST,
    SiteConfig,
    fetch_all_coupons,
    mark_shared_listing,
    now_iso,
    now_jst,
    today_str,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "yahoo_travel_coupon_data"
MASTER_FILE = DATA_DIR / "master_ids.json"

SITE = SiteConfig(
    provider="yahoo_travel",
    provider_label="Yahoo!トラベル",
    base_url="https://travel.yahoo.co.jp",
    login_label="Yahoo! JAPAN ID",
    site_targets=("welltrip", "yakushimafan"),
    article_slug="yahoo-travel-coupon",
    sibling_provider="ikkyu",
    sibling_data_dir="ikkyu_coupon_data",
)
PROVIDER = SITE.provider
PROVIDER_LABEL = SITE.provider_label
DATA_RETENTION_DAYS = 30


def setup_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def load_master_ids() -> dict[str, Any]:
    if MASTER_FILE.exists():
        return json.loads(MASTER_FILE.read_text(encoding="utf-8"))
    return {"ids": {}, "last_updated": ""}


def save_master_ids(coupons: list[dict[str, Any]], master: dict[str, Any]) -> None:
    ids = master.get("ids", {})
    today = today_str()
    for coupon in coupons:
        cid = coupon["id"]
        item = ids.get(cid, {})
        ids[cid] = {
            "first_seen": item.get("first_seen", today),
            "last_seen": today,
            "title": coupon.get("title", ""),
            "category": coupon.get("category", ""),
            "discount": coupon.get("discount", ""),
            "stock_status": coupon.get("stock_status", ""),
        }
    MASTER_FILE.write_text(
        json.dumps({"ids": ids, "last_updated": now_iso()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_change_log(coupons: list[dict[str, Any]], master: dict[str, Any]) -> list[dict[str, str]]:
    today = today_str()
    previous = master.get("ids", {})
    previous_ids = set(previous.keys())
    current_ids = {coupon["id"] for coupon in coupons}
    by_id = {coupon["id"]: coupon for coupon in coupons}
    changes: list[dict[str, str]] = []

    for cid in sorted(current_ids - previous_ids):
        coupon = by_id[cid]
        changes.append({
            "date": today, "type": "new", "category": coupon.get("category", ""),
            "id": cid, "title": coupon.get("title", ""), "discount": coupon.get("discount", ""),
        })
    for cid in sorted(previous_ids - current_ids):
        item = previous.get(cid, {})
        changes.append({
            "date": today, "type": "gone", "category": item.get("category", ""),
            "id": cid, "title": item.get("title", ""), "discount": item.get("discount", ""),
        })
    for cid in sorted(current_ids & previous_ids):
        before = previous.get(cid, {}).get("stock_status", "")
        after = by_id[cid].get("stock_status", "")
        if before and after and before != after:
            changes.append({
                "date": today, "type": "status", "category": by_id[cid].get("category", ""),
                "id": cid, "title": by_id[cid].get("title", ""),
                "discount": by_id[cid].get("discount", ""), "detail": f"{before} → {after}",
            })
    return changes


def append_change_log(changes: list[dict[str, str]]) -> None:
    if not changes:
        return
    path = DATA_DIR / "change_log.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.extend(changes)
    path.write_text(json.dumps(existing[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cleanup_old_files() -> None:
    cutoff = now_jst() - timedelta(days=DATA_RETENTION_DAYS)
    for pattern in ("coupons_*.json", "report_*.md"):
        for path in DATA_DIR.glob(pattern):
            try:
                date_part = path.stem.split("_", 1)[1]
                file_date = datetime.strptime(date_part[:10], "%Y-%m-%d").replace(tzinfo=JST)
            except (IndexError, ValueError):
                continue
            if file_date < cutoff:
                path.unlink()


def write_report(coupons: list[dict[str, Any]], validation_report: dict[str, Any], dry_run: bool = False) -> Path:
    prefix = "dry_run_report" if dry_run else "report"
    path = DATA_DIR / f"{prefix}_{today_str()}.md"
    hotel = [c for c in coupons if c.get("category") == "宿限定クーポン"]
    gov = [c for c in coupons if c.get("category") == "自治体クーポン"]
    shared = sum(1 for c in coupons if (c.get("detail_data") or {}).get("shared_with"))
    no_region = sum(1 for c in coupons if not c.get("regions"))
    lines = [
        f"# {PROVIDER_LABEL} クーポン監視レポート（{today_str()}）",
        "",
        f"- 取得件数: {len(coupons)}件（宿限定 {len(hotel)}件・自治体 {len(gov)}件）",
        f"- 配布中: {sum(1 for c in coupons if c.get('stock_status') == '配布中')}件",
        f"- 配布終了: {sum(1 for c in coupons if c.get('stock_status') == '配布終了')}件",
        f"- 一休.com にも同じクーポン: {shared}件",
        f"- 地域が分からない: {no_region}件",
        f"- バリデーション警告: {len(validation_report.get('warnings', []))}件",
        "",
        "## 主なクーポン",
    ]
    for coupon in coupons[:25]:
        lines.append(
            f"- {coupon.get('stock_status', '')}: {coupon.get('title', '')} / "
            f"{coupon.get('discount', '')} / {coupon.get('booking_period', '')} / {'・'.join(coupon.get('regions') or []) or '地域不明'}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_coupons(coupons: list[dict[str, Any]], dry_run: bool = False) -> Path:
    prefix = "dry_run_coupons" if dry_run else "coupons"
    path = DATA_DIR / f"{prefix}_{today_str()}.json"
    path.write_text(json.dumps(coupons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run(dry_run: bool = False, init: bool = False, include_government: bool = True) -> list[dict[str, Any]]:
    setup_dirs()
    master = load_master_ids()
    coupons = fetch_all_coupons(SITE, include_government=include_government)
    if not coupons:
        raise SystemExit("取得0件。ページの作りが変わった可能性")
    marked = mark_shared_listing(coupons, SITE, BASE_DIR)
    if marked:
        print(f"🔗 一休.com の最新データにも同じクーポン: {marked}件")
    coupons, validation_report = validate_coupons(coupons, master_ids=master, service_name=PROVIDER_LABEL)
    if not validation_report.get("is_healthy", True):
        raise SystemExit("validation failed: unhealthy coupon snapshot")

    path = save_coupons(coupons, dry_run=dry_run)
    report_path = write_report(coupons, validation_report, dry_run=dry_run)
    print(f"💾 保存: {path}")
    print(f"📝 レポート: {report_path}")

    if not dry_run:
        changes = [] if init else generate_change_log(coupons, master)
        append_change_log(changes)
        save_master_ids(coupons, master)
        cleanup_old_files()
        print(f"🧭 master_ids 更新: {MASTER_FILE}")
        if changes:
            print(f"🆕 差分: {len(changes)}件")
    return coupons


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{PROVIDER_LABEL} 公式クーポン監視")
    parser.add_argument("--dry-run", action="store_true", help="保存ファイルをdry_run名にし、master_idsを更新しない")
    parser.add_argument("--init", action="store_true", help="初回実行として差分ログを出さずmaster_idsを初期化")
    parser.add_argument("--no-government", action="store_true", help="自治体クーポン特集を読まない")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    run(dry_run=args.dry_run, init=args.init, include_government=not args.no_government)


if __name__ == "__main__":
    main()
