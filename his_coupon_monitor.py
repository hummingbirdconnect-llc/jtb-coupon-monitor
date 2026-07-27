#!/usr/bin/env python3
"""
HIS クーポン監視スクリプト（Playwright版）
================================================================
HISの割引クーポンページ（施策ページ）からクーポン情報を収集。
Playwrightで旅行タブを順に開き、公式画面で実際に表示されているカードだけを
アコーディオン展開して解析し、カード単位の証拠画像も保存する。

HTML構造（2026-02時点）:
  <div class="content__wrapper is-ovs|is-dom|is-gakusei|...">
    <p class="plan__dst">海外旅行</p>
    <div class="plan__dspbox">
      <h2 class="plan__title">...</h2>
      <ul class="term__list"><li>予約期間：...</li><li>出発期間：...</li></ul>
    </div>
    <div class="plan__details">
      <div class="coupon__box">
        <ul class="coupon__list">
          <li>
            <p class="coupon__condition">旅行代金総額10万円以上</p>
            <p class="coupon__price">1グループ5,000円割引</p>
            <p class="coupon__code" data-name="CODE123">CODE123</p>
          </li>
        </ul>
      </div>
    </div>
  </div>

使い方:
  python his_coupon_monitor.py           # 通常実行
  python his_coupon_monitor.py --init    # 初回セットアップ
"""

import json
import os
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from coupon_validator import validate_coupons

# ============================================================
# 設定
# ============================================================
PAGE_URL = "https://www.his-j.com/campaign/shisaku/"

DATA_DIR = Path("./his_coupon_data")
MASTER_FILE = DATA_DIR / "master_ids.json"
VISUAL_SUMMARY_FILE = DATA_DIR / "visual_observation_latest.json"
EVIDENCE_DIR = Path("./dashboard/evidence/his")
EVIDENCE_URL_PREFIX = "evidence/his"

TAB_CONFIG = (
    ("海外旅行", "li[data-tab='tab1']"),
    ("国内旅行", "li[data-tab='tab2']"),
)

# データ保持日数
DATA_RETENTION_DAYS = 30

JST = timezone(timedelta(hours=9))

EXPLICIT_END_MARKERS = (
    "終了しました",
    "終了いたしました",
    "受付終了",
    "配布終了",
)

THANKS_END_MARKERS = (
    "ご予約ありがとうございました",
)


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def today_str():
    return datetime.now(JST).strftime("%Y-%m-%d")


def make_coupon_id(title, category):
    """タイトル+カテゴリからユニークなIDを生成"""
    raw = f"{category}_{title}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _normalize_campaign_text(text):
    """キャンペーン名照合用に表記ゆれを小さくする"""
    text = re.sub(r"[【】〖〗\[\]（）()「」『』]", "", text or "")
    return re.sub(r"\s+", "", text)


def _extract_campaign_from_end_text(text):
    """「○○は終了しました」から○○部分を取り出す"""
    compact = _normalize_campaign_text(text)
    if not compact:
        return None

    for marker in EXPLICIT_END_MARKERS:
        if marker not in compact:
            continue
        campaign = compact.split(marker, 1)[0]
        campaign = re.sub(r"(?:は|が|の)?$", "", campaign)
        campaign = campaign.strip("。:：・-ー")
        # 「配布終了クーポンについて」のような一般説明をキャンペーン名にしない
        if len(campaign) >= 6 and any(k in campaign for k in ("クーポン", "キャンペーン", "セール")):
            return campaign

    return None


def _looks_like_campaign_name(text):
    """終了告知の対象になり得るキャンペーン名かをゆるく判定"""
    compact = _normalize_campaign_text(text).strip("。:：・-ー")
    return len(compact) >= 6 and any(k in compact for k in ("クーポン", "キャンペーン", "セール"))


def _next_text_has_thanks_end_marker(node, max_steps=3):
    """直後の要素に「ご予約ありがとうございました」系の終了補助文があるか確認"""
    sibling = node
    for _ in range(max_steps):
        sibling = sibling.find_next_sibling()
        if sibling is None:
            return False
        text = sibling.get_text(" ", strip=True)
        if any(marker in text for marker in THANKS_END_MARKERS):
            return True
    return False


def _extract_explicit_ended_campaigns(soup):
    """公式ページ上で明示的に終了告知されているキャンペーン名を抽出"""
    ended_campaigns = set()
    for node in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = node.get_text(" ", strip=True)
        if not text:
            continue

        campaign = _extract_campaign_from_end_text(text)
        if campaign:
            ended_campaigns.add(campaign)
            continue

        if _looks_like_campaign_name(text) and _next_text_has_thanks_end_marker(node):
            ended_campaigns.add(_normalize_campaign_text(text).strip("。:：・-ー"))

    return ended_campaigns


