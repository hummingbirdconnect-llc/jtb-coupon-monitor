#!/usr/bin/env python3
"""
ツイート文面自動生成スクリプト
================================================================
change_log.json から当日の新規/配布再開クーポンを抽出し、
日次スナップショットから詳細情報を補完してツイート文面を生成する。

出力:
  tweets_output/tweets_YYYY-MM-DD.json  (構造化データ)
  tweets_output/tweets_YYYY-MM-DD.txt   (コピペ用テキスト)

使い方:
  python generate_tweets.py              # 通常実行（当日分）
  python generate_tweets.py --date 2026-02-20  # 指定日の分を生成
  python generate_tweets.py --dry-run    # ファイル保存せずstdout出力
================================================================
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "tweets_output"

# OTAごとの設定
OTA_CONFIG = {
    "JTB": {
        "data_dir": BASE_DIR / "jtb_coupon_data",
        "hashtags": "#JTB #クーポン #旅行",
        "base_url": "https://www.jtb.co.jp",
    },
    "KNT": {
        "data_dir": BASE_DIR / "knt_coupon_data",
        "hashtags": "#近畿日本ツーリスト #KNT #クーポン #旅行",
        "base_url": "https://www.knt.co.jp",
    },
    "HIS": {
        "data_dir": BASE_DIR / "his_coupon_data",
        "hashtags": "#HIS #クーポン #旅行",
        "base_url": "https://www.his-j.com",
    },
}

# ツイート対象の変動タイプ
TARGET_TYPES = {"🆕 新規", "🟢 配布再開"}

MAX_TWEET_LENGTH = 280


def load_json(filepath):
    """JSONファイルを読み込む。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  Warning: {filepath} の読み込みに失敗: {e}")
        return None


def find_today_snapshot(data_dir, date_str):
    """当日のスナップショットファイルを探す。"""
    snapshot = data_dir / f"coupons_{date_str}.json"
    if snapshot.exists():
        return load_json(snapshot)
    return None


def get_new_coupons(data_dir, target_date):
    """change_log.json から対象日の新規/配布再開クーポンIDを取得。"""
    change_log = load_json(data_dir / "change_log.json")
    if not change_log:
        return []

    return [
        entry for entry in change_log
        if entry.get("date") == target_date and entry.get("type") in TARGET_TYPES
    ]


def enrich_with_snapshot(log_entries, snapshot_data, ota_name):
    """スナップショットから詳細情報を補完する。"""
    if not snapshot_data:
        return log_entries

    # スナップショットをID辞書に
    snap_by_id = {c.get("id"): c for c in snapshot_data}

    enriched = []
    for entry in log_entries:
        coupon_id = entry.get("id")
        snap = snap_by_id.get(coupon_id, {})

        enriched.append({
            "id": coupon_id,
            "ota": ota_name,
            "type": entry.get("type"),
            "title": entry.get("title", ""),
            "category": entry.get("category", ""),
            "discount": snap.get("discount") or entry.get("discount", ""),
            "area": snap.get("area") or entry.get("area", ""),
            "detail_url": snap.get("detail_url", ""),
            "booking_period": snap.get("booking_period", ""),
            "stay_period": snap.get("stay_period") or snap.get("travel_period", ""),
        })

    return enriched


def truncate(text, max_len):
    """テキストを最大長で切り詰める。"""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def format_tweet(coupon, config):
    """1クーポン分のツイート文面を生成する。"""
    ota = coupon["ota"]
    title = coupon["title"]
    discount = coupon["discount"]
    area = coupon["area"]
    url = coupon["detail_url"]
    hashtags = config["hashtags"]

    # 配布再開かどうか
    is_restock = coupon["type"] == "🟢 配布再開"
    action = "配布再開" if is_restock else "新クーポン配布開始"

    # ツイート組み立て
    lines = [f"【{ota}】{action}"]
    lines.append(f"「{truncate(title, 60)}」")

    if discount:
        lines.append(f"割引: {discount}")
    if area:
        lines.append(f"エリア: {area}")
    if url:
        lines.append(f"詳細→ {url}")

    lines.append(hashtags)

    tweet = "\n".join(lines)

    # 280文字制限チェック（URLは23文字としてカウント）
    check_len = len(tweet)
    if url:
        check_len = check_len - len(url) + 23  # X は URL を 23 文字に短縮

    if check_len > MAX_TWEET_LENGTH:
        # タイトルを短縮してリトライ
        shorter_title = truncate(title, 40)
        lines[1] = f"「{shorter_title}」"
        tweet = "\n".join(lines)

    return tweet


def generate_all_tweets(target_date):
    """全OTAのツイートを生成する。"""
    all_tweets = []

    for ota_name, config in OTA_CONFIG.items():
        data_dir = config["data_dir"]

        if not data_dir.exists():
            print(f"  {ota_name}: データディレクトリなし、スキップ")
            continue

        # 新規/配布再開エントリを取得
        new_entries = get_new_coupons(data_dir, target_date)
        if not new_entries:
            print(f"  {ota_name}: 新規クーポンなし")
            continue

        print(f"  {ota_name}: {len(new_entries)}件の新規/配布再開を検出")

        # スナップショットで詳細補完
        snapshot = find_today_snapshot(data_dir, target_date)
        enriched = enrich_with_snapshot(new_entries, snapshot, ota_name)

        # ツイート生成
        for coupon in enriched:
            tweet_text = format_tweet(coupon, config)
            all_tweets.append({
                "ota": ota_name,
                "coupon_id": coupon["id"],
                "title": coupon["title"],
                "type": coupon["type"],
                "tweet": tweet_text,
            })

    return all_tweets


def save_tweets(tweets, target_date, dry_run=False):
    """ツイートをファイルに保存する。"""
    if not tweets:
        print("\nツイート対象なし。ファイル生成をスキップします。")
        return None, None

    if dry_run:
        print(f"\n--- DRY RUN ({len(tweets)}件) ---\n")
        for i, t in enumerate(tweets, 1):
            print(f"━━━ ツイート {i}/{len(tweets)} [{t['ota']}] ━━━")
            print(t["tweet"])
            print()
        return None, None

    OUTPUT_DIR.mkdir(exist_ok=True)

    # JSON 出力
    json_path = OUTPUT_DIR / f"tweets_{target_date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)
    print(f"\n  JSON: {json_path}")

    # テキスト出力（コピペ用）
    txt_path = OUTPUT_DIR / f"tweets_{target_date}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# ツイート文面 {target_date}\n")
        f.write(f"# 生成件数: {len(tweets)}\n\n")
        for i, t in enumerate(tweets, 1):
            f.write(f"━━━ {i}/{len(tweets)} [{t['ota']}] ━━━\n")
            f.write(t["tweet"])
            f.write("\n\n")
    print(f"  TXT:  {txt_path}")

    return json_path, txt_path


def main():
    dry_run = "--dry-run" in sys.argv

    # 日付指定オプション
    target_date = datetime.now(JST).strftime("%Y-%m-%d")
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            target_date = sys.argv[idx + 1]

    print("=" * 50)
    print("ツイート文面生成")
    print("=" * 50)
    print(f"対象日: {target_date}")

    tweets = generate_all_tweets(target_date)

    print(f"\n合計: {len(tweets)}件のツイートを生成")

    save_tweets(tweets, target_date, dry_run=dry_run)

    print("\n完了")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nError: ツイート生成でエラーが発生: {e}")
        import traceback
        traceback.print_exc()
        print("ワークフローを続行します。")
        sys.exit(0)
