#!/usr/bin/env python3
"""his_coupon_monitor.py の終了判定テスト"""

import sys

sys.path.insert(0, ".")

from bs4 import BeautifulSoup

from his_coupon_monitor import (
    _assign_variant_id,
    _card_content_signature,
    _capture_card_png,
    _extract_campaign_from_end_text,
    _extract_explicit_ended_campaigns,
    _fetch_region_with_retry,
    _finalize_regional_observations,
    enrich_visible_coupons,
    make_coupon_id,
    parse_coupons,
)
from his_regions import (
    HIS_REGIONS,
    PRIMARY_REGION_CODE,
    area_matches_region,
    select_region_coupons,
)


def test_official_region_registry_is_complete():
    """公式の11地域と切替パラメータを重複なく保持する。"""
    assert len(HIS_REGIONS) == 11
    assert HIS_REGIONS[0].code == PRIMARY_REGION_CODE
    assert {region.param for region in HIS_REGIONS} == set(range(1, 12))
    assert len({region.code for region in HIS_REGIONS}) == 11
    assert area_matches_region("甲信越版", HIS_REGIONS[3])
    assert area_matches_region("甲信越（新潟）版", HIS_REGIONS[3])
    print("  ✅ test_official_region_registry_is_complete PASSED")


def test_primary_id_is_preserved_and_region_variant_is_split():
    """首都圏版の従来IDを維持し、内容差がある地域版だけ派生IDにする。"""
    base_id = make_coupon_id("同名クーポン", "国内旅行")
    variants = {}

    primary_id = _assign_variant_id(
        base_id, "a" * 64, "kanto", variants
    )
    common_id = _assign_variant_id(
        base_id, "a" * 64, "hokkaido", variants
    )
    regional_id = _assign_variant_id(
        base_id, "b" * 64, "hokkaido", variants
    )

    assert primary_id == base_id
    assert common_id == base_id
    assert regional_id == f"{base_id}-bbbbbb"
    print("  ✅ test_primary_id_is_preserved_and_region_variant_is_split PASSED")


def test_region_only_coupon_does_not_take_legacy_primary_id():
    """首都圏にない地域限定カードは従来IDを予約せず派生IDにする。"""
    base_id = make_coupon_id("北海道限定", "国内旅行")
    regional_id = _assign_variant_id(
        base_id, "c" * 64, "hokkaido", {}
    )
    assert regional_id == f"{base_id}-cccccc"
    print("  ✅ test_region_only_coupon_does_not_take_legacy_primary_id PASSED")


def test_semantic_card_signature_ignores_unrelated_attributes():
    """DOM属性差だけで共通カードを地域別クーポンへ分裂させない。"""
    first = BeautifulSoup(
        '<div class="content__wrapper" data-v-a="1"><p class="plan__dst">国内旅行</p>'
        '<h2 class="plan__title">共通クーポン</h2><a href="/same">詳細</a>'
        '<p class="coupon__code" data-name="CODE1">CODE1</p></div>',
        "html.parser",
    ).select_one(".content__wrapper")
    second = BeautifulSoup(
        '<div class="content__wrapper" data-v-b="2"><p class="plan__dst">国内旅行</p>'
        '<h2 class="plan__title">共通クーポン</h2><a href="/same">詳細</a>'
        '<p class="coupon__code" data-name="CODE1">CODE1</p></div>',
        "html.parser",
    ).select_one(".content__wrapper")
    assert _card_content_signature(first) == _card_content_signature(second)
    print("  ✅ test_semantic_card_signature_ignores_unrelated_attributes PASSED")


def test_regional_observations_are_finalized_and_selectable():
    """全国共通判定と地域別選択に使うメタデータを確定できる。"""
    all_codes = [region.code for region in HIS_REGIONS]
    all_labels = [region.label for region in HIS_REGIONS]
    observations = {
        "common": {
            "region_codes": all_codes.copy(),
            "region_labels": all_labels.copy(),
            "official_tabs": ["国内旅行"],
        },
        "limited": {
            "region_codes": ["kanto", "hokkaido"],
            "region_labels": ["首都圏版", "北海道版"],
            "official_tabs": ["国内旅行"],
        },
    }
    _finalize_regional_observations(observations, len(HIS_REGIONS))

    assert observations["common"]["official_area"] == "全国共通"
    assert observations["common"]["is_nationwide"] is True
    assert observations["limited"]["official_area"] == "首都圏版、北海道版"
    assert observations["limited"]["is_nationwide"] is False

    selected = select_region_coupons(
        [
            {"id": "legacy"},
            {"id": "common", "region_codes": all_codes},
            {"id": "hokkaido", "region_codes": ["hokkaido"]},
        ],
        "kanto",
    )
    assert [coupon["id"] for coupon in selected] == ["legacy", "common"]
    print("  ✅ test_regional_observations_are_finalized_and_selectable PASSED")


