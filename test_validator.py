#!/usr/bin/env python3
"""coupon_validator.py のユニットテスト"""
import sys
sys.path.insert(0, ".")

from coupon_validator import (
    detect_duplicates,
    check_data_integrity,
    check_sanity,
    check_cross_field_consistency,
    validate_coupons,
    _fix_yearless_end_date,
    _title_similarity,
    _extract_any_end_date,
)


def test_title_similarity():
    """タイトル類似度の計算"""
    # 同一タイトル
    assert _title_similarity("沖縄ホテル割引クーポン", "沖縄ホテル割引クーポン") == 1.0
    # 微妙に違う
    sim = _title_similarity("沖縄ホテル割引クーポン", "沖縄ホテル 割引クーポン")
    assert sim > 0.9, f"Expected > 0.9, got {sim}"
    # 全然違う
    sim = _title_similarity("沖縄ホテル", "北海道ツアー")
    assert sim < 0.5, f"Expected < 0.5, got {sim}"
    print("  ✅ test_title_similarity PASSED")


def test_detect_duplicates_by_id():
    """ID重複の検出"""
    coupons = [
        {"id": "A001", "title": "クーポンA", "discount": "1,000円引"},
        {"id": "A001", "title": "クーポンA", "discount": "1,000円引"},
        {"id": "B001", "title": "クーポンB", "discount": "2,000円引"},
    ]
    deduped, warnings = detect_duplicates(coupons, "TEST")
    assert len(deduped) == 2, f"Expected 2, got {len(deduped)}"
    assert len(warnings) == 1
    assert "ID重複" in warnings[0]
    print("  ✅ test_detect_duplicates_by_id PASSED")


def test_detect_duplicates_by_similarity():
    """タイトル類似度による類似クーポンの警告（自動除去ではなく警告のみ）"""
    coupons = [
        {"id": "A001", "title": "沖縄ホテル最大5,000円割引クーポン", "discount": "最大5,000円引"},
        {"id": "B001", "title": "沖縄ホテル最大5,000円割引クーポン配布中", "discount": "最大5,000円引"},  # 類似度90%超
        {"id": "C001", "title": "北海道ツアー割引", "discount": "10,000円引"},  # 全く別
    ]
    deduped, warnings = detect_duplicates(coupons, "TEST")
    # 類似チェックは警告のみ（自動除去しない）→ 3件すべて残る
    assert len(deduped) == 3, f"Expected 3, got {len(deduped)}"
    assert any("類似クーポン検出" in w for w in warnings)
    print("  ✅ test_detect_duplicates_by_similarity PASSED")


def test_check_data_integrity_missing_title():
    """タイトル欠損の検出"""
    coupons = [
        {"id": "A001", "title": "", "discount": "1,000円引"},
        {"id": "B001", "title": "正常なクーポン", "discount": "2,000円引"},
    ]
    fixed, warnings = check_data_integrity(coupons, "TEST")
    assert len(warnings) == 1
    assert "タイトルが不正" in warnings[0]
    print("  ✅ test_check_data_integrity_missing_title PASSED")


def test_check_data_integrity_discount_range():
    """割引額の範囲チェック"""
    coupons = [
        {"id": "A001", "title": "テスト", "discount": "50円引"},      # 小さすぎ
        {"id": "B001", "title": "テスト2", "discount": "999,999円引"}, # 大きすぎ
        {"id": "C001", "title": "テスト3", "discount": "5,000円引"},   # 正常
    ]
    fixed, warnings = check_data_integrity(coupons, "TEST")
    assert len(warnings) == 2, f"Expected 2, got {len(warnings)}: {warnings}"
    assert any("小さすぎ" in w for w in warnings)
    assert any("大きすぎ" in w for w in warnings)
    print("  ✅ test_check_data_integrity_discount_range PASSED")


