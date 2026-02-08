#!/usr/bin/env python3
"""
JTB クーポン監視スクリプト（国内＋海外 / Stock API対応版）
================================================================
一覧ページからクーポンを収集し、Stock APIで配布終了を判定。
前回との差分（新規・消失）も検出する。

Stock API: /myjtb/campaign/coupon/api/groupkey-stock?groupkey=ID1,ID2,...
  StockFlag=1 → 配布中、StockFlag=0 → 配布終了

使い方:
  python jtb_coupon_monitor.py           # 通常実行
  python jtb_coupon_monitor.py --init    # 初回セットアップ
"""

import requests
from bs4 import BeautifulSoup
import json
import sys
import hashlib
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
        "stock_api": f"{BASE_URL}/myjtb/campaign/coupon/api/groupkey-stock",
    },
    {
        "name": "海外",
        "url": f"{BASE_URL}/myjtb/campaign/kaigaicoupon/",
        "detail_pattern": "/myjtb/campaign/kaigaicoupon/detail/",
        "stock_api": f"{BASE_URL}/myjtb/campaign/kaigaicoupon/api/groupkey-stock",
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
# スクレイピング: 一覧ページ
# ============================================================
def scrape_coupon_list_page(page_config):
    page_name = page_config["name"]
    page_url = page_config["url"]
    detail_pattern = page_config["detail_pattern"]

    print(f"📡 [{page_name}] 一覧ページを取得中... {page_url}")
    resp = requests.get(page_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    coupons = []

    coupon_links = soup.select(f'a[href*="{detail_pattern}"]')

    seen_urls = set()
    for link in coupon_links:
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        detail_url = href if href.startswith("http") else BASE_URL + href

        # カード全体のコンテナを探す
        card = link
        for _ in range(5):
            parent = card.parent
            if parent is None:
                break
            card = parent
            card_text = card.get_text(separator="\n", strip=True)
            if "円引" in card_text or "予約対象期間" in card_text:
                break

        card_text = card.get_text(separator="\n", strip=True)

        # タイトル
        title_el = link.find("h3") or link
        title = title_el.get_text(strip=True)
        if not title:
            title = link.get_text(strip=True)

        # 割引額（円引 + ％引）
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

        # 期間
        booking_period = ""
        stay_period = ""
        period_lines = re.findall(
            r'(予約対象期間|宿泊対象期間|出発対象期間)[：:\s]*(.+?)(?:\n|$)', card_text
        )
        for label, period in period_lines:
            if "予約" in label:
                booking_period = period.strip()
            elif "宿泊" in label or "出発" in label:
                stay_period = period.strip()

        if not booking_period:
            bp_match = re.search(
                r'予約対象期間[：:\s]*\n?\s*(\d{4}年?\d{1,2}月?\d{1,2}日?.+?)(?:\n\n|\n[^\d])',
                card_text, re.DOTALL
            )
            if bp_match:
                booking_period = bp_match.group(1).replace("\n", "").strip()

        if not stay_period:
            sp_match = re.search(
                r'(?:宿泊|出発)対象期間[：:\s]*\n?\s*(\d{4}年?\d{1,2}月?\d{1,2}日?.+?)(?:\n\n|\n[^\d])',
                card_text, re.DOTALL
            )
            if sp_match:
                stay_period = sp_match.group(1).replace("\n", "").strip()

        # タイプ
        coupon_type = ""
        type_keywords = [
            "海外航空券＋ホテル", "海外航空券+ホテル",
            "海外現地オプショナルツアー",
            "海外航空券", "海外ツアー",
            "宿泊", "ツアー",
        ]
        found_types = []
        for kw in type_keywords:
            if kw in card_text:
                if kw == "海外航空券" and any("海外航空券＋" in ft or "海外航空券+" in ft for ft in found_types):
                    continue
                if kw not in found_types:
                    found_types.append(kw)
        if found_types:
            coupon_type = "・".join(found_types)

        # 店舗利用可
        store_available = "店舗利用可" in card_text or "店舗でも使える" in card_text

        # エリア
        area = ""
        area_candidates = [
            "全方面", "全国",
            "ハワイ", "グアム", "サイパン", "アジア", "ヨーロッパ", "アメリカ", "オセアニア",
            "欧州ロシア/アフリカ",
            "北海道", "東北", "関東", "甲信越", "北陸", "東海",
            "近畿", "関西", "中国", "四国", "九州", "沖縄",
            "福島県", "新潟県", "山梨県", "長野県", "石川県", "静岡県",
            "三重県", "京都府", "大阪府", "兵庫県", "岡山県", "広島県", "愛媛県",
            "高知県", "鳥取県", "島根県", "山口県",
            "東京都", "千葉県", "群馬県", "滋賀県",
            "福岡県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
        ]
        # card_textの前半（エリアラベル部分）とタイトル周辺から検出
        search_area = card_text[:120]
        for a in area_candidates:
            if a in search_area:
                area = a
                break

        # ID
        id_match = re.search(r'/detail/([^/]+)/', detail_url)
        coupon_id = id_match.group(1) if id_match else hashlib.md5(detail_url.encode()).hexdigest()[:12]

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


def scrape_all_coupon_lists():
    all_coupons = []
    for page_config in COUPON_PAGES:
        time.sleep(REQUEST_DELAY)
        coupons = scrape_coupon_list_page(page_config)

        # Stock API で配布状況を一括チェック
        stock_api_url = page_config.get("stock_api")
        if stock_api_url and coupons:
            stock_map = check_stock_status(stock_api_url, coupons)
            for c in coupons:
                c["stock_status"] = stock_map.get(c["id"], "不明")

        all_coupons.extend(coupons)
    return all_coupons


# ============================================================
# Stock API: 配布状況チェック
# ============================================================
def check_stock_status(api_url, coupons):
    """
    Stock API を叩いて各クーポンの配布状況を取得する。
    StockFlag=1 → 配布中、StockFlag=0 → 配布終了

    Returns: {coupon_id: "配布中" or "配布終了"}
    """
    ids = list(dict.fromkeys(c["id"] for c in coupons))  # 重複排除・順序保持
    stock_map = {}

    # APIは一度に大量のIDを受け付けるが、念のため50件ずつ分割
    BATCH_SIZE = 50
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

                active = sum(1 for v in stock_map.values() if v == "配布中")
                ended = sum(1 for v in stock_map.values() if v == "配布終了")
                print(f"  📊 Stock API: {len(batch)}件チェック → 配布中={active}, 配布終了={ended}")
            else:
                print(f"  ⚠️ Stock API: 予期しないレスポンス: {data.get('Result', 'N/A')}")
                for bid in batch:
                    stock_map.setdefault(bid, "不明")

        except Exception as e:
            print(f"  ⚠️ Stock API エラー: {e}")
            for bid in batch:
                stock_map.setdefault(bid, "不明")

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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        run_init()
    else:
        run_full()


if __name__ == "__main__":
    main()