def test_parse_coupons_uses_injected_regional_variant_id():
    """地域差分用に付与したIDを再計算で失わない。"""
    html = """
    <div class="content__wrapper" data-monitor-id="regional-variant-a1b2c3">
      <p class="plan__dst">国内旅行</p>
      <h2 class="plan__title">地域別クーポン</h2>
    </div>
    """
    coupons = parse_coupons(html)
    assert coupons[0]["id"] == "regional-variant-a1b2c3"
    print("  ✅ test_parse_coupons_uses_injected_regional_variant_id PASSED")


def test_region_retry_discards_partial_attempt_state():
    """失敗した試行のカード・ID・画像を次の試行へ混入させない。"""
    calls = []

    def fake_fetch(
        browser,
        region,
        beautiful_soup,
        checked_at,
        variants,
        observations,
        html,
        evidence,
    ):
        calls.append(region.code)
        if len(calls) == 1:
            variants["failed"] = {"sig": "failed-id"}
            observations["failed-id"] = {"region_codes": [region.code]}
            html.append("<div>failed</div>")
            evidence["failed-id"] = b"failed"
            raise RuntimeError("一時的な遷移競合")
        assert "failed" not in variants
        assert "failed-id" not in observations
        assert "<div>failed</div>" not in html
        assert "failed-id" not in evidence
        observations["success-id"] = {"region_codes": [region.code]}
        html.append("<div>success</div>")
        evidence["success-id"] = b"success"
        return {"code": region.code, "visible_count": 1}

    variants = {}
    observations = {}
    html = []
    evidence = {}
    summary = _fetch_region_with_retry(
        object(),
        HIS_REGIONS[0],
        object(),
        "2026-08-11T07:00:00+09:00",
        variants,
        observations,
        html,
        evidence,
        fetch_region=fake_fetch,
        attempts=2,
        retry_delay_seconds=0,
    )

    assert calls == ["kanto", "kanto"]
    assert summary["visible_count"] == 1
    assert observations == {"success-id": {"region_codes": ["kanto"]}}
    assert html == ["<div>success</div>"]
    assert evidence == {"success-id": b"success"}
    print("  ✅ test_region_retry_discards_partial_attempt_state PASSED")


def test_extract_campaign_from_end_text():
    """公式の終了見出しからキャンペーン名を抽出できる。"""
    campaign = _extract_campaign_from_end_text(
        "総額1億円！スーパーサマーセールウルトラクーポンは終了しました。"
    )
    assert campaign == "総額1億円！スーパーサマーセールウルトラクーポン"
    print("  ✅ test_extract_campaign_from_end_text PASSED")


def test_explicit_ended_campaign_overrides_future_booking_period():
    """予約期間が未来でも、公式の終了告知があれば配布終了にする。"""
    html = """
    <html><body>
      <h2>総額1億円！スーパーサマーセールウルトラクーポンは終了しました。</h2>
      <p>ご予約ありがとうございました。</p>
      <div class="content__wrapper">
        <p class="plan__dst">国内ホテル</p>
        <h2 class="plan__title">【総額1億円！スーパーサマーセールウルトラクーポン】1グループ10％OFF</h2>
        <ul class="term__list">
          <li>予約期間：2099年5月8日(金)9:00～2099年6月30日(火)10:00</li>
          <li>宿泊期間：2099年5月9日(土)～2099年12月25日(金)</li>
        </ul>
        <ul class="coupon__list">
          <li>
            <p class="coupon__condition">オンライン予約限定</p>
            <p class="coupon__price">10％OFF</p>
            <p class="coupon__code" data-name="TESTCODE">TESTCODE</p>
          </li>
        </ul>
      </div>
      <div class="content__wrapper">
        <p class="plan__dst">国内航空券＋ホテル</p>
        <h2 class="plan__title">沖縄行き航空券＋ホテルがお得！ 1グループ2,000円引き</h2>
        <ul class="term__list">
          <li>予約期間：2099年5月8日(金)9:00～2099年6月30日(火)10:00</li>
          <li>出発期間：2099年5月9日(土)～2099年12月25日(金)</li>
        </ul>
      </div>
    </body></html>
    """

    coupons = parse_coupons(html)
    ended_campaigns = _extract_explicit_ended_campaigns(BeautifulSoup(html, "html.parser"))

    assert ended_campaigns == {"総額1億円！スーパーサマーセールウルトラクーポン"}
    assert coupons[0]["stock_status"] == "配布終了"
    assert "ended_reason" in coupons[0]
    assert coupons[1]["stock_status"] == "配布中"
    assert coupons[1]["discount"] == "2,000円引き"
    print("  ✅ test_explicit_ended_campaign_overrides_future_booking_period PASSED")


