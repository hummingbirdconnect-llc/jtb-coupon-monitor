#!/usr/bin/env python3
"""
JALパック クーポン監視スクリプト（パイロット版）
================================================================
JAL公式の公開ページを取得し、JALパック記事で扱うクーポン候補を
共通JSONへ変換する。公式ページがタイムアウトする場合は、既存の
coupon-master を入力元にして DRY-RUN 検証を継続する。

使い方:
  python jalpack_coupon_monitor.py --dry-run
  python jalpack_coupon_monitor.py --source master --dry-run
  python jalpack_coupon_monitor.py --source official
  python jalpack_coupon_monitor.py --source official --fetch-method chrome --dry-run
  python jalpack_coupon_monitor.py --init --source master
"""

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from coupon_validator import validate_coupons


# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "jalpack_coupon_data"
MASTER_FILE = DATA_DIR / "master_ids.json"
LOCAL_COUPON_MASTER = (
    BASE_DIR.parent
    / "vault_blog/ウェルトリップ/会社別/JALパック/coupon-master/master.md"
)

PROVIDER = "jalpack"
PROVIDER_LABEL = "JALパック"
SITE_TARGETS = ["welltrip"]
ARTICLE_SLUG = "jal-pack-coupon"

DATA_RETENTION_DAYS = 30
JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
    "Connection": "close",
}


@dataclass(frozen=True)
class SourceRule:
    coupon_id: str
    title: str
    category: str
    product_type: str
    url: str
    placement_hint: str
    fallback_discount: str = ""
    fallback_booking_period: str = ""
    fallback_travel_period: str = ""
    fallback_conditions: tuple[str, ...] = ()


OFFICIAL_SOURCES = [
    SourceRule(
        coupon_id="jalpack-domestic-birthday",
        title="バースデークーポン（国内）",
        category="公式クーポン",
        product_type="国内ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/domtour/birthday-cpn/index.html",
        placement_hint="公式クーポンのJALパッククーポン一覧",
        fallback_discount="15,000円/8,000円割引",
        fallback_conditions=("JMB会員", "他クーポンと併用不可", "予約前にクーポン獲得"),
    ),
    SourceRule(
        coupon_id="jalpack-overseas-birthday",
        title="バースデークーポン（海外）",
        category="公式クーポン",
        product_type="海外ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/intltour/birthday-cpn/index.html",
        placement_hint="公式クーポンのJALパッククーポン一覧",
        fallback_discount="10,000円割引",
        fallback_conditions=("JMB会員", "他クーポンと併用不可", "予約前にクーポン獲得"),
    ),
    SourceRule(
        coupon_id="jalpack-domestic-timesale",
        title="期間限定タイムセールクーポン（国内）",
        category="公式クーポン",
        product_type="国内ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/domtour/jaldp/time_sale/index.html",
        placement_hint="公式クーポンのJALパッククーポン一覧",
        fallback_discount="最大30,000円割引",
        fallback_conditions=("JMB会員", "先着順", "対象施設・方面限定"),
    ),
    SourceRule(
        coupon_id="jalpack-overseas-timesale",
        title="海外タイムセールクーポン",
        category="公式クーポン",
        product_type="海外ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/intltour/jaldp/timesale/index2.html/",
        placement_hint="公式クーポンのJALパッククーポン一覧",
        fallback_discount="5,000円/10,000円/20,000円/40,000円割引",
        fallback_conditions=("公開コード型", "対象方面・旅行代金条件あり", "他割引クーポンと併用不可"),
    ),
    SourceRule(
        coupon_id="jalpack-lsp-star",
        title="LSP Star特典クーポン",
        category="会員ランク",
        product_type="国内ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/domtour/lsp-coupon/index.html",
        placement_hint="会員ランクのJALパッククーポン一覧",
        fallback_discount="5,000円割引",
        fallback_conditions=("Starグレード会員", "旅行代金80,000円以上", "他クーポンと併用可"),
    ),
    SourceRule(
        coupon_id="jalpack-hayakime",
        title="早決プラン55/60/90",
        category="公式クーポン",
        product_type="国内ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/domtour/hayakime-cpn/",
        placement_hint="公式クーポンのJALパッククーポン一覧",
        fallback_discount="プラン単位で割引価格",
        fallback_conditions=("対象ホテル・対象商品限定", "出発55/60/90日前まで"),
    ),
    SourceRule(
        coupon_id="jalpack-jalcard",
        title="JALカード会員限定ツアー割引",
        category="カード連携",
        product_type="国内・海外ダイナミックパッケージ",
        url="https://www.jal.co.jp/jp/ja/jalcard/service/",
        placement_hint="カード連携のJALパッククーポン一覧",
        fallback_discount="2%OFF",
        fallback_conditions=("JALカード会員", "専用ページからのWEB予約"),
    ),
]

