#!/usr/bin/env python3
"""
るるぶトラベル クーポン監視スクリプト
================================================================
るるぶトラベル公式の割引クーポンページに埋め込まれている
`window.dealsProp` を取得し、クーポン監視ダッシュボード用の共通JSONへ変換する。

使い方:
  python3 rurubu_travel_coupon_monitor.py --dry-run
  python3 rurubu_travel_coupon_monitor.py
  python3 rurubu_travel_coupon_monitor.py --init
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from coupon_validator import validate_coupons


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "rurubu_travel_coupon_data"
MASTER_FILE = DATA_DIR / "master_ids.json"

PROVIDER = "rurubu_travel"
PROVIDER_LABEL = "るるぶトラベル"
SITE_TARGETS = ["welltrip", "yakushimafan"]
ARTICLE_SLUG = "rurubu-travel-coupon"

BASE_URL = "https://www.rurubu.travel"
DEALS_URL = f"{BASE_URL}/deals"

JST = timezone(timedelta(hours=9))
DATA_RETENTION_DAYS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}


# ============================================================
# ユーティリティ
# ============================================================

def setup_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def now_jst() -> datetime:
    return datetime.now(JST)


def today_str() -> str:
    return now_jst().strftime("%Y-%m-%d")


def now_iso() -> str:
    return now_jst().isoformat()


def normalize_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_period(text: str | None) -> str:
    text = normalize_space(text)
    if not text:
        return ""
    text = re.sub(r"\s*-\s*", "～", text)
    text = re.sub(r"\s*[〜~]\s*", "～", text)
    return text


def stable_id(group_name: str, card_name: str, codes: list[str]) -> str:
    source = "|".join([group_name, card_name, *codes])
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    code_slug = "-".join(code.lower() for code in codes[:3] if code)
    if code_slug:
        return f"{PROVIDER}-{code_slug}-{digest}"
    return f"{PROVIDER}-{digest}"


def parse_japanese_date(value: str) -> date | None:
    value = normalize_space(value)
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def period_end_date(period: str) -> date | None:
    if not period:
        return None
    parts = re.split(r"[～〜~]", period)
    target = parts[-1] if parts else period
    return parse_japanese_date(target)


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
    previous_ids = set(master.get("ids", {}).keys())
    current_ids = {coupon["id"] for coupon in coupons}
    by_id = {coupon["id"]: coupon for coupon in coupons}
    changes: list[dict[str, str]] = []

    for cid in sorted(current_ids - previous_ids):
        coupon = by_id[cid]
        changes.append({
            "date": today,
            "type": "new",
            "category": coupon.get("category", ""),
            "id": cid,
            "title": coupon.get("title", ""),
            "discount": coupon.get("discount", ""),
        })

    for cid in sorted(previous_ids - current_ids):
        previous = master.get("ids", {}).get(cid, {})
        changes.append({
            "date": today,
            "type": "gone",
            "category": previous.get("category", ""),
            "id": cid,
            "title": previous.get("title", ""),
            "discount": previous.get("discount", ""),
        })

    return changes


def append_change_log(changes: list[dict[str, str]]) -> None:
    if not changes:
        return
    path = DATA_DIR / "change_log.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.extend(changes)
    path.write_text(json.dumps(existing[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ============================================================
# 公式ページ取得・変換
# ============================================================

def fetch_deals_html(timeout: int = 30) -> str:
    response = requests.get(DEALS_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_braced_json(text: str, marker: str) -> str:
    marker_index = text.find(marker)
    if marker_index < 0:
        raise RuntimeError(f"marker not found: {marker}")

    start = text.find("{", marker_index + len(marker))
    if start < 0:
        raise RuntimeError(f"JSON object start not found after marker: {marker}")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise RuntimeError("unterminated JSON object for window.dealsProp")


def extract_deals_prop(html: str) -> dict[str, Any]:
    raw = extract_braced_json(html, "window.dealsProp =")
    payload = json.loads(raw)
    if not isinstance(payload.get("pageData", {}).get("couponGroups"), list):
        raise RuntimeError("window.dealsProp.pageData.couponGroups is missing")
    return payload


def stock_status(card: dict[str, Any], options: list[dict[str, Any]], booking_period: str) -> str:
    if options and all(option.get("expired") is True for option in options):
        return "配布終了"
    end = period_end_date(booking_period)
    if end and end < now_jst().date():
        return "配布終了"
    return "配布中"


def compact_coupon_option(option: dict[str, Any]) -> dict[str, Any]:
    search_link = option.get("searchLink") or ""
    compact = {
        "code": normalize_space(option.get("promoCode")),
        "discount": normalize_space(option.get("discount")),
        "expired": bool(option.get("expired")),
        "min_booking_amount": normalize_space(option.get("minBookingAmount")),
        "coupons_left": normalize_space(option.get("couponsLeft")),
        "search_url": urljoin(BASE_URL, search_link) if search_link else "",
        "booking_period": normalize_period(option.get("bookBy")),
        "stay_period": normalize_period(option.get("stayBy")),
        "guests": normalize_space(option.get("noOfGuests")),
    }
    return {key: value for key, value in compact.items() if value not in ("", None, [])}


def option_conditions(options: list[dict[str, Any]]) -> list[str]:
    conditions: list[str] = []
    min_amounts = [normalize_space(option.get("minBookingAmount")) for option in options]
    min_amounts = [item for item in min_amounts if item and item != "なし"]
    if min_amounts:
        conditions.append("最低利用金額: " + " / ".join(dict.fromkeys(min_amounts[:4])))

    left_values = [normalize_space(option.get("couponsLeft")) for option in options]
    left_values = [item for item in left_values if item]
    if left_values:
        conditions.append("残数表示: " + " / ".join(dict.fromkeys(left_values[:4])))

    guest_values = [normalize_space(option.get("noOfGuests")) for option in options]
    guest_values = [item for item in guest_values if item]
    if guest_values:
        conditions.append("宿泊人数: " + " / ".join(dict.fromkeys(guest_values[:3])))

    if any(option.get("showActionButtons") for option in options):
        conditions.append("クーポン欄の「宿泊施設を検索」から対象施設検索が必要")

    if any(option.get("expired") for option in options):
        conditions.append("一部または全てのコードにexpiredフラグあり")

    return conditions


def product_type(group_name: str, card: dict[str, Any]) -> str:
    title = normalize_space(card.get("name"))
    if "アプリ" in group_name or "アプリ" in title:
        return "国内宿泊（アプリ購入限定）"
    return "国内宿泊"


def build_coupon(group_name: str, card: dict[str, Any]) -> dict[str, Any]:
    raw_content = card.get("couponContent")
    content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
    raw_options = content.get("coupons")
    options: list[dict[str, Any]] = [item for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
    codes = [normalize_space(option.get("promoCode")) for option in options if normalize_space(option.get("promoCode"))]
    coupon_codes = []
    for option in options:
        compact = compact_coupon_option(option)
        if compact:
            coupon_codes.append(compact)
    booking_period = normalize_period(card.get("book") or content.get("bookByDate"))
    stay_period = normalize_period(card.get("stay") or content.get("useByDate"))
    title = normalize_space(card.get("name") or content.get("modalTitle"))
    location = normalize_space(card.get("location"))
    conditions = option_conditions(options)
    if location:
        conditions.insert(0, f"対象エリア・施設: {location}")

    return {
        "id": stable_id(group_name, title, codes),
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "site_targets": SITE_TARGETS,
        "article_slug": ARTICLE_SLUG,
        "title": title,
        "category": group_name,
        "discount": normalize_space(card.get("discount")) or ", ".join(
            dict.fromkeys(option.get("discount", "") for option in options if option.get("discount"))
        ),
        "stock_status": stock_status(card, options, booking_period),
        "product_type": product_type(group_name, card),
        "booking_period": booking_period,
        "travel_period": stay_period,
        "stay_period": stay_period,
        "coupon_codes": coupon_codes,
        "conditions": conditions,
        "source_url": DEALS_URL,
        "detail_url": DEALS_URL,
        "source_type": "official_deals",
        "fetch_method": "window.dealsProp",
        "last_checked": today_str(),
        "confidence": "high",
        "display_type": "table",
        "placement_hint": "るるぶトラベル公式割引クーポンページ",
        "detail_data": {
            "modal_title": normalize_space(content.get("modalTitle")),
            "location": location,
            "coupon_option_count": len(options),
            "coupon_codes": coupon_codes,
            "search_urls": [item.get("search_url") for item in coupon_codes if item.get("search_url")],
            "source_page_title": "【お得な割引クーポン】今日のおトク情報をチェック！",
        },
    }


def fetch_all_coupons() -> list[dict[str, Any]]:
    print(f"📥 るるぶトラベル公式クーポンページを取得中... {DEALS_URL}")
    html = fetch_deals_html()
    payload = extract_deals_prop(html)
    raw_page_data = payload.get("pageData")
    page_data: dict[str, Any] = raw_page_data if isinstance(raw_page_data, dict) else {}
    raw_groups = page_data.get("couponGroups")
    groups = [item for item in raw_groups if isinstance(item, dict)] if isinstance(raw_groups, list) else []
    coupons: list[dict[str, Any]] = []
    for group in groups:
        group_name = normalize_space(group.get("name"))
        raw_cards = group.get("cards")
        cards = [item for item in raw_cards if isinstance(item, dict)] if isinstance(raw_cards, list) else []
        for card in cards:
            coupon = build_coupon(group_name, card)
            if coupon.get("title"):
                coupons.append(coupon)
    if not coupons:
        raise RuntimeError("no coupons extracted from rurubu deals page")
    print(f"  ✅ 公式ページから {len(coupons)}件のクーポンカードを抽出")
    return coupons


# ============================================================
# 保存・レポート
# ============================================================

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


def save_coupons(coupons: list[dict[str, Any]], dry_run: bool = False) -> Path:
    prefix = "dry_run_coupons" if dry_run else "coupons"
    path = DATA_DIR / f"{prefix}_{today_str()}.json"
    path.write_text(json.dumps(coupons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_report(coupons: list[dict[str, Any]], validation_report: dict[str, Any], dry_run: bool = False) -> Path:
    prefix = "dry_run_report" if dry_run else "report"
    path = DATA_DIR / f"{prefix}_{today_str()}.md"
    active = sum(1 for coupon in coupons if coupon.get("stock_status") == "配布中")
    ended = sum(1 for coupon in coupons if coupon.get("stock_status") == "配布終了")
    code_count = sum(len(coupon.get("coupon_codes") or []) for coupon in coupons)
    category_counts: dict[str, int] = {}
    for coupon in coupons:
        category_counts[coupon.get("category", "未分類")] = category_counts.get(coupon.get("category", "未分類"), 0) + 1

    lines = [
        f"# るるぶトラベル クーポン監視レポート（{today_str()}）",
        "",
        f"- 取得元: {DEALS_URL}",
        f"- 取得件数: {len(coupons)}件",
        f"- クーポンコード候補: {code_count}件",
        f"- 配布中: {active}件",
        f"- 配布終了: {ended}件",
        f"- バリデーション警告: {len(validation_report.get('warnings', []))}件",
        "",
        "## カテゴリ別件数",
    ]
    for category, count in category_counts.items():
        lines.append(f"- {category}: {count}件")
    lines.extend(["", "## 主な公式クーポン"])
    for coupon in coupons[:25]:
        codes = [item.get("code", "") for item in coupon.get("coupon_codes", []) if item.get("code")]
        code_text = ", ".join(codes[:3])
        lines.append(
            f"- {coupon.get('stock_status', '')}: {coupon.get('title', '')} / "
            f"{coupon.get('discount', '')} / {coupon.get('booking_period', '')}"
            + (f" / {code_text}" if code_text else "")
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(dry_run: bool = False, init: bool = False) -> list[dict[str, Any]]:
    setup_dirs()
    master = load_master_ids()
    coupons = fetch_all_coupons()
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
    parser = argparse.ArgumentParser(description="るるぶトラベル公式クーポン監視")
    parser.add_argument("--dry-run", action="store_true", help="保存ファイルをdry_run名にし、master_idsを更新しない")
    parser.add_argument("--init", action="store_true", help="初回実行として差分ログを出さずmaster_idsを初期化")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    run(dry_run=args.dry_run, init=args.init)


if __name__ == "__main__":
    main()
