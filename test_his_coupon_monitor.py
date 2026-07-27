#!/usr/bin/env python3
"""his_coupon_monitor.py の終了判定テスト"""

import sys

sys.path.insert(0, ".")

from bs4 import BeautifulSoup

from his_coupon_monitor import (
    _extract_campaign_from_end_text,
    _extract_explicit_ended_campaigns,
    enrich_visible_coupons,
    parse_coupons,
)


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
            }
        }
    }

    enriched = enrich_visible_coupons(coupons, snapshot)

    assert enriched[0]["official_visibility"] == "visible"
    assert enriched[0]["official_tab"] == "国内旅行"
    assert enriched[0]["screenshot_url"] == "evidence/his/visible001.png"
    assert enriched[0]["source_type"] == "official_visible_dom"
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


if __name__ == "__main__":
    print("\n🧪 HISクーポン監視 終了判定テスト開始\n")
    test_extract_campaign_from_end_text()
    test_explicit_ended_campaign_overrides_future_booking_period()
    test_thanks_text_can_mark_previous_campaign_heading_as_ended()
    test_visible_observation_is_attached_to_coupon()
    test_visible_observation_count_mismatch_is_rejected()
    print("\n✅ 全テスト PASSED")
