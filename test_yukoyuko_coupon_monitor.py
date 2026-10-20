import pytest

from yukoyuko_coupon_monitor import build_coupon, parse_api_config


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


def test_parse_api_config_reads_relative_endpoint_without_v1key():
    html = """
    YUKOYUKO.api = {
      gaiaApi: {
        endpoint: '/api/gaia',
      },
    };
    """

    endpoint, api_key = parse_api_config(html)

    assert endpoint == "https://www.yukoyuko.net/api/gaia"
    assert api_key == ""


def test_parse_api_config_keeps_absolute_endpoint_and_public_key():
    html = """
    YUKOYUKO.api = {
      gaiaApi: {
        endpoint: "https:\\/\\/gaia-api.yukoyuko.net",
        v1Key: "public-example-key",
      },
    };
    """

    endpoint, api_key = parse_api_config(html)

    assert endpoint == "https://gaia-api.yukoyuko.net"
    assert api_key == "public-example-key"


def test_parse_api_config_requires_endpoint():
    with pytest.raises(RuntimeError):
        parse_api_config("YUKOYUKO.api = { gaiaApi: {} };")


def test_mass_disappearance_keeps_previous_data_and_asks_for_review(tmp_path, monkeypatch):
    """取得が半分以下に減ったら、今回分を採用せず要確認として残す"""
    import json
    from datetime import timedelta

    import yukoyuko_coupon_monitor as yukoyuko
    from coupon_validator import NEEDS_REVIEW_EXIT_CODE

    def snapshot(count, prefix="Y"):
        return [
            {
                "id": f"{prefix}{index:03d}",
                "title": f"クーポン{index}",
                "category": "宿クーポン",
                "discount": "2,000円引",
                "stock_status": "配布中",
            }
            for index in range(count)
        ]

    monkeypatch.setattr(yukoyuko, "DATA_DIR", tmp_path)
    monkeypatch.setattr(yukoyuko, "MASTER_FILE", tmp_path / "master_ids.json")
    yesterday = (yukoyuko.now_jst() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"coupons_{yesterday}.json").write_text(
        json.dumps(snapshot(49), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "master_ids.json").write_text(
        json.dumps({"ids": {item["id"]: item for item in snapshot(100)}, "last_updated": ""}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        yukoyuko, "fetch_all_coupons", lambda detail_limit=None, include_static=True: snapshot(4)
    )

    with pytest.raises(SystemExit) as excinfo:
        yukoyuko.run()

    today = yukoyuko.today_str()
    assert excinfo.value.code == NEEDS_REVIEW_EXIT_CODE
    assert (tmp_path / f"needs_review_coupons_{today}.json").exists()
    assert not (tmp_path / f"coupons_{today}.json").exists()
    assert not (tmp_path / "change_log.json").exists()


def test_previous_snapshot_is_the_baseline_not_the_cumulative_master(tmp_path, monkeypatch):
    """累積台帳が100件でも、前回49件→今回52件なら消失0件で保存する"""
    import json
    from datetime import timedelta

    import yukoyuko_coupon_monitor as yukoyuko

    def snapshot(count, prefix="Y"):
        return [
            {
                "id": f"{prefix}{index:03d}",
                "title": f"クーポン{index}",
                "category": "宿クーポン",
                "discount": "2,000円引",
                "stock_status": "配布中",
            }
            for index in range(count)
        ]

    monkeypatch.setattr(yukoyuko, "DATA_DIR", tmp_path)
    monkeypatch.setattr(yukoyuko, "MASTER_FILE", tmp_path / "master_ids.json")
    yesterday = (yukoyuko.now_jst() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"coupons_{yesterday}.json").write_text(
        json.dumps(snapshot(49), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "master_ids.json").write_text(
        json.dumps({"ids": {item["id"]: item for item in snapshot(100)}, "last_updated": ""}),
        encoding="utf-8",
    )
    fetched = snapshot(49) + snapshot(3, prefix="NEW")
    monkeypatch.setattr(
        yukoyuko, "fetch_all_coupons", lambda detail_limit=None, include_static=True: fetched
    )

    coupons = yukoyuko.run()

    today = yukoyuko.today_str()
    assert len(coupons) == 52
    assert (tmp_path / f"coupons_{today}.json").exists()
    changes = json.loads((tmp_path / "change_log.json").read_text(encoding="utf-8"))
    assert [item["type"] for item in changes] == ["new", "new", "new"]
