import json
from datetime import datetime, timezone, timedelta

import pytest

import ikyu_platform_coupons as platform
from ikyu_platform_coupons import (
    SiteConfig,
    build_coupon,
    coupon_regions,
    discount_text,
    extract_coupon_list,
    mark_shared_listing,
    parse_government_page,
    prefecture_from_text,
    unflatten_devalue,
)

JST = timezone(timedelta(hours=9))

YAHOO = SiteConfig(
    provider="yahoo_travel", provider_label="Yahoo!トラベル", base_url="https://travel.yahoo.co.jp",
    login_label="Yahoo! JAPAN ID", site_targets=("welltrip",), article_slug="yahoo-travel-coupon",
    sibling_provider="ikkyu", sibling_data_dir="ikkyu_coupon_data",
)
IKKYU = SiteConfig(
    provider="ikkyu", provider_label="一休.com", base_url="https://www.ikyu.com",
    login_label="一休会員", site_targets=("welltrip",), article_slug="ikyu-coupon",
    sibling_provider="yahoo_travel", sibling_data_dir="yahoo_travel_coupon_data",
)

RAW = {
    "acquired": False,
    "manageId": 1122685,
    "name": "登別万世閣",
    "description": "先着30名様限定 3,000円OFFクーポン",
    "expiredAt": "2026-10-26T12:00:00+09:00",
    "limitation": {
        "checkOutDate": {"begin": "2026-10-18T00:00:00+09:00", "end": "2027-03-31T00:00:00+09:00"},
        "minAmount": 25000,
    },
    "isHotelGroupCoupon": False,
    "dayUseExclude": True,
    "accommodations": [
        {
            "name": "登別万世閣", "accommodationId": "00031335", "prefectureName": "北海道",
            "areaName": "登別温泉", "largeAreaId": "100000", "imageURL": "//example/x.jpg",
        }
    ],
}


def test_unflatten_devalue_restores_nested_objects_and_references():
    flat = [
        ["ShallowReactive", 1],
        {"data": 2},
        {"accommodationCouponsAvailable": 3},
        {"coupons": 4},
        [5, 5],
        {"manageId": 6, "name": 7},
        1122685,
        "登別万世閣",
    ]
    data = unflatten_devalue(flat)
    coupons = data["data"]["accommodationCouponsAvailable"]["coupons"]
    assert len(coupons) == 2
    assert coupons[0]["manageId"] == 1122685
    assert coupons[0] is coupons[1]


def test_extract_coupon_list_reads_nuxt_data_script():
    flat = [
        ["ShallowReactive", 1],
        {"data": 2},
        {"accommodationCouponsAvailable": 3},
        {"coupons": 4},
        [5],
        {"manageId": 6, "name": 7, "description": 8, "accommodations": 9},
        1122685,
        "登別万世閣",
        "先着30名様限定 3,000円OFFクーポン",
        [],
    ]
    html = f'<html><body><script type="application/json" id="__NUXT_DATA__">{json.dumps(flat)}</script></body></html>'
    items = extract_coupon_list(html)
    assert items[0]["manageId"] == 1122685


def test_build_coupon_maps_fields():
    now = datetime(2026, 10, 21, 9, 0, tzinfo=JST)
    coupon = build_coupon(RAW, YAHOO, now)
    assert coupon["id"] == "yahoo_travel-1122685"
    assert coupon["provider"] == "yahoo_travel"
    assert coupon["title"] == "登別万世閣 先着30名様限定 3,000円OFFクーポン"
    assert coupon["discount"] == "3,000円割引"
    assert coupon["stock_status"] == "配布中"
    assert coupon["regions"] == ["北海道"]
    assert coupon["area"] == "北海道／登別温泉"
    assert coupon["booking_period"] == "～ 2026/10/26 12:00"
    assert coupon["stay_period"] == "2026/10/18 ～ 2027/03/31"
    assert "予約金額25,000円以上" in coupon["conditions"]
    assert "先着30名様限定" in coupon["conditions"]
    assert "デイユース（日帰り）は対象外" in coupon["conditions"]
    assert coupon["detail_url"] == "https://travel.yahoo.co.jp/coupons/accommodations?cmid=1122685"
    assert coupon["detail_data"]["platform_manage_id"] == "1122685"
    assert coupon["detail_data"]["origin_provider"] == "ikkyu"
    assert coupon["detail_data"]["hotel_url"].startswith("https://travel.yahoo.co.jp/00031335/")


