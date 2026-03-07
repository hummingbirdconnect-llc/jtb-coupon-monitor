#!/usr/bin/env python3
"""knt_coupon_monitor.py のクーポンコード抽出テスト"""
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, ".")

from knt_coupon_monitor import _extract_coupon_codes


def test_extract_coupon_codes_from_copy_inputs():
    html = """
    <div class="cpn_btm">
      <p class="cpn_code_txt"><input id="copyTarget8" type="text" value="263DSNH5" readonly></p>
      <p class="cpn_btn"><button onclick="copyToClipboard(8)">クリックしてクーポンコードをコピー</button></p>
    </div>
    <div class="cpn_btm">
      <p class="cpn_code_txt"><input id="copyTarget9" type="text" value="25DSNH50" readonly></p>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    codes = _extract_coupon_codes(soup, soup.get_text("\n", strip=True))
    assert codes == ["263DSNH5", "25DSNH50"], f"Got: {codes}"
    print("  ✅ test_extract_coupon_codes_from_copy_inputs PASSED")


def test_extract_coupon_codes_from_visible_text():
    html = """
    <div>
      <p>クーポンコード：ABC123</p>
      <p>コード：XYZ789</p>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    codes = _extract_coupon_codes(soup, soup.get_text("\n", strip=True))
    assert codes == ["ABC123", "XYZ789"], f"Got: {codes}"
    print("  ✅ test_extract_coupon_codes_from_visible_text PASSED")


if __name__ == "__main__":
    print("\n🧪 knt_coupon_monitor テスト開始\n")
    test_extract_coupon_codes_from_copy_inputs()
    test_extract_coupon_codes_from_visible_text()
    print("\n✅ 全テスト PASSED")
