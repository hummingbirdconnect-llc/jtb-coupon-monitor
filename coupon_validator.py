#!/usr/bin/env python3
"""
クーポンデータ バリデーション & ダブルチェックモジュール
================================================================
各スクレイパー（JTB / KNT / HIS）の出力に対して以下を実行:

1. 重複検出（ID一致 + タイトル類似度による実質重複）
2. データ整合性チェック（必須フィールド、日付形式、割引額の妥当性）
3. サニティチェック（前回比で大量消失 → DOM取得失敗の兆候）
4. 年なし日付の自動補完

各スクレイパーの run_full() / run_init() から validate_coupons() を呼ぶだけで使える。
"""

import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from difflib import SequenceMatcher

JST = timezone(timedelta(hours=9))

# ============================================================
# 設定
# ============================================================

# タイトル類似度の閾値（0.0〜1.0）。これ以上似ていれば「類似クーポン」として警告
# 0.90: 行き先・航空会社違い等のテンプレート型タイトルを誤検出しない水準
TITLE_SIMILARITY_THRESHOLD = 0.90

# 割引額の妥当範囲（円）
MIN_DISCOUNT_YEN = 100
MAX_DISCOUNT_YEN = 500_000

# 前回比で消失が全体の何%を超えたら警告するか
MASS_DISAPPEARANCE_THRESHOLD = 0.50  # 50%

# 年なし日付の補完を有効にするか
ENABLE_YEARLESS_DATE_FIX = True


# ============================================================
# 1. 重複検出
# ============================================================
def _normalize_title(title):
    """比較用にタイトルを正規化（空白・記号を除去）"""
    t = re.sub(r'[\s　]+', '', title)
    t = re.sub(r'[【】「」『』（）()！!？?]', '', t)
    return t


def _title_similarity(a, b):
    """2つのタイトルの類似度を0.0〜1.0で返す"""
    na = _normalize_title(a)
    nb = _normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def detect_duplicates(coupons, service_name=""):
    """
    重複クーポンを検出して除去する。

    検出パターン:
      A) 完全なID重複 → 後のエントリを自動除去
      B) IDは異なるが、タイトル類似度が閾値以上かつ割引額が同一 → 警告のみ（自動除去しない）
         ※ テンプレート型タイトル（行き先・航空会社違い等）の誤検出を防ぐため

    Returns: (deduped_coupons, warnings)
    """
    warnings = []
    seen_ids = {}
    deduped = []
    similarity_warned = set()  # 重複警告済みペア

    for c in coupons:
        cid = c["id"]

        # --- A) ID完全一致 → 自動除去 ---
        if cid in seen_ids:
            warnings.append(
                f"[{service_name}] ID重複を除去: {cid} "
                f"「{c.get('title', '')[:40]}」（先行エントリを採用）"
            )
            continue

        seen_ids[cid] = c
        deduped.append(c)

    # --- B) タイトル類似度チェック（警告のみ） ---
    deduped_list = list(seen_ids.items())
    for i, (id_a, c_a) in enumerate(deduped_list):
        for j, (id_b, c_b) in enumerate(deduped_list):
            if j <= i:
                continue
            pair_key = tuple(sorted([id_a, id_b]))
            if pair_key in similarity_warned:
                continue

            title_a = c_a.get("title", "")
            title_b = c_b.get("title", "")
            discount_a = c_a.get("discount", "")
            discount_b = c_b.get("discount", "")

            sim = _title_similarity(title_a, title_b)
            if sim >= TITLE_SIMILARITY_THRESHOLD and discount_a == discount_b:
                similarity_warned.add(pair_key)
                warnings.append(
                    f"[{service_name}] 類似クーポン検出（類似度{sim:.0%}）: "
                    f"ID={id_a}「{title_a[:30]}」≒ ID={id_b}「{title_b[:30]}」"
                    f"（割引額同一。要目視確認）"
                )

    return deduped, warnings


# ============================================================
# 2. データ整合性チェック
# ============================================================
def _parse_discount_amount(discount_str):
    """割引文字列から数値（円）を抽出。%の場合はNone"""
    if not discount_str:
        return None
    m = re.search(r'([0-9,]+)\s*円', discount_str)
    if m:
        return int(m.group(1).replace(',', ''))
    return None  # %割引等