def test_build_coupon_marks_expired_as_ended():
    now = datetime(2026, 11, 1, 9, 0, tzinfo=JST)
    coupon = build_coupon(RAW, IKKYU, now)
    assert coupon["id"] == "ikkyu-1122685"
    assert coupon["stock_status"] == "配布終了"


def test_regions_stay_empty_when_prefecture_unknown():
    raw = dict(RAW, accommodations=[{"name": "宿", "accommodationId": "1", "prefectureName": "", "areaName": "", "largeAreaId": "100000"}])
    assert coupon_regions(raw) == []
    assert build_coupon(raw, YAHOO)["regions"] == []


def test_prefecture_from_text_and_discount_text():
    assert prefecture_from_text("静岡県伊東市") == "静岡県"
    assert prefecture_from_text("北海道") == "北海道"
    assert prefecture_from_text("中之島") == ""
    assert discount_text("枚数限定 10,000円OFFクーポン") == "10,000円割引"
    assert discount_text("特別クーポン") == "特別クーポン"


GOV_HTML = """
<section><h2>北海道・東北</h2>
<article>
  <div class="relative"><img alt="岩手県のキャンペーン画像"/><div><p class="font-bold text-white">このキャンペーンは終了しました</p></div></div>
  <div><p class="text-lg font-bold">岩手県</p><h3>おでんせ！いわて旅割キャンペーン</h3>
  <p class="w-full font-bold"><span>最大</span><span>50</span><span>%OFFクーポン</span></p></div>
  <div><a href="/tohoku/120220/">宿を探す</a><a href="/feature/iwate02">詳細をみる</a></div>
</article>
<article>
  <div class="relative"><img alt="秋田県のキャンペーン画像"/></div>
  <div><p class="text-lg font-bold">秋田県</p><h3>Go To Akita! 得旅キャンペーン</h3>
  <p class="w-full font-bold"><span>最大</span><span>40,000</span><span>円OFFクーポン</span></p></div>
  <div><a href="/tohoku/120100/">宿を探す</a><a href="/feature/akita">詳細をみる</a></div>
</article>
</section>
"""


def test_parse_government_page_reads_region_and_ended_label():
    items = parse_government_page(GOV_HTML, IKKYU)
    assert len(items) == 2
    iwate, akita = items
    assert iwate["category"] == "自治体クーポン"
    assert iwate["regions"] == ["岩手県"]
    assert iwate["stock_status"] == "配布終了"
    assert iwate["discount"] == "最大50%割引"
    assert iwate["detail_url"] == "https://www.ikyu.com/feature/iwate02"
    assert akita["stock_status"] == "配布中"
    assert akita["discount"] == "最大40,000円割引"
    assert akita["regions"] == ["秋田県"]


def test_mark_shared_listing_records_sibling(tmp_path):
    sibling_dir = tmp_path / "ikkyu_coupon_data"
    sibling_dir.mkdir()
    (sibling_dir / "coupons_2026-10-21.json").write_text(
        json.dumps([{"id": "ikkyu-1122685", "detail_data": {"platform_manage_id": "1122685"}}]),
        encoding="utf-8",
    )
    coupons = [build_coupon(RAW, YAHOO), build_coupon(dict(RAW, manageId=999), YAHOO)]
    marked = mark_shared_listing(coupons, YAHOO, tmp_path)
    assert marked == 1
    assert coupons[0]["detail_data"]["shared_with"] == ["ikkyu"]
    assert coupons[1]["detail_data"]["shared_with"] == []


def test_mark_shared_listing_without_sibling_data(tmp_path):
    coupons = [build_coupon(RAW, YAHOO)]
    assert mark_shared_listing(coupons, YAHOO, tmp_path) == 0
    assert coupons[0]["detail_data"]["shared_with"] == []


# ------------------------------------------------------------
# 本人が開いたページの保存ファイルから読む（2026-11-10〜）
# ------------------------------------------------------------

def flatten_devalue(root):
    """テスト用: 普通の値を devalue の配列（先頭が根、参照は添字）にする。"""
    flat = []

    def add(value):
        if value is None:
            return -1
        index = len(flat)
        flat.append(None)
        if isinstance(value, dict):
            flat[index] = {key: add(child) for key, child in value.items()}
        elif isinstance(value, list):
            flat[index] = [add(child) for child in value]
        else:
            flat[index] = value
        return index

    add(root)
    return flat


def nuxt_coupon_html(coupons):
    flat = flatten_devalue({"data": {"accommodationCouponsAvailable": {"coupons": coupons}}})
    return f'<script type="application/json" id="__NUXT_DATA__">{json.dumps(flat, ensure_ascii=False)}</script>'


