from rurubu_travel_coupon_monitor import build_coupon, extract_deals_prop


def test_extract_deals_prop_from_embedded_window_object():
    html = '''
    <html><body>
    <script>window.dealsProp = {"pageData":{"couponGroups":[{"name":"複数エリア対象クーポン","cards":[]}]}};</script>
    </body></html>
    '''

    payload = extract_deals_prop(html)

    assert payload["pageData"]["couponGroups"][0]["name"] == "複数エリア対象クーポン"


def test_build_coupon_normalizes_rurubu_deals_card():
    card = {
        "name": "今月の値引きクーポン（最大500円引き）",
        "location": "",
        "discount": "最大2%引き",
        "book": "2099年6月25日 - 2099年7月8日",
        "stay": "2099年7月1日 - 2099年10月7日",
        "couponContent": {
            "modalTitle": "今月の値引きクーポン（最大500円引き）",
            "coupons": [
                {
                    "discount": "2% クーポン",
                    "expired": False,
                    "promoCode": "RUSUM99JUN2",
                    "minBookingAmount": "¥ 1",
                    "couponsLeft": "クーポン残り772枚！",
                    "searchLink": "/?cid=1839358",
                    "bookBy": "2099年6月25日 - 2099年7月8日",
                    "stayBy": "2099年7月1日 - 2099年10月7日",
                    "showActionButtons": True,
                }
            ],
        },
    }

    coupon = build_coupon("複数エリア対象クーポン", card)

    assert coupon["provider"] == "rurubu_travel"
    assert coupon["category"] == "複数エリア対象クーポン"
    assert coupon["discount"] == "最大2%引き"
    assert coupon["stock_status"] == "配布中"
    assert coupon["booking_period"] == "2099年6月25日～2099年7月8日"
    assert coupon["travel_period"] == "2099年7月1日～2099年10月7日"
    assert coupon["coupon_codes"][0]["code"] == "RUSUM99JUN2"
    assert coupon["coupon_codes"][0]["search_url"] == "https://www.rurubu.travel/?cid=1839358"
    assert "最低利用金額: ¥ 1" in coupon["conditions"]
    assert "クーポン欄の「宿泊施設を検索」から対象施設検索が必要" in coupon["conditions"]


def test_build_coupon_marks_all_expired_options_as_ended():
    card = {
        "name": "配布終了クーポン",
        "discount": "1,000円引き",
        "book": "2099年1月1日 - 2099年12月31日",
        "couponContent": {
            "coupons": [
                {
                    "discount": "1,000円 クーポン",
                    "expired": True,
                    "promoCode": "RUEND99",
                }
            ]
        },
    }

    coupon = build_coupon("施設限定クーポン", card)

    assert coupon["stock_status"] == "配布終了"
    assert "一部または全てのコードにexpiredフラグあり" in coupon["conditions"]
