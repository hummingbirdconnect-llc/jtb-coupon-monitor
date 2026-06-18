#!/usr/bin/env python3
"""
ゆこゆこネット クーポン監視スクリプト
================================================================
ゆこゆこ公式の公開APIと公式ページを取得し、クーポン監視ダッシュボード用
の共通JSONへ変換する。

使い方:
  python3 yukoyuko_coupon_monitor.py --dry-run
  python3 yukoyuko_coupon_monitor.py
  python3 yukoyuko_coupon_monitor.py --init
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from coupon_validator import validate_coupons


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "yukoyuko_coupon_data"
MASTER_FILE = DATA_DIR / "master_ids.json"

PROVIDER = "yukoyuko"
PROVIDER_LABEL = "ゆこゆこネット"
SITE_TARGETS = ["welltrip", "yakushimafan"]
ARTICLE_SLUG = "yukoyuko-coupon"

JST = timezone(timedelta(hours=9))
DATA_RETENTION_DAYS = 30

BASE_URL = "https://www.yukoyuko.net"
COUPON_TOP_URL = f"{BASE_URL}/cp/otocoupon"
DEFAULT_GAIA_API_BASE = "https://gaia-api.yukoyuko.net"
CAMPAIGN_LIST_PATH = "/external/v4/otocoupon/entry/campaign-list"
CAMPAIGN_DETAIL_PATH = "/external/v4/otocoupon/entry/campaign"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}


@dataclass(frozen=True)
class StaticDealSource:
    deal_id: str
    title: str
    category: str
    product_type: str
    url: str
    fallback_discount: str = ""
    fallback_conditions: tuple[str, ...] = ()


STATIC_DEAL_SOURCES = [
    StaticDealSource(
        deal_id="yukoyuko-coupon-howto",
        title="ゆこゆこクーポンの使い方",
        category="クーポン利用条件",
        product_type="宿泊",
        url="https://www.yukoyuko.net/lp/coupon_howtouse/",
        fallback_discount="最大5枚まで併用可能",
        fallback_conditions=("会員登録・ログインが必要", "予約入力画面でクーポンを選択"),
    ),
    StaticDealSource(
        deal_id="yukoyuko-kuchikomi-campaign",
        title="口コミ投稿キャンペーン",
        category="キャンペーン",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/t00373/ALL",
        fallback_discount="次回予約に使えるクーポン",
        fallback_conditions=("公式キャンペーンページで詳細確認",),
    ),
    StaticDealSource(
        deal_id="yukoyuko-takarabako-sale",
        title="ゆこゆこ宝箱セール",
        category="セール",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/t00230",
        fallback_discount="値下げ・特典付きプラン",
        fallback_conditions=("対象宿・対象プラン限定",),
    ),
    StaticDealSource(
        deal_id="yukoyuko-under-10000",
        title="1泊1万円以下の宿",
        category="常設お得プラン",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/g00206",
        fallback_discount="1泊1万円以下プラン",
        fallback_conditions=("対象宿・対象プラン限定",),
    ),
    StaticDealSource(
        deal_id="yukoyuko-early-bird",
        title="先割プラン",
        category="常設お得プラン",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/t00029",
        fallback_discount="早期予約向けプラン",
        fallback_conditions=("対象宿・対象プラン限定",),
    ),
    StaticDealSource(
        deal_id="yukoyuko-last-minute",
        title="直前割プラン",
        category="常設お得プラン",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/t00033",
        fallback_discount="直前予約向けプラン",
        fallback_conditions=("対象宿・対象プラン限定",),
    ),
    StaticDealSource(
        deal_id="yukoyuko-limited-deal",
        title="期間限定のお得プラン",
        category="常設お得プラン",
        product_type="宿泊",
        url="https://www.yukoyuko.net/special/g00009",
        fallback_discount="期間限定プラン",
        fallback_conditions=("対象宿・対象プラン限定",),
    ),
]


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


def format_yen(amount: int | None) -> str:
    if not amount or amount <= 0:
        return ""
    return f"{amount:,}円割引"


def format_percent(rate: int | float | None) -> str:
    if rate is None or rate <= 0:
        return ""
    if float(rate).is_integer():
        return f"{int(rate)}%割引"
    return f"{rate}%割引"


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def format_period(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} ～ {end}"
    return start or end or ""


def stable_id(*parts: str) -> str:
    source = "|".join(part for part in parts if part)
    digest = hashlib.md5(source.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^0-9A-Za-z]+", "-", source).strip("-").lower()[:36]
    return f"{PROVIDER}-{slug}-{digest}" if slug else f"{PROVIDER}-{digest}"


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
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    else:
        existing = []
    existing.extend(changes)
    path.write_text(json.dumps(existing[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_api_config(timeout: int = 20) -> tuple[str, str]:
    response = requests.get(COUPON_TOP_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    html = response.text
    endpoint_match = re.search(r"gaiaApi:\s*\{.*?endpoint:\s*\"([^\"]+)\"", html, re.S)
    key_match = re.search(r"gaiaApi:\s*\{.*?v1Key:\s*\"([^\"]+)\"", html, re.S)
    if not endpoint_match or not key_match:
        raise RuntimeError("failed to extract gaia API config from official coupon page")
    endpoint = endpoint_match.group(1).replace("\\/", "/")
    api_key = key_match.group(1)
    return endpoint, api_key


def api_headers(api_key: str) -> dict[str, str]:
    return {
        **HEADERS,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-api-key": api_key,
    }


def fetch_json_with_key(
    url: str,
    api_key: str,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    response = requests.get(url, headers=api_headers(api_key), params=params or {}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"official API returned status={payload.get('status')} url={url}")
    return payload


def fetch_campaign_list(api_base: str, api_key: str) -> list[dict[str, Any]]:
    payload = fetch_json_with_key(f"{api_base}{CAMPAIGN_LIST_PATH}", api_key)
    result = payload.get("result")
    if not isinstance(result, list):
        raise RuntimeError("campaign-list result is not a list")
    return result


def fetch_campaign_detail(campaign_id: str, api_base: str, api_key: str) -> dict[str, Any]:
    payload = fetch_json_with_key(
        f"{api_base}{CAMPAIGN_DETAIL_PATH}",
        api_key,
        params={"campaign_id": campaign_id},
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"campaign detail result is not an object: {campaign_id}")
    return result


def campaign_category(campaign: dict[str, Any]) -> str:
    campaign_id = campaign.get("campaignId", "")
    name = campaign.get("campaignName", "")
    if campaign_id.startswith("yrcp") or "エリアクーポン" in name or "宿泊割" in name or "Go To" in name:
        return "エリアクーポン・地域割"
    if campaign_id.startswith("ykwm"):
        return "期間限定クーポン"
    if campaign_id.startswith("ydcp") or "宿特別クーポン" in name:
        return "宿限定クーポン"
    return "公式クーポン"


def campaign_discount(campaign: dict[str, Any], coupons: list[dict[str, Any]]) -> str:
    discounts = []
    for coupon in coupons:
        discount = format_percent(coupon.get("discountRate")) if coupon.get("discountRateFlag") else format_yen(coupon.get("discountAmount"))
        if discount:
            discounts.append(discount)
    if discounts:
        unique = list(dict.fromkeys(discounts))
        if len(unique) == 1:
            return unique[0]
        return " / ".join(unique[:4])
    if campaign.get("discountRateFlag"):
        return format_percent(campaign.get("discountRate"))
    return format_yen(campaign.get("discountAmount"))


def stock_status(campaign: dict[str, Any], coupons: list[dict[str, Any]]) -> str:
    now = now_jst()
    start = parse_datetime(campaign.get("entryStartedAt"))
    end = parse_datetime(campaign.get("entryEndedAt"))
    if campaign.get("isAllOverIssueLimit") or (coupons and all(coupon.get("issueLimitOver") for coupon in coupons)):
        return "上限到達"
    if end and end < now:
        return "配布終了"
    if start and start > now:
        return "配布前"
    return "配布中"


def stay_period(coupons: list[dict[str, Any]]) -> str:
    periods = []
    for coupon in coupons:
        period = format_period(coupon.get("stayStartedOn"), coupon.get("stayEndedOn"))
        if period:
            periods.append(period)
    unique = list(dict.fromkeys(periods))
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return " / ".join(unique[:3])


def build_conditions(detail: dict[str, Any], coupons: list[dict[str, Any]]) -> list[str]:
    campaign = detail.get("campaign") or {}
    description = campaign.get("discountDescription") or ""
    conditions = ["ゆこゆこネット会員登録・ログイン後にクーポン獲得"]

    for coupon in coupons:
        min_amount = coupon.get("leastReserveAmount")
        max_amount = coupon.get("atmostReserveAmount")
        if min_amount and min_amount > 0:
            if max_amount and max_amount > 0:
                conditions.append(f"予約金額{min_amount:,}円～{max_amount:,}円で利用可")
            else:
                conditions.append(f"予約金額{min_amount:,}円以上で利用可")
        if coupon.get("ngStayDescription"):
            conditions.append(f"対象外日: {coupon['ngStayDescription']}")
        if coupon.get("combinationFlag") is False:
            conditions.append("他のゆこゆこクーポンと併用不可")
        if coupon.get("issueLimitOver"):
            conditions.append("先着上限に達したクーポンあり")

    if "電話予約では利用不可" in description:
        conditions.append("電話予約では利用不可")
    elif "電話予約でも利用" in description:
        conditions.append("電話予約でも利用可能")
    if "WEB限定" in description or "ゆこゆこネットでご予約時のみ" in description:
        conditions.append("WEB予約限定")

    return list(dict.fromkeys(conditions))[:12]


def compact_coupon(coupon: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "couponId",
        "couponName",
        "discountRateFlag",
        "discountAmount",
        "discountRate",
        "useStartedOn",
        "useEndedOn",
        "stayStartedOn",
        "stayEndedOn",
        "ngStayDescription",
        "leastReserveAmount",
        "atmostReserveAmount",
        "issueCount",
        "issueLimit",
        "issueLimitOver",
        "combinationFlag",
        "hotelListLink",
    ]
    return {key: coupon.get(key) for key in keys if key in coupon}


def build_coupon(
    campaign: dict[str, Any],
    detail: dict[str, Any],
    campaign_detail_api: str = f"{DEFAULT_GAIA_API_BASE}{CAMPAIGN_DETAIL_PATH}",
) -> dict[str, Any]:
    campaign_id = campaign.get("campaignId") or (detail.get("campaign") or {}).get("campaignId", "")
    campaign_detail = detail.get("campaign") or campaign
    coupons = detail.get("coupons") if isinstance(detail.get("coupons"), list) else []
    title = normalize_space(campaign_detail.get("campaignName") or campaign.get("campaignName") or campaign_id)
    source_url = f"{BASE_URL}/cp/otocoupon/{campaign_id}"

    return {
        "id": f"{PROVIDER}-{campaign_id}",
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "site_targets": SITE_TARGETS,
        "article_slug": ARTICLE_SLUG,
        "title": title,
        "category": campaign_category(campaign),
        "discount": campaign_discount(campaign_detail, coupons),
        "stock_status": stock_status(campaign, coupons),
        "product_type": "宿泊",
        "booking_period": format_period(campaign_detail.get("entryStartedAt"), campaign_detail.get("entryEndedAt")),
        "travel_period": stay_period(coupons),
        "coupon_codes": [],
        "conditions": build_conditions(detail, coupons),
        "source_url": source_url,
        "detail_url": source_url,
        "source_type": "official_api",
        "fetch_method": "gaia_api",
        "last_checked": today_str(),
        "confidence": "high",
        "display_type": "table",
        "placement_hint": "ゆこゆこクーポン一覧・公式確認済み枠",
        "detail_data": {
            "campaign_id": campaign_id,
            "campaign_overview": campaign_detail.get("campaignOverview", ""),
            "discount_description": normalize_space(campaign_detail.get("discountDescription", ""))[:1500],
            "coupon_count": detail.get("count", {}).get("coupon") or campaign.get("couponCount"),
            "is_all_over_issue_limit": campaign.get("isAllOverIssueLimit", False),
            "conditions": campaign.get("conditions", {}),
            "coupons": [compact_coupon(coupon) for coupon in coupons],
            "official_api": campaign_detail_api,
        },
    }


def fetch_static_deal(source: StaticDealSource, timeout: int = 20) -> dict[str, Any] | None:
    try:
        response = requests.get(source.url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        print(f"  ⚠️ static page fetch failed: {source.url} ({exc.__class__.__name__})")
        return None
    if response.status_code >= 500:
        print(f"  ⚠️ static page server error: {source.url} ({response.status_code})")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_title = normalize_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    h1 = soup.find("h1")
    heading = normalize_space(h1.get_text(" ", strip=True) if h1 else "")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = normalize_space(meta.get("content", "") if meta else "")
    title = heading or page_title or source.title

    return {
        "id": stable_id(source.deal_id, source.url),
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "site_targets": SITE_TARGETS,
        "article_slug": ARTICLE_SLUG,
        "title": title if title != "ゆこゆこ" else source.title,
        "category": source.category,
        "discount": source.fallback_discount,
        "stock_status": "掲載中" if response.status_code < 400 else "要確認",
        "product_type": source.product_type,
        "booking_period": "",
        "travel_period": "",
        "coupon_codes": [],
        "conditions": list(source.fallback_conditions),
        "source_url": source.url,
        "detail_url": source.url,
        "source_type": "official_page",
        "fetch_method": "requests_html",
        "last_checked": today_str(),
        "confidence": "medium" if response.status_code < 400 else "low",
        "display_type": "table",
        "placement_hint": "ゆこゆこ常設お得情報・使い方補足枠",
        "detail_data": {
            "http_status": response.status_code,
            "page_title": page_title,
            "description": description[:500],
        },
    }


def fetch_all_coupons(detail_limit: int | None = None, include_static: bool = True) -> list[dict[str, Any]]:
    print("📥 ゆこゆこ公式キャンペーン一覧APIを取得中...")
    api_base, api_key = fetch_api_config()
    campaigns = fetch_campaign_list(api_base, api_key)
    coupons: list[dict[str, Any]] = []
    campaign_detail_api = f"{api_base}{CAMPAIGN_DETAIL_PATH}"

    selected_campaigns = campaigns[:detail_limit] if detail_limit else campaigns
    for index, campaign in enumerate(selected_campaigns, 1):
        campaign_id = campaign.get("campaignId", "")
        if not campaign_id:
            continue
        print(f"  [{index}/{len(selected_campaigns)}] {campaign_id} {campaign.get('campaignName', '')}")
        try:
            detail = fetch_campaign_detail(campaign_id, api_base, api_key)
        except Exception as exc:
            print(f"    ⚠️ 個別API取得失敗: {campaign_id} ({exc.__class__.__name__})")
            detail = {
                "campaign": campaign,
                "coupons": [],
                "count": {"coupon": campaign.get("couponCount", 0)},
            }
        coupons.append(build_coupon(campaign, detail, campaign_detail_api=campaign_detail_api))

    if include_static:
        print("📥 ゆこゆこ公式のお得情報ページを確認中...")
        for source in STATIC_DEAL_SOURCES:
            item = fetch_static_deal(source)
            if item:
                coupons.append(item)

    return coupons


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
    active = sum(1 for coupon in coupons if coupon.get("stock_status") == "配布中")
    over_limit = sum(1 for coupon in coupons if coupon.get("stock_status") == "上限到達")
    static_count = sum(1 for coupon in coupons if coupon.get("source_type") == "official_page")

    lines = [
        f"# ゆこゆこネット クーポン監視レポート（{today_str()}）",
        "",
        f"- 取得件数: {len(coupons)}件",
        f"- 配布中: {active}件",
        f"- 上限到達: {over_limit}件",
        f"- 公式ページ由来のお得情報: {static_count}件",
        f"- バリデーション警告: {len(validation_report.get('warnings', []))}件",
        "",
        "## 主な公式クーポン",
    ]
    for coupon in coupons[:20]:
        lines.append(
            f"- {coupon.get('stock_status', '')}: {coupon.get('title', '')} / "
            f"{coupon.get('discount', '')} / {coupon.get('booking_period', '')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_coupons(coupons: list[dict[str, Any]], dry_run: bool = False) -> Path:
    prefix = "dry_run_coupons" if dry_run else "coupons"
    path = DATA_DIR / f"{prefix}_{today_str()}.json"
    path.write_text(json.dumps(coupons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run(dry_run: bool = False, init: bool = False, detail_limit: int | None = None, include_static: bool = True) -> list[dict[str, Any]]:
    setup_dirs()
    master = load_master_ids()
    coupons = fetch_all_coupons(detail_limit=detail_limit, include_static=include_static)
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
    parser = argparse.ArgumentParser(description="ゆこゆこネット公式クーポン監視")
    parser.add_argument("--dry-run", action="store_true", help="保存ファイルをdry_run名にし、master_idsを更新しない")
    parser.add_argument("--init", action="store_true", help="初回実行として差分ログを出さずmaster_idsを初期化")
    parser.add_argument("--detail-limit", type=int, default=0, help="個別API取得数の上限。0は全件")
    parser.add_argument("--no-static", action="store_true", help="常設お得情報ページを取得しない")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    detail_limit = args.detail_limit if args.detail_limit > 0 else None
    run(
        dry_run=args.dry_run,
        init=args.init,
        detail_limit=detail_limit,
        include_static=not args.no_static,
    )


if __name__ == "__main__":
    main()