def test_thanks_text_can_mark_previous_campaign_heading_as_ended():
    """感謝文だけが別要素に分かれても、直前のキャンペーン見出しを終了扱いにできる。"""
    html = """
    <html><body>
      <h2>総額1億円！スーパーサマーセールウルトラクーポン</h2>
      <p>ご予約ありがとうございました。</p>
    </body></html>
    """

    ended_campaigns = _extract_explicit_ended_campaigns(BeautifulSoup(html, "html.parser"))
    assert ended_campaigns == {"総額1億円！スーパーサマーセールウルトラクーポン"}
    print("  ✅ test_thanks_text_can_mark_previous_campaign_heading_as_ended PASSED")


def test_visible_observation_is_attached_to_coupon():
    """公式画面の表示情報と証拠画像をクーポンへ付与できる。"""
    coupons = [{
        "id": "visible001",
        "title": "表示中クーポン",
        "category": "国内旅行",
    }]
    snapshot = {
        "observations": {
            "visible001": {
                "official_visibility": "visible",
                "official_tab": "国内旅行",
                "official_area": "首都圏版",
                "official_checked_at": "2026-07-27T19:05:00+09:00",
                "screenshot_url": "evidence/his/visible001.png",
                "screenshot_capture_method": "element_screenshot",
            }
        }
    }

    enriched = enrich_visible_coupons(coupons, snapshot)

    assert enriched[0]["official_visibility"] == "visible"
    assert enriched[0]["official_tab"] == "国内旅行"
    assert enriched[0]["screenshot_url"] == "evidence/his/visible001.png"
    assert enriched[0]["screenshot_capture_method"] == "element_screenshot"
    assert enriched[0]["source_type"] == "official_visible_dom"
    assert enriched[0]["fetch_method"] == (
        "playwright_visible_dom+regional_contexts+"
        "deduplicated_element_screenshot"
    )
    print("  ✅ test_visible_observation_is_attached_to_coupon PASSED")


def test_visible_observation_count_mismatch_is_rejected():
    """画面観測と解析件数がずれた場合は保存前に停止する。"""
    snapshot = {
        "observations": {
            "visible001": {"official_visibility": "visible"},
            "visible002": {"official_visibility": "visible"},
        }
    }

    try:
        enrich_visible_coupons([{"id": "visible001"}], snapshot)
    except ValueError:
        print("  ✅ test_visible_observation_count_mismatch_is_rejected PASSED")
        return
    raise AssertionError("件数不一致が検出されませんでした")


def test_card_is_captured_from_its_own_element():
    """カード自身の範囲を使い、直接撮影する。"""
    class FakeCard:
        def __init__(self):
            self.options = None

        def screenshot(self, **options):
            self.options = options
            return b"direct-card-png"

    card = FakeCard()
    image_bytes = _capture_card_png(card)

    assert image_bytes == b"direct-card-png"
    assert card.options == {
        "animations": "disabled",
        "timeout": 30000,
    }
    print("  ✅ test_card_is_captured_from_its_own_element PASSED")


if __name__ == "__main__":
    print("\n🧪 HISクーポン監視 終了判定テスト開始\n")
    test_official_region_registry_is_complete()
    test_primary_id_is_preserved_and_region_variant_is_split()
    test_region_only_coupon_does_not_take_legacy_primary_id()
    test_semantic_card_signature_ignores_unrelated_attributes()
    test_regional_observations_are_finalized_and_selectable()
    test_parse_coupons_uses_injected_regional_variant_id()
    test_region_retry_discards_partial_attempt_state()
    test_extract_campaign_from_end_text()
    test_explicit_ended_campaign_overrides_future_booking_period()
    test_thanks_text_can_mark_previous_campaign_heading_as_ended()
    test_visible_observation_is_attached_to_coupon()
    test_visible_observation_count_mismatch_is_rejected()
    test_card_is_captured_from_its_own_element()
    print("\n✅ 全テスト PASSED")
