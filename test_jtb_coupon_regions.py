"""JTB予約対象地域の省略・条件混入・表示漏れを防ぐテスト。"""

import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from generate_dashboard import COMMON_COLUMNS, format_coupon_row
from jtb_coupon_monitor import (
    COUPON_PAGES,
    _scrape_coupon_list_page_fallback,
    extract_booking_regions,
    extract_list_regions,
    scrape_coupon_list_page,
    scrape_detail_page,
)


class JTBCouponRegionsTest(unittest.TestCase):
    def soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def test_all_list_regions_survive_including_fourth_and_later(self):
        item = self.soup('''<div data-pref='["北海道","青森県","岩手県","長野県","北海道"]'></div>''').div
        self.assertEqual(extract_list_regions(item), ["北海道", "青森県", "岩手県", "長野県"])

    def test_bad_or_non_list_attributes_use_visible_area(self):
        for attribute in ['invalid', '{}', 'null', '[null,42,""]']:
            with self.subTest(attribute=attribute):
                item = self.soup(f'''<div data-pref='{attribute}'><span class="c-coupon__area">全方面</span></div>''').div
                self.assertEqual(extract_list_regions(item), ["全方面"])

    def test_detail_keeps_facility_restriction_and_ignores_residence(self):
        soup = self.soup('''<div class="sec-conditions"><ul>
          <li><span class="item">予約対象地域</span><span>北海道・東北・北関東・甲信越・北陸の対象施設</span></li>
          <li><span class="item">居住地条件</span><span>東京都</span></li>
        </ul></div>''')
        self.assertEqual(extract_booking_regions(soup), "北海道・東北・北関東・甲信越・北陸の対象施設")

    def test_missing_label_does_not_infer_regions_from_other_page_text(self):
        self.assertEqual(extract_booking_regions(self.soup('<main>北海道のおすすめ旅行</main>')), "")

    def test_international_regions_are_preserved(self):
        soup = self.soup('''<div class="sec-conditions"><li><span class="item">予約対象地域：</span><span>ハワイ<br>グアムの対象ツアー</span></li></div>''')
        self.assertEqual(extract_booking_regions(soup), "ハワイ グアムの対象ツアー")

    def test_list_scraper_saves_all_regions(self):
        html = '''<div class="c-coupon__item" data-id="ski" data-pref='["北海道","青森県","岩手県","長野県"]'>
          <h3 class="c-coupon__title"><a href="/myjtb/campaign/coupon/detail/ski/page.asp">スキー</a></h3></div>'''
        response = Mock(text=html, apparent_encoding="utf-8")
        with patch("jtb_coupon_monitor.requests.get", return_value=response):
            coupon = scrape_coupon_list_page(COUPON_PAGES[0])[0]
        self.assertEqual(coupon["area"], "北海道・青森県・岩手県・長野県")
        self.assertEqual(coupon["regions"][-1], "長野県")

    def test_fallback_scraper_retains_visible_regions(self):
        soup = self.soup('''<div class="c-coupon__item"><span class="c-coupon__area">北海道</span>
          <a href="/myjtb/campaign/coupon/detail/ski/page.asp">スキー</a></div>''')
        coupon = _scrape_coupon_list_page_fallback(COUPON_PAGES[0], soup)[0]
        self.assertEqual(coupon["regions"], ["北海道"])

    def test_detail_scraper_persists_region_field(self):
        response = Mock(text='''<div class="sec-conditions"><li><span class="item">予約対象地域</span><span>北海道の対象施設</span></li></div>''', apparent_encoding="utf-8")
        with patch("jtb_coupon_monitor.requests.get", return_value=response), patch("jtb_coupon_monitor.time.sleep"):
            detail = scrape_detail_page("https://www.jtb.co.jp/example")
        self.assertEqual(detail["booking_regions"], "北海道の対象施設")

    def test_dashboard_prefers_detail_over_list_and_legacy_area(self):
        coupon = {"area": "全国", "regions": ["北海道", "長野県"],
                  "detail_data": {"booking_regions": "北海道・長野県の対象施設"}}
        self.assertIn("予約対象地域", COMMON_COLUMNS)
        self.assertEqual(format_coupon_row(coupon, {"id": "jtb"}, "coupons")["予約対象地域"], "北海道・長野県の対象施設")
        coupon["detail_data"] = {}
        self.assertEqual(format_coupon_row(coupon, {"id": "jtb"}, "coupons")["予約対象地域"], "北海道 / 長野県")
        coupon.pop("regions")
        self.assertEqual(format_coupon_row(coupon, {"id": "jtb"}, "coupons")["予約対象地域"], "全国")

    def test_other_providers_do_not_relabel_departure_areas(self):
        row = format_coupon_row({"area": "東京発"}, {"id": "his"}, "coupons")
        self.assertEqual(row["予約対象地域"], "")


if __name__ == "__main__":
    unittest.main()
