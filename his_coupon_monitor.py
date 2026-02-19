#!/usr/bin/env python3
"""
HIS クーポン監視スクリプト（Playwright版）
================================================================
HISの割引クーポンページ（施策ページ）からクーポン情報を収集。
ページはアコーディオンUIのため、Playwrightで全展開後にHTMLを解析する。

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
import sys
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
import re

# ============================================================
# 設定
# ============================================================
PAGE_URL = "https://www.his-j.com/campaign/shisaku/"

DATA_DIR = Path("./his_coupon_data")
MASTER_FILE = DATA_DIR / "master_ids.json"

# データ保持日数
DATA_RETENTION_DAYS = 30


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def make_coupon_id(title, category):
    """タイトル+カテゴリからユニークなIDを生成"""
    raw = f"{category}_{title}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ============================================================
# マスターID管理
# ============================================================
def load_master_ids():
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "ids": {}}


def save_master_ids(master):
    master["last_updated"] = datetime.now().isoformat()
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)


# ============================================================
# Playwright でページ取得 & アコーディオン展開
# ============================================================
def fetch_page_html():
    """Playwright で HIS クーポンページを取得し、アコーディオンを全展開してHTMLを返す"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("⚠️ Playwright未インストール。pip install playwright && playwright install chromium")
        sys.exit(1)

    print(f"🎭 Playwright でページ取得中... {PAGE_URL}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--headless=new",
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
        if resp.status != 200:
            print(f"⚠️ HTTP {resp.status} - アクセスがブロックされた可能性")
            browser.close()
            return None

        page.wait_for_timeout(3000)

        # アコーディオンをすべて展開
        page.evaluate(
            'document.querySelectorAll(".accordion__button").forEach(btn => btn.click());'
        )
        page.wait_for_timeout(1500)

        html = page.content()
        browser.close()

    print(f"  ✅ HTML取得完了（{len(html):,}文字）")
    return html


# ============================================================
# HTML解析 → クーポンデータ抽出
# ============================================================
def parse_coupons(html):
    """HTMLからクーポン情報を抽出"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    wrappers = soup.find_all(class_="content__wrapper")
    print(f"📦 クーポンカード検出: {len(wrappers)}件")

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
        m = re.search(r"(?:最大)?([0-9,]+)円割引", title)
        if m:
            discount = m.group(0)
        else:
            m2 = re.search(r"(\d+)[％%](?:OFF|割引)", title)
            if m2:
                discount = f"{m2.group(1)}%OFF"

        # --- クーポンコード & 条件 ---
        coupon_codes = []
        for li in w.select(".coupon__list li"):
            code_el = li.find(class_="coupon__code")
            code = code_el.get("data-name", "") if code_el else ""

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
        end_date = _extract_booking_end_date(booking_period)
        if end_date and end_date < today_str():
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
        coupons.append(coupon)

    return coupons


def _extract_booking_end_date(period_str):
    """予約期間文字列から終了日をYYYY-MM-DD形式で返す"""
    if not period_str:
        return None

    # 「～」の後を取得
    parts = re.split(r"[～〜~]", period_str)
    if len(parts) < 2:
        return None

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
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    existing = [e for e in existing if e.get("date", "") >= cutoff]

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def cleanup_old_files():
    """DATA_RETENTION_DAYS より古い日次ファイルとレポートを削除"""
    cutoff = (datetime.now() - timedelta(days=DATA_RETENTION_DAYS)).strftime("%Y-%m-%d")
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

    html = fetch_page_html()
    if not html:
        print("🚨 ページ取得失敗")
        sys.exit(1)

    coupons = parse_coupons(html)

    if not coupons:
        print("🚨 異常検知: クーポンが0件です。サイト構造が変更された可能性があります。")
        sys.exit(1)

    save_daily_data(coupons)

    master_ids = update_master_ids({"last_updated": "", "ids": {}}, coupons)
    save_master_ids(master_ids)

    generate_report(coupons, [])
    print(f"\n✅ HIS 初期化完了: {len(coupons)}件")


def run_full():
    setup_dirs()

    html = fetch_page_html()
    if not html:
        print("🚨 ページ取得失敗")
        sys.exit(1)

    coupons = parse_coupons(html)

    if not coupons:
        print("🚨 異常検知: クーポンが0件です。サイト構造が変更された可能性があります。")
        sys.exit(1)

    save_daily_data(coupons)

    master_ids = load_master_ids()
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        run_init()
    else:
        run_full()


if __name__ == "__main__":
    main()
