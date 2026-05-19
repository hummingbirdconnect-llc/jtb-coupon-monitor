#!/usr/bin/env python3
"""his_coupon_monitor.py の終了判定テスト"""

import sys

sys.path.insert(0, ".")

from bs4 import BeautifulSoup

from his_coupon_monitor import (
    _extract_campaign_from_end_text,
    _extract_explicit_ended_campaigns,
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


if __name__ == "__main__":
    print("\n🧪 HISクーポン監視 終了判定テスト開始\n")
    test_extract_campaign_from_end_text()
    test_explicit_ended_campaign_overrides_future_booking_period()
    test_thanks_text_can_mark_previous_campaign_heading_as_ended()
    print("\n✅ 全テスト PASSED")
