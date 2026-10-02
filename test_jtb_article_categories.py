import unittest
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup
from jtb_article_categories import extract_category_fields, classify_coupon, collect_article_category_sources


class ArticleCategoriesTest(unittest.TestCase):
    def test_only_active_plans_are_eligible(self):
        soup = BeautifulSoup('''<li class="c-plan__item">JTB宿泊プラン</li><li class="c-plan__item is-active">JR＋宿泊プラン</li>''', 'html.parser')
        fields = extract_category_fields(soup)
        self.assertEqual(fields['available_plans'], ['JR＋宿泊プラン'])
        categories, evidence = classify_coupon({'category': '国内', 'detail_data': fields})
        self.assertIn('新幹線ツアー', categories)
        self.assertIn('具体的な列車', evidence['新幹線ツアー']['note'])

    def test_member_requirement_does_not_imply_birthday_or_line(self):
        cats, _ = classify_coupon({'category': '国内', 'title': 'お得な旅行', 'detail_data': {'eligible_users': 'JTBトラベルメンバー会員限定'}})
        self.assertEqual(cats, ['国内旅行'])

    def test_initial_coupon_can_also_be_domestic_and_jr(self):
        cats, evidence = classify_coupon({'category': '国内', 'title': '新規会員限定！初めてのJTB', 'detail_data': {'available_plans': ['JR＋宿泊プラン']}})
        self.assertEqual(cats, ['国内旅行', '初回限定', '新幹線ツアー'])
        self.assertEqual(evidence['初回限定']['field'], 'title')

    def test_specific_discount_category_does_not_match_fifty_percent_or_1500_yen(self):
        for discount in ['最大50％引', '1,500円引', '15％引', '100,500円引']:
            with self.subTest(discount=discount):
                self.assertNotIn('5％・3％・500円', classify_coupon({'discount': discount})[0])
        for discount in ['最大5％引', '3%OFF', '500円引']:
            self.assertIn('5％・3％・500円', classify_coupon({'discount': discount})[0])

    def test_specific_discount_in_lower_tier_is_collected(self):
        cats, _ = classify_coupon({'discount': '最大5,000円引', 'detail_data': {'discount_rules': ['10,000円以上のご利用で500円引']}})
        self.assertIn('5％・3％・500円', cats)

    def test_tabimonogatari_markup_retains_brand_destination_and_departure(self):
        soup = BeautifulSoup('''<li class="conditionsItem"><p class="conditionsItem_tit">対象ブランド</p><p class="conditionsItem_content">JTB旅物語</p></li>
          <li class="conditionsItem"><p class="conditionsItem_tit">目的地</p><p class="conditionsItem_content">沖縄</p></li>
          <li class="conditionsItem"><p class="conditionsItem_tit">出発地</p><p class="conditionsItem_content">首都圏発</p></li>''', 'html.parser')
        fields = extract_category_fields(soup)
        self.assertEqual(fields['booking_regions'], '沖縄')
        self.assertEqual(fields['departure_area'], '首都圏発')
        self.assertIn('JTB旅物語', classify_coupon({'detail_data': fields})[0])

    def test_redirected_line_page_is_not_classified_as_line_coupon(self):
        response = Mock(url='https://www.jtb.co.jp/med/discountcoupon/', text='<main>旅物語</main>', apparent_encoding='utf-8')
        with patch('jtb_article_categories.ARTICLE_CATEGORY_SOURCES', [('LINE', 'https://www.jtb.co.jp/med/lineshinkicpn/', '要確認')]), patch('jtb_article_categories.requests.get', return_value=response):
            source = collect_article_category_sources({}, delay=0)[0]
        self.assertEqual(source['status'], 'redirected_review_required')
        self.assertEqual(source['evidence_text'], '')

    def test_sns_login_dependency_is_not_reported_as_no_coupons(self):
        with patch('jtb_article_categories.ARTICLE_CATEGORY_SOURCES', [('SNS', 'https://x.com/JTB_jp', '投稿確認が必要')]), patch('jtb_article_categories.requests.get') as get:
            source = collect_article_category_sources({}, delay=0)[0]
        get.assert_not_called()
        self.assertEqual(source['status'], 'recipient_or_screen_check_required')


if __name__ == '__main__':
    unittest.main()