END_PATTERNS = [
    "終了いたしました",
    "終了しました",
    "配布は終了",
    "配布終了",
    "販売終了",
    "予約受け付けを停止",
]

CODE_PATTERN = re.compile(r"\b[A-Z]{2,4}\d{4,6}\b")


# ============================================================
# ユーティリティ
# ============================================================

def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now(JST).strftime("%Y-%m-%d")


def now_iso():
    return datetime.now(JST).isoformat()


def slugify(text):
    cleaned = re.sub(r"https?://", "", text)
    cleaned = re.sub(r"[^0-9A-Za-zぁ-んァ-ン一-龥]+", "-", cleaned).strip("-")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:40]}-{digest}" if cleaned else digest


def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def split_markdown_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def is_separator_row(line):
    cells = split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells)


def strip_markdown(text):
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text or "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    return normalize_space(text)


def extract_urls(text):
    return re.findall(r"https?://[^\s\)|＞<>]+", text or "")


def extract_codes(text):
    return sorted(set(CODE_PATTERN.findall(text or "")))


def extract_discount(text, fallback=""):
    text = normalize_space(text)
    yen_matches = re.findall(r"(?:最大|お一人様|1グループにつき)?\s*[0-9,]+円(?:OFF|割引|引き|分)?", text)
    percent_matches = re.findall(r"[0-9]+(?:\.[0-9]+)?\s*%OFF|[0-9]+(?:\.[0-9]+)?\s*％OFF", text)
    matches = [normalize_space(m) for m in yen_matches + percent_matches]
    if matches:
        return " / ".join(dict.fromkeys(matches[:4]))
    return fallback


def extract_period(text, labels):
    normalized = normalize_space(text)
    for label in labels:
        pattern = rf"{label}[：:\s]*([^。|\n]+?)(?=(?:対象|条件|利用|販売|設定|予約|出発|ご注意|$))"
        m = re.search(pattern, normalized)
        if m:
            return normalize_space(m.group(1))[:120]
    return ""


def status_from_text(text):
    return "配布終了" if any(p in text for p in END_PATTERNS) else "配布中"


def normalize_official_discount(rule, text, extracted):
    """公式ページ本文から、記事更新に使いやすい割引額だけを残す。"""
    if rule.coupon_id == "jalpack-domestic-birthday":
        amounts = [amount for amount in ("15,000円", "8,000円") if amount in text]
        if amounts:
            return " / ".join(f"{amount}割引" for amount in amounts)
    if rule.coupon_id == "jalpack-overseas-birthday":
        m = re.search(r"10,000円(?:分)?", text)
        if m:
            return m.group(0)
    if rule.coupon_id == "jalpack-domestic-timesale":
        m = re.search(r"最大\s*30,000円(?:割引|OFF|引き)?", text)
        if m:
            value = normalize_space(m.group(0))
            return value if any(s in value for s in ("割引", "OFF", "引き")) else f"{value}割引"
    if rule.coupon_id == "jalpack-overseas-timesale":
        m = re.search(r"最大\s*[0-9,]+円(?:割引|OFF|引き)?", text)
        if m:
            return normalize_space(m.group(0))
    if rule.coupon_id == "jalpack-lsp-star":
        m = re.search(r"5,000円(?:割引|OFF|引き)?", text)
        if m:
            value = normalize_space(m.group(0))
            return value if any(s in value for s in ("割引", "OFF", "引き")) else f"{value}割引"
    if rule.coupon_id == "jalpack-hayakime":
        m = re.search(r"1グループにつき\s*3,000円(?:割引|OFF|引き)?", text)
        if m:
            return normalize_space(m.group(0))
    if rule.coupon_id == "jalpack-jalcard":
        m = re.search(r"2\s*%OFF|2\s*％OFF", text)
        if m:
            return normalize_space(m.group(0))
        return rule.fallback_discount
    return extracted or rule.fallback_discount


def load_master_ids():
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "ids": {}}


def save_master_ids(master):
    master["last_updated"] = now_iso()
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)