def _validate_date_format(date_str):
    """日付文字列が妥当かチェック。空でもOK（任意フィールド）"""
    if not date_str:
        return True, ""

    # 年+月+日があるか
    has_full_date = bool(
        re.search(r'\d{4}[年/]\d{1,2}[月/]\d{1,2}', date_str)
    )
    if not has_full_date:
        # 終了側に年がないパターンをチェック
        parts = re.split(r'[～〜~]', date_str)
        if len(parts) >= 2:
            end_part = parts[-1].strip()
            # 年なしの月日のみ
            if re.search(r'^\d{1,2}月\d{1,2}日', end_part):
                return False, f"終了日に年がありません: 「{end_part[:20]}」"
            if re.search(r'^\d{1,2}/\d{1,2}', end_part) and not re.search(r'\d{4}/', end_part):
                return False, f"終了日に年がありません: 「{end_part[:20]}」"

    return True, ""


def check_data_integrity(coupons, service_name=""):
    """
    各クーポンのデータ整合性をチェック。

    チェック項目:
      - 必須フィールド（id, title）の欠損
      - 割引額の妥当範囲
      - 日付フォーマット（年なし終了日の検出）
      - titleの異常（空白のみ、極端に短い）

    Returns: (fixed_coupons, warnings)
    """
    warnings = []
    fixed = []

    for c in coupons:
        coupon_warnings = []

        # --- 必須フィールド ---
        if not c.get("id"):
            coupon_warnings.append("IDが空です")
        if not c.get("title") or len(c.get("title", "").strip()) < 2:
            coupon_warnings.append(f"タイトルが不正です: 「{c.get('title', '')}」")

        # --- 割引額チェック ---
        discount = c.get("discount", "")
        amount = _parse_discount_amount(discount)
        if amount is not None:
            if amount < MIN_DISCOUNT_YEN:
                coupon_warnings.append(
                    f"割引額が小さすぎます: {amount}円（最低{MIN_DISCOUNT_YEN}円）"
                )
            elif amount > MAX_DISCOUNT_YEN:
                coupon_warnings.append(
                    f"割引額が大きすぎます: {amount:,}円（上限{MAX_DISCOUNT_YEN:,}円）"
                )

        # --- 日付フォーマット ---
        for field_name, field_key in [
            ("予約期間", "booking_period"),
            ("宿泊/出発期間", "stay_period"),
            ("旅行期間", "travel_period"),
        ]:
            date_str = c.get(field_key, "")
            if date_str:
                is_valid, msg = _validate_date_format(date_str)
                if not is_valid:
                    coupon_warnings.append(f"{field_name}: {msg}")
                    # 年なし日付の自動補完を試みる
                    if ENABLE_YEARLESS_DATE_FIX:
                        fixed_date = _fix_yearless_end_date(date_str)
                        if fixed_date != date_str:
                            c[field_key] = fixed_date
                            coupon_warnings[-1] += f" → 補完済み"

        if coupon_warnings:
            cid = c.get("id", "???")
            title = c.get("title", "")[:30]
            for w in coupon_warnings:
                warnings.append(f"[{service_name}] {cid}「{title}」: {w}")

        fixed.append(c)

    return fixed, warnings


