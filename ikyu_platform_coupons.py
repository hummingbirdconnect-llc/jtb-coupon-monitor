#!/usr/bin/env python3
"""
一休の仕組みで動くクーポン一覧（一休.com・Yahoo!トラベル）の共通部品
================================================================
Yahoo!トラベルの宿泊予約は一休が運営しており、公式の「割引クーポンの一覧」
（/coupons/accommodations）は両サイトとも同じ作り（Nuxt 3）で、クーポンの
管理ID（manageId）も共有している。本モジュールは次を受け持つ。

- 公開ページの __NUXT_DATA__（devalue 形式）を Python の値に戻す
- 宿泊クーポン一覧を監視ダッシュボード用の共通JSONへ変換する
- 自治体クーポン特集（/campaign/government/）の状態を読む
- 相手サイトの最新データと突き合わせ、同じクーポンの出所を記録する

配布元の考え方:
  クーポンの提供主体は宿で、配布の仕組みは一休のシステム。両サイトに同じ
  manageId で載るものは「一休の仕組みで配る1つのクーポン」を2か所に掲示した
  ものとして扱い、origin_provider は常に "ikkyu" にする。片方にしか載らない
  ものは、その宿がそのサイトだけに出したクーポン。

使う側: yahoo_travel_coupon_monitor.py / ikkyu_coupon_monitor.py
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}

COUPON_LIST_PATH = "/coupons/accommodations"
GOVERNMENT_PATH = "/campaign/government/"

# 一覧の「東日本／中日本／西日本」タブ（largeAreaId）
LARGE_AREA_LABELS = {"100000": "東日本", "200000": "中日本", "300000": "西日本"}

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]


@dataclass(frozen=True)
class SiteConfig:
    provider: str            # データのID（provider_registry.json の id と合わせる）
    provider_label: str      # 表示名
    base_url: str            # 公式サイトの起点
    login_label: str         # クーポン獲得に要るログインの呼び名
    site_targets: tuple[str, ...]
    article_slug: str
    sibling_provider: str    # 同じ仕組みのもう1社の provider
    sibling_data_dir: str    # その会社の保存フォルダ名


# ============================================================
# 日付・文字の整形
# ============================================================

def now_jst() -> datetime:
    return datetime.now(JST)


def today_str() -> str:
    return now_jst().strftime("%Y-%m-%d")


def now_iso() -> str:
    return now_jst().isoformat()


def normalize_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def format_date(value: str | None, with_time: bool = False) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return ""
    if with_time:
        return parsed.strftime("%Y/%m/%d %H:%M")
    return parsed.strftime("%Y/%m/%d")


def prefecture_from_text(text: str) -> str:
    """「静岡県伊東市」「北海道」のような文字から都道府県だけを返す。無ければ空。"""
    t = normalize_space(text)
    for pref in PREFECTURES:
        if t.startswith(pref):
            return pref
    for pref in PREFECTURES:
        if pref in t:
            return pref
    return ""


def stable_id(provider: str, *parts: str) -> str:
    source = "|".join(part for part in parts if part)
    digest = hashlib.md5(source.encode("utf-8")).hexdigest()[:10]
    return f"{provider}-gov-{digest}"


# ============================================================
# Nuxt 3 の __NUXT_DATA__（devalue 形式）を戻す
# ============================================================

_WRAPPERS = {"ShallowReactive", "Reactive", "ShallowRef", "Ref", "EmptyRef", "EmptyShallowRef"}


def unflatten_devalue(flat: list[Any]) -> Any:
    """devalue が作る配列（先頭が根、参照は添字）を普通の値に戻す。"""
    cache: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index == -1:
            return None
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in _WRAPPERS:
                    result = hydrate(value[1])
                    cache[index] = result
                    return result
                if tag == "Date":
                    cache[index] = value[1]
                    return value[1]
                if tag == "Set":
                    result = []
                    cache[index] = result
                    result.extend(hydrate(x) for x in value[1:])
                    return result
                if tag == "Map":
                    result = {}
                    cache[index] = result
                    for key_index, value_index in zip(value[1::2], value[2::2]):
                        result[hydrate(key_index)] = hydrate(value_index)
                    return result
                if tag in ("BigInt", "RegExp", "null", "Object", "undefined"):
                    result = hydrate(value[1]) if len(value) > 1 and isinstance(value[1], int) else None
                    cache[index] = result
                    return result
            result = []
            cache[index] = result
            result.extend(hydrate(x) for x in value)
            return result
        if isinstance(value, dict):
            result = {}
            cache[index] = result
            for key, child in value.items():
                result[key] = hydrate(child)
            return result
        cache[index] = value
        return value

    return hydrate(0)


def load_nuxt_data(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NUXT_DATA__")
    if not script:
        raise RuntimeError("__NUXT_DATA__ が見つからない。ページの作りが変わった可能性")
    data = unflatten_devalue(json.loads(script.get_text()))
    if not isinstance(data, dict):
        raise RuntimeError("__NUXT_DATA__ の根が辞書でない")
    return data


def extract_coupon_list(html: str) -> list[dict[str, Any]]:
    """クーポン一覧ページの HTML から、宿泊クーポンの生データ一覧を取り出す。"""
    data = load_nuxt_data(html)
    payload = (data.get("data") or {}).get("accommodationCouponsAvailable") or {}
    coupons = payload.get("coupons")
    if not isinstance(coupons, list):
        raise RuntimeError("accommodationCouponsAvailable.coupons が無い")
    return coupons


# ============================================================
# 共通JSONへの変換（宿泊クーポン）
# ============================================================

_OFF_PATTERN = re.compile(r"([0-9,]+)\s*円\s*OFF", re.I)
_LIMIT_PATTERN = re.compile(r"(先着\s*[0-9,]+\s*名様?限定|枚数限定)")


def discount_text(description: str) -> str:
    m = _OFF_PATTERN.search(description or "")
    if m:
        return f"{m.group(1)}円割引"
    return normalize_space(description)


def limit_text(description: str) -> str:
    m = _LIMIT_PATTERN.search(description or "")
    return normalize_space(m.group(1)) if m else ""


def coupon_stock_status(raw: dict[str, Any], now: datetime | None = None) -> str:
    now = now or now_jst()
    expired_at = parse_iso(raw.get("expiredAt"))
    if expired_at and expired_at < now:
        return "配布終了"
    return "配布中"


def coupon_regions(raw: dict[str, Any]) -> list[str]:
    """宿の所在地（prefectureName）から都道府県を並べる。分からなければ空のまま。"""
    regions: list[str] = []
    for acc in raw.get("accommodations") or []:
        pref = prefecture_from_text(acc.get("prefectureName") or "")
        if pref and pref not in regions:
            regions.append(pref)
    return regions


def build_coupon(raw: dict[str, Any], site: SiteConfig, now: datetime | None = None) -> dict[str, Any]:
    manage_id = str(raw.get("manageId") or "")
    accommodations = raw.get("accommodations") or []
    first = accommodations[0] if accommodations else {}
    hotel_name = normalize_space(raw.get("name") or first.get("name") or "")
    description = normalize_space(raw.get("description") or "")
    limitation = raw.get("limitation") or {}
    checkout = limitation.get("checkOutDate") or {}
    min_amount = limitation.get("minAmount") or 0
    regions = coupon_regions(raw)
    area_parts = [normalize_space(first.get("prefectureName") or ""), normalize_space(first.get("areaName") or "")]
    area = "／".join(p for p in area_parts if p)
    large_area = LARGE_AREA_LABELS.get(str(first.get("largeAreaId") or ""), "")

    conditions: list[str] = []
    if min_amount:
        conditions.append(f"予約金額{int(min_amount):,}円以上")
    limit = limit_text(description)
    if limit:
        conditions.append(limit)
    if raw.get("dayUseExclude"):
        conditions.append("デイユース（日帰り）は対象外")
    if raw.get("isHotelGroupCoupon") or len(accommodations) > 1:
        conditions.append(f"対象施設{len(accommodations)}軒（グループ共通）")
    conditions.append(f"{site.login_label}でログインしてクーポンを獲得（予約入力画面で自動選択）")

    hotel_url = f"{site.base_url}/{first.get('accommodationId')}/?discsort=1&lc=1&ppc=2&rc=1&st=1" if first.get("accommodationId") else ""
    detail_url = f"{site.base_url}{COUPON_LIST_PATH}?cmid={manage_id}" if manage_id else f"{site.base_url}{COUPON_LIST_PATH}"

    return {
        "id": f"{site.provider}-{manage_id}",
        "provider": site.provider,
        "provider_label": site.provider_label,
        "site_targets": list(site.site_targets),
        "article_slug": site.article_slug,
        "title": f"{hotel_name} {description}".strip(),
        "category": "宿限定クーポン",
        "discount": discount_text(description),
        "stock_status": coupon_stock_status(raw, now),
        "product_type": "宿泊",
        "area": area,
        "regions": regions,
        "booking_period": f"～ {format_date(raw.get('expiredAt'), with_time=True)}" if raw.get("expiredAt") else "",
        "travel_period": " ～ ".join(x for x in (format_date(checkout.get("begin")), format_date(checkout.get("end"))) if x),
        "stay_period": " ～ ".join(x for x in (format_date(checkout.get("begin")), format_date(checkout.get("end"))) if x),
        "coupon_codes": [],
        "conditions": conditions,
        "source_url": f"{site.base_url}{COUPON_LIST_PATH}",
        "detail_url": detail_url,
        "source_type": "official_page",
        "fetch_method": "nuxt_data",
        "last_checked": today_str(),
        "confidence": "high",
        "display_type": "table",
        "placement_hint": f"{site.provider_label}の宿限定クーポン一覧（公式確認済み）",
        "detail_data": {
            "platform": "ikyu",
            "platform_manage_id": manage_id,
            "origin_provider": "ikkyu",
            "shared_with": [],
            "hotel_name": hotel_name,
            "hotel_url": hotel_url,
            "accommodation_ids": [a.get("accommodationId") for a in accommodations if a.get("accommodationId")],
            "accommodations": [
                {
                    "name": normalize_space(a.get("name") or ""),
                    "accommodation_id": a.get("accommodationId"),
                    "prefecture": normalize_space(a.get("prefectureName") or ""),
                    "area": normalize_space(a.get("areaName") or ""),
                    "large_area": LARGE_AREA_LABELS.get(str(a.get("largeAreaId") or ""), ""),
                }
                for a in accommodations
            ],
            "large_area": large_area,
            "description": description,
            "min_amount": int(min_amount) if min_amount else None,
            "expired_at": raw.get("expiredAt"),
            "checkout_begin": checkout.get("begin"),
            "checkout_end": checkout.get("end"),
            "day_use_exclude": bool(raw.get("dayUseExclude")),
            "is_hotel_group_coupon": bool(raw.get("isHotelGroupCoupon")),
        },
    }


# ============================================================
# 自治体クーポン特集（/campaign/government/）
# ============================================================

def parse_government_page(html: str, site: SiteConfig) -> list[dict[str, Any]]:
    """自治体クーポン特集のカードを読む。終了の帯があれば「配布終了」にする。"""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    source_url = f"{site.base_url}{GOVERNMENT_PATH}"
    for article in soup.find_all("article"):
        texts = [normalize_space(p.get_text(" ", strip=True)) for p in article.find_all(["p", "h3"])]
        texts = [t for t in texts if t]
        if not texts:
            continue
        ended = any("終了しました" in t for t in texts)
        pref_text = ""
        campaign_name = ""
        heading = article.find("h3")
        if heading:
            campaign_name = normalize_space(heading.get_text(" ", strip=True))
        for t in texts:
            if prefecture_from_text(t) and "終了" not in t and len(t) <= 20:
                pref_text = t
                break
        discount = ""
        for p in article.find_all("p"):
            txt = normalize_space(p.get_text("", strip=True))
            if re.search(r"(円|%|％)OFF", txt):
                discount = txt
                break
        if not campaign_name and not discount:
            continue
        links = {normalize_space(a.get_text(" ", strip=True)): a.get("href") for a in article.find_all("a", href=True)}
        detail = links.get("詳細をみる") or ""
        search = links.get("宿を探す") or ""
        if detail and detail.startswith("/"):
            detail = f"{site.base_url}{detail}"
        if search and search.startswith("/"):
            search = f"{site.base_url}{search}"
        pref = prefecture_from_text(pref_text)
        items.append({
            "id": stable_id(site.provider, pref_text, campaign_name),
            "provider": site.provider,
            "provider_label": site.provider_label,
            "site_targets": list(site.site_targets),
            "article_slug": site.article_slug,
            "title": f"{campaign_name}（{pref_text}）" if pref_text else campaign_name,
            "category": "自治体クーポン",
            "discount": discount.replace("OFFクーポン", "割引").replace("OFF", "割引"),
            "stock_status": "配布終了" if ended else "配布中",
            "product_type": "宿泊",
            "area": pref_text,
            "regions": [pref] if pref else [],
            "booking_period": "",
            "travel_period": "",
            "stay_period": "",
            "coupon_codes": [],
            "conditions": ["予約時に自動で割引（手続き不要）", "対象施設・条件は自治体の定めによる"],
            "source_url": source_url,
            "detail_url": detail or source_url,
            "source_type": "official_page",
            "fetch_method": "requests_html",
            "last_checked": today_str(),
            "confidence": "medium",
            "display_type": "table",
            "placement_hint": f"{site.provider_label}の自治体クーポン特集",
            "detail_data": {
                "platform": "ikyu",
                "origin_provider": "ikkyu",
                "shared_with": [],
                "campaign_name": campaign_name,
                "prefecture_text": pref_text,
                "ended_label": ended,
                "search_url": search,
                "raw_discount": discount,
            },
        })
    return items


# ============================================================
# 取得
# ============================================================

def fetch_html(url: str, timeout: int = 30) -> str:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_all_coupons(site: SiteConfig, include_government: bool = True) -> list[dict[str, Any]]:
    print(f"📥 {site.provider_label} 公式のクーポン一覧を取得中... {site.base_url}{COUPON_LIST_PATH}")
    html = fetch_html(f"{site.base_url}{COUPON_LIST_PATH}")
    raw_items = extract_coupon_list(html)
    now = now_jst()
    coupons = [build_coupon(raw, site, now) for raw in raw_items if raw.get("manageId")]
    print(f"  宿限定クーポン: {len(coupons)}件（東日本・中日本・西日本の全タブ）")

    if include_government:
        print(f"📥 {site.provider_label} 自治体クーポン特集を確認中... {site.base_url}{GOVERNMENT_PATH}")
        try:
            gov_html = fetch_html(f"{site.base_url}{GOVERNMENT_PATH}")
            gov_items = parse_government_page(gov_html, site)
            print(f"  自治体クーポン: {len(gov_items)}件（配布中 {sum(1 for g in gov_items if g['stock_status'] == '配布中')}件）")
            coupons.extend(gov_items)
        except requests.RequestException as exc:
            print(f"  ⚠️ 自治体クーポン特集の取得失敗: {exc.__class__.__name__}")
    return coupons


# ============================================================
# 相手サイトとの突き合わせ（出所の記録）
# ============================================================

def latest_saved_file(data_dir: Path) -> Path | None:
    files = sorted(glob.glob(str(data_dir / "coupons_*.json")))
    return Path(files[-1]) if files else None


def mark_shared_listing(coupons: list[dict[str, Any]], site: SiteConfig, base_dir: Path) -> int:
    """相手サイトの最新保存データに同じ管理IDがあれば shared_with に相手を記す。

    相手のデータが無ければ何もしない（取得はしない）。戻り値は印を付けた件数。
    """
    sibling_file = latest_saved_file(base_dir / site.sibling_data_dir)
    if not sibling_file:
        return 0
    try:
        sibling = json.loads(sibling_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    sibling_ids = {
        str((c.get("detail_data") or {}).get("platform_manage_id") or "")
        for c in sibling
        if isinstance(c, dict)
    }
    sibling_ids.discard("")
    marked = 0
    for coupon in coupons:
        detail = coupon.setdefault("detail_data", {})
        manage_id = str(detail.get("platform_manage_id") or "")
        shared = [p for p in detail.get("shared_with", []) if p != site.sibling_provider]
        if manage_id and manage_id in sibling_ids:
            shared.append(site.sibling_provider)
            marked += 1
        detail["shared_with"] = shared
        detail["sibling_checked_file"] = sibling_file.name
    return marked