def _matched_ended_campaign(title, ended_campaigns):
    """クーポンタイトルが終了告知済みキャンペーンに属するか判定"""
    normalized_title = _normalize_campaign_text(title)
    for campaign in ended_campaigns:
        normalized_campaign = _normalize_campaign_text(campaign)
        if normalized_campaign and normalized_campaign in normalized_title:
            return campaign
    return None


# ============================================================
# マスターID管理
# ============================================================
def load_master_ids():
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "ids": {}}


def save_master_ids(master):
    master["last_updated"] = datetime.now(JST).isoformat()
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)


# ============================================================
# Playwright で表示中カードを取得 & アコーディオン展開
# ============================================================
def _current_area_label(page):
    """公式ページの「現在のエリア」表示を返す。"""
    button = page.get_by_role("button", name=re.compile(r"現在のエリア")).first
    if button.count() == 0:
        return "未確認"
    text = button.inner_text().strip()
    area = re.sub(r"^現在のエリア[：:]?\s*", "", text).strip()
    return area or "未確認"


def _prune_stale_evidence(active_ids):
    """最新画面に存在しないHIS証拠画像を削除し、容量増加を防ぐ。"""
    active_names = {f"{coupon_id}.png" for coupon_id in active_ids}
    for path in EVIDENCE_DIR.glob("*.png"):
        if path.name not in active_names:
            path.unlink()


def fetch_visible_page_snapshot():
    """公式画面で実際に表示中のカードだけを展開・撮影して返す。

    HTML内に存在してもCSSやJavaScriptで非表示の地域限定・時間限定・
    切替済みカードは含めない。
    """
    try:
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup
    except ImportError:
        print("⚠️ Playwright未インストール。pip install playwright && playwright install chromium")
        sys.exit(1)

    headless = os.environ.get("HIS_BROWSER_HEADLESS", "false").lower() == "true"
    browser_mode = "headless" if headless else "headed"
    print(f"🎭 Playwright で公式表示を確認中... {PAGE_URL} ({browser_mode})")
    checked_at = datetime.now(JST).isoformat(timespec="seconds")
    observations = {}
    visible_card_html = []
    tab_counts = {}
    missing_tabs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            # HISはheadless Chromiumへ403を返すため、ActionsではXvfb上の
            # 通常ブラウザを使う。HIS_BROWSER_HEADLESS=true で明示変更可能。
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        page = context.new_page()
        page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", { get: () => false });'
        )

        resp = page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        if resp is None or resp.status != 200:
            status = resp.status if resp else "応答なし"
            print(f"⚠️ HTTP {status} - アクセスがブロックされた可能性")
            browser.close()
            return None

        page.wait_for_timeout(3000)

        area = _current_area_label(page)
        total_dom_count = page.locator(".content__wrapper").count()

        for tab_label, tab_selector in TAB_CONFIG:
            tab = page.locator(tab_selector).first
            if tab.count() == 0:
                tab_counts[tab_label] = 0
                missing_tabs.append(tab_label)
                continue

            tab.click()
            page.wait_for_timeout(300)
            cards = page.locator(".content__wrapper:visible")
            tab_counts[tab_label] = cards.count()

            for index in range(cards.count()):
                card = cards.nth(index)
                accordion = card.locator(".accordion__button").first
                if accordion.count():
                    button_text = accordion.inner_text().strip()
                    if "詳細" in button_text:
                        accordion.click()
                        page.wait_for_timeout(80)

                # BeautifulSoup側と同じ文字列化でIDを作り、改行・span境界による
                # Playwright inner_textとの差をなくす。
                outer_html = card.evaluate("(element) => element.outerHTML")
                card_soup = BeautifulSoup(outer_html, "html.parser")
                title_node = card_soup.select_one(".plan__title")
                category_node = card_soup.select_one(".plan__dst")
                title = title_node.get_text(strip=True) if title_node else ""
                category = category_node.get_text(strip=True) if category_node else ""
                if not title:
                    continue

                coupon_id = make_coupon_id(title, category)
                screenshot_name = f"{coupon_id}.png"
                screenshot_path = EVIDENCE_DIR / screenshot_name
                card.screenshot(
                    path=str(screenshot_path),
                    animations="disabled",
                )

                if coupon_id not in observations:
                    visible_card_html.append(outer_html)
                    observations[coupon_id] = {
                        "official_visibility": "visible",
                        "official_tab": tab_label,
                        "official_area": area,
                        "official_checked_at": checked_at,
                        "screenshot_url": f"{EVIDENCE_URL_PREFIX}/{screenshot_name}",
                    }

        browser.close()

    if missing_tabs:
        print("🚨 旅行タブを確認できませんでした: " + " / ".join(missing_tabs))
        return None
    if not observations:
        print("🚨 公式画面で表示中のクーポンカードを取得できませんでした。")
        return None

    html = "<html><body>" + "\n".join(visible_card_html) + "</body></html>"
    snapshot = {
        "html": html,
        "observations": observations,
        "checked_at": checked_at,
        "area": area,
        "tab_counts": tab_counts,
        "total_dom_count": total_dom_count,
        "visible_count": len(observations),
        "hidden_dom_count": max(total_dom_count - len(observations), 0),
    }
    print(
        "  ✅ 公式表示取得完了"
        f"（表示中={snapshot['visible_count']}件 / HTML内非表示={snapshot['hidden_dom_count']}件"
        f" / エリア={area}）"
    )
    return snapshot


