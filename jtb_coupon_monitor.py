#!/usr/bin/env python3
"""
JTB クーポン監視スクリプト（国内＋海外 / Stock API + Playwright対応版）
================================================================
一覧ページからクーポンを収集し、配布終了を判定。
- 国内: Stock API（StockFlag）で判定
- 海外: Playwright でJS描画後のDOM（.c-close__txt）から判定
前回との差分（新規・消失）も検出する。

HTML構造（2026-02時点）:
  国内:
    <div class="c-coupon__item" data-id="XXX" data-category='["宿泊"]' data-pref='["全国"]'>
      ...
    </div>
  海外（JS描画後）:
    <div class="c-coupon__item" data-category='["海外ツアー"]'>
      ...
      <div class="c-close__txt"><br>配布終了いたしました</div>  ← 配布終了時のみ出現
    </div>

Stock API（国内のみ）: /myjtb/campaign/coupon/api/groupkey-stock?groupkey=ID1,ID2,...
  StockFlag=1 → 配布中、StockFlag=0 → 配布終了
  ※バッチサイズ10件以下で使用すること（大量IDだとResult=-20001エラー）

使い方:
  python jtb_coupon_monitor.py           # 通常実行
  python jtb_coupon_monitor.py --init    # 初回セットアップ
"""

import requests
from bs4 import BeautifulSoup
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
import time
import re

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://www.jtb.co.jp"

COUPON_PAGES = [
    {
        "name": "国内",
        "url": f"{BASE_URL}/myjtb/campaign/coupon/",
        "detail_pattern": "/myjtb/campaign/coupon/detail/",
        "stock_method": "playwright",  # Stock APIは判定が不正確なためPlaywrightに変更
    },
    {
        "name": "海外",
        "url": f"{BASE_URL}/myjtb/campaign/kaigaicoupon/",
        "detail_pattern": "/myjtb/campaign/kaigaicoupon/detail/",
        "stock_method": "playwright",  # Stock APIが存在しないためPlaywrightで判定
    },
]

DATA_DIR = Path("./jtb_coupon_data")
MASTER_FILE = DATA_DIR / "master_ids.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

REQUEST_DELAY = 2