def base_coupon(
    *,
    coupon_id,
    category,
    title,
    discount,
    product_type,
    booking_period,
    travel_period,
    coupon_codes,
    conditions,
    source_url,
    source_type,
    fetch_method,
    stock_status,
    confidence,
    placement_hint,
    notes=None,
):
    notes = notes or []
    coupon = {
        "id": coupon_id,
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "site_targets": SITE_TARGETS,
        "article_slug": ARTICLE_SLUG,
        "category": category,
        "title": strip_markdown(title),
        "discount": strip_markdown(discount),
        "area": "",
        "type": product_type,
        "product_type": product_type,
        "booking_period": strip_markdown(booking_period),
        "travel_period": strip_markdown(travel_period),
        "coupon_codes": coupon_codes,
        "conditions": [strip_markdown(c) for c in conditions if strip_markdown(c)],
        "source_url": source_url,
        "source_type": source_type,
        "fetch_method": fetch_method,
        "last_checked": today_str(),
        "confidence": confidence,
        "display_type": "yellow_list",
        "placement_hint": placement_hint,
        "stock_status": stock_status,
        "detail_url": source_url,
        "detail_data": {
            "discount": strip_markdown(discount),
            "conditions": [strip_markdown(c) for c in conditions if strip_markdown(c)],
            "booking_period": strip_markdown(booking_period),
            "stay_period": strip_markdown(travel_period),
            "coupon_codes": coupon_codes,
            "notes": notes,
        },
    }
    return coupon


# ============================================================
# 公式ページ取得
# ============================================================

def fetch_official_html(url, timeout):
    resp = requests.get(url, headers=HEADERS, timeout=(8, timeout))
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_official_html_with_chrome(browser, url, timeout):
    page = browser.new_page(user_agent=HEADERS["User-Agent"])
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        return page.content()
    finally:
        page.close()


def open_chrome_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - optional local fallback
        raise RuntimeError(f"Playwrightを読み込めません: {exc}") from exc

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=True,
        )
    except Exception:
        playwright.stop()
        raise

    return playwright, browser


def parse_official_source(rule, html, fetch_method):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text_norm = normalize_space(text)
    title = rule.title
    discount = normalize_official_discount(
        rule,
        text_norm,
        extract_discount(text_norm, rule.fallback_discount),
    )
    booking_period = extract_period(text_norm, ["予約受付期間", "クーポン獲得 / 予約受付期間", "販売期間", "予約期間"])
    travel_period = extract_period(text_norm, ["出発対象期間", "設定期間", "対象期間", "旅行期間"])
    coupon_codes = extract_codes(text_norm)
    stock_status = status_from_text(text_norm)

    conditions = list(rule.fallback_conditions)
    for label in ["対象商品", "旅行代金条件", "利用人数", "割引併用", "対象のお客さま"]:
        period = extract_period(text_norm, [label])
        if period and period not in conditions:
            conditions.append(f"{label}: {period}")

    notes = []
    if not booking_period and rule.fallback_booking_period:
        booking_period = rule.fallback_booking_period
    if not travel_period and rule.fallback_travel_period:
        travel_period = rule.fallback_travel_period
    if not discount:
        notes.append("割引額を公式HTMLから自動抽出できませんでした")

    return base_coupon(
        coupon_id=rule.coupon_id,
        category=rule.category,
        title=title,
        discount=discount,
        product_type=rule.product_type,
        booking_period=booking_period,
        travel_period=travel_period,
        coupon_codes=coupon_codes,
        conditions=conditions,
        source_url=rule.url,
        source_type="official_html",
        fetch_method=fetch_method,
        stock_status=stock_status,
        confidence="high" if discount or booking_period or coupon_codes else "medium",
        placement_hint=rule.placement_hint,
        notes=notes,
    )


def scrape_official_sources(timeout, fetch_method):
    coupons = []
    failures = []
    chrome = None

    try:
        if fetch_method == "chrome":
            chrome = open_chrome_browser()

        for rule in OFFICIAL_SOURCES:
            print(f"📡 [公式] {rule.title}: {rule.url}")
            try:
                method_used = fetch_method
                if fetch_method == "chrome":
                    html = fetch_official_html_with_chrome(chrome[1], rule.url, timeout)
                else:
                    html = fetch_official_html(rule.url, timeout)
                    method_used = "requests"
                coupon = parse_official_source(rule, html, method_used)
                coupons.append(coupon)
                print(f"  ✅ 取得: {coupon['stock_status']} / {coupon.get('discount') or '割引額未抽出'}")
            except Exception as exc:
                failures.append({
                    "title": rule.title,
                    "url": rule.url,
                    "method": fetch_method,
                    "error": str(exc),
                })
                print(f"  ⚠️ 取得失敗: {exc}")
            time.sleep(1)
    finally:
        if chrome:
            playwright, browser = chrome
            browser.close()
            playwright.stop()

    return coupons, failures