def enrich_visible_coupons(coupons, snapshot):
    """表示観測メタデータと公式証拠画像をクーポン単位で付与する。"""
    observations = snapshot.get("observations") or {}
    enriched = []
    missing_ids = []

    for coupon in coupons:
        observation = observations.get(coupon["id"])
        if not observation:
            missing_ids.append(coupon["id"])
            continue
        enriched_coupon = dict(coupon)
        enriched_coupon.update(observation)
        enriched_coupon.update({
            "source_url": PAGE_URL,
            "source_type": "official_visible_dom",
            "fetch_method": "playwright_visible_dom+screenshot",
            "confidence": "公式画面表示確認済み",
        })
        enriched.append(enriched_coupon)

    if missing_ids:
        raise ValueError("表示観測と解析結果が一致しません: " + ", ".join(missing_ids))
    if len(enriched) != len(observations):
        raise ValueError(
            f"表示観測件数({len(observations)})と解析件数({len(enriched)})が一致しません"
        )
    return enriched


def save_visual_summary(snapshot):
    """ダッシュボードの会社別概要に使う最新の画面観測結果を保存する。"""
    summary = {
        "source_url": PAGE_URL,
        "official_checked_at": snapshot["checked_at"],
        "official_area": snapshot["area"],
        "tab_counts": snapshot["tab_counts"],
        "visible_count": snapshot["visible_count"],
        "total_dom_count": snapshot["total_dom_count"],
        "hidden_dom_count": snapshot["hidden_dom_count"],
        "evidence_directory": EVIDENCE_URL_PREFIX,
    }
    with open(VISUAL_SUMMARY_FILE, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


# ============================================================
# HTML解析 → クーポンデータ抽出
# ============================================================
def parse_coupons(html):
    """HTMLからクーポン情報を抽出"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    wrappers = soup.find_all(class_="content__wrapper")
    print(f"📦 クーポンカード検出: {len(wrappers)}件")

    ended_campaigns = _extract_explicit_ended_campaigns(soup)
    if ended_campaigns:
        print("🛑 終了キャンペーン検出: " + " / ".join(sorted(ended_campaigns)))

    coupons = []

    for w in wrappers:
        # --- カテゴリ ---
        dst = w.find(class_="plan__dst")
        category = dst.get_text(strip=True) if dst else ""

        # --- タイトル ---
        h2 = w.find("h2", class_="plan__title")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            continue

        # --- ID ---
        coupon_id = make_coupon_id(title, category)

        # --- 期間情報 ---
        booking_period = ""
        travel_period = ""
        for li in w.select(".term__list li"):
            text = li.get_text(strip=True)
            if "予約期間" in text:
                booking_period = re.sub(r"^.*?予約期間[：:]?\s*", "", text).strip()
            elif any(k in text for k in ["出発", "宿泊", "滞在"]):
                booking_label_match = re.match(r"^(.+?)[：:]", text)
                travel_period = text.split("：", 1)[-1].strip() if "：" in text else text

        # --- 割引額（タイトルから） ---
        discount = ""
        m = re.search(r"(?:最大)?[0-9,]+円(?:割引|引き)", title)
        if m:
            discount = m.group(0)
        else:
            m2 = re.search(r"(?:最大)?\d+[％%](?:OFF|割引|引き)?", title)
            if m2:
                discount = m2.group(0).replace("％", "%")

        # --- クーポンコード & 条件 ---
        coupon_codes = []
        for li in w.select(".coupon__list li"):
            code_el = li.find(class_="coupon__code")
            code = ""
            if code_el:
                code = code_el.get("data-name", "") or code_el.get_text(strip=True)

            cond_el = li.find(class_="coupon__condition")
            condition = cond_el.get_text(strip=True) if cond_el else ""

            price_el = li.find(class_="coupon__price")
            price = price_el.get_text(strip=True) if price_el else ""

            if code:
                coupon_codes.append({
                    "code": code,
                    "condition": condition,
                    "discount": price,
                })

        # --- 対象商品 ---
        target = ""
        for items_div in w.select(".plan__items"):
            midashi = items_div.find(class_="detail__midashi")
            if midashi and "対象商品" in midashi.get_text():
                target = items_div.get_text(separator=" ", strip=True)
                target = re.sub(r"^対象商品\s*", "", target).strip()[:200]
                break

        # --- 注意事項 ---
        notes = []
        for items_div in w.select(".plan__items"):
            midashi = items_div.find(class_="detail__midashi")
            if midashi and "注意事項" in midashi.get_text():
                for li in items_div.select(".notice li"):
                    t = li.get_text(strip=True)[:120]
                    if t:
                        notes.append(t)
                break

        # --- 配布状況（予約期間終了チェック） ---
        stock_status = "配布中"
        ended_reason = ""
        ended_campaign = _matched_ended_campaign(title, ended_campaigns)
        end_date = _extract_booking_end_date(booking_period)
        if ended_campaign:
            stock_status = "配布終了"
            ended_reason = f"公式ページの終了告知: {ended_campaign}"
        elif end_date and end_date <= today_str():
            stock_status = "配布終了"

        coupon = {
            "id": coupon_id,
            "category": category,
            "title": title,
            "discount": discount,
            "stock_status": stock_status,
            "booking_period": booking_period,
            "travel_period": travel_period,
            "coupon_codes": coupon_codes,
            "target": target,
            "notes": notes,
        }
        if ended_reason:
            coupon["ended_reason"] = ended_reason
        coupons.append(coupon)

    return coupons


def _extract_booking_end_date(period_str):
    """予約期間文字列から終了日をYYYY-MM-DD形式で返す。
    年なし終了日にも対応（開始日の年から推定）。
    """
    if not period_str:
        return None

    # 「～」の後を取得
    parts = re.split(r"[～〜~]", period_str)
    if len(parts) < 2:
        return None

    start_part = parts[0].strip()
    end_part = parts[-1].strip()

    # "2026年3月6日(金)9:00" or "2026年3月31日(火)"
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", end_part)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    # "2026/3/6" 形式
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", end_part)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    # --- 年なし終了日: 開始日から年を推定 ---
    end_m = re.search(r"(\d{1,2})[月/](\d{1,2})", end_part)
    if end_m:
        start_y = re.search(r"(\d{4})[年/](\d{1,2})[月/]", start_part)
        if start_y:
            start_year = int(start_y.group(1))
            start_month = int(start_y.group(2))
            end_month = int(end_m.group(1))
            end_day = int(end_m.group(2))
            inferred_year = start_year + 1 if end_month < start_month else start_year
            try:
                return f"{inferred_year:04d}-{end_month:02d}-{end_day:02d}"
            except ValueError:
                return None

    return None


# ============================================================
# 差分検出（新規・消失）
# ============================================================
def detect_changes(master_ids, current_coupons):
    prev_ids = set(master_ids.get("ids", {}).keys())
    curr_map = {c["id"]: c for c in current_coupons}
    curr_id_set = set(curr_map.keys())

    new_ids = curr_id_set - prev_ids
    gone_ids = prev_ids - curr_id_set

    events = []

    for cid in sorted(new_ids):
        c = curr_map[cid]
        events.append({
            "date": today_str(),
            "type": "🆕 新規",
            "id": cid,
            "category": c["category"],
            "title": c["title"],
            "discount": c.get("discount", ""),
        })

    for cid in sorted(gone_ids):
        prev_info = master_ids["ids"].get(cid, {})
        events.append({
            "date": today_str(),
            "type": "❌ 消失",
            "id": cid,
            "category": prev_info.get("category", ""),
            "title": prev_info.get("title", ""),
            "discount": prev_info.get("discount", ""),
        })

    return events


def update_master_ids(master_ids, current_coupons):
    new_ids = {}
    for c in current_coupons:
        new_ids[c["id"]] = {
            "category": c["category"],
            "title": c["title"],
            "discount": c.get("discount", ""),
        }
    master_ids["ids"] = new_ids
    return master_ids


# ============================================================
# データ保存
# ============================================================
def save_daily_data(coupons):
    today = today_str()
    daily_file = DATA_DIR / f"coupons_{today}.json"
    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(coupons, f, ensure_ascii=False, indent=2)
    print(f"💾 日次データ保存: {daily_file}（{len(coupons)}件）")


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
    """DATA_RETENTION_DAYS より古い日次ファイルとレポートを削除"""
    cutoff = (datetime.now(JST) - timedelta(days=DATA_RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0

    for pattern in ["coupons_*.json", "report_*.md"]:
        for f in DATA_DIR.glob(pattern):
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if date_match and date_match.group(1) < cutoff:
                f.unlink()
                removed += 1

    if removed:
        print(f"🧹 古いファイル {removed}件を削除（{DATA_RETENTION_DAYS}日超過分）")


# ============================================================
# レポート
# ============================================================
def generate_report(coupons, events):
    today = today_str()
    overseas = [c for c in coupons if "海外" in c["category"]]
    domestic = [c for c in coupons if "国内" in c["category"]]
    other = [c for c in coupons if "海外" not in c["category"] and "国内" not in c["category"]]
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    ended = [c for c in coupons if c.get("stock_status") == "配布終了"]

    lines = [
        f"# HISクーポンレポート {today}",
        "",
        "## 概要",
        f"- 合計: {len(coupons)}件（配布中={len(active)}, 配布終了={len(ended)}）",
        f"- 海外旅行: {len(overseas)}件",
        f"- 国内旅行: {len(domestic)}件",
        f"- その他: {len(other)}件",
        "",
    ]

    if events:
        lines.append("## 変動")
        for e in events:
            lines.append(f"- {e['type']} [{e['category']}] {e['title']} ({e['id']})")
        lines.append("")
    else:
        lines.append("## 変動: なし")
        lines.append("")

    report_text = "\n".join(lines)
    report_file = DATA_DIR / f"report_{today}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"📝 レポート保存: {report_file}")

    print("\n" + "=" * 60)
    for line in lines:
        print(line)
    print("=" * 60)


# ============================================================
# メイン
# ============================================================
def run_init():
    print("🔄 HIS 初期化モード")
    setup_dirs()

    snapshot = fetch_visible_page_snapshot()
    if not snapshot:
        print("🚨 ページ取得失敗")
        sys.exit(1)

    coupons = parse_coupons(snapshot["html"])

    if not coupons:
        print("🚨 異常検知: クーポンが0件です。サイト構造が変更された可能性があります。")
        sys.exit(1)

    try:
        coupons = enrich_visible_coupons(coupons, snapshot)
    except ValueError as exc:
        print(f"🚨 公式画面の観測結果と抽出結果が不一致です: {exc}")
        sys.exit(1)

    # バリデーション（ダブルチェック）
    coupons, validation_report = validate_coupons(coupons, service_name="HIS")

    save_daily_data(coupons)
    save_visual_summary(snapshot)
    _prune_stale_evidence({coupon["id"] for coupon in coupons})

    master_ids = update_master_ids({"last_updated": "", "ids": {}}, coupons)
    save_master_ids(master_ids)

    generate_report(coupons, [])
    print(f"\n✅ HIS 初期化完了: {len(coupons)}件")

    return validation_report


def run_full():
    setup_dirs()

    snapshot = fetch_visible_page_snapshot()
    if not snapshot:
        print("🚨 ページ取得失敗")
        sys.exit(1)

    coupons = parse_coupons(snapshot["html"])

    if not coupons:
        print("🚨 異常検知: クーポンが0件です。サイト構造が変更された可能性があります。")
        sys.exit(1)

    try:
        coupons = enrich_visible_coupons(coupons, snapshot)
    except ValueError as exc:
        print(f"🚨 公式画面の観測結果と抽出結果が不一致です: {exc}")
        sys.exit(1)

    # バリデーション（ダブルチェック）
    master_ids = load_master_ids()
    coupons, validation_report = validate_coupons(
        coupons, master_ids=master_ids, service_name="HIS"
    )

    save_daily_data(coupons)
    save_visual_summary(snapshot)
    _prune_stale_evidence({coupon["id"] for coupon in coupons})

    events = detect_changes(master_ids, coupons)

    if events:
        print(f"\n📢 変動検出: {len(events)}件")
        for e in events:
            print(f"  {e['type']} [{e['category']}] {e['title']}")
    else:
        print("\n📢 変動なし")

    master_ids = update_master_ids(master_ids, coupons)
    save_master_ids(master_ids)

    if events:
        save_change_log(events)

    generate_report(coupons, events)

    cleanup_old_files()

    return validation_report


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        run_init()
    else:
        run_full()


if __name__ == "__main__":
    main()
