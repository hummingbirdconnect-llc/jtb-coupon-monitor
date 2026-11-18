import json
import tempfile
import unittest
from pathlib import Path

from deal_catalog import build_catalog, group_key
from sync_deal_coverage import article_matches


class DealCatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.put("config/provider_registry.json", {"providers": [{"id": "ota", "data_dir": "snapshots"}]})
        self.url = "https://example.com/coupons/"
        self.provider = {"id": "ota", "label": "旅行会社", "data_date": "2026-11-18", "rows": []}

    def put(self, name, obj):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")

    def catalog(self):
        return build_catalog(self.root, [self.provider])["rows"]

    def row(self, key, title, code=""):
        return {"ID": key, "種別": "セール", "タイトル": title, "詳細URL": self.url, "割引額": "",
                "配布状況": "配布中", "予約期間": "", "出発/宿泊期間": "", "データ元": "official_codex_audit",
                "クーポンコード": code}

    def lead(self, title, day, folder="discovery_hints"):
        self.put(f"{folder}/ota/hints_{day}.json", {"generated_at": day, "candidates": [
            {"title": title, "official_url": self.url, "kind": "sale"}]})

    def test_repeated_observations_survive_empty_run_and_shared_listing_does_not_merge_offers(self):
        self.provider["rows"] = [self.row("one", "週末限定セール")]
        self.lead("週末限定セール", "2026-10-01")
        self.lead("週末限定セール", "2026-11-15", "ai_intake")
        self.lead("連休限定セール", "2026-11-16")
        self.put("discovery_hints/ota/hints_2026-11-18.json", {"status": "error", "candidates": []})
        first, second = self.catalog()
        self.assertEqual(first["sources"], ["監視データ", "AI収集"])
        self.assertEqual(first["ai_date"], "2026-11-15")
        self.assertEqual(second["stock"], "要確認")

    def test_manual_publication_survives_promotion_to_a_monitored_offer(self):
        title = "春の限定セール"
        self.lead(title, "2026-11-15", "ai_intake")
        self.put("editorial_data/publications.json", [dict(key=group_key("ota", title, self.url),
                 date="2026-11-18", site="自社", url="https://x.com/our_test/status/123")])
        self.provider["rows"] = [self.row("new-id", title)]
        row, = self.catalog()
        self.assertEqual(row["x"][0]["stage"], "posted")
        self.assertEqual(row["articles"], [])

    def test_only_unique_historical_x_thread_is_linked(self):
        self.provider["rows"] = [self.row("one", "特典A"), self.row("two", "特典B")]
        self.put("snapshots/coupons_2026-11-17.json", [
            {"id":"one", "title":"似た題名"}, {"id":"three", "title":"似た題名"}, {"id":"two", "title":"特典B"}])
        self.put("tweets_output/x_threads_2026-11-17.json", {"date":"2026-11-17", "sites":[{
            "display_name":"自社", "threads":[{"provider_id":"ota","title":"似た題名"},
                                         {"provider_id":"ota","title":"特典B","article_url":self.url}]}]})
        first, second = self.catalog()
        self.assertEqual(first["x"], [])
        self.assertEqual(second["x"][0]["stage"], "draft")
        self.assertEqual(second["articles"], [])

    def test_publication_evidence_is_invalidated_when_code_changes(self):
        self.provider["rows"] = [self.row("one", "旧コードのある特典", "NEW2026")]
        self.put("editorial_data/articles.json", [{
            "provider": "ota", "site": "自社", "date": "2026-11-17", "url": self.url, "status": "照合済み",
            "matches": [{"key":"ota/one","title":"旧コードのある特典","basis":"コード一致","code":"old2026"}]}])
        self.assertEqual(self.catalog()[0]["articles"], [])

    def test_code_boundaries_and_ambiguous_names(self):
        rows = [{"key":"a", "title":"同じ会社の重複している長い特典名"},
                {"key":"b", "title":"同じ会社の重複している長い特典名"}]
        codes = {"a":{"travel2026"}, "b":{"travel2026plus"}}
        html = "<p>同じ会社の重複している長い特典名</p><p>TRAVEL2026PLUS</p><script>TRAVEL2026</script>"
        matches = article_matches(rows, codes, html)
        self.assertEqual([(m["key"],m["basis"]) for m in matches], [("b","コード一致")])


if __name__ == "__main__":
    unittest.main()
