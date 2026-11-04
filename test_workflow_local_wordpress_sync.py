"""GitHub ActionsがWordPress本番同期を再開しないための契約テスト。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "coupon-monitor.yml"


class WorkflowLocalWordPressSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_actions_only_validates_the_feed(self) -> None:
        calls = [
            line.strip()
            for line in self.workflow.splitlines()
            if "push_his_wordpress_feed.py" in line
        ]
        self.assertEqual(
            ["if ! python wordpress_feed/push_his_wordpress_feed.py --dry-run; then"],
            calls,
        )

    def test_wordpress_credentials_are_not_exposed_to_the_workflow(self) -> None:
        for variable in ("YF_WP_URL", "YF_WP_USER", "YF_WP_APP_PASSWORD"):
            with self.subTest(variable=variable):
                self.assertNotIn(variable, self.workflow)

    def test_validated_feed_is_still_committed(self) -> None:
        self.assertIn("wordpress_feed/his-monitor-feed.json", self.workflow)

    def test_his_feed_failure_does_not_drop_other_provider_commits(self) -> None:
        feed = self.workflow.split(
            "- name: HIS WordPress用フィード生成・ローカル同期前検証", 1
        )[1].split("\n      - name:", 1)[0]
        self.assertIn("id: his_feed", feed)
        self.assertIn("continue-on-error: true", feed)
        self.assertIn("git checkout -- wordpress_feed/his-monitor-feed.json", feed)

        commit = self.workflow.split("- name: コミット＆プッシュ", 1)[1].split(
            "\n      - name:", 1
        )[0]
        self.assertIn("if: ${{ !cancelled() }}", commit)
        self.assertLess(
            self.workflow.index("- name: コミット＆プッシュ"),
            self.workflow.index("- name: HIS公式監視の失敗をジョブ結果へ反映"),
        )
        reflect = self.workflow.split(
            "- name: HIS公式監視の失敗をジョブ結果へ反映", 1
        )[1]
        self.assertIn("steps.his_feed.outcome == 'failure'", reflect)
        self.assertIn("exit 1", reflect)

        feed_builder = (ROOT / "wordpress_feed" / "build-his-wordpress-feed.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("latest.status !== \"success\"", feed_builder)
        self.assertIn("HIS公式監視の最新実行が成功状態ではありません。", feed_builder)


if __name__ == "__main__":
    unittest.main()
