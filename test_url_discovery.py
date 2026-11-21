#!/usr/bin/env python3
"""登録URLの移転先候補を探す機能（url_discovery）の回帰テスト。

実際の通信はしない。FakeSession が決めた応答だけを返し、知らないURLは即失敗にする。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import generate_dashboard
import jalpack_coupon_monitor as jalpack
import official_deal_monitor as monitor
import provider_check_runner as runner
import url_discovery as ud

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "provider_registry.json"


class FakeResponse:
    def __init__(self, status: int, text: str = "", location: str = "") -> None:
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}


class FakeSession:
    """URL→(status, text, location)。登録外のURLは 404。遮断ホストへ来たら即失敗。"""

    def __init__(self, routes: dict[str, tuple]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False, "転送は自前で辿り、毎回ホストを確かめる"
        assert timeout, "タイムアウト必須"
        host = ud.host_of(url)
        assert not ud.is_blocked_host(host), f"遮断ホストへ通信しようとした: {url}"
        self.calls.append(url)
        status, text, location = (self.routes.get(url) or (404, "", ""))[:3] + ("",) * 0
        return FakeResponse(status, text, location)


def route(status: int, text: str = "", location: str = "") -> tuple:
    return (status, text, location)


def make_client(routes: dict[str, tuple], max_requests: int = 10) -> tuple[ud.DiscoveryClient, FakeSession]:
    session = FakeSession(routes)
    sleeps: list[float] = []
    client = ud.DiscoveryClient(
        session=session, max_requests=max_requests, delay_seconds=0.5, sleep=sleeps.append
    )
    client._test_sleeps = sleeps  # type: ignore[attr-defined]
    return client, session


ROBOTS_OK = route(200, "User-agent: *\nDisallow: /private/\n")


# ---------------------------------------------------------------------------
# きっかけの判定
# ---------------------------------------------------------------------------


def test_trigger_statuses_and_redirect_to_top() -> None:
    url = "https://www.example.co.jp/jp/ja/domtour/sale/"
    assert ud.needs_discovery(url, 404) == "http_404"
    assert ud.needs_discovery(url, 410) == "http_410"
    assert ud.needs_discovery(url, 403) == "http_403"
    assert ud.needs_discovery(url, 200) == ""
    assert ud.needs_discovery(url, 500) == ""
    # サイトのトップ・言語別トップ・別の区画への転送
    assert ud.needs_discovery(url, 200, "https://www.example.co.jp/") == "redirect_to_generic_page"
    assert ud.needs_discovery(url, 200, "https://www.example.co.jp/jp/ja/") == "redirect_to_generic_page"
    assert ud.needs_discovery(url, 200, "https://www.example.co.jp/en/") == "redirect_to_generic_page"
    # 末尾「/」だけの違いや、同じ区画の下の階層は対象外
    assert ud.needs_discovery(url, 200, "https://www.example.co.jp/jp/ja/domtour/sale") == ""
    assert ud.needs_discovery(url, 200, "https://www.example.co.jp/jp/ja/domtour/sale/2026/") == ""


# ---------------------------------------------------------------------------
# 探し方 a / b / c
# ---------------------------------------------------------------------------


def test_orion_like_403_without_slash_finds_slash_variant() -> None:
    provider = {"id": "orion_tour", "official_domains": ["orion-tour.co.jp"]}
    old = "https://www.orion-tour.co.jp/air/fair/feature/coupon"
    new = old + "/"
    client, session = make_client(
        {
            "https://www.orion-tour.co.jp/robots.txt": ROBOTS_OK,
            new: route(200, "<html>クーポン</html>"),
        }
    )
    entry = ud.check_registered_url(provider, old, status=403, client=client)
    assert entry["trigger"] == "http_403"
    assert [c["url"] for c in entry["candidates"]] == [new]
    assert entry["candidates"][0]["method"] == "slash_scheme_variant"
    assert entry["candidates"][0]["status"] == 200
    # a で見つかったら b・c へ進まない（通信は robots.txt と候補の2回だけ）
    assert session.calls == ["https://www.orion-tour.co.jp/robots.txt", new]
    assert client._test_sleeps == [0.5]  # 2回目の前に間隔を空けた


def test_404_finds_candidates_from_sitemap_index() -> None:
    provider = {"id": "sample", "official_domains": ["example.com"]}
    old = "https://www.example.com/campaign/summer-sale/"
    robots = route(200, "User-agent: *\nDisallow:\nSitemap: https://www.example.com/sitemap_index.xml\n")
    index = route(
        200,
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://www.example.com/sitemap-news.xml</loc></sitemap>"
        "<sitemap><loc>https://www.example.com/sitemap-campaign.xml</loc></sitemap>"
        "</sitemapindex>",
    )
    child = route(
        200,
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.example.com/about/</loc></url>"
        "<url><loc>https://www.example.com/campaign/summer-sale-2026/</loc></url>"
        "<url><loc>https://px.a8.net/svt/ejp?a8mat=campaign</loc></url>"
        "<url><loc>https://other.example.org/campaign/summer-sale/</loc></url>"
        "</urlset>",
    )
    client, session = make_client(
        {
            "https://www.example.com/robots.txt": robots,
            "https://www.example.com/sitemap_index.xml": index,
            "https://www.example.com/sitemap-campaign.xml": child,
            "https://www.example.com/campaign/summer-sale-2026/": route(200, "ok"),
        }
    )
    entry = ud.check_registered_url(provider, old, status=404, client=client)
    urls = [c["url"] for c in entry["candidates"]]
    assert urls[0] == "https://www.example.com/campaign/summer-sale-2026/"
    assert entry["candidates"][0]["method"] == "sitemap"
    assert "https://www.example.com/about/" not in urls  # キーワードに合わない
    assert all("px.a8.net" not in call and "example.org" not in call for call in session.calls)
    # 索引から辿ったのはキーワードに合う1件だけ
    assert "https://www.example.com/sitemap-news.xml" not in session.calls
    assert len(session.calls) <= 10


def test_jal_like_404_finds_blackflyday_from_top_page_links() -> None:
    provider = {"id": "jalpack", "official_domains": ["jal.co.jp"]}
    old = "https://www.jal.co.jp/jp/ja/domtour/jaldp/blackfriday/"
    new = "https://www.jal.co.jp/jp/ja/campaign/blackflyday/"
    top_html = """
      <a href="/jp/ja/campaign/blackflyday/">JAL ブラックフライデー 開催中</a>
      <a href="/jp/ja/domtour/">国内ツアー</a>
      <a href="/private/campaign/">非公開キャンペーン</a>
      <a href="https://px.a8.net/svt/ejp?a8mat=x">クーポン</a>
      <a href="https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1">キャンペーン</a>
      <a href="https://t.afi-b.com/visit.php?a=1">ブラックフライデー</a>
      <a href="https://www.other-site.com/campaign/">キャンペーン</a>
      <a href="/img/blackfriday.png">画像</a>
    """
    client, session = make_client(
        {
            "https://www.jal.co.jp/robots.txt": ROBOTS_OK,
            "https://www.jal.co.jp/": route(200, top_html),
            new: route(200, "ブラックフライデー"),
        }
    )
    entry = ud.check_registered_url(provider, old, status=404, client=client)
    assert entry["candidates"], entry
    best = entry["candidates"][0]
    assert best["url"] == new
    assert best["method"] == "page_link"
    assert best["status"] == 200
    for call in session.calls:
        assert not ud.is_blocked_host(ud.host_of(call))
        assert ud.same_site(call, old)
    assert "https://www.jal.co.jp/private/campaign/" not in session.calls  # robots.txt で禁止
    assert len(session.calls) <= 10


def test_redirect_to_top_triggers_discovery_through_official_monitor() -> None:
    provider = {
        "id": "sample",
        "label": "Sample",
        "data_dir": "official_coupon_data/sample",
        "official_domains": ["example.com"],
        "official_sources": [{"url": "https://www.example.com/campaign/winter/", "fetch_method": "html"}],
    }

    def fake_fetch(source, timeout=30):
        return {
            "url": source["url"],
            "ok": True,
            "status_code": 200,
            "http_status": 200,
            "final_url": "https://www.example.com/",
            "fetch_method": "html",
            "text": "トップページ " * 100,
            "error": "",
        }

    client, session = make_client(
        {
            "https://www.example.com/robots.txt": ROBOTS_OK,
            "https://www.example.com/": route(200, '<a href="/campaign/winter-2026/">冬のキャンペーン</a>'),
            "https://www.example.com/campaign/winter-2026/": route(200, "ok"),
        }
    )
    old_root = monitor.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        monitor.configure_root(Path(tmp))
        try:
            with patch.object(monitor, "fetch_official_source", side_effect=fake_fetch), patch.object(
                ud, "DiscoveryClient", lambda: client
            ):
                payload = monitor.run_official_deal_monitor(provider)
        finally:
            monitor.configure_root(old_root)
    assert payload["url_change_suspected"] is True
    assert payload["url_change_label"] == "URL変更の可能性"
    assert payload["url_check_status"] == 200
    assert payload["url_checks"][0]["trigger"] == "redirect_to_generic_page"
    assert payload["url_candidates"][0]["url"] == "https://www.example.com/campaign/winter-2026/"
    assert payload["url_candidates"][0]["registered_url"] == "https://www.example.com/campaign/winter/"


def test_official_monitor_without_trigger_makes_no_discovery_requests() -> None:
    provider = {
        "id": "sample",
        "label": "Sample",
        "data_dir": "official_coupon_data/sample",
        "official_sources": [{"url": "https://www.example.com/deals/", "fetch_method": "html"}],
    }

    def fake_fetch(source, timeout=30):
        return {"url": source["url"], "ok": True, "status_code": 200, "http_status": 200,
                "fetch_method": "html", "text": "セール " * 100, "error": ""}

    def forbidden_client():
        raise AssertionError("きっかけが無いのに探索した")

    old_root = monitor.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        monitor.configure_root(Path(tmp))
        try:
            with patch.object(monitor, "fetch_official_source", side_effect=fake_fetch), patch.object(
                ud, "DiscoveryClient", forbidden_client
            ):
                payload = monitor.run_official_deal_monitor(provider)
        finally:
            monitor.configure_root(old_root)
    assert "url_change_suspected" not in payload


def test_fetch_official_source_keeps_http_status_for_discovery() -> None:
    def fake_requests_fetch(url, timeout=30, retries=2):
        raise monitor.OfficialFetchError("HTTP 404", status_code=404)

    with patch.object(monitor, "_requests_fetch", side_effect=fake_requests_fetch):
        result = monitor.fetch_official_source({"url": "https://www.example.com/x/", "fetch_method": "html"})
    assert result["ok"] is False
    assert result["http_status"] == 404


# ---------------------------------------------------------------------------
# 守ること: 遮断ホスト・除外の2社・回数上限・robots.txt・登録表
# ---------------------------------------------------------------------------


def test_blocked_tracking_hosts_constant_and_guard() -> None:
    assert ud.BLOCKED_TRACKING_HOSTS == {
        "ck.jp.ap.valuecommerce.com",
        "ad.jp.ap.valuecommerce.com",
        "px.a8.net",
        "t.afi-b.com",
    }
    for host in ud.BLOCKED_TRACKING_HOSTS:
        assert not ud.is_request_allowed(f"https://{host}/anything")
        assert not ud.is_request_allowed(f"http://{host.upper()}./x")
    client, session = make_client({})
    for host in ud.BLOCKED_TRACKING_HOSTS:
        try:
            client._raw_get(f"https://{host}/")
        except ud.DiscoveryBlocked:
            pass
        else:  # pragma: no cover
            raise AssertionError(host)
    assert session.calls == []


def test_redirect_to_tracking_host_is_never_followed() -> None:
    provider = {"id": "sample"}
    old = "https://www.example.com/sale"
    client, session = make_client(
        {
            "https://www.example.com/robots.txt": ROBOTS_OK,
            old + "/": route(302, "", "https://px.a8.net/svt/ejp?a8mat=1"),
            "https://www.example.com/": route(302, "", "https://t.afi-b.com/visit.php?a=1"),
            "https://www.example.com/sitemap.xml": route(
                200,
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://www.example.com/sale-go/</loc></url></urlset>",
            ),
            "https://www.example.com/sale-go/": route(301, "", "https://ad.jp.ap.valuecommerce.com/x"),
        }
    )
    entry = ud.check_registered_url(provider, old, status=404, client=client)
    assert entry["candidates"] == []
    assert all(not ud.is_blocked_host(ud.host_of(call)) for call in session.calls)
    assert {item["reason"] for item in client.skipped} >= {"blocked_host"}


def test_ikyu_and_yahoo_travel_are_excluded_without_any_request() -> None:
    client, session = make_client({})
    cases = [
        ({"id": "ikkyu"}, "https://www.ikyu.com/campaign/"),
        ({"id": "yahoo_travel"}, "https://travel.yahoo.co.jp/campaign/"),
        ({"id": "renamed", "official_domains": ["ikyu.com"]}, "https://www.ikyu.com/x/"),
        ({"id": "other"}, "https://furusato.ikyu.com/coupon/"),
        ({"id": "other"}, "https://travel.yahoo.co.jp/promo/"),
    ]
    for provider, url in cases:
        assert ud.check_registered_url(provider, url, status=404, client=client) is None
        sources = [{"url": url}]
        assert ud.check_sources(provider, sources, [{"ok": False, "http_status": 404}], client=client) == {}
    assert session.calls == []
    assert ud.is_excluded_provider({"id": "ikkyu"})
    assert ud.is_excluded_provider({"id": "yahoo_travel"})


def test_registry_ikyu_and_yahoo_entries_are_excluded() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    providers = {p["id"]: p for p in registry["providers"]}
    assert ud.is_excluded_provider(providers["ikkyu"])
    assert ud.is_excluded_provider(providers["yahoo_travel"])


def test_request_budget_and_robots_disallow_are_respected() -> None:
    provider = {"id": "sample"}
    old = "https://www.example.com/campaign/a/"
    links = "".join(f'<a href="/campaign/a-{i}/">キャンペーン {i}</a>' for i in range(30))
    routes = {
        "https://www.example.com/robots.txt": ROBOTS_OK,
        "https://www.example.com/": route(200, links),
    }
    routes.update({f"https://www.example.com/campaign/a-{i}/": route(200, "ok") for i in range(30)})
    client, session = make_client(routes, max_requests=6)
    ud.check_registered_url(provider, old, status=404, client=client)
    assert len(session.calls) <= 6

    # robots.txt が全体を禁止していれば、robots.txt 以外は取らない
    client2, session2 = make_client({"https://www.example.com/robots.txt": route(200, "User-agent: *\nDisallow: /\n")})
    entry = ud.check_registered_url(provider, old, status=404, client=client2)
    assert entry["candidates"] == []
    assert session2.calls == ["https://www.example.com/robots.txt"]


def test_discovery_never_modifies_registry() -> None:
    before = hashlib.sha256(REGISTRY.read_bytes()).hexdigest()
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    orion = next(p for p in registry["providers"] if p["id"] == "orion_tour")
    registered = orion["official_sources"][0]["url"]
    client, _ = make_client(
        {
            "https://www.orion-tour.co.jp/robots.txt": ROBOTS_OK,
            registered.rstrip("/"): route(200, "ok"),
        }
    )
    entry = ud.check_registered_url(orion, registered, status=404, client=client)
    assert entry["candidates"]
    # 返すだけで、渡した登録情報にもファイルにも書き戻さない
    assert orion["official_sources"][0]["url"] == registered
    assert hashlib.sha256(REGISTRY.read_bytes()).hexdigest() == before
    source = (ROOT / "url_discovery.py").read_text(encoding="utf-8")
    assert "provider_registry" not in source.replace("config/provider_registry.json）は書き換えない", "")
    assert "write_text" not in source and "open(" not in source


# ---------------------------------------------------------------------------
# 記録と表示（状態ファイル・実行サマリー・ダッシュボード・Macの取得）
# ---------------------------------------------------------------------------


SAMPLE_URL_CHANGE = {
    "url_change_suspected": True,
    "url_change_label": "URL変更の可能性",
    "url_check_status": 404,
    "url_candidates": [
        {"url": "https://www.jal.co.jp/jp/ja/campaign/blackflyday/", "status": 200, "method": "page_link", "score": 5}
    ],
}


def test_run_summary_and_warning_list_url_change_providers(capsys) -> None:
    summaries = [
        {"provider_id": "jalpack", "provider_label": "JALパック", "status": "success", **SAMPLE_URL_CHANGE},
        {"provider_id": "his", "status": "success"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(runner, "STATUS_ROOT", Path(tmp)):
            latest = runner.write_run_summary("jp_mac", summaries)
        payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["url_change_suspected_provider_ids"] == ["jalpack"]
    assert payload["url_change_suspected_count"] == 1
    runner.report_run_outcome(summaries, fail_on_error=False)
    out = capsys.readouterr().out
    assert "URL変更の可能性" in out and "blackflyday" in out


def test_real_monitor_status_reads_fresh_url_check_file_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data_dir = root / "jalpack_coupon_data"
        data_dir.mkdir()
        started = runner.now_jst()
        provider = {"id": "jalpack", "data_dir": "jalpack_coupon_data"}
        path = data_dir / runner.URL_CHECK_FILE
        with patch.object(runner, "ROOT", root):
            path.write_text(json.dumps({"checked_at": started.isoformat(), **SAMPLE_URL_CHANGE}), encoding="utf-8")
            assert runner.load_url_change(provider, started)["url_check_status"] == 404
            old = (started - timedelta(hours=1)).isoformat()
            path.write_text(json.dumps({"checked_at": old, **SAMPLE_URL_CHANGE}), encoding="utf-8")
            assert runner.load_url_change(provider, started) == {}


def test_dashboard_payload_and_html_show_url_change() -> None:
    provider = {
        "id": "sample_ota",
        "label": "サンプル",
        "data_dir": "official_coupon_data/sample_ota",
        "coverage_status": "official_codex",
        "check_frequency": "daily",
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        status_dir = root / "provider_check_data" / "sample_ota"
        status_dir.mkdir(parents=True)
        (status_dir / "latest.json").write_text(
            json.dumps({"check_type": "official_page_candidate", **SAMPLE_URL_CHANGE}), encoding="utf-8"
        )
        with patch.multiple(generate_dashboard, ROOT=root, CHECK_STATUS_ROOT=root / "provider_check_data"):
            payload = generate_dashboard.build_provider_payload(provider, {})
    assert payload["url_change_suspected"] is True
    assert payload["url_change_label"] == "URL変更の可能性"
    assert payload["url_candidates"][0]["url"].endswith("/blackflyday/")
    assert payload["summary"]["URL変更"].startswith("URL変更の可能性（404） 候補: https://www.jal.co.jp/")
    assert "URL変更" in generate_dashboard.SUMMARY_COLUMNS
    html = generate_dashboard.generate_html({"providers": [], "generated_at": "", "summary_rows": [], "columns": {}})
    assert "function urlChangeHtml(provider)" in html
    assert "details.push(urlChangeHtml(provider));" in html


def test_dashboard_summary_is_none_without_url_change() -> None:
    assert generate_dashboard.url_change_summary({}) == "なし"
    assert generate_dashboard.url_change_summary({"url_change_suspected": False}) == "なし"


def test_jalpack_mac_fetch_writes_url_check_with_candidates() -> None:
    old = "https://www.jal.co.jp/jp/ja/domtour/jaldp/blackfriday/"
    client, _ = make_client(
        {
            "https://www.jal.co.jp/robots.txt": ROBOTS_OK,
            "https://www.jal.co.jp/": route(200, '<a href="/jp/ja/campaign/blackflyday/">ブラックフライデー</a>'),
            "https://www.jal.co.jp/jp/ja/campaign/blackflyday/": route(200, "ok"),
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "url_check_latest.json"
        with patch.object(jalpack, "URL_CHECK_FILE", path), patch.object(ud, "DiscoveryClient", lambda: client):
            summary = jalpack.write_url_check(
                [
                    (old, 404, old),
                    ("https://www.jal.co.jp/jp/ja/domtour/hayakime-cpn/", 200, "https://www.jal.co.jp/jp/ja/domtour/hayakime-cpn/"),
                ]
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
    assert summary["url_change_suspected"] is True
    assert saved["url_change_suspected"] is True
    assert saved["checked_at"]
    assert [c["url"] for c in saved["url_candidates"]] == ["https://www.jal.co.jp/jp/ja/campaign/blackflyday/"]
    assert len(saved["url_checks"]) == 1  # 200 のページは探さない


def test_jalpack_url_check_records_false_when_all_pages_ok() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "url_check_latest.json"

        def forbidden_client():
            raise AssertionError("きっかけが無いのに探索した")

        with patch.object(jalpack, "URL_CHECK_FILE", path), patch.object(ud, "DiscoveryClient", forbidden_client):
            jalpack.write_url_check([("https://www.jal.co.jp/jp/ja/domtour/hayakime-cpn/", 200, "")])
        saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["url_change_suspected"] is False


def test_registered_urls_keep_trailing_slash_for_codex_targets() -> None:
    """Codexへ渡す登録URLは登録どおり（末尾「/」つき）。旧実装は削って403になった。"""
    import grok_deal_discovery as g

    provider = {
        "id": "orion_tour",
        "official_sources": [
            {"url": "https://www.orion-tour.co.jp/air/fair/feature/coupon/"},
            {"url": "https://www.orion-tour.co.jp/air/fair/feature/coupon"},
        ],
    }
    urls = g.registered_urls(provider)
    assert urls == ["https://www.orion-tour.co.jp/air/fair/feature/coupon/"]
    classified = g.classify_candidate(
        {"title": "x", "official_url": "https://www.orion-tour.co.jp/air/fair/feature/coupon"},
        provider,
        titles=[],
        urls=urls,
    )
    assert classified["status"] == "known_url"


def test_registry_has_owner_given_urls_and_not_old_ones() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    assert "https://www.jal.co.jp/jp/ja/campaign/blackflyday/" in text
    assert "https://www.orion-tour.co.jp/air/fair/feature/coupon/" in text
    assert "/domtour/jaldp/blackfriday/" not in text.replace("旧 /jp/ja/domtour/jaldp/blackfriday/ は404", "")
    assert '"https://www.orion-tour.co.jp/air/fair/feature/coupon"' not in text


def test_403_without_candidates_is_recorded_but_not_flagged() -> None:
    """サイト全体が自動取得を断る（robots.txt も403）ときは「URL変更の可能性」にしない。"""
    provider = {"id": "jalpack"}
    old = "https://www.jal.co.jp/jp/ja/domtour/jaldp/blackfriday/"
    client, session = make_client({"https://www.jal.co.jp/robots.txt": route(403, "Access Denied")})
    entry = ud.check_registered_url(provider, old, status=403, client=client)
    assert entry["candidates"] == [] and entry["suspected"] is False
    assert session.calls == ["https://www.jal.co.jp/robots.txt"]  # 拒否後は何も取らない
    summary = ud.summarize_url_checks([entry])
    assert summary["url_change_suspected"] is False
    assert "url_change_label" not in summary
    # 404 なら候補が無くても可能性ありとして表示する
    client2, _ = make_client({"https://www.jal.co.jp/robots.txt": route(403, "Access Denied")})
    entry404 = ud.check_registered_url(provider, old, status=404, client=client2)
    assert ud.summarize_url_checks([entry404])["url_change_suspected"] is True
