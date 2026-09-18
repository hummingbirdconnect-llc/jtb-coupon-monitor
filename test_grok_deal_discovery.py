#!/usr/bin/env python3
"""grok_deal_discovery のテスト。Grok CLI は呼ばず、runner を差し替えて確認する。"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import grok_deal_discovery as g


def _provider(**overrides):
    provider = {
        "id": "relux",
        "label": "Relux",
        "official_domains": ["rlx.jp"],
        "official_sources": [{"url": "https://rlx.jp/timesale/", "fetch_method": "auto"}],
        "data_dir": "official_coupon_data/relux",
        "legacy_data_dir": "manual_coupon_data/relux",
        "check_frequency": "on_demand",
        "cadence_days": 0,
    }
    provider.update(overrides)
    return provider


def test_select_providers_daily_every_day_and_weekly_only_on_monday() -> None:
    registry = g.load_registry()
    daily = {p["id"] for p in g.select_providers(registry, "daily", "")}
    assert daily == {"his", "jtb", "knt", "jalpack", "jalan", "rakuten_travel"}
    weekly = {p["id"] for p in g.select_providers(registry, "weekly", "")}
    assert {"relux", "booking", "toku"} <= weekly
    assert not (daily & weekly)
    monday = {p["id"] for p in g.select_providers(registry, "auto", "", date(2026, 9, 21))}
    tuesday = {p["id"] for p in g.select_providers(registry, "auto", "", date(2026, 9, 22))}
    assert monday == daily | weekly
    assert tuesday == daily
    # 公式ページ未登録の指定時会社（記事由来だけ）は対象外
    assert "newt" not in weekly


def test_classify_marks_known_url_known_title_and_new() -> None:
    provider = _provider()
    titles = ["Relux新規会員限定5％クーポン", "三太郎の日クーポン"]
    urls = ["https://rlx.jp/timesale"]
    known_url = g.classify_candidate(
        {"title": "秋のタイムセール", "kind": "sale", "found_url": "https://x.com/relux/status/1",
         "official_url": "https://rlx.jp/timesale/?utm=x", "evidence": "最大50％OFF", "confidence": "high"},
        provider, titles=titles, urls=urls,
    )
    assert known_url["status"] == "known_url"
    assert known_url["official_domain_match"] is True
    known_title = g.classify_candidate(
        {"title": "Relux 新規会員限定 5%クーポン", "kind": "coupon", "found_url": "https://x.com/a/status/2",
         "official_url": "", "evidence": "5％OFF", "confidence": "low"},
        provider, titles=titles, urls=urls,
    )
    assert known_title["status"] == "known_title"
    assert known_title["matched_title"] == "Relux新規会員限定5％クーポン"
    new = g.classify_candidate(
        {"title": "Relux×PayPay 10%戻ってくるキャンペーン", "kind": "campaign",
         "found_url": "https://x.com/b/status/3", "official_url": "https://paypay.ne.jp/event/relux/",
         "evidence": "10％戻ってくる", "confidence": "medium"},
        provider, titles=titles, urls=urls,
    )
    assert new["status"] == "new"
    assert new["official_domain_match"] is False
    assert new["kind"] == "campaign"


def test_parse_grok_output_accepts_wrapped_and_plain_json() -> None:
    plain = json.dumps({"candidates": [{"title": "A"}]})
    assert g.parse_grok_output(plain) == [{"title": "A"}]
    wrapped = json.dumps({"type": "result", "result": json.dumps({"candidates": [{"title": "B"}]})})
    assert g.parse_grok_output(wrapped) == [{"title": "B"}]
    streaming = "\n".join([json.dumps({"type": "system"}), json.dumps({"structured_output": {"candidates": []}})])
    assert g.parse_grok_output(streaming) == []
    try:
        g.parse_grok_output("no json here")
    except ValueError:
        pass
    else:
        raise AssertionError("candidates が無い出力は ValueError になるべき")


def test_discover_provider_writes_hints_and_continues_on_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        g.configure_root(Path(tmp))
        try:
            provider = _provider(data_dir="nope", legacy_data_dir="nope")
            at = datetime(2026, 9, 21, 7, 0, tzinfo=g.JST)

            def ok_runner(prompt: str) -> str:
                assert "Relux" in prompt and "rlx.jp" in prompt
                return json.dumps({"candidates": [
                    {"title": "秋セール", "kind": "sale", "found_url": "https://x.com/r/status/9",
                     "official_url": "https://rlx.jp/special/autumn/", "evidence": "最大30％OFF", "confidence": "medium"},
                    {"title": "秋セール", "kind": "sale", "found_url": "https://x.com/r/status/9",
                     "official_url": "https://rlx.jp/special/autumn/", "evidence": "重複", "confidence": "low"},
                ]})

            result = g.discover_provider(provider, runner=ok_runner, at=at)
            assert result["status"] == "ok"
            assert result["candidate_count"] == 1  # 重複は1件にまとめる
            assert result["new_count"] == 1
            saved = json.loads((Path(tmp) / result["hints_file"]).read_text(encoding="utf-8"))
            assert saved["verification"] == "unverified"
            assert saved["candidates"][0]["official_domain_match"] is True

            def bad_runner(prompt: str) -> str:
                raise RuntimeError("grok exit=1: not logged in")

            failed = g.discover_provider(provider, runner=bad_runner, at=at)
            assert failed["status"] == "error"
            assert "not logged in" in failed["error"]
            assert failed["candidate_count"] == 0
            summary_path = g.write_run_summary("auto", [result, failed], at)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            assert summary["error_count"] == 1
            assert summary["new_count"] == 1
        finally:
            g.configure_root(Path(__file__).resolve().parent)


TESTS = [
    test_select_providers_daily_every_day_and_weekly_only_on_monday,
    test_classify_marks_known_url_known_title_and_new,
    test_parse_grok_output_accepts_wrapped_and_plain_json,
    test_discover_provider_writes_hints_and_continues_on_error,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  PASSED {test.__name__}")
    print("grok deal discovery tests passed")
