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


def _snapshot(count, prefix="A"):
    return [
        {
            "id": f"{prefix}{index:03d}",
            "title": f"クーポン{index}",
            "category": "エリア限定クーポン",
            "discount": "1,000円引",
            "stock_status": "配布中",
        }
        for index in range(count)
    ]


def _prepare(tmp_path, monkeypatch, previous_count, cumulative_count, fetched):
    """前回スナップショット・累積台帳・今回の取得結果を用意する。"""
    import json
    from datetime import timedelta

    import rurubu_travel_coupon_monitor as rurubu

    monkeypatch.setattr(rurubu, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rurubu, "MASTER_FILE", tmp_path / "master_ids.json")

    yesterday = (rurubu.now_jst() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"coupons_{yesterday}.json").write_text(
        json.dumps(_snapshot(previous_count), ensure_ascii=False), encoding="utf-8"
    )
    # 7月から積み上がった累積台帳（前回スナップショットより多い）
    (tmp_path / "master_ids.json").write_text(
        json.dumps(
            {"ids": {item["id"]: item for item in _snapshot(cumulative_count)}, "last_updated": ""},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rurubu, "fetch_all_coupons", lambda: fetched)
    return rurubu


def test_cumulative_master_no_longer_counts_as_disappeared(tmp_path, monkeypatch):
    """累積台帳が165件でも、前回60件→今回63件なら消失0件で保存する"""
    import json

    fetched = _snapshot(60) + _snapshot(3, prefix="NEW")
    rurubu = _prepare(tmp_path, monkeypatch, previous_count=60, cumulative_count=165, fetched=fetched)

    coupons = rurubu.run()

    today = rurubu.today_str()
    assert len(coupons) == 63
    assert (tmp_path / f"coupons_{today}.json").exists()
    assert not (tmp_path / f"needs_review_coupons_{today}.json").exists()
    changes = json.loads((tmp_path / "change_log.json").read_text(encoding="utf-8"))
    assert [item["type"] for item in changes] == ["new", "new", "new"]


def test_mass_disappearance_keeps_previous_data_and_asks_for_review(tmp_path, monkeypatch):
    """取得が半分以下に減ったら、今回分を採用せず要確認として残す"""
    import pytest
    from coupon_validator import NEEDS_REVIEW_EXIT_CODE

    rurubu = _prepare(
        tmp_path, monkeypatch, previous_count=60, cumulative_count=165, fetched=_snapshot(5)
    )

    with pytest.raises(SystemExit) as excinfo:
        rurubu.run()

    today = rurubu.today_str()
    assert excinfo.value.code == NEEDS_REVIEW_EXIT_CODE
    # 今回分は「要確認」に退避し、通常の日次ファイルは作らない
    assert (tmp_path / f"needs_review_coupons_{today}.json").exists()
    assert not (tmp_path / f"coupons_{today}.json").exists()
    # 消失として記録しない／台帳も書き換えない
    assert not (tmp_path / "change_log.json").exists()
    master = __import__("json").loads((tmp_path / "master_ids.json").read_text(encoding="utf-8"))
    assert len(master["ids"]) == 165