def test_fix_yearless_end_date():
    """年なし日付の補完"""
    # 漢字形式
    result = _fix_yearless_end_date("2026年1月1日～3月31日")
    assert "2026年3月31日" in result, f"Got: {result}"

    # 年跨ぎ（12月→3月 = 翌年）
    result = _fix_yearless_end_date("2025年12月15日～3月28日")
    assert "2026年3月28日" in result, f"Got: {result}"

    # 同年（1月→2月）
    result = _fix_yearless_end_date("2026年1月4日～2月28日")
    assert "2026年2月28日" in result, f"Got: {result}"

    # すでに年がある場合は変更しない
    result = _fix_yearless_end_date("2026年1月1日～2026年3月31日")
    assert result == "2026年1月1日～2026年3月31日", f"Got: {result}"

    print("  ✅ test_fix_yearless_end_date PASSED")


def test_check_sanity_mass_disappearance():
    """大量消失の検出"""
    master_ids = {
        "ids": {
            "A001": {"title": "A"},
            "A002": {"title": "B"},
            "A003": {"title": "C"},
            "A004": {"title": "D"},
            "A005": {"title": "E"},
            "A006": {"title": "F"},
        }
    }
    # 6件中5件が消失
    current = [{"id": "A001", "title": "A"}]
    is_ok, warnings = check_sanity(current, master_ids, "TEST")
    assert len(warnings) >= 1
    assert "大量消失" in warnings[0]
    print("  ✅ test_check_sanity_mass_disappearance PASSED")


def test_check_sanity_all_gone():
    """全件消失の検出"""
    master_ids = {"ids": {"A001": {}, "A002": {}, "A003": {}}}
    current = []
    is_ok, warnings = check_sanity(current, master_ids, "TEST")
    assert not is_ok
    assert "全クーポンが消失" in warnings[0]
    print("  ✅ test_check_sanity_all_gone PASSED")


def test_cross_field_expired_but_active():
    """配布中なのに期間終了済みの矛盾を検出・修正"""
    coupons = [{
        "id": "A001",
        "title": "テストクーポン",
        "discount": "1,000円引",
        "stock_status": "配布中",
        "booking_period": "2024年1月1日～2024年12月31日",  # 終了済み
    }]
    fixed, warnings = check_cross_field_consistency(coupons, "TEST")
    assert fixed[0]["stock_status"] == "配布終了"
    assert any("配布中→配布終了" in w for w in warnings)
    print("  ✅ test_cross_field_expired_but_active PASSED")


def test_validate_coupons_integration():
    """validate_coupons の統合テスト"""
    coupons = [
        {"id": "A001", "title": "正常なクーポン", "discount": "5,000円引",
         "stock_status": "配布中", "booking_period": "2026年1月1日～2026年12月31日"},
        {"id": "A001", "title": "正常なクーポン", "discount": "5,000円引",
         "stock_status": "配布中", "booking_period": "2026年1月1日～2026年12月31日"},  # 重複
    ]
    validated, report = validate_coupons(coupons, service_name="TEST")
    assert len(validated) == 1
    assert report["duplicates_removed"] == 1
    assert report["is_healthy"] is True
    print("  ✅ test_validate_coupons_integration PASSED")


def test_extract_any_end_date():
    """汎用終了日抽出"""
    assert _extract_any_end_date("2026年1月1日～2026年3月31日") == "2026-03-31"
    assert _extract_any_end_date("2025/10/1 ～ 2026/2/28") == "2026-02-28"
    assert _extract_any_end_date("") is None
    assert _extract_any_end_date("単発テキスト") is None
    print("  ✅ test_extract_any_end_date PASSED")


if __name__ == "__main__":
    print("\n🧪 coupon_validator テスト開始\n")
    test_title_similarity()
    test_detect_duplicates_by_id()
    test_detect_duplicates_by_similarity()
    test_check_data_integrity_missing_title()
    test_check_data_integrity_discount_range()
    test_fix_yearless_end_date()
    test_check_sanity_mass_disappearance()
    test_check_sanity_all_gone()
    test_cross_field_expired_but_active()
    test_validate_coupons_integration()
    test_extract_any_end_date()
    print("\n✅ 全テスト PASSED")
