from yukoyuko_coupon_monitor import build_coupon


def test_build_coupon_from_campaign_detail():
    campaign = {
        "campaignId": "ykwm2662",
        "campaignName": "【10日間限定】夏旅先取りクーポン",
        "discountRateFlag": False,
        "discountAmount": 3000,
        "entryStartedAt": "2026/06/12 10:00",
        "entryEndedAt": "2026/06/21 23:59",
        "isAllOverIssueLimit": False,
        "couponCount": 2,
    }
    detail = {
        "campaign": {
            **campaign,
            "discountDescription": "WEB限定。他のゆこゆこクーポンと併用は不可",
        },
        "coupons": [
            {
                "couponId": "ykwm266a",
                "couponName": "1予約3,000円引",
                "discountRateFlag": False,
                "discountAmount": 3000,
                "useStartedOn": "2026/06/12 10:00",
                "useEndedOn": "2026/06/21 23:59",
                "stayStartedOn": "2026/07/01",
                "stayEndedOn": "2026/09/30",
                "leastReserveAmount": 35000,
                "atmostReserveAmount": -1,
                "ngStayDescription": "土曜除く",
                "issueLimitOver": False,
                "combinationFlag": False,
            },
            {
                "couponId": "ykwm266b",
                "couponName": "1予約2,000円引",
                "discountRateFlag": False,
                "discountAmount": 2000,
                "useStartedOn": "2026/06/12 10:00",
                "useEndedOn": "2026/06/21 23:59",
                "stayStartedOn": "2026/07/01",
                "stayEndedOn": "2026/09/30",
                "leastReserveAmount": 25000,
                "atmostReserveAmount": 34999,
                "ngStayDescription": "土曜除く",
                "issueLimitOver": False,
                "combinationFlag": False,
            },
        ],
        "count": {"coupon": 2},
    }

    coupon = build_coupon(campaign, detail)

    assert coupon["id"] == "yukoyuko-ykwm2662"
    assert coupon["category"] == "期間限定クーポン"
    assert coupon["discount"] == "3,000円割引 / 2,000円割引"
    assert coupon["booking_period"] == "2026/06/12 10:00 ～ 2026/06/21 23:59"
    assert coupon["travel_period"] == "2026/07/01 ～ 2026/09/30"
    assert "予約金額35,000円以上で利用可" in coupon["conditions"]
    assert "WEB予約限定" in coupon["conditions"]


def test_build_coupon_marks_issue_limit_over():
    campaign = {
        "campaignId": "yrcp0016",
        "campaignName": "【エリアクーポン】昼神温泉",
        "discountRateFlag": False,
        "discountAmount": 2000,
        "entryStartedAt": "2026/04/20 00:00",
        "entryEndedAt": "2026/07/17 23:59",
        "isAllOverIssueLimit": True,
        "couponCount": 1,
    }
    detail = {
        "campaign": campaign,
        "coupons": [
            {
                "couponId": "yrcp0016a",
                "couponName": "2,000円引",
                "discountRateFlag": False,
                "discountAmount": 2000,
                "issueLimitOver": True,
                "combinationFlag": False,
            }
        ],
        "count": {"coupon": 1},
    }

    coupon = build_coupon(campaign, detail)

    assert coupon["category"] == "エリアクーポン・地域割"
    assert coupon["stock_status"] == "上限到達"
    assert "先着上限に達したクーポンあり" in coupon["conditions"]