# データ保持日数（これより古い日次ファイルは自動削除）
DATA_RETENTION_DAYS = 30


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================
# マスターID管理（前回のID一覧を記憶して差分検出に使う）
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
# スクレイピング: 一覧ページ（CSS セレクタ方式）
# ============================================================
def scrape_coupon_list_page(page_config):
    """
    JTBの一覧ページからクーポン情報を抽出。

    HTML構造:
      .c-coupon__item[data-id][data-category][data-pref]
        .c-coupon__head > .c-coupon__price > em  → 割引額
        .c-coupon__head > .c-coupon__area         → エリア
        .c-coupon__bottom > .c-coupon__title > a  → タイトル & 詳細URL
        .c-coupon__bottom > .c-coupon__term        → 期間
        .c-coupon__bottom > .c-coupon__tags        → タイプ
    """
    page_name = page_config["name"]
    page_url = page_config["url"]
    detail_pattern = page_config["detail_pattern"]

    print(f"📡 [{page_name}] 一覧ページを取得中... {page_url}")
    resp = requests.get(page_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    coupons = []

    # .c-coupon__item を直接取得（data-id属性を持つもの）
    items = soup.select(".c-coupon__item[data-id]")

    if not items:
        # フォールバック: data-id がない場合はリンクベースで探す
        print(f"  ⚠️ [{page_name}] .c-coupon__item[data-id] が見つかりません。フォールバック処理中...")
        return _scrape_coupon_list_page_fallback(page_config, soup)

    for item in items:
        # ----- ID -----
        coupon_id = item.get("data-id", "").strip()
        if not coupon_id:
            continue

        # ----- 割引額 -----
        discount = ""
        price_el = item.select_one(".c-coupon__price")
        if price_el:
            em_el = price_el.select_one("em")
            if em_el:
                amount = em_el.get_text(strip=True)
                price_text = price_el.get_text(strip=True)
                if "％引" in price_text or "%引" in price_text:
                    discount = f"最大{amount}％引" if "最大" in price_text else f"{amount}％引"
                else:
                    discount = f"最大{amount}円引" if "最大" in price_text else f"{amount}円引"

        # ----- タイトル & 詳細URL -----
        title = ""
        detail_url = ""
        title_a = item.select_one(".c-coupon__title a")
        if title_a:
            title = title_a.get_text(strip=True)
            href = title_a.get("href", "")
            detail_url = href if href.startswith("http") else BASE_URL + href
        else:
            # h3 直接
            title_h3 = item.select_one(".c-coupon__title")
            if title_h3:
                title = title_h3.get_text(strip=True)

        if not detail_url:
            # detail_patternを使ってリンクを探す
            link = item.select_one(f'a[href*="{detail_pattern}"]')
            if link:
                href = link.get("href", "")
                detail_url = href if href.startswith("http") else BASE_URL + href

        # ----- エリア -----
        area = ""
        # data-pref 属性から取得（JSON配列）
        pref_data = item.get("data-pref", "")
        if pref_data:
            try:
                prefs = json.loads(pref_data)
                if isinstance(prefs, list) and prefs:
                    area = "・".join(prefs[:3])
            except (json.JSONDecodeError, TypeError):
                pass
        if not area:
            area_el = item.select_one(".c-coupon__area")
            if area_el:
                area = area_el.get_text(strip=True)

        # ----- 期間 -----
        booking_period = ""
        stay_period = ""
        term_el = item.select_one(".c-coupon__term")
        if term_el:
            term_text = term_el.get_text(separator="\n", strip=True)

            bp_match = re.search(
                r'予約対象期間[：:\s]*(.+?)(?:\n|$)', term_text
            )
            if bp_match:
                booking_period = bp_match.group(1).strip()

            sp_match = re.search(
                r'(?:宿泊|出発)対象期間[：:\s]*(.+?)(?:\n|$)', term_text
            )
            if sp_match:
                stay_period = sp_match.group(1).strip()

        # ----- タイプ（data-category属性） -----
        coupon_type = ""
        cat_data = item.get("data-category", "")
        if cat_data:
            try:
                cats = json.loads(cat_data)
                if isinstance(cats, list) and cats:
                    coupon_type = "・".join(cats)
            except (json.JSONDecodeError, TypeError):
                pass
        if not coupon_type:
            tags_el = item.select_one(".c-coupon__tags")
            if tags_el:
                spans = tags_el.select("span")
                found_types = [s.get_text(strip=True) for s in spans if s.get_text(strip=True)]
                if found_types:
                    coupon_type = "・".join(found_types)

        # ----- 店舗利用可 -----
        item_text = item.get_text(strip=True)
        store_available = "店舗利用可" in item_text or "店舗でも使える" in item_text

        coupons.append({
            "id": coupon_id,
            "category": page_name,
            "title": title,
            "discount": discount,
            "area": area,
            "type": coupon_type,
            "booking_period": booking_period,
            "stay_period": stay_period,
            "store_available": store_available,
            "stock_status": "不明",  # Stock APIで後から更新
            "detail_url": detail_url,
            "detail_data": None,
        })

    print(f"  ✅ [{page_name}] {len(coupons)}件検出")
    return coupons


def _scrape_coupon_list_page_fallback(page_config, soup):
    """data-id属性が見つからない場合のフォールバック（リンクベース）"""
    page_name = page_config["name"]
    detail_pattern = page_config["detail_pattern"]
    coupons = []

    coupon_links = soup.select(f'a[href*="{detail_pattern}"]')
    seen_urls = set()

    for link in coupon_links:
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        detail_url = href if href.startswith("http") else BASE_URL + href

        # カード全体のコンテナを探す（.c-coupon__item まで遡る）
        card = link
        for _ in range(8):
            parent = card.parent
            if parent is None:
                break
            card = parent
            if card.get("class") and "c-coupon__item" in card.get("class", []):
                break

        card_text = card.get_text(separator="\n", strip=True)

        title = link.get_text(strip=True)

        # 割引額
        discount = ""
        discount_match = re.search(
            r'最大[\s]*([0-9,]+)[\s]*円引|([0-9,]+)[\s]*円引'
            r'|最大[\s]*([0-9,]+)[\s]*[％%]引|([0-9,]+)[\s]*[％%]引',
            card_text
        )
        if discount_match:
            if discount_match.group(1):
                discount = f"最大{discount_match.group(1)}円引"
            elif discount_match.group(2):
                discount = f"{discount_match.group(2)}円引"
            elif discount_match.group(3):
                discount = f"最大{discount_match.group(3)}％引"
            elif discount_match.group(4):
                discount = f"{discount_match.group(4)}％引"

        # ID
        id_match = re.search(r'/detail/([^/]+)/', detail_url)
        coupon_id = id_match.group(1) if id_match else ""
        if not coupon_id:
            continue

        coupons.append({
            "id": coupon_id,
            "category": page_name,
            "title": title,
            "discount": discount,
            "area": "",
            "type": "",
            "booking_period": "",
            "stay_period": "",
            "store_available": False,
            "stock_status": "不明",
            "detail_url": detail_url,
            "detail_data": None,
        })

    print(f"  ✅ [{page_name}] {len(coupons)}件検出（フォールバック）")
    return coupons


def scrape_all_coupon_lists():
    all_coupons = []
    for page_config in COUPON_PAGES:
        time.sleep(REQUEST_DELAY)
        coupons = scrape_coupon_list_page(page_config)

        if not coupons:
            continue

        # Playwright で配布状況を判定（国内: 詳細ページ確認、海外: DOMの.c-close__txt）
        stock_map = check_stock_status_playwright(
            page_config["url"], coupons, page_config.get("detail_pattern", ""),
        )
        for c in coupons:
            c["stock_status"] = stock_map.get(c["id"], "不明")

        all_coupons.extend(coupons)

    # 異常検知: 0件の場合はサイト構造変更の可能性
    if not all_coupons:
        print("🚨 異常検知: クーポンが0件です。サイト構造が変更された可能性があります。")
        sys.exit(1)

    # 予約対象期間が終了しているクーポンを「配布終了」に上書き
    expired_count = mark_expired_by_booking_period(all_coupons)
    if expired_count:
        print(f"📅 予約対象期間終了による配布終了: {expired_count}件")

    return all_coupons


# ============================================================
# 予約対象期間による配布終了判定
# ============================================================
def parse_booking_end_date(booking_period):
    """
    予約対象期間文字列から終了日を解析する。
    対応フォーマット:
      - "2025/10/1(水) ～ 2026/2/28(土)"   ← スラッシュ形式
      - "2025年10月1日(水)～2026年3月23日(月)" ← 漢字形式
    Returns: datetime.date or None
    """
    if not booking_period:
        return None

    # 「～」「〜」「~」の後ろの日付を取得
    parts = re.split(r'[～〜~]', booking_period)
    if len(parts) < 2:
        return None

    end_part = parts[-1].strip()

    # スラッシュ形式: "2026/2/28(土)" or "2026/02/28(土)"
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', end_part)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    # 漢字形式: "2026年3月23日(月)"
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', end_part)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    return None


def mark_expired_by_booking_period(coupons):
    """
    予約対象期間の終了日が過ぎたクーポンを「配布終了」に上書きする。
    既に「配布終了」のものはスキップ。
    Returns: 上書きした件数
    """
    today = datetime.now().date()
    expired_count = 0

    for c in coupons:
        if c.get("stock_status") == "配布終了":
            continue

        end_date = parse_booking_end_date(c.get("booking_period", ""))
        if end_date and end_date < today:
            c["stock_status"] = "配布終了"
            expired_count += 1
            print(f"  📅 期間終了: [{c['category']}] {c['title'][:50]} "
                  f"(予約期限: {end_date})")

    return expired_count


# ============================================================
# Stock API: 配布状況チェック
# ============================================================
def check_stock_status(api_url, coupons):
    """
    Stock API を叩いて各クーポンの配布状況を取得する。
    StockFlag=1 → 配布中、StockFlag=0 → 配布終了

    注意: バッチサイズは10件以下にすること。
    20件以上だとAPIがResult=-20001を返しエラーになる。

    Returns: {coupon_id: "配布中" or "配布終了"}
    """
    ids = list(dict.fromkeys(c["id"] for c in coupons))  # 重複排除・順序保持
    stock_map = {}

    # APIはバッチサイズ10が安全上限（20以上でResult=-20001エラー）
    BATCH_SIZE = 10
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        params = {"groupkey": ",".join(batch)}

        try:
            time.sleep(1)
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("Result") == "0" and "GroupKeyInfo" in data:
                for item in data["GroupKeyInfo"]:
                    gk = item.get("GroupKey", "")
                    flag = item.get("StockFlag", -1)
                    if flag == 1:
                        stock_map[gk] = "配布中"
                    elif flag == 0:
                        stock_map[gk] = "配布終了"
                    else:
                        stock_map[gk] = "不明"

                batch_active = sum(1 for bid in batch if stock_map.get(bid) == "配布中")
                batch_ended = sum(1 for bid in batch if stock_map.get(bid) == "配布終了")
                print(f"  📊 Stock API [{i+1}-{i+len(batch)}]: 配布中={batch_active}, 配布終了={batch_ended}")
            else:
                result_code = data.get("Result", "N/A")
                print(f"  ⚠️ Stock API: 予期しないレスポンス (Result={result_code})")
                for bid in batch:
                    stock_map.setdefault(bid, "不明")

        except Exception as e:
            print(f"  ⚠️ Stock API エラー: {e}")
            for bid in batch:
                stock_map.setdefault(bid, "不明")

    total_active = sum(1 for v in stock_map.values() if v == "配布中")
    total_ended = sum(1 for v in stock_map.values() if v == "配布終了")
    total_unknown = sum(1 for v in stock_map.values() if v == "不明")
    print(f"  📊 Stock API 合計: 配布中={total_active}, 配布終了={total_ended}, 不明={total_unknown}")

    return stock_map


# ============================================================
# Playwright: 海外クーポン配布状況チェック
# ============================================================
def check_stock_status_playwright(page_url, coupons, detail_pattern=""):
    """
    Playwright で配布状況を判定する。

    海外: 一覧ページのDOM上の .c-close__txt「配布終了」テキストで判定
    国内: 一覧ページでは判別不可のため、各詳細ページに
          「本クーポンは終了いたしました」が表示されるかで判定

    Returns: {coupon_id: "配布中" or "配布終了"}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️ Playwright未インストール。全件「不明」にフォールバック")
        return {c["id"]: "不明" for c in coupons}

    stock_map = {}
    is_domestic = "kaigaicoupon" not in page_url

    try:
        print(f"  🎭 Playwright で配布状況を取得中... {page_url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
            )

            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)

            # JS描画を待機
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3000)

            items = page.query_selector_all(".c-coupon__item")
            print(f"  🎭 {len(items)}件のクーポン要素を検出")

            seen_ids = set()
            for item in items:
                # ID取得: data-id属性（国内）優先、なければリンクから抽出（海外）
                coupon_id = item.get_attribute("data-id") or ""
                if not coupon_id:
                    link = item.query_selector("a[href*='detail']")
                    if not link:
                        continue
                    href = link.get_attribute("href") or ""
                    id_match = re.search(r'/detail/([^/]+)/', href)
                    if not id_match:
                        continue
                    coupon_id = id_match.group(1)

                if coupon_id in seen_ids:
                    continue
                seen_ids.add(coupon_id)

                if not is_domestic:
                    # 海外: .c-close__txt 内の「配布終了」で判定
                    close_el = item.query_selector(".c-close__txt")
                    if close_el:
                        close_text = close_el.inner_text()
                        if "配布終了" in close_text:
                            stock_map[coupon_id] = "配布終了"
                            continue
                    stock_map[coupon_id] = "配布中"
                else:
                    # 国内: 一覧では判別不可、後で詳細ページで確認
                    stock_map[coupon_id] = "要確認"

            # 国内: 各詳細ページで「本クーポンは終了いたしました」を確認
            if is_domestic:
                pending_ids = [cid for cid, st in stock_map.items() if st == "要確認"]
                print(f"  🎭 国内: {len(pending_ids)}件の詳細ページを確認中...")
                for i, cid in enumerate(pending_ids):
                    detail_url = f"{BASE_URL}{detail_pattern}{cid}/page.asp"
                    try:
                        page.goto(detail_url, wait_until="domcontentloaded", timeout=10000)
                        page.wait_for_timeout(800)

                        is_ended = page.evaluate('''() => {
                            const els = document.querySelectorAll('h1, h2, h3, p, div');
                            for (const el of els) {
                                if (el.textContent.trim().startsWith('本クーポンは終了いたしました')) {
                                    let p = el;
                                    while (p) {
                                        if (p.className && (p.className.includes('notice') || p.className.includes('note') || p.className.includes('attention'))) {
                                            return false;
                                        }
                                        p = p.parentElement;
                                    }
                                    return true;
                                }
                            }
                            return false;
                        }''')

                        stock_map[cid] = "配布終了" if is_ended else "配布中"
                    except Exception:
                        stock_map[cid] = "不明"

                    if (i + 1) % 20 == 0:
                        print(f"    ... {i + 1}/{len(pending_ids)}件完了")

            browser.close()

        active = sum(1 for v in stock_map.values() if v == "配布中")
        ended = sum(1 for v in stock_map.values() if v == "配布終了")
        print(f"  🎭 Playwright 結果: 配布中={active}, 配布終了={ended}")

    except Exception as e:
        print(f"  ⚠️ Playwright エラー: {e}")
        print("  ⚠️ 全件「不明」にフォールバック")
        for c in coupons:
            stock_map.setdefault(c["id"], "不明")

    return stock_map


# ============================================================
# スクレイピング: 詳細ページ
# ============================================================
def scrape_detail_page(url):
    try:
        time.sleep(REQUEST_DELAY)
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text(separator="\n", strip=True)

        detail = {
            "coupon_codes": [],
            "passwords": [],
            "conditions": [],
            "notes": [],
        }

        # クーポンコード
        code_patterns = [
            r'クーポンコード[：:\s]*([A-Za-z0-9_\-]+)',
            r'コード[：:\s]*([A-Za-z0-9_\-]+)',
        ]
        for pat in code_patterns:
            for match in re.finditer(pat, page_text, re.IGNORECASE):
                code = match.group(1)
                if code not in detail["coupon_codes"] and len(code) >= 3:
                    detail["coupon_codes"].append(code)

        # パスワード
        pw_patterns = [
            r'パスワード[：:\s]*([A-Za-z0-9_\-]+)',
            r'PASSWORD[：:\s]*([A-Za-z0-9_\-]+)',
        ]
        for pat in pw_patterns:
            for match in re.finditer(pat, page_text, re.IGNORECASE):
                pw = match.group(1)
                if pw not in detail["passwords"]:
                    detail["passwords"].append(pw)

        # 条件
        for pat in [r'([0-9,]+)円以上[のでご利用時に]*([0-9,]+)円[\s]*(割引|引|OFF)']:
            for match in re.finditer(pat, page_text):
                cond = match.group(0)
                if cond not in detail["conditions"]:
                    detail["conditions"].append(cond)

        # 注意事項
        note_keywords = ["併用不可", "1回限り", "先着", "枚数限定", "1予約につき"]
        for keyword in note_keywords:
            if keyword in page_text:
                idx = page_text.find(keyword)
                start = max(0, idx - 10)
                end = min(len(page_text), idx + 50)
                snippet = page_text[start:end].replace("\n", " ").strip()
                if snippet and snippet not in detail["notes"]:
                    detail["notes"].append(snippet)

        return detail

    except Exception as e:
        print(f"    ⚠️ 詳細ページ取得エラー: {e}")
        return {"coupon_codes": [], "passwords": [], "conditions": [], "notes": []}


# ============================================================
# 差分検出（新規・消失・配布終了）
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
            "discount": c["discount"],
            "stock_status": c.get("stock_status", "不明"),
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
            "stock_status": prev_info.get("stock_status", ""),
        })

    # 配布状況の変化を検出（配布中→配布終了、配布終了→配布中）
    continuing_ids = curr_id_set & prev_ids
    for cid in sorted(continuing_ids):
        prev_stock = master_ids["ids"].get(cid, {}).get("stock_status", "不明")
        curr_stock = curr_map[cid].get("stock_status", "不明")

        if prev_stock != curr_stock and curr_stock != "不明" and prev_stock != "不明":
            events.append({
                "date": today_str(),
                "type": "🔴 配布終了" if curr_stock == "配布終了" else "🟢 配布再開",
                "id": cid,
                "category": curr_map[cid]["category"],
                "title": curr_map[cid]["title"],
                "discount": curr_map[cid]["discount"],
                "stock_status": curr_stock,
            })

    return events


def update_master_ids(master_ids, current_coupons):
    new_ids = {}
    for c in current_coupons:
        new_ids[c["id"]] = {
            "category": c["category"],
            "title": c["title"],
            "discount": c["discount"],
            "area": c["area"],
            "stock_status": c.get("stock_status", "不明"),
        }
    master_ids["ids"] = new_ids
    return master_ids


# ============================================================
# データ保存
# ============================================================
def save_daily_data(coupons):
    today = today_str()
    daily_file = DATA_DIR / f"coupons_{today}.json"
    data = []
    for c in coupons:
        entry = {k: v for k, v in c.items() if k != "detail_data"}
        entry["detail_data"] = c.get("detail_data") or {}
        data.append(entry)

    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 日次データ保存: {daily_file}（{len(data)}件）")


def save_change_log(events):
    log_file = DATA_DIR / "change_log.json"
    existing = []
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing.extend(events)

    # 直近90日分だけ保持
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
            # ファイル名から日付を抽出
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', f.name)
            if date_match and date_match.group(1) < cutoff:
                f.unlink()
                removed += 1

    if removed:
        print(f"🧹 古いファイル {removed}件を削除（{DATA_RETENTION_DAYS}日超過分）")


# ============================================================
# レポート生成
# ============================================================
def generate_report(coupons, events):
    today = today_str()
    domestic = [c for c in coupons if c["category"] == "国内"]
    overseas = [c for c in coupons if c["category"] == "海外"]

    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    ended = [c for c in coupons if c.get("stock_status") == "配布終了"]

    lines = [
        f"# JTBクーポンレポート {today}",
        f"",
        f"## 概要",
        f"- 国内クーポン: {len(domestic)}件",
        f"- 海外クーポン: {len(overseas)}件",
        f"- 合計: {len(coupons)}件",
        f"- 📊 配布中: {len(active)}件 / 配布終了: {len(ended)}件",
        f"",
    ]

    if events:
        lines.append("## 変動")
        for e in events:
            lines.append(f"- {e['type']} [{e['category']}] {e['title']} ({e['id']})")
        lines.append("")
    else:
        lines.append("## 変動: なし")
        lines.append("")

    if ended:
        lines.append("## 配布終了クーポン一覧")
        for c in ended:
            lines.append(f"- [{c['category']}] {c['title']} ({c['id']}) {c['discount']}")
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
    print("🔄 初期化モード")
    setup_dirs()

    coupons = scrape_all_coupon_lists()

    print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
    for i, coupon in enumerate(coupons):
        print(f"  [{i+1}/{len(coupons)}] [{coupon['category']}] {coupon['id']}")
        detail = scrape_detail_page(coupon["detail_url"])
        coupon["detail_data"] = detail

    save_daily_data(coupons)

    master_ids = update_master_ids({"last_updated": "", "ids": {}}, coupons)
    save_master_ids(master_ids)

    generate_report(coupons, [])
    print(f"\n✅ 初期化完了: {len(coupons)}件")


def run_full():
    setup_dirs()

    coupons = scrape_all_coupon_lists()

    print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
    for i, coupon in enumerate(coupons):
        print(f"  [{i+1}/{len(coupons)}] [{coupon['category']}] {coupon['id']}")
        detail = scrape_detail_page(coupon["detail_url"])
        coupon["detail_data"] = detail

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

    # 古いファイルを自動削除
    cleanup_old_files()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        run_init()
    else:
        run_full()


if __name__ == "__main__":
    main()
