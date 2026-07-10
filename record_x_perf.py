#!/usr/bin/env python3
"""
X投稿パターン実績記録・淘汰レポート
================================================================
投稿後のインプレッション等を記録し、パターン別の成績を集計する。
記録が溜まると generate_x_threads.py が成績の良いパターンを
出やすくする（実績比0.5〜2.0倍の重み付け）。

使い方:
  # 実績を記録（パターンIDは使用ログから自動判定）
  python record_x_perf.py add --date 2026-07-10 --site yakushimafan --tree 1 \
      --imp 3500 [--likes 8] [--clicks 2]

  # サイト略称も可: yf=yakushimafan / wt=welltrip / tb=tripbooking
  python record_x_perf.py add --date 2026-07-10 --site yf --tree 1 --imp 3500

  # パターン別成績レポート＋引退候補の提案
  python record_x_perf.py report
================================================================
"""

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
USAGE_LOG = BASE_DIR / "tweets_output" / "x_pattern_usage.json"
PERF_LOG = BASE_DIR / "tweets_output" / "x_perf_log.json"
PATTERNS_CONFIG = BASE_DIR / "config" / "x_thread_patterns.json"

SITE_ALIASES = {
    "yf": "yakushimafan",
    "wt": "welltrip",
    "tb": "tripbooking",
    "yakushimafan": "yakushimafan",
    "welltrip": "welltrip",
    "tripbooking": "tripbooking",
}

# 引退候補・好調の判定しきい値
RETIRE_MIN_RECORDS = 3   # この記録数以上で判定対象
RETIRE_SCORE = 0.6       # 正規化スコアがこれ未満なら引退候補
STAR_SCORE = 1.3         # これ以上なら好調


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
    return default


def cmd_add(args):
    site = SITE_ALIASES.get(args.site.lower())
    if not site:
        raise SystemExit(f"不明なサイト: {args.site}（yakushimafan/welltrip/tripbooking か yf/wt/tb）")
    usage = load_json(USAGE_LOG, [])
    hit = next((u for u in usage
                if u.get("date") == args.date and u.get("site") == site
                and u.get("tree") == args.tree), None)
    if not hit:
        print(f"⚠️ 使用ログに {args.date} {site} ツリー{args.tree} が見つかりません。")
        print("   パターン不明のまま記録します（集計時は pattern=unknown 扱い）")
    entry = {
        "date": args.date,
        "site": site,
        "tree": args.tree,
        "pattern": hit.get("pattern") if hit else "unknown",
        "ota": hit.get("ota") if hit else "",
        "title": hit.get("title") if hit else "",
        "impressions": args.imp,
        "likes": args.likes,
        "link_clicks": args.clicks,
        "recorded_at": datetime.now(JST).strftime("%Y-%m-%d"),
    }
    perf = load_json(PERF_LOG, [])
    # 同一 date+site+tree は上書き（記録し直し対応）
    perf = [p for p in perf
            if not (p.get("date") == args.date and p.get("site") == site
                    and p.get("tree") == args.tree)]
    perf.append(entry)
    PERF_LOG.write_text(json.dumps(perf, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 記録しました: {args.date} {site} ツリー{args.tree} "
          f"パターン={entry['pattern']} imp={args.imp:,}")
    print(f"   累計記録: {len(perf)}件 → 次回生成からこの実績が選択率に反映されます")


def aggregate_stats(perf, usage, patterns_conf):
    """サイト別×パターン別の成績を集計する（CLI・スプレッドシート連携で共用）。
    返り値: (stats, retire_candidates)
      stats = {site: {"records": n, "median": m, "rows": [(pattern_id, name, used, n, score, verdict), ...]}}
    """
    pattern_names = {p["id"]: p["name"] for p in patterns_conf}
    pattern_status = {p["id"]: p.get("status", "active") for p in patterns_conf}
    stats = {}
    retire_candidates = []
    for site in sorted({p["site"] for p in perf}):
        recs = [p for p in perf if p["site"] == site and p.get("impressions")]
        if not recs:
            continue
        imps = sorted(r["impressions"] for r in recs)
        median = imps[len(imps) // 2] or 1
        by_pattern = {}
        for r in recs:
            by_pattern.setdefault(r.get("pattern", "unknown"), []).append(r["impressions"] / median)
        used_count = {}
        for u in usage:
            if u.get("site") == site:
                used_count[u["pattern"]] = used_count.get(u["pattern"], 0) + 1
        rows = []
        for pat, ratios in sorted(by_pattern.items(), key=lambda x: -sum(x[1]) / len(x[1])):
            score = sum(ratios) / len(ratios)
            n = len(ratios)
            name = pattern_names.get(pat, pat)
            status = pattern_status.get(pat, "-")
            if status == "retired":
                verdict = "（引退済み）"
            elif n >= RETIRE_MIN_RECORDS and score < RETIRE_SCORE:
                verdict = "🔴 引退候補"
                retire_candidates.append((site, pat, name, score, n))
            elif score >= STAR_SCORE:
                verdict = "⭐ 好調"
            else:
                verdict = "継続"
            rows.append((pat, name, used_count.get(pat, 0), n, score, verdict))
        stats[site] = {"records": len(recs), "median": median, "rows": rows}
    return stats, retire_candidates


def cmd_report(_args):
    perf = load_json(PERF_LOG, [])
    usage = load_json(USAGE_LOG, [])
    patterns_conf = load_json(PATTERNS_CONFIG, {}).get("patterns", [])

    if not perf:
        print("実績記録がまだありません。")
        print("記録例: python record_x_perf.py add --date 2026-07-11 --site yf --tree 1 --imp 3500")
        if usage:
            print(f"\n（パターン使用ログは{len(usage)}件あります — 投稿から2〜3日後の数字が安定した頃に記録するのがおすすめ）")
        return

    stats, retire_candidates = aggregate_stats(perf, usage, patterns_conf)
    for site, s in stats.items():
        print(f"\n## {site}（記録{s['records']}件・インプレ中央値 {s['median']:,}）")
        print(f"{'パターン':<18}{'使用':>4}{'記録':>4}{'平均スコア':>8}  判定")
        for _pat, name, used, n, score, verdict in s["rows"]:
            print(f"{name:<18}{used:>4}{n:>4}{score:>8.2f}  {verdict}")

    if retire_candidates:
        print("\n## 🔴 引退候補（自動では引退させません — ご判断ください）")
        for site, pat, name, score, n in retire_candidates:
            print(f"- {site} の「{name}」: 記録{n}件・平均スコア{score:.2f}")
        print("引退させる場合: config/x_thread_patterns.json の該当パターンの "
              "\"status\" を \"retired\" に変更（サイト単位で外す場合は \"sites\" から削除）")
    else:
        print("\n引退候補はありません（記録3件以上・平均スコア0.6未満で候補になります）")


def main():
    parser = argparse.ArgumentParser(description="X投稿パターン実績の記録と淘汰レポート")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="実績を記録")
    p_add.add_argument("--date", required=True, help="投稿日 YYYY-MM-DD")
    p_add.add_argument("--site", required=True, help="yakushimafan/welltrip/tripbooking（yf/wt/tb可）")
    p_add.add_argument("--tree", type=int, required=True, help="ツリー番号 1-3")
    p_add.add_argument("--imp", type=int, required=True, help="インプレッション数")
    p_add.add_argument("--likes", type=int, default=None, help="いいね数（任意）")
    p_add.add_argument("--clicks", type=int, default=None, help="リンククリック数（任意）")
    p_add.set_defaults(func=cmd_add)

    p_report = sub.add_parser("report", help="パターン別成績レポート")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
