#!/usr/bin/env python3
"""速報タブ・キャンペーン欄・JTB残り枚数判定の回帰テスト。"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from generate_dashboard import (
    DEFAULT_CHANGE_RULES,
    annotate_change_log,
    apply_change_rules,
    change_kind,
    format_coupon_row,
    format_log_rows,
    load_registry,
    period_is_over,
    provider_change_rules,
    stock_label,
)
from jtb_coupon_monitor import JST, summarize_password_stock


class ChangeKindTest(unittest.TestCase):
    def test_normalizes_each_provider_wording(self) -> None:
        self.assertEqual(change_kind("🆕 新規"), "new")
        self.assertEqual(change_kind("new"), "new")
        self.assertEqual(change_kind("❌ 消失"), "gone")
        self.assertEqual(change_kind("gone"), "gone")
        self.assertEqual(change_kind("🔴 配布終了"), "ended")
        self.assertEqual(change_kind("ステータス変更 配布中→配布終了"), "ended")
        self.assertEqual(change_kind("🟢 配布再開"), "reopened")
        self.assertEqual(change_kind("ステータス変更 配布終了→配布中"), "reopened")
        self.assertEqual(change_kind("status"), "status")


class AnnotateChangeLogTest(unittest.TestCase):
    def test_marks_reappearance_flaps_and_return(self) -> None:
        log = [
            {"date": "2026-10-01", "type": "🆕 新規", "id": "night"},
            {"date": "2026-10-02", "type": "❌ 消失", "id": "night"},
            {"date": "2026-10-02", "type": "🆕 新規", "id": "night"},
            {"date": "2026-10-03", "type": "🆕 新規", "id": "fresh"},
        ]
        rows = {(row["date"], row["type"], row["id"]): row for row in annotate_change_log(log)}

        first = rows[("2026-10-01", "🆕 新規", "night")]
        self.assertFalse(first["_reappeared"])
        gone = rows[("2026-10-02", "❌ 消失", "night")]
        self.assertTrue(gone["_came_back"])
        again = rows[("2026-10-02", "🆕 新規", "night")]
        self.assertTrue(again["_reappeared"])
        self.assertEqual(again["_flaps"], 3)
        self.assertFalse(rows[("2026-10-03", "🆕 新規", "fresh")]["_reappeared"])


class ChangeRulesTest(unittest.TestCase):
    """公式ページの掲載を正とし、配布終了・再開をイベントにしないこと（2026-11-13 オーナー決定）。"""

    JTB_RULES = {
        "new_requires_unseen_days": 7,
        "gone_requires_unlisted": True,
    }
    LISTING = {
        # 10/18から10/20まで公式ページに載り続けているクーポン
        "stay": ["2026-10-18", "2026-10-19", "2026-10-20"],
        # 10/18で公式ページから消えたクーポン
        "left": ["2026-10-17", "2026-10-18"],
    }

    def rows(self) -> list[dict]:
        return [
            {"日付": "2026-10-20", "ID": "stay", "種別": "🔴 配布終了", "_kind": "ended"},
            {"日付": "2026-10-20", "ID": "stay", "種別": "🟢 配布再開", "_kind": "reopened"},
            {"日付": "2026-10-20", "ID": "stay", "種別": "🆕 新規", "_kind": "new"},
            {"日付": "2026-10-20", "ID": "stay", "種別": "❌ 消失", "_kind": "gone"},
            {"日付": "2026-10-19", "ID": "left", "種別": "❌ 消失", "_kind": "gone"},
            {"日付": "2026-10-19", "ID": "fresh", "種別": "🆕 新規", "_kind": "new"},
        ]

    def applied(self, rules: dict) -> list[dict]:
        return apply_change_rules(self.rows(), rules, self.LISTING)

    def test_default_rules_drop_stock_toggles_and_keep_new_and_gone(self) -> None:
        rows = self.applied(dict(DEFAULT_CHANGE_RULES))
        self.assertEqual(
            [(row["ID"], row["_kind"]) for row in rows],
            [("stay", "new"), ("stay", "gone"), ("left", "gone"), ("fresh", "new")],
        )
        self.assertFalse(any(row["_minor"] for row in rows))

    def test_stock_end_and_reopen_are_never_events(self) -> None:
        for rules in (dict(DEFAULT_CHANGE_RULES), self.JTB_RULES):
            kinds = {row["_kind"] for row in self.applied(rules)}
            self.assertFalse(kinds & {"ended", "reopened", "status"})

    def test_new_on_a_coupon_already_in_recent_official_data_is_dropped(self) -> None:
        rows = self.applied(self.JTB_RULES)
        self.assertNotIn(("stay", "new"), [(row["ID"], row["_kind"]) for row in rows])

    def test_gone_while_still_listed_officially_is_not_an_end_event(self) -> None:
        rows = self.applied(self.JTB_RULES)
        self.assertNotIn(("stay", "gone"), [(row["ID"], row["_kind"]) for row in rows])

    def test_only_removal_from_official_page_and_first_appearance_remain(self) -> None:
        rows = self.applied(self.JTB_RULES)
        self.assertEqual(
            [(row["ID"], row["_kind"]) for row in rows],
            [("left", "gone"), ("fresh", "new")],
        )
        self.assertFalse(any(row["_minor"] for row in rows))
        self.assertFalse(any("_demoted_from" in row for row in rows))

    def test_registry_enables_the_rule_for_jtb_only(self) -> None:
        registry = {provider["id"]: provider for provider in load_registry()}
        jtb = provider_change_rules(registry["jtb"])
        self.assertTrue(jtb["gone_requires_unlisted"])
        self.assertGreater(jtb["new_requires_unseen_days"], 0)
        others = [
            provider_id
            for provider_id, provider in registry.items()
            if provider_id != "jtb" and provider.get("change_rules")
        ]
        self.assertEqual(others, [])
        self.assertEqual(provider_change_rules(registry["knt"]), DEFAULT_CHANGE_RULES)

    def test_unknown_rule_names_are_ignored(self) -> None:
        rules = provider_change_rules({"change_rules": {"nonsense": True}})
        self.assertEqual(rules, DEFAULT_CHANGE_RULES)


class ChangeLogStockToggleTest(unittest.TestCase):
    """変更ログから、各社の書き方の配布終了・再開を取り除くこと。"""

    def test_stock_toggles_of_every_provider_are_not_logged(self) -> None:
        log = [
            {"date": "2026-10-01", "type": "🆕 新規", "id": "a"},
            {"date": "2026-10-02", "type": "🔴 配布終了", "id": "a"},
            {"date": "2026-10-03", "type": "🟢 配布再開", "id": "a"},
            {"date": "2026-10-03", "type": "ステータス変更 配布中→配布終了", "id": "b"},
            {"date": "2026-10-04", "type": "ステータス変更 配布終了→配布中", "id": "b"},
            {"date": "2026-10-04", "type": "status", "id": "c", "detail": "配布中 → 配布終了"},
            {"date": "2026-10-05", "type": "❌ 消失", "id": "a"},
        ]
        rows = format_log_rows(log)
        self.assertEqual([(row["ID"], row["_kind"]) for row in rows], [("a", "gone"), ("a", "new")])
        gone = rows[0]
        # 終了・再開は出入りの回数にも数えない
        self.assertEqual(gone["_flaps"], 2)
        self.assertFalse(gone["_came_back"])


class SoldOutStillListedTest(unittest.TestCase):
    """公式ページに載っている品切れクーポンは配布中のまま、残り枚数は属性で残すこと。"""

    PROVIDER = {"id": "knt", "label": "テスト"}

    def row(self, **coupon) -> dict:
        return format_coupon_row({"id": "x", "title": "t", **coupon}, self.PROVIDER, "coupons")

    def test_sold_out_coupon_stays_active_with_remaining_count(self) -> None:
        row = self.row(stock_status="配布終了", stock_end_reason="品切れ", stock_remaining=0, stock_total=100)
        self.assertEqual(row["配布状況"], "配布中")
        self.assertEqual(row["残り枚数"], "0 / 100（品切れ）")

    def test_booking_period_end_is_still_shown_as_ended(self) -> None:
        row = self.row(stock_status="配布終了", stock_end_reason="予約受付の終了", stock_remaining=0, stock_total=100)
        self.assertEqual(row["配布状況"], "配布終了")

    def test_active_coupon_is_unchanged(self) -> None:
        row = self.row(stock_status="配布中", stock_remaining=1, stock_total=100)
        self.assertEqual(row["配布状況"], "配布中")
        self.assertEqual(row["残り枚数"], "1 / 100")


class CampaignPeriodTest(unittest.TestCase):
    def test_only_drops_periods_whose_dates_are_all_past(self) -> None:
        today = date(2026, 10, 15)
        self.assertTrue(period_is_over("予約2026年2月17日～2026年9月30日、出発2026/4/1～2026/9/30", today))
        self.assertFalse(period_is_over("予約2026年8月17日～2026年11月16日", today))
        self.assertFalse(period_is_over("毎月20～29日（終了日は未確認）", today))


class PasswordStockTest(unittest.TestCase):
    NOW = datetime(2026, 10, 15, 9, 0, tzinfo=JST)

    def tier(self, stock: int, count: int, reserve_to: str = "202611010000") -> dict:
        return {
            "Password": "pw",
            "CouponCount": count,
            "StockCount": stock,
            "SoldOutFlag": 0 if stock else 1,
            "ReserveDateTimeFrom": "202609011000",
            "ReserveDateTimeTo": reserve_to,
            "DiscountAmount": 3000,
            "MinimumCharge": 20000,
        }

    def test_any_tier_with_stock_is_active(self) -> None:
        summary = summarize_password_stock([self.tier(0, 200), self.tier(12, 300)], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布中")
        self.assertEqual((summary["stock_remaining"], summary["stock_total"]), (12, 500))

    def test_all_sold_out_is_ended_with_reason(self) -> None:
        summary = summarize_password_stock([self.tier(0, 333)], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布終了")
        self.assertEqual(summary["stock_end_reason"], "品切れ")
        self.assertEqual(stock_label(summary), "0 / 333（品切れ）")

    def test_booking_window_closed_is_ended(self) -> None:
        summary = summarize_password_stock([self.tier(50, 100, reserve_to="202610010000")], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布終了")
        self.assertEqual(summary["stock_end_reason"], "予約受付の終了")

    def test_no_api_data_keeps_previous_judgement(self) -> None:
        self.assertIsNone(summarize_password_stock([], now=self.NOW))


if __name__ == "__main__":
    unittest.main()
