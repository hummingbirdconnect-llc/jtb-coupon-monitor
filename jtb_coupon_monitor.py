#!/usr/bin/env python3
"""
JTB クーポン監視スクリプト（国内＋海外 / ライフサイクル追跡版）
================================================================
クーポンの「配布中 → 配布終了 → ページ消滅 → 復活」を追跡し、
マスター台帳（master_coupons.json）で全履歴を管理する。

ステータス定義:
  🟢 配布中      ページに表示されており「配布終了」表記なし
  🔴 配布終了    ページに表示されているが「配布終了」表記あり
  ⚫ ページ消滅   一覧ページから消えた（期限切れ or 掲載終了）
  🔵 予約期間外   予約対象期間がまだ始まっていない or 過ぎた（日付判定）
  🔄 復活        一度消えた/終了したクーポンが再び配布中に戻った

使い方:
  python jtb_coupon_monitor.py           # 通常実行
  python jtb_coupon_monitor.py --init    # 初回セットアップ
  python jtb_coupon_monitor.py --report  # レポートのみ
  python jtb_coupon_monitor.py --status  # 現在のマスター台帳サマリー表示
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
import time
import re
import copy

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://www.jtb.co.jp"

COUPON_PAGES = [
    {
        "name": "国内",
        "url": f"{BASE_URL}/myjtb/campaign/coupon/",
        "detail_pattern": "/myjtb/campaign/coupon/detail/",
    },
    {
        "name": "海外",
        "url": f"{BASE_URL}/myjtb/campaign/kaigaicoupon/",
        "detail_pattern": "/myjtb/campaign/kaigaicoupon/detail/",
    },
]

DATA_DIR = Path("./jtb_coupon_data")
REPORT_DIR = DATA_DIR / "reports"
MASTER_FILE = DATA_DIR / "master_coupons.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

REQUEST_DELAY = 2

# ステータス定数
STATUS_ACTIVE = "🟢 配布中"
STATUS_ENDED = "🔴 配布終了"
STATUS_GONE = "⚫ ページ消滅"
STATUS_REVIVED = "🔄 復活"


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def now_iso():
    return datetime.now().isoformat()


def parse_date_loose(text):
    """日付テキストからdatetimeを抽出（複数フォーマット対応）"""
    if not text:
        return None
    # パターン: 2026/3/31(火) or 2026年3月31日(火)
    m = re.search(r'(\d{4})[/年](\d{1,2})[/月](\d{1,2})', text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def is_booking_expired(booking_period):
    """予約対象期間の終了日が過ぎているか判定"""
    if not booking_period:
        return False
    # 「～」の後ろ側を取得
    parts = re.split(r'[～〜~]', booking_period)
    if len(parts) >= 2:
        end_date = parse_date_loose(parts[1])
        if end_date and end_date.date() < datetime.now().date():
            return True
    return False


# ============================================================
# マスター台帳の読み書き
# ============================================================
def load_master():
    """マスター台帳を読み込む（なければ空で返す）"""
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "coupons": {}}


def save_master(master):
    """マスター台帳を保存"""
    master["last_updated"] = now_iso()
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)
    print(f"💾 マスター台帳保存: {MASTER_FILE}")


# ============================================================
# スクレイピング: 一覧ページ
# ============================================================
def scrape_coupon_list_page(page_config):
    """1つのクーポン一覧ページからクーポン情報を抽出"""
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

        # ----- 配布終了の検出 -----
        # JTBが配布終了時に追加するバッジ/ラベル要素を直接探す。
        # テキスト全文検索はページ上部の注意書き（「配布終了」表記がない場合…）を
        # 誤検出するため使わない。
        is_ended = False
        # 方法1: link要素内の短いテキスト要素から「配布終了」を探す
        for el in link.find_all(string=True):
            el_text = el.strip()
            # 短いテキスト（30文字以下）に「配布終了」が含まれていれば、
            # それはバッジ/ラベルであり注意書きではない
            if el_text and len(el_text) <= 30:
                if "配布終了" in el_text or "受付終了" in el_text:
                    is_ended = True
                    break
        # 方法2: card要素内のclass名に"end"や"sold"等が含まれる要素を探す
        if not is_ended:
            for el in card.find_all(class_=True):
                classes = " ".join(el.get("class", []))
                if any(kw in classes.lower() for kw in ["end", "sold", "finish", "close", "stop"]):
                    is_ended = True
                    break
        # 方法3: imgタグのalt属性に「配布終了」が含まれる場合
        if not is_ended:
            for img in card.find_all("img"):
                alt = img.get("alt", "")
                if "配布終了" in alt or "受付終了" in alt:
                    is_ended = True
                    break

        # タイトル抽出
        title_el = link.find("h3") or link
        title = title_el.get_text(strip=True)
        if not title:
            title = link.get_text(strip=True)

        # 割引額
        discount = ""
        discount_match = re.search(r'最大[\s]*([0-9,]+)[\s]*円引|([0-9,]+)[\s]*円引', card_text)
        if discount_match:
            amount = discount_match.group(1) or discount_match.group(2)
            discount = f"最大{amount}円引" if discount_match.group(1) else f"{amount}円引"

        # 期間抽出
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
                r'予約対象期間[：:\s]*\n?\s*(\d{4}年\d{1,2}月\d{1,2}日.+?)(?:\n\n|\n[^\d])',
                card_text, re.DOTALL
            )
            if bp_match:
                booking_period = bp_match.group(1).replace("\n", "").strip()

        if not stay_period:
            sp_match = re.search(
                r'出発対象期間[：:\s]*\n?\s*(\d{4}年\d{1,2}月\d{1,2}日.+?)(?:\n\n|\n[^\d])',
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

        store_available = "店舗利用可" in card_text or "店舗でも使える" in card_text

        area = ""
        area_candidates = [
            "全方面", "全国",
            "ハワイ", "グアム", "サイパン", "アジア", "ヨーロッパ", "アメリカ", "オセアニア",
            "北海道", "東北", "関東", "甲信越", "北陸", "東海",
            "近畿", "関西", "中国", "四国", "九州", "沖縄",
            "福島県", "新潟県", "山梨県", "長野県", "石川県", "静岡県",
            "三重県", "京都府", "大阪府", "兵庫県", "広島県", "愛媛県",
            "福岡県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
        ]
        for a in area_candidates:
            if a in card_text[:80]:
                area = a
                break

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
            "detail_url": detail_url,
            "is_ended_on_page": is_ended,
            "detail_data": None,
        })

    print(f"  ✅ [{page_name}] {len(coupons)}件検出（うち配布終了表記: {sum(1 for c in coupons if c['is_ended_on_page'])}件）")
    return coupons


def scrape_all_coupon_lists():
    all_coupons = []
    for page_config in COUPON_PAGES:
        time.sleep(REQUEST_DELAY)
        coupons = scrape_coupon_list_page(page_config)
        all_coupons.extend(coupons)
    return all_coupons


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
            "is_ended_on_detail": False,
            "raw_text_hash": hashlib.md5(page_text.encode()).hexdigest(),
        }

        # 詳細ページでも配布終了を検出（HTML要素レベルで判定）
        # 方法1: 短いテキスト要素から「配布終了」を探す（注意書きの長文は除外）
        for el in soup.find_all(string=True):
            el_text = el.strip()
            if el_text and len(el_text) <= 30:
                if any(kw in el_text for kw in ["配布終了", "受付終了", "配布は終了しました"]):
                    detail["is_ended_on_detail"] = True
                    break
        # 方法2: 「配布上限に達し」は明確な終了シグナル（注意書きとは別）
        if not detail["is_ended_on_detail"]:
            for el in soup.find_all(string=True):
                el_text = el.strip()
                if any(kw in el_text for kw in ["配布上限に達し", "上限に達した"]):
                    detail["is_ended_on_detail"] = True
                    break
        # 方法3: class名やimg altで検出
        if not detail["is_ended_on_detail"]:
            for el in soup.find_all(class_=True):
                classes = " ".join(el.get("class", []))
                if any(kw in classes.lower() for kw in ["end", "sold", "finish", "close"]):
                    detail["is_ended_on_detail"] = True
                    break
            for img in soup.find_all("img"):
                alt = img.get("alt", "")
                if "配布終了" in alt or "受付終了" in alt:
                    detail["is_ended_on_detail"] = True
                    break

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
        for keyword in ["配布終了", "枚数上限", "先着", "1回限り", "併用不可",
                        "対象外", "配布上限", "残りわずか"]:
            if keyword in page_text:
                idx = page_text.find(keyword)
                start = max(0, idx - 20)
                end = min(len(page_text), idx + 50)
                snippet = page_text[start:end].replace("\n", " ").strip()
                if snippet not in detail["notes"]:
                    detail["notes"].append(snippet)

        return detail

    except Exception as e:
        return {"error": str(e), "raw_text_hash": "", "is_ended_on_detail": False}


# ============================================================
# マスター台帳の更新（ライフサイクル管理の核心）
# ============================================================
def update_master(master, scraped_coupons):
    """
    今日のスクレイピング結果でマスター台帳を更新し、変動イベントを返す。

    判定ロジック:
    - ページにある + 配布終了表記なし → 🟢 配布中
    - ページにある + 配布終了表記あり → 🔴 配布終了
    - ページにない + 以前あった       → ⚫ ページ消滅
    - 以前「終了/消滅」→ 今日「配布中」→ 🔄 復活
    """
    events = []  # 変動イベントのリスト
    today = today_str()
    scraped_ids = {c["id"]: c for c in scraped_coupons}
    master_coupons = master.get("coupons", {})

    # --- 1. ページに存在するクーポンを処理 ---
    for cid, coupon in scraped_ids.items():
        # ステータス判定
        is_ended = coupon.get("is_ended_on_page", False)
        detail_ended = (coupon.get("detail_data") or {}).get("is_ended_on_detail", False)
        booking_expired = is_booking_expired(coupon.get("booking_period", ""))

        # デバッグ: 各判定フラグを表示
        print(f"  📋 [{cid}] ended_page={is_ended} ended_detail={detail_ended} booking_expired={booking_expired} period='{coupon.get('booking_period', '')}'")

        if is_ended or detail_ended:
            new_status = STATUS_ENDED
            print(f"     → 🔴 配布終了（{'一覧:配布終了表記' if is_ended else '詳細:配布終了検出'}）")
        elif booking_expired:
            new_status = STATUS_ENDED
            print(f"     → 🔴 配布終了（予約期間切れ）")
        else:
            new_status = STATUS_ACTIVE
            print(f"     → 🟢 配布中")

        if cid in master_coupons:
            old_status = master_coupons[cid].get("status", "")
            old_entry = master_coupons[cid]

            # 復活検出: 以前「終了」「消滅」→ 今日「配布中」
            if new_status == STATUS_ACTIVE and old_status in [STATUS_ENDED, STATUS_GONE]:
                new_status = STATUS_REVIVED
                events.append({
                    "type": "🔄 復活",
                    "coupon": coupon,
                    "detail": f"{old_status} → {new_status}",
                })
            elif new_status != old_status:
                events.append({
                    "type": f"ステータス変更",
                    "coupon": coupon,
                    "detail": f"{old_status} → {new_status}",
                })

            # 内容変更の検出
            content_changes = []
            for field in ["title", "discount", "booking_period", "stay_period"]:
                old_val = old_entry.get(field, "")
                new_val = coupon.get(field, "")
                if old_val != new_val and new_val:
                    content_changes.append(f"{field}: {old_val} → {new_val}")
            if content_changes:
                events.append({
                    "type": "✏️ 内容変更",
                    "coupon": coupon,
                    "detail": " / ".join(content_changes),
                })

            # マスター更新（既存エントリを上書き、履歴は保持）
            history = old_entry.get("status_history", [])
            if new_status != old_status:
                history.append({"date": today, "from": old_status, "to": new_status})

            master_coupons[cid].update({
                "title": coupon["title"],
                "category": coupon["category"],
                "discount": coupon["discount"],
                "area": coupon["area"],
                "type": coupon["type"],
                "booking_period": coupon["booking_period"],
                "stay_period": coupon["stay_period"],
                "store_available": coupon["store_available"],
                "detail_url": coupon["detail_url"],
                "detail_data": coupon.get("detail_data"),
                "status": new_status,
                "last_seen": today,
                "last_updated": today,
                "status_history": history,
            })

        else:
            # 新規クーポン
            events.append({
                "type": "🆕 新規",
                "coupon": coupon,
                "detail": f"{new_status}",
            })
            master_coupons[cid] = {
                "id": cid,
                "title": coupon["title"],
                "category": coupon["category"],
                "discount": coupon["discount"],
                "area": coupon["area"],
                "type": coupon["type"],
                "booking_period": coupon["booking_period"],
                "stay_period": coupon["stay_period"],
                "store_available": coupon["store_available"],
                "detail_url": coupon["detail_url"],
                "detail_data": coupon.get("detail_data"),
                "status": new_status,
                "first_seen": today,
                "last_seen": today,
                "last_updated": today,
                "status_history": [{"date": today, "from": "", "to": new_status}],
            }

    # --- 2. ページから消えたクーポンを処理 ---
    for cid, entry in master_coupons.items():
        if cid not in scraped_ids and entry.get("status") not in [STATUS_GONE]:
            old_status = entry.get("status", "")
            if old_status in [STATUS_ACTIVE, STATUS_ENDED, STATUS_REVIVED]:
                events.append({
                    "type": "⚫ ページ消滅",
                    "coupon": entry,
                    "detail": f"{old_status} → {STATUS_GONE}",
                })
                history = entry.get("status_history", [])
                history.append({"date": today, "from": old_status, "to": STATUS_GONE})
                entry["status"] = STATUS_GONE
                entry["last_updated"] = today
                entry["status_history"] = history

    master["coupons"] = master_coupons
    return events


# ============================================================
# データ保存
# ============================================================
def save_daily_data(coupons):
    filepath = DATA_DIR / f"coupons_{today_str()}.json"
    domestic = [c for c in coupons if c["category"] == "国内"]
    overseas = [c for c in coupons if c["category"] == "海外"]
    data = {
        "scraped_at": now_iso(),
        "total_count": len(coupons),
        "domestic_count": len(domestic),
        "overseas_count": len(overseas),
        "coupons": coupons,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 日次データ保存: {filepath}")
    print(f"   内訳: 国内 {len(domestic)}件 / 海外 {len(overseas)}件")
    return filepath


# ============================================================
# レポート生成
# ============================================================
def generate_report(events, master):
    lines = []
    mc = master.get("coupons", {})

    # ステータス別集計
    active = [c for c in mc.values() if c["status"] == STATUS_ACTIVE]
    revived = [c for c in mc.values() if c["status"] == STATUS_REVIVED]
    ended = [c for c in mc.values() if c["status"] == STATUS_ENDED]
    gone = [c for c in mc.values() if c["status"] == STATUS_GONE]

    lines.append("=" * 60)
    lines.append(f"📊 JTB クーポン変動レポート（国内＋海外）")
    lines.append(f"   日付: {today_str()}")
    lines.append(f"   マスター台帳: 全{len(mc)}件")
    lines.append("=" * 60)
    lines.append("")
    lines.append("■ ステータス別サマリー")
    lines.append(f"  🟢 配布中:     {len(active)}件  ← 今すぐ取得可能")
    lines.append(f"  🔄 復活:       {len(revived)}件  ← 再配布あり！要チェック")
    lines.append(f"  🔴 配布終了:   {len(ended)}件")
    lines.append(f"  ⚫ ページ消滅: {len(gone)}件")

    lines.append("")
    lines.append(f"■ 本日の変動: {len(events)}件")

    if not events:
        lines.append("  ✅ 変化なし")
    else:
        for ev in events:
            c = ev["coupon"]
            lines.append("")
            cat = c.get("category", "")
            title = c.get("title", c.get("id", ""))
            discount = c.get("discount", "")
            lines.append(f"  {ev['type']}")
            lines.append(f"    [{cat}]【{discount}】{title}")
            lines.append(f"    {ev['detail']}")
            if c.get("detail_url"):
                lines.append(f"    URL: {c['detail_url']}")

    # 今取得可能なクーポン一覧
    available = active + revived
    if available:
        lines.append("")
        lines.append("━" * 40)
        lines.append(f"■ 現在取得可能なクーポン一覧（{len(available)}件）")
        lines.append("━" * 40)

        # カテゴリ別にソート
        for cat in ["国内", "海外"]:
            cat_coupons = [c for c in available if c.get("category") == cat]
            if cat_coupons:
                lines.append(f"\n  【{cat}】{len(cat_coupons)}件")
                for c in cat_coupons:
                    status_mark = "🔄" if c["status"] == STATUS_REVIVED else "🟢"
                    codes = ", ".join((c.get("detail_data") or {}).get("coupon_codes", []))
                    code_str = f" [コード: {codes}]" if codes else ""
                    lines.append(f"    {status_mark} {c['discount']} {c['title'][:50]}{code_str}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_report(report_text):
    report_file = REPORT_DIR / f"report_{today_str()}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"📁 レポート保存: {report_file}")


# ============================================================
# ステータスサマリー表示
# ============================================================
def show_status(master):
    mc = master.get("coupons", {})
    if not mc:
        print("マスター台帳が空です。--init で初期データを取得してください。")
        return

    active = [c for c in mc.values() if c["status"] in [STATUS_ACTIVE, STATUS_REVIVED]]
    ended = [c for c in mc.values() if c["status"] == STATUS_ENDED]
    gone = [c for c in mc.values() if c["status"] == STATUS_GONE]

    print(f"\n📋 マスター台帳サマリー（全{len(mc)}件）")
    print(f"   最終更新: {master.get('last_updated', '不明')}")
    print()

    print(f"🟢 現在取得可能（{len(active)}件）:")
    for c in sorted(active, key=lambda x: x.get("category", "")):
        codes = ", ".join((c.get("detail_data") or {}).get("coupon_codes", []))
        code_str = f" [コード: {codes}]" if codes else ""
        print(f"   [{c['category']}] {c['discount']} {c['title'][:50]}{code_str}")

    print(f"\n🔴 配布終了（{len(ended)}件）:")
    for c in ended:
        print(f"   [{c['category']}] {c['discount']} {c['title'][:50]}")

    print(f"\n⚫ ページ消滅（{len(gone)}件）:")
    for c in gone:
        print(f"   [{c['category']}] {c['discount']} {c['title'][:50]} (最終確認: {c.get('last_seen', '?')})")


# ============================================================
# メイン処理
# ============================================================
def run_full():
    """通常実行: スクレイピング → マスター更新 → レポート"""
    # 1. スクレイピング
    coupons = scrape_all_coupon_lists()

    print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
    for i, coupon in enumerate(coupons):
        cat = coupon.get("category", "")
        print(f"  [{i+1}/{len(coupons)}] [{cat}] {coupon['id']}: {coupon['title'][:40]}...")
        detail = scrape_detail_page(coupon["detail_url"])
        coupon["detail_data"] = detail

    # 2. 日次データ保存
    save_daily_data(coupons)

    # 3. マスター台帳更新
    master = load_master()
    events = update_master(master, coupons)
    save_master(master)

    # 4. レポート生成
    report = generate_report(events, master)
    print(report)
    save_report(report)

    return master, events


def main():
    setup_dirs()

    if "--init" in sys.argv:
        print("🚀 初回セットアップ（国内＋海外 / マスター台帳作成）")
        print("-" * 40)
        run_full()
        print("\n✅ 初期セットアップ完了。マスター台帳を作成しました。")

    elif "--report" in sys.argv:
        master = load_master()
        if not master.get("coupons"):
            print("⚠️ マスター台帳がありません。先に通常実行してください。")
            return
        print("📊 マスター台帳からレポート生成")
        report = generate_report([], master)
        print(report)

    elif "--status" in sys.argv:
        master = load_master()
        show_status(master)

    else:
        print(f"🔄 JTB クーポン監視（国内＋海外）- {today_str()}")
        print("-" * 40)
        run_full()


if __name__ == "__main__":
    main()