# ============================================================
# coupon-master 代替入力
# ============================================================

def infer_category_from_heading(heading):
    if "カード" in heading:
        return "カード連携"
    if "ポイント" in heading:
        return "ポイントサイト還元"
    if "ギフト" in heading or "制度" in heading:
        return "ギフト・制度"
    if "会員ランク" in heading:
        return "会員ランク"
    if "OTA" in heading:
        return "OTA固有カテゴリ"
    if "終了" in heading:
        return "終了済みアーカイブ"
    return "公式クーポン"


def placement_from_category(category):
    return {
        "カード連携": "カード連携のJALパッククーポン一覧",
        "ポイントサイト還元": "ポイントサイト還元のJALパッククーポン一覧",
        "ギフト・制度": "ギフト・制度のJALパッククーポン一覧",
        "会員ランク": "会員ランクのJALパッククーポン一覧",
        "終了済みアーカイブ": "終了済み・次回確認のキャンペーン",
    }.get(category, "公式クーポンのJALパッククーポン一覧")


def status_from_cell(text):
    text = text or ""
    if any(k in text for k in ["❌", "終了", "停止"]):
        return "配布終了"
    if "⚠️" in text or "要確認" in text or "不明" in text:
        return "要確認"
    return "配布中"


def choose_first(row, headers, names, default=""):
    for name in names:
        if name in headers:
            return row.get(name, default)
    return default


