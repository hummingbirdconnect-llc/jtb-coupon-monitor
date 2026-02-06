#!/usr/bin/env python3
"""
JTB クーポン監視スクリプト
==========================
毎日実行して、JTBクーポンページの変化（追加・削除・変更）を検出する。

使い方:
  python jtb_coupon_monitor.py           # 通常実行（スクレイピング→比較→レポート）
  python jtb_coupon_monitor.py --init    # 初回セットアップ（初期データ取得のみ）
  python jtb_coupon_monitor.py --report  # 前回データとの比較レポートのみ表示

データ保存先: ./jtb_coupon_data/ ディレクトリ
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
import difflib

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://www.jtb.co.jp"
COUPON_LIST_URL = f"{BASE_URL}/myjtb/campaign/coupon/"
DATA_DIR = Path("./jtb_coupon_data")
REPORT_DIR = DATA_DIR / "reports"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

# リクエスト間隔（秒）- サーバーに優しく
REQUEST_DELAY = 2


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    """データディレクトリを作成"""
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def get_latest_data_file():
    """最新のデータファイルパスを返す（今日以外で最新）"""
    files = sorted(DATA_DIR.glob("coupons_*.json"), reverse=True)
    today = f"coupons_{today_str()}.json"
    for f in files:
        if f.name != today:
            return f
    return None


# ============================================================
# スクレイピング: メインページ
# ============================================================
def scrape_coupon_list():
    """メインのクーポン一覧ページからクーポン情報を抽出"""
    print(f"📡 クーポン一覧ページを取得中... {COUPON_LIST_URL}")
    resp = requests.get(COUPON_LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    coupons = []

    # クーポンカードを探す - リンクを含む見出し（h3）からたどる
    # ページ構造: 各クーポンは <a> リンクを含むカードとして配置されている
    coupon_links = soup.select('a[href*="/myjtb/campaign/coupon/detail/"]')

    seen_urls = set()
    for link in coupon_links:
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        detail_url = href if href.startswith("http") else BASE_URL + href

        # リンクの親要素からテキスト情報を集める
        # カード全体のコンテナを探す
        card = link
        for _ in range(5):
            parent = card.parent
            if parent is None:
                break
            card = parent
            # カードらしい要素を見つけたら止まる
            card_text = card.get_text(separator="\n", strip=True)
            if "円引" in card_text or "予約対象期間" in card_text:
                break

        card_text = card.get_text(separator="\n", strip=True)

        # タイトル抽出（リンクテキストまたはh3内テキスト）
        title_el = link.find("h3") or link
        title = title_el.get_text(strip=True)
        if not title:
            title = link.get_text(strip=True)

        # 割引額抽出
        discount = ""
        discount_match = re.search(r'最大[\s]*([0-9,]+)[\s]*円引|([0-9,]+)[\s]*円引', card_text)
        if discount_match:
            amount = discount_match.group(1) or discount_match.group(2)
            if discount_match.group(1):
                discount = f"最大{amount}円引"
            else:
                discount = f"{amount}円引"

        # 期間抽出
        booking_period = ""
        stay_period = ""
        period_lines = re.findall(r'(予約対象期間|宿泊対象期間|出発対象期間)：(.+?)(?:\n|$)', card_text)
        for label, period in period_lines:
            if "予約" in label:
                booking_period = period.strip()
            elif "宿泊" in label or "出発" in label:
                stay_period = period.strip()

        # タイプ（宿泊/ツアー）
        coupon_type = ""
        if "宿泊" in card_text and "ツアー" in card_text:
            coupon_type = "宿泊・ツアー"
        elif "宿泊" in card_text:
            coupon_type = "宿泊"
        elif "ツアー" in card_text:
            coupon_type = "ツアー"

        # 店舗利用可
        store_available = "店舗利用可" in card_text

        # 対象エリア（カード上部のテキスト）
        area = ""
        area_candidates = ["全国", "北海道", "東北", "関東", "甲信越", "北陸", "東海",
                          "近畿", "関西", "中国", "四国", "九州", "沖縄"]
        for a in area_candidates:
            if a in card_text[:50]:
                area = a
                break

        # IDをURLから抽出
        id_match = re.search(r'/detail/([^/]+)/', detail_url)
        coupon_id = id_match.group(1) if id_match else hashlib.md5(detail_url.encode()).hexdigest()[:12]

        coupons.append({
            "id": coupon_id,
            "title": title,
            "discount": discount,
            "area": area,
            "type": coupon_type,
            "booking_period": booking_period,
            "stay_period": stay_period,
            "store_available": store_available,
            "detail_url": detail_url,
            "detail_data": None,  # 後で詳細ページから取得
        })

    print(f"  ✅ {len(coupons)}件のクーポンを検出")
    return coupons


# ============================================================
# スクレイピング: 詳細ページ
# ============================================================
def scrape_detail_page(url):
    """個別クーポンの詳細ページからクーポンコード等を抽出"""
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
            "raw_text_hash": hashlib.md5(page_text.encode()).hexdigest(),
        }

        # クーポンコード検出パターン
        code_patterns = [
            r'クーポンコード[：:\s]*([A-Za-z0-9_\-]+)',
            r'コード[：:\s]*([A-Za-z0-9_\-]+)',
            r'COUPON\s*CODE[：:\s]*([A-Za-z0-9_\-]+)',
        ]
        for pat in code_patterns:
            for match in re.finditer(pat, page_text, re.IGNORECASE):
                code = match.group(1)
                if code not in detail["coupon_codes"]:
                    detail["coupon_codes"].append(code)

        # パスワード検出
        pw_patterns = [
            r'パスワード[：:\s]*([A-Za-z0-9_\-]+)',
            r'PASSWORD[：:\s]*([A-Za-z0-9_\-]+)',
        ]
        for pat in pw_patterns:
            for match in re.finditer(pat, page_text, re.IGNORECASE):
                pw = match.group(1)
                if pw not in detail["passwords"]:
                    detail["passwords"].append(pw)

        # 割引額の条件テーブル検出（例: ○○円以上で△△円引）
        condition_patterns = [
            r'([0-9,]+)円以上[のでご利用時に]*([0-9,]+)円[\s]*(割引|引|OFF)',
            r'旅行代金.*?([0-9,]+)円以上.*?([0-9,]+)円',
        ]
        for pat in condition_patterns:
            for match in re.finditer(pat, page_text):
                detail["conditions"].append(match.group(0))

        # 注意事項や制限
        note_keywords = ["配布終了", "枚数上限", "先着", "1回限り", "併用不可", "対象外"]
        for keyword in note_keywords:
            if keyword in page_text:
                # キーワード周辺のテキストを取得
                idx = page_text.find(keyword)
                start = max(0, idx - 20)
                end = min(len(page_text), idx + 50)
                snippet = page_text[start:end].replace("\n", " ").strip()
                detail["notes"].append(snippet)

        return detail

    except Exception as e:
        return {"error": str(e), "raw_text_hash": ""}


# ============================================================
# データ保存・読み込み
# ============================================================
def save_data(coupons):
    """今日のデータをJSONファイルに保存"""
    filepath = DATA_DIR / f"coupons_{today_str()}.json"
    data = {
        "scraped_at": datetime.now().isoformat(),
        "total_count": len(coupons),
        "coupons": coupons,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 データ保存: {filepath}")
    return filepath


def load_data(filepath):
    """JSONデータを読み込み"""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 比較・レポート生成
# ============================================================
def compare_data(old_data, new_data):
    """2日分のデータを比較して差分を返す"""
    old_coupons = {c["id"]: c for c in old_data["coupons"]}
    new_coupons = {c["id"]: c for c in new_data["coupons"]}

    old_ids = set(old_coupons.keys())
    new_ids = set(new_coupons.keys())

    added = new_ids - old_ids
    removed = old_ids - new_ids
    common = old_ids & new_ids

    changed = []
    for cid in common:
        old_c = old_coupons[cid]
        new_c = new_coupons[cid]

        changes = []
        # 主要フィールドの変化をチェック
        check_fields = ["title", "discount", "booking_period", "stay_period", "type", "store_available"]
        for field in check_fields:
            if old_c.get(field) != new_c.get(field):
                changes.append({
                    "field": field,
                    "old": old_c.get(field),
                    "new": new_c.get(field),
                })

        # 詳細ページの変化（ハッシュ比較）
        old_hash = (old_c.get("detail_data") or {}).get("raw_text_hash", "")
        new_hash = (new_c.get("detail_data") or {}).get("raw_text_hash", "")
        if old_hash and new_hash and old_hash != new_hash:
            changes.append({
                "field": "detail_page_content",
                "old": f"hash:{old_hash[:8]}",
                "new": f"hash:{new_hash[:8]}",
            })

        # クーポンコードの変化
        old_codes = set((old_c.get("detail_data") or {}).get("coupon_codes", []))
        new_codes = set((new_c.get("detail_data") or {}).get("coupon_codes", []))
        if old_codes != new_codes:
            changes.append({
                "field": "coupon_codes",
                "old": list(old_codes),
                "new": list(new_codes),
            })

        if changes:
            changed.append({
                "id": cid,
                "title": new_c["title"],
                "changes": changes,
            })

    return {
        "added": [new_coupons[cid] for cid in added],
        "removed": [old_coupons[cid] for cid in removed],
        "changed": changed,
        "unchanged_count": len(common) - len(changed),
    }


def generate_report(diff, old_data, new_data):
    """差分レポートを生成"""
    lines = []
    lines.append("=" * 60)
    lines.append(f"📊 JTB クーポン変動レポート")
    lines.append(f"   比較: {old_data['scraped_at'][:10]} → {new_data['scraped_at'][:10]}")
    lines.append(f"   総数: {old_data['total_count']}件 → {new_data['total_count']}件")
    lines.append("=" * 60)

    # サマリー
    lines.append("")
    has_changes = diff["added"] or diff["removed"] or diff["changed"]
    if not has_changes:
        lines.append("✅ 変化なし - 全クーポンに変更はありませんでした。")
    else:
        lines.append(f"  🆕 新規追加: {len(diff['added'])}件")
        lines.append(f"  ❌ 終了/削除: {len(diff['removed'])}件")
        lines.append(f"  ✏️  内容変更: {len(diff['changed'])}件")
        lines.append(f"  ─  変化なし: {diff['unchanged_count']}件")

    # 新規追加
    if diff["added"]:
        lines.append("")
        lines.append("━" * 40)
        lines.append("🆕 新規追加されたクーポン")
        lines.append("━" * 40)
        for c in diff["added"]:
            lines.append(f"")
            lines.append(f"  【{c['discount']}】{c['title']}")
            lines.append(f"  エリア: {c['area']} | タイプ: {c['type']}")
            lines.append(f"  予約期間: {c['booking_period']}")
            lines.append(f"  対象期間: {c['stay_period']}")
            if c.get("detail_data", {}).get("coupon_codes"):
                lines.append(f"  コード: {', '.join(c['detail_data']['coupon_codes'])}")
            lines.append(f"  URL: {c['detail_url']}")

    # 削除/終了
    if diff["removed"]:
        lines.append("")
        lines.append("━" * 40)
        lines.append("❌ 終了/削除されたクーポン")
        lines.append("━" * 40)
        for c in diff["removed"]:
            lines.append(f"")
            lines.append(f"  【{c['discount']}】{c['title']}")
            lines.append(f"  エリア: {c['area']} | タイプ: {c['type']}")

    # 内容変更
    if diff["changed"]:
        lines.append("")
        lines.append("━" * 40)
        lines.append("✏️  内容が変更されたクーポン")
        lines.append("━" * 40)
        for item in diff["changed"]:
            lines.append(f"")
            lines.append(f"  {item['title']}")
            for ch in item["changes"]:
                field_names = {
                    "title": "タイトル",
                    "discount": "割引額",
                    "booking_period": "予約期間",
                    "stay_period": "対象期間",
                    "type": "タイプ",
                    "store_available": "店舗利用",
                    "detail_page_content": "詳細ページ内容",
                    "coupon_codes": "クーポンコード",
                }
                fname = field_names.get(ch["field"], ch["field"])
                lines.append(f"    {fname}: {ch['old']} → {ch['new']}")

    lines.append("")
    lines.append("=" * 60)

    report_text = "\n".join(lines)
    return report_text


# ============================================================
# メイン処理
# ============================================================
def run_scrape(include_details=True):
    """スクレイピング実行"""
    coupons = scrape_coupon_list()

    if include_details:
        print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
        for i, coupon in enumerate(coupons):
            print(f"  [{i+1}/{len(coupons)}] {coupon['id']}: {coupon['title'][:40]}...")
            detail = scrape_detail_page(coupon["detail_url"])
            coupon["detail_data"] = detail

    filepath = save_data(coupons)
    return filepath, coupons


def run_compare():
    """前回データとの比較"""
    today_file = DATA_DIR / f"coupons_{today_str()}.json"
    if not today_file.exists():
        print("⚠️ 今日のデータがありません。先にスクレイピングを実行してください。")
        return None

    prev_file = get_latest_data_file()
    if prev_file is None:
        print("ℹ️ 比較対象の過去データがありません（初回実行）。明日以降に差分が確認できます。")
        return None

    print(f"📊 比較中: {prev_file.name} vs {today_file.name}")
    old_data = load_data(prev_file)
    new_data = load_data(today_file)

    diff = compare_data(old_data, new_data)
    report = generate_report(diff, old_data, new_data)

    # レポートをファイルに保存
    report_file = REPORT_DIR / f"report_{today_str()}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print(f"\n📁 レポート保存: {report_file}")
    return report


def main():
    setup_dirs()

    if "--init" in sys.argv:
        print("🚀 初回セットアップ - データ取得のみ")
        run_scrape(include_details=True)
        print("\n✅ 初期データ取得完了。明日同じスクリプトを実行すると差分レポートが生成されます。")

    elif "--report" in sys.argv:
        print("📊 レポートのみ表示")
        run_compare()

    elif "--list-only" in sys.argv:
        print("📋 一覧ページのみスクレイピング（詳細ページはスキップ）")
        run_scrape(include_details=False)
        run_compare()

    else:
        # 通常実行: スクレイピング → 比較
        print(f"🔄 JTB クーポン監視 - {today_str()}")
        print("-" * 40)
        run_scrape(include_details=True)
        print()
        run_compare()


if __name__ == "__main__":
    main()