def write_saved(saved_dir, site, kind, body, url=None):
    url = url or f"{site.base_url}{platform.PAGE_PATHS[kind]}"
    path = platform.saved_page_path(saved_dir, site, kind)
    path.write_text(
        f"<!-- saved-from: {url} -->\n<!-- saved-at: 2026-11-10T09:00:00+09:00 -->\n"
        f"<!DOCTYPE html><html><body>{body}</body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def no_network(monkeypatch):
    def fail(url, timeout=30):
        raise AssertionError(f"保存ファイルから読むときは通信しない: {url}")

    monkeypatch.setattr(platform, "fetch_html", fail)


def test_fetch_all_coupons_reads_saved_pages_without_network(tmp_path, no_network):
    write_saved(tmp_path, IKKYU, "coupons", nuxt_coupon_html([RAW, dict(RAW, manageId=999)]))
    write_saved(tmp_path, IKKYU, "government", GOV_HTML)
    coupons = platform.fetch_all_coupons(IKKYU, saved_dir=tmp_path)
    assert [c["id"] for c in coupons[:2]] == ["ikkyu-1122685", "ikkyu-999"]
    assert coupons[0]["title"] == "登別万世閣 先着30名様限定 3,000円OFFクーポン"
    assert coupons[0]["detail_url"] == "https://www.ikyu.com/coupons/accommodations?cmid=1122685"
    assert len(coupons) == 4
    assert [c["category"] for c in coupons[2:]] == ["自治体クーポン", "自治体クーポン"]


def test_saved_government_page_missing_is_skipped(tmp_path, no_network, capsys):
    write_saved(tmp_path, YAHOO, "coupons", nuxt_coupon_html([RAW]))
    coupons = platform.fetch_all_coupons(YAHOO, saved_dir=tmp_path)
    assert [c["id"] for c in coupons] == ["yahoo_travel-1122685"]
    assert "自治体クーポン特集の取得失敗: FileNotFoundError" in capsys.readouterr().out


def test_saved_coupon_list_missing_stops(tmp_path, no_network):
    write_saved(tmp_path, YAHOO, "government", GOV_HTML)
    with pytest.raises(FileNotFoundError):
        platform.fetch_all_coupons(YAHOO, saved_dir=tmp_path)


def test_saved_page_from_other_site_is_rejected(tmp_path, no_network):
    write_saved(tmp_path, YAHOO, "coupons", nuxt_coupon_html([RAW]), url="https://www.ikyu.com/coupons/accommodations")
    with pytest.raises(RuntimeError, match="ページが違う"):
        platform.fetch_all_coupons(YAHOO, saved_dir=tmp_path)


def test_saved_page_without_mark_is_rejected(tmp_path, no_network):
    platform.saved_page_path(tmp_path, IKKYU, "coupons").write_text(
        f"<!DOCTYPE html><html><body>{nuxt_coupon_html([RAW])}</body></html>", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="印が無い"):
        platform.fetch_all_coupons(IKKYU, saved_dir=tmp_path, include_government=False)


def test_check_saved_page_ignores_query_and_trailing_slash():
    platform.check_saved_page("<!-- saved-from: https://www.ikyu.com/campaign/government?from=top#x -->", IKKYU, "government")
    platform.check_saved_page("<!-- saved-from: https://travel.yahoo.co.jp/coupons/accommodations/ -->", YAHOO, "coupons")


def test_fetch_all_coupons_without_saved_dir_still_fetches(monkeypatch):
    calls = []

    def fake_fetch(url, timeout=30):
        calls.append(url)
        return nuxt_coupon_html([RAW]) if url.endswith("/coupons/accommodations") else GOV_HTML

    monkeypatch.setattr(platform, "fetch_html", fake_fetch)
    coupons = platform.fetch_all_coupons(IKKYU)
    assert calls == ["https://www.ikyu.com/coupons/accommodations", "https://www.ikyu.com/campaign/government/"]
    assert len(coupons) == 3


def test_monitors_accept_from_saved(tmp_path):
    import ikkyu_coupon_monitor
    import yahoo_travel_coupon_monitor

    for monitor in (ikkyu_coupon_monitor, yahoo_travel_coupon_monitor):
        args = monitor.parse_args(["--from-saved", str(tmp_path), "--dry-run"])
        assert args.from_saved == tmp_path
        assert args.dry_run is True
        assert monitor.parse_args([]).from_saved is None