def parse_master_tables(master_path):
    if not master_path.exists():
        raise FileNotFoundError(f"coupon-masterが見つかりません: {master_path}")

    text = master_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    coupons = []
    current_heading = ""
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            current_heading = line.lstrip("#").strip()
        if line.startswith("|") and i + 1 < len(lines) and is_separator_row(lines[i + 1]):
            headers = split_markdown_row(line)
            table_rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                cells = split_markdown_row(lines[i])
                if len(cells) == len(headers):
                    table_rows.append(dict(zip(headers, cells)))
                i += 1

            if not headers:
                continue
            primary_keys = {"正式名称", "カード名", "ポイントサイト", "種類", "ランク名/プログラム", "セール名/カテゴリ"}
            if not primary_keys.intersection(headers):
                continue

            category = infer_category_from_heading(current_heading)
            for row in table_rows:
                raw_title = choose_first(
                    row,
                    headers,
                    ["正式名称", "カード名", "ポイントサイト", "種類", "ランク名/プログラム", "セール名/カテゴリ"],
                )
                title = strip_markdown(raw_title)
                if not title or title in {"販売枠"}:
                    continue

                raw_all = " ".join(row.values())
                source_urls = extract_urls(raw_all)
                source_url = source_urls[0] if source_urls else ""
                discount = choose_first(
                    row,
                    headers,
                    ["割引額", "内容", "現行還元率（取得日）", "特典内容", "もらえるもの"],
                )
                discount = extract_discount(discount, strip_markdown(discount))
                product_type = choose_first(row, headers, ["対象商品", "対象", "成果対象"], "")
                booking_period = choose_first(row, headers, ["利用可能期間", "開催時期/条件"], "")
                if not booking_period:
                    booking_period = extract_period(raw_all, ["予約期限", "販売", "開催時期"])
                travel_period = extract_period(raw_all, ["出発", "旅行期間", "宿泊期間"])
                status = status_from_cell(choose_first(row, headers, ["ステータス", "開催ステータス"], raw_all))
                coupon_codes = extract_codes(raw_all)
                conditions = []
                for key in ["取得条件", "条件/金額目安", "承認ルール", "仕組み", "開催時期/条件", "有効期限/注意", "併用可否"]:
                    if key in headers and row.get(key):
                        conditions.append(f"{key}: {row[key]}")

                # 同じ公式URL内に複数クーポンが並ぶため、URLだけでなく
                # クーポン名もID材料に含める。
                coupon_id = f"jalpack-{slugify(title + source_url)}"
                coupons.append(
                    base_coupon(
                        coupon_id=coupon_id,
                        category=category,
                        title=title,
                        discount=discount,
                        product_type=strip_markdown(product_type),
                        booking_period=booking_period,
                        travel_period=travel_period,
                        coupon_codes=coupon_codes,
                        conditions=conditions,
                        source_url=source_url,
                        source_type="local_coupon_master",
                        fetch_method="markdown_master",
                        stock_status=status,
                        confidence="medium" if status == "配布中" else "low",
                        placement_hint=placement_from_category(category),
                        notes=[f"coupon-masterから変換: {master_path}"],
                    )
                )
            continue
        i += 1

    # 同一タイトル/URLの重複を抑える
    deduped = []
    seen = set()
    for c in coupons:
        key = (c["title"], c.get("source_url", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    print(f"📄 coupon-masterから {len(deduped)}件を変換")
    return deduped


# ============================================================
# 差分・保存・レポート
# ============================================================

def update_master_ids(master_ids, current_coupons):
    master_ids["ids"] = {
        c["id"]: {
            "category": c["category"],
            "title": c["title"],
            "discount": c.get("discount", ""),
            "stock_status": c.get("stock_status", "不明"),
            "source_url": c.get("source_url", ""),
        }
        for c in current_coupons
    }
    return master_ids


def detect_changes(master_ids, current_coupons):
    prev = master_ids.get("ids", {})
    curr = {c["id"]: c for c in current_coupons}
    events = []

    for cid in sorted(set(curr) - set(prev)):
        c = curr[cid]
        events.append({
            "date": today_str(),
            "type": "新規",
            "id": cid,
            "category": c["category"],
            "title": c["title"],
            "discount": c.get("discount", ""),
        })

    for cid in sorted(set(prev) - set(curr)):
        p = prev[cid]
        events.append({
            "date": today_str(),
            "type": "消失",
            "id": cid,
            "category": p.get("category", ""),
            "title": p.get("title", ""),
            "discount": p.get("discount", ""),
        })

    for cid in sorted(set(prev) & set(curr)):
        old_status = prev[cid].get("stock_status", "")
        new_status = curr[cid].get("stock_status", "")
        if old_status != new_status:
            events.append({
                "date": today_str(),
                "type": f"ステータス変更 {old_status}→{new_status}",
                "id": cid,
                "category": curr[cid]["category"],
                "title": curr[cid]["title"],
                "discount": curr[cid].get("discount", ""),
            })

    return events


def save_daily_data(coupons, dry_run=False):
    today = today_str()
    if dry_run:
        stamp = datetime.now(JST).strftime("%Y-%m-%d_%H%M%S")
        daily_file = DATA_DIR / f"dry_run_coupons_{stamp}.json"
    else:
        daily_file = DATA_DIR / f"coupons_{today}.json"

    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(coupons, f, ensure_ascii=False, indent=2)

    label = "DRY-RUNデータ" if dry_run else "日次データ"
    print(f"💾 {label}保存: {daily_file}（{len(coupons)}件）")
    return daily_file


def save_change_log(events):
    log_file = DATA_DIR / "change_log.json"
    existing = []
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing.extend(events)
    cutoff = (datetime.now(JST) - timedelta(days=90)).strftime("%Y-%m-%d")
    existing = [e for e in existing if e.get("date", "") >= cutoff]

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def cleanup_old_files():
    cutoff = (datetime.now(JST) - timedelta(days=DATA_RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for pattern in ["coupons_*.json", "report_*.md", "dry_run_coupons_*.json"]:
        for f in DATA_DIR.glob(pattern):
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if date_match and date_match.group(1) < cutoff:
                f.unlink()
                removed += 1
    if removed:
        print(f"🧹 古いファイル {removed}件を削除（{DATA_RETENTION_DAYS}日超過分）")


def generate_report(coupons, events, fetch_failures, source_mode, dry_run=False):
    today = today_str()
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    ended = [c for c in coupons if c.get("stock_status") == "配布終了"]
    check = [c for c in coupons if c.get("stock_status") == "要確認"]
    official = [c for c in coupons if c.get("source_type") == "official_html"]
    master = [c for c in coupons if c.get("source_type") == "local_coupon_master"]

    lines = [
        f"# JALパッククーポン監視レポート {today}",
        "",
        "## 概要",
        f"- 実行モード: {'DRY-RUN' if dry_run else '通常'}",
        f"- 取得モード: {source_mode}",
        f"- 合計: {len(coupons)}件",
        f"- 配布中: {len(active)}件 / 配布終了: {len(ended)}件 / 要確認: {len(check)}件",
        f"- 公式HTML由来: {len(official)}件 / coupon-master由来: {len(master)}件",
        "",
    ]

    if fetch_failures:
        lines.extend(["## 公式ページ取得失敗"])
        for f in fetch_failures:
            lines.append(f"- {f['title']}: {f['url']} / {f['error']}")
        lines.append("")

    if events:
        lines.append("## 変動")
        for e in events:
            lines.append(f"- {e['type']} [{e['category']}] {e['title']} ({e['id']})")
        lines.append("")
    else:
        lines.extend(["## 変動: なし", ""])

    lines.append("## 取得クーポン")
    for c in coupons:
        lines.append(
            f"- [{c['stock_status']}] {c['category']} / {c['title']} / "
            f"{c.get('discount') or '割引額未抽出'} / {c.get('source_type')}"
        )

    report_file = DATA_DIR / (
        f"dry_run_report_{datetime.now(JST).strftime('%Y-%m-%d_%H%M%S')}.md"
        if dry_run
        else f"report_{today}.md"
    )
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"📝 レポート保存: {report_file}")
    print("\n" + "=" * 60)
    for line in lines[:40]:
        print(line)
    if len(lines) > 40:
        print(f"...（残り{len(lines) - 40}行）")
    print("=" * 60)
    return report_file


# ============================================================
# 実行
# ============================================================

def collect_coupons(source_mode, timeout, fetch_method):
    fetch_failures = []
    official_coupons = []

    if source_mode in {"official", "auto"}:
        official_coupons, fetch_failures = scrape_official_sources(timeout, fetch_method)
        if official_coupons:
            return official_coupons, fetch_failures, "official"
        if source_mode == "official":
            return [], fetch_failures, "official"
        print("⚠️ 公式ページから取得できなかったため、coupon-masterにフォールバックします")

    master_coupons = parse_master_tables(LOCAL_COUPON_MASTER)
    return master_coupons, fetch_failures, "master"


def run(args):
    setup_dirs()

    coupons, fetch_failures, effective_source = collect_coupons(
        args.source,
        args.timeout,
        args.fetch_method,
    )
    if not coupons:
        print("🚨 異常検知: JALパックのクーポン候補が0件です")
        sys.exit(1)

    master_ids = load_master_ids()
    coupons, validation_report = validate_coupons(
        coupons,
        master_ids=None if args.init else master_ids,
        service_name="JALPACK",
    )

    data_file = save_daily_data(coupons, dry_run=args.dry_run)
    events = [] if args.init else detect_changes(master_ids, coupons)

    if events:
        print(f"\n📢 変動検出: {len(events)}件")
        for e in events:
            print(f"  {e['type']} [{e['category']}] {e['title']}")
    else:
        print("\n📢 変動なし")

    report_file = generate_report(
        coupons,
        events,
        fetch_failures,
        source_mode=effective_source,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        master_ids = update_master_ids(master_ids, coupons)
        save_master_ids(master_ids)
        if events:
            save_change_log(events)
        cleanup_old_files()
    else:
        print("🧪 DRY-RUNのため master_ids / change_log は更新していません")

    return {
        "coupons": coupons,
        "validation": validation_report,
        "data_file": str(data_file),
        "report_file": str(report_file),
        "fetch_failures": fetch_failures,
        "source": effective_source,
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description="JALパック クーポン監視パイロット")
    parser.add_argument("--init", action="store_true", help="初回セットアップとして実行")
    parser.add_argument("--dry-run", action="store_true", help="日次master/change_logを更新しない")
    parser.add_argument(
        "--source",
        choices=["auto", "official", "master"],
        default="auto",
        help="取得元。autoは公式取得に失敗したらcoupon-masterへフォールバック",
    )
    parser.add_argument(
        "--fetch-method",
        choices=["requests", "chrome"],
        default="requests",
        help="公式ページの取得方法。chromeはローカルGoogle ChromeをPlaywright経由で使う",
    )
    parser.add_argument("--timeout", type=int, default=20, help="公式ページ取得の読み取りタイムアウト秒")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    run(args)


if __name__ == "__main__":
    main()
