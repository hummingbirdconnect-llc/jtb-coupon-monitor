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
    """公式ページの掲載実績を優先し、配布終了・再開を主な変化に数えないこと。"""

    JTB_RULES = {
        "count_status_toggle_as_change": False,
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

    def test_default_rules_keep_every_recorded_kind(self) -> None:
        rows = self.applied(dict(DEFAULT_CHANGE_RULES))
        self.assertEqual(
            [row["_kind"] for row in rows],
            ["ended", "reopened", "new", "gone", "gone", "new"],
        )
        self.assertFalse(any(row["_minor"] for row in rows))

    def test_status_toggle_is_kept_only_in_the_quiet_section(self) -> None:
        ended, reopened = self.applied(self.JTB_RULES)[:2]
        self.assertEqual((ended["_kind"], reopened["_kind"]), ("ended", "reopened"))
        self.assertTrue(ended["_minor"])
        self.assertTrue(reopened["_minor"])

    def test_new_on_a_coupon_already_in_recent_official_data_is_not_new(self) -> None:
        row = self.applied(self.JTB_RULES)[2]
        self.assertEqual(row["_kind"], "reopened")
        self.assertEqual(row["_demoted_from"], "new")
        self.assertTrue(row["_minor"])

    def test_gone_while_still_listed_officially_is_not_gone(self) -> None:
        row = self.applied(self.JTB_RULES)[3]
        self.assertEqual(row["_kind"], "ended")
        self.assertEqual(row["_demoted_from"], "gone")
        self.assertTrue(row["_minor"])

    def test_real_new_and_real_gone_stay_as_main_changes(self) -> None:
        left_gone, fresh_new = self.applied(self.JTB_RULES)[4:]
        self.assertEqual((left_gone["_kind"], fresh_new["_kind"]), ("gone", "new"))
        self.assertFalse(left_gone["_minor"])
        self.assertFalse(fresh_new["_minor"])
        self.assertNotIn("_demoted_from", left_gone)
        self.assertNotIn("_demoted_from", fresh_new)

    def test_registry_enables_the_rule_for_jtb_only(self) -> None:
        registry = {provider["id"]: provider for provider in load_registry()}
        jtb = provider_change_rules(registry["jtb"])
        self.assertFalse(jtb["count_status_toggle_as_change"])
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
