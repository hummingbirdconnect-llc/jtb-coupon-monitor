"""JTBのクーポンコード／パスワード取得とダッシュボード表示の回帰テスト。"""

import unittest

from bs4 import BeautifulSoup

from generate_dashboard import COMMON_COLUMNS, format_coupon_row
from jtb_coupon_monitor import extract_coupon_credentials


class JTBCouponCredentialsTest(unittest.TestCase):
    def test_extracts_credentials_from_jtb_code_block(self):
        soup = BeautifulSoup(
            """
            <div class="c-code">
              <p><span class="txt">クーポンコード</span><span class="label">cpnew</span></p>
              <p><span class="txt">パスワード</span><span class="label">jvDR</span></p>
            </div>
            """,
            "html.parser",
        )

        codes, passwords = extract_coupon_credentials(
            soup, soup.get_text(separator="\n", strip=True)
        )

        self.assertEqual(codes, ["cpnew"])
        self.assertEqual(passwords, ["jvDR"])

    def test_falls_back_to_page_text_when_markup_changes(self):
        soup = BeautifulSoup(
            "<main>クーポンコード：CP-2026\nパスワード：PW_42</main>",
            "html.parser",
        )

        codes, passwords = extract_coupon_credentials(
            soup, soup.get_text(separator="\n", strip=True)
        )

        self.assertEqual(codes, ["CP-2026"])
        self.assertEqual(passwords, ["PW_42"])

    def test_dashboard_keeps_password_in_its_own_column(self):
        coupon = {
            "id": "sample",
            "title": "テストクーポン",
            "detail_data": {
                "coupon_codes": ["cpnew"],
                "passwords": ["jvDR"],
                "notes": ["先着 2000枚"],
            },
        }

        row = format_coupon_row(coupon, {"id": "jtb"}, "coupons")

        self.assertIn("パスワード", COMMON_COLUMNS)
        self.assertEqual(row["クーポンコード"], "cpnew")
        self.assertEqual(row["パスワード"], "jvDR")


if __name__ == "__main__":
    unittest.main()