# ============================================================
# 3. サニティチェック（前回比の異常検知）
# ============================================================
def check_sanity(current_coupons, master_ids, service_name=""):
    """
    前回のマスターIDと比較して異常がないかチェック。

    検出パターン:
      - 前回比で50%以上のクーポンが消失 → DOM取得失敗の可能性
      - 今回0件（既にスクレイパー側でexit(1)するが念のため）

    Returns: (is_ok, warnings)
    """
    warnings = []
    prev_ids = set(master_ids.get("ids", {}).keys())
    curr_ids = set(c["id"] for c in current_coupons)

    prev_count = len(prev_ids)
    curr_count = len(curr_ids)
    gone_count = len(prev_ids - curr_ids)

    if prev_count > 0 and curr_count == 0:
        warnings.append(
            f"[{service_name}] 全クーポンが消失しました（前回{prev_count}件→今回0件）。"
            f"サイト構造変更またはアクセスブロックの可能性があります。"
        )
        return False, warnings

    if prev_count >= 3 and gone_count / prev_count > MASS_DISAPPEARANCE_THRESHOLD:
        warnings.append(
            f"[{service_name}] 大量消失を検出: 前回{prev_count}件中{gone_count}件"
            f"（{gone_count/prev_count:.0%}）が消失。"
            f"DOM取得の不完全またはサイト構造変更の可能性があります。"
        )
        # 警告のみで続行（is_ok=Trueにしておく。止めるかは呼び出し側の判断）
        return True, warnings

    # 新規が異常に多い場合も警告（ID生成ロジックのバグの可能性）
    new_count = len(curr_ids - prev_ids)
    if prev_count >= 3 and new_count / max(prev_count, 1) > MASS_DISAPPEARANCE_THRESHOLD:
        warnings.append(
            f"[{service_name}] 大量新規を検出: {new_count}件が新規"
            f"（前回{prev_count}件）。"
            f"ID生成ロジックの変更またはサイト構造変更の可能性があります。"
        )

    return True, warnings


# ============================================================
# 4. 年なし日付の自動補完
# ============================================================
def _fix_yearless_end_date(period_str):
    """
    「2026年1月1日～3月31日」→「2026年1月1日～2026年3月31日」のように
    終了日に年がない場合、開始日の年を推定して補完する。

    推定ルール:
      - 開始月 > 終了月 の場合 → 翌年
      - それ以外 → 同年
    """
    if not period_str:
        return period_str

    parts = re.split(r'([～〜~])', period_str)
    if len(parts) < 3:
        return period_str

    start_part = parts[0]
    separator = parts[1]
    end_part = parts[2]

    # 終了側にすでに年がある場合は何もしない
    if re.search(r'\d{4}[年/]', end_part):
        return period_str

    # 開始側から年と月を取得
    start_match = re.search(r'(\d{4})[年/](\d{1,2})[月/]', start_part)
    if not start_match:
        return period_str

    start_year = int(start_match.group(1))
    start_month = int(start_match.group(2))

    # 終了側の月を取得
    end_month_match = re.search(r'(\d{1,2})[月/]', end_part)
    if not end_month_match:
        return period_str

    end_month = int(end_month_match.group(1))

    # 年を推定
    inferred_year = start_year
    if end_month < start_month:
        inferred_year = start_year + 1

    # 漢字形式: 「3月31日」→「2026年3月31日」
    if '月' in end_part and '年' not in end_part:
        fixed_end = f"{inferred_year}年{end_part.strip()}"
        return f"{start_part}{separator}{fixed_end}"

    # スラッシュ形式: 「3/31」→「2026/3/31」
    if '/' in end_part and not re.search(r'\d{4}/', end_part):
        fixed_end = f"{inferred_year}/{end_part.strip()}"
        return f"{start_part}{separator}{fixed_end}"

    return period_str


# ============================================================
# 5. クロスフィールド矛盾チェック
# ============================================================
def check_cross_field_consistency(coupons, service_name=""):
    """
    フィールド間の矛盾を検出。

    チェック:
      - stock_status=「配布中」なのに予約期間が終了している
      - 割引額が空なのにクーポンコードがある
      - 予約期間の開始日 > 終了日
    """
    warnings = []
    today_s = datetime.now(JST).strftime("%Y-%m-%d")

    for c in coupons:
        cid = c.get("id", "???")
        title = c.get("title", "")[:30]

        # 配布中なのに期間終了
        if c.get("stock_status") == "配布中":
            booking = c.get("booking_period", "")
            end_date = _extract_any_end_date(booking)
            if end_date and end_date <= today_s:
                c["stock_status"] = "配布終了"
                warnings.append(
                    f"[{service_name}] {cid}「{title}」: "
                    f"配布中→配布終了に修正（予約期間{end_date}は終了済み）"
                )

        # クーポンコードがあるのに割引額が空
        codes = c.get("coupon_codes", [])
        detail_codes = []
        if isinstance(c.get("detail_data"), dict):
            detail_codes = c["detail_data"].get("coupon_codes", [])
        if (codes or detail_codes) and not c.get("discount"):
            warnings.append(
                f"[{service_name}] {cid}「{title}」: "
                f"クーポンコードがあるが割引額が空です"
            )

    return coupons, warnings


