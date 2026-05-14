#!/usr/bin/env python3
"""wp_coupon_updater.py / section_matcher.py の安全チェックテスト"""

import sys

sys.path.insert(0, ".")

from section_matcher import match_sections_to_coupons
from wp_coupon_updater import (
    filter_coupons_for_page,
    public_page_options,
    safety_check,
)


OLD_HTML = """
<!-- wp:heading {"level":2} -->
<h2>JTBクーポン一覧</h2>
<!-- /wp:heading -->
<!-- wp:heading {"level":3} -->
<h3>国内ツアー</h3>
<!-- /wp:heading -->
<!-- wp:table -->
<figure class="wp-block-table"><table><tbody>
<tr><th>旧A</th><td>1,000円引</td></tr>
<tr><th>旧B</th><td>2,000円引</td></tr>
</tbody></table></figure>
<!-- /wp:table -->
"""

NEW_HTML = OLD_HTML.replace(
    "<tbody>\n<tr><th>旧A</th><td>1,000円引</td></tr>\n<tr><th>旧B</th><td>2,000円引</td></tr>\n</tbody>",
    "<tbody><tr><th>新A</th><td>3,000円引</td></tr><tr><th>新B</th><td>4,000円引</td></tr></tbody>",
)


def test_public_options_are_safe_for_dashboard():
    """ダッシュボードへ秘密情報や内部設定を埋め込まない。"""
    options = public_page_options()
    pages = options["sites"]["yakushimafan"]["pages"]
    assert pages
    for page in pages:
        assert set(page.keys()) == {"ota", "slug", "label", "url"}
    print("  ✅ test_public_options_are_safe_for_dashboard PASSED")


def test_filter_coupons_for_page():
    """include/exclude でページ対象クーポンだけに絞れる。"""
    coupons = [
        {"title": "国内旅行クーポン", "category": "国内", "stock_status": "配布中"},
        {"title": "海外旅行クーポン", "category": "海外", "stock_status": "配布中"},
        {"title": "ホテルクーポン", "category": "宿泊", "stock_status": "配布中"},
    ]
    filtered, summary = filter_coupons_for_page(
        coupons,
        {"include_keywords": ["国内"], "exclude_keywords": ["海外"]},
    )
    assert [c["title"] for c in filtered] == ["国内旅行クーポン"]
    assert summary["input"] == 3
    assert summary["output"] == 1
    print("  ✅ test_filter_coupons_for_page PASSED")


def test_safety_allows_tbody_only_change():
    """tbodyだけの差し替えは通す。"""
    result = safety_check(OLD_HTML, NEW_HTML, 1)
    assert result["passed"], result
    assert result["old_rows"] == 2
    assert result["new_rows"] == 2
    print("  ✅ test_safety_allows_tbody_only_change PASSED")


def test_safety_blocks_no_replacement():
    """差し替え対象テーブルがない場合は止める。"""
    result = safety_check(OLD_HTML, OLD_HTML, 0)
    assert not result["passed"]
    assert "差し替え対象" in result["reason"]
    print("  ✅ test_safety_blocks_no_replacement PASSED")


def test_safety_blocks_outside_tbody_change():
    """見出しや本文が変わる場合は止める。"""
    changed = NEW_HTML.replace("JTBクーポン一覧", "JTB割引一覧")
    result = safety_check(OLD_HTML, changed, 1)
    assert not result["passed"]
    assert "tbody以外" in result["reason"]
    print("  ✅ test_safety_blocks_outside_tbody_change PASSED")


def test_safety_blocks_affiliate_marker_loss():
    """表内のアフィリエイトURLや計測タグが消える場合は止める。"""
    old_html = OLD_HTML.replace(
        "旧A",
        '<a href="https://t.afi-b.com/visit.php?a=Q10113i-test&amp;p=X653459L">旧A</a>',
    )
    new_html = NEW_HTML.replace("新A", "新Aリンクなし")
    result = safety_check(old_html, new_html, 1)
    assert not result["passed"]
    assert "アフィリエイトURL" in result["reason"]
    print("  ✅ test_safety_blocks_affiliate_marker_loss PASSED")


def test_section_matcher_tuple_when_no_tables():
    """テーブルがない記事では tuple を返し、呼び出し側を壊さない。"""
    sections = [{"type": "heading", "level": 3, "text": "国内ツアー", "start": 0}]
    coupons = [{"title": "国内旅行", "category": "国内ツアー", "stock_status": "配布中"}]
    tables, unmatched = match_sections_to_coupons(sections, coupons, "jtb")
    assert tables == []
    assert len(unmatched) == 1
    print("  ✅ test_section_matcher_tuple_when_no_tables PASSED")


def test_section_matcher_jtb_type_match():
    """JTB/KNT は category/title/type/area を見てマッチできる。"""
    sections = [
        {"type": "heading", "level": 3, "text": "国内ツアー", "start": 0},
        {"type": "table", "parent_h3": "国内ツアー", "start": 10},
    ]
    coupons = [
        {
            "title": "宿泊に使えるクーポン",
            "category": "その他",
            "type": "国内ツアー",
            "stock_status": "配布中",
        }
    ]
    tables, unmatched = match_sections_to_coupons(sections, coupons, "jtb")
    assert len(tables[0]["matched_coupons"]) == 1
    assert unmatched == []
    print("  ✅ test_section_matcher_jtb_type_match PASSED")


if __name__ == "__main__":
    print("\n🧪 WPクーポン更新 安全チェックテスト開始\n")
    test_public_options_are_safe_for_dashboard()
    test_filter_coupons_for_page()
    test_safety_allows_tbody_only_change()
    test_safety_blocks_no_replacement()
    test_safety_blocks_outside_tbody_change()
    test_safety_blocks_affiliate_marker_loss()
    test_section_matcher_tuple_when_no_tables()
    test_section_matcher_jtb_type_match()
    print("\n✅ 全テスト PASSED")