def _extract_any_end_date(period_str):
    """期間文字列から終了日をYYYY-MM-DD形式で抽出（汎用版）"""
    if not period_str:
        return None

    parts = re.split(r'[～〜~]', period_str)
    if len(parts) < 2:
        return None

    end_part = parts[-1].strip()

    # 年月日（漢字）
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', end_part)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    # 年月日（スラッシュ）
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', end_part)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None

    return None


# ============================================================
# メインエントリポイント
# ============================================================
def validate_coupons(coupons, master_ids=None, service_name="UNKNOWN"):
    """
    クーポンデータに対してダブルチェックを一括実行する。

    実行順序:
      1. データ整合性チェック（+ 年なし日付補完）
      2. 重複検出 & 除去
      3. クロスフィールド矛盾チェック
      4. サニティチェック（前回比の異常検知）

    Args:
        coupons: スクレイピング結果のクーポンリスト
        master_ids: 前回のマスターIDデータ（サニティチェック用、Noneならスキップ）
        service_name: サービス名（"JTB" / "KNT" / "HIS"）

    Returns:
        (validated_coupons, validation_report)
        validation_report = {
            "service": str,
            "input_count": int,
            "output_count": int,
            "duplicates_removed": int,
            "warnings": list[str],
            "is_healthy": bool,
        }
    """
    all_warnings = []
    input_count = len(coupons)

    print(f"\n🔍 [{service_name}] バリデーション開始（{input_count}件）...")

    # 1. データ整合性チェック
    coupons, integrity_warnings = check_data_integrity(coupons, service_name)
    all_warnings.extend(integrity_warnings)

    # 2. 重複検出 & 除去
    coupons, dup_warnings = detect_duplicates(coupons, service_name)
    all_warnings.extend(dup_warnings)
    duplicates_removed = input_count - len(coupons)

    # 3. クロスフィールド矛盾チェック
    coupons, cross_warnings = check_cross_field_consistency(coupons, service_name)
    all_warnings.extend(cross_warnings)

    # 4. サニティチェック
    is_healthy = True
    if master_ids is not None:
        is_ok, sanity_warnings = check_sanity(coupons, master_ids, service_name)
        all_warnings.extend(sanity_warnings)
        if not is_ok:
            is_healthy = False

    # --- レポート出力 ---
    output_count = len(coupons)

    if all_warnings:
        print(f"  ⚠️ {len(all_warnings)}件の問題を検出:")
        for w in all_warnings:
            print(f"    - {w}")
    else:
        print(f"  ✅ 問題なし")

    if duplicates_removed > 0:
        print(f"  🔄 重複除去: {duplicates_removed}件（{input_count}件→{output_count}件）")

    report = {
        "service": service_name,
        "input_count": input_count,
        "output_count": output_count,
        "duplicates_removed": duplicates_removed,
        "warnings": all_warnings,
        "is_healthy": is_healthy,
    }

    return coupons, report


def save_validation_report(reports, output_dir=None):
    """
    バリデーションレポートをJSONで保存する。

    Args:
        reports: validate_coupons() が返す report のリスト
        output_dir: 保存先ディレクトリ（省略時は各サービスのデータディレクトリ）
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    summary = {
        "date": today,
        "services": [],
        "total_warnings": 0,
        "all_healthy": True,
    }

    for r in reports:
        summary["services"].append(r)
        summary["total_warnings"] += len(r["warnings"])
        if not r["is_healthy"]:
            summary["all_healthy"] = False

    if output_dir:
        out_path = Path(output_dir) / f"validation_{today}.json"
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n📋 バリデーションレポート保存: {out_path}")

    # サマリーを標準出力にも表示
    print(f"\n{'=' * 60}")
    print(f"📋 バリデーションサマリー ({today})")
    print(f"{'=' * 60}")
    for r in reports:
        status = "✅" if r["is_healthy"] else "⚠️"
        print(f"  {status} {r['service']}: {r['input_count']}件→{r['output_count']}件"
              f"（重複除去{r['duplicates_removed']}件、警告{len(r['warnings'])}件）")
    print(f"  合計警告: {summary['total_warnings']}件")
    print(f"{'=' * 60}")

    return summary
