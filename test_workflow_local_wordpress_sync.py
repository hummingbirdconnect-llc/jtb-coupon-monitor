"""GitHub ActionsがWordPress本番同期を再開しないための契約テスト。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "coupon-monitor.yml"
HIS_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "his-monitor.yml"


class WorkflowLocalWordPressSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.his_workflow = HIS_WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_actions_only_validates_the_feed(self) -> None:
        calls = [
            line.strip()
            for line in self.his_workflow.splitlines()
            if "push_his_wordpress_feed.py" in line
        ]
        self.assertEqual(
            ["if ! .venv/bin/python wordpress_feed/push_his_wordpress_feed.py --dry-run; then"],
            calls,
        )
        # 本線（GitHubホストのランナー）はフィードに触らない
        self.assertNotIn("push_his_wordpress_feed.py", self.workflow)
        self.assertNotIn("build-his-wordpress-feed.mjs", self.workflow)

    def test_wordpress_credentials_are_not_exposed_to_the_workflow(self) -> None:
        for variable in ("YF_WP_URL", "YF_WP_USER", "YF_WP_APP_PASSWORD"):
            with self.subTest(variable=variable):
                self.assertNotIn(variable, self.workflow)
                self.assertNotIn(variable, self.his_workflow)

    def test_validated_feed_is_still_committed(self) -> None:
        self.assertIn("wordpress_feed/his-monitor-feed.json", self.his_workflow)

    def test_his_runs_on_the_japanese_self_hosted_runner(self) -> None:
        self.assertIn("runs-on: [self-hosted, his]", self.his_workflow)
        self.assertIn("provider_check_runner.py --provider-id his", self.his_workflow)
        # 平日 7:00 / 15:45 JST（UTC指定）
        self.assertIn("cron: '0 22 * * 0-4'", self.his_workflow)
        self.assertIn("cron: '45 6 * * 1-5'", self.his_workflow)
        # 本線はHISを取得しない
        self.assertIn("--exclude-provider-id his", self.workflow)
        self.assertNotIn("generate_his_html.py", self.workflow)
        self.assertNotIn("generate_his_list_html.py", self.workflow)

    def test_his_feed_failure_does_not_drop_the_fetched_data(self) -> None:
        feed = self.his_workflow.split(
            "- name: HIS WordPress用フィード生成・ローカル同期前検証", 1
        )[1].split("\n      - name:", 1)[0]
        # HISのステップは continue-on-error にせず、失敗はジョブの失敗として残す
        self.assertNotIn("continue-on-error", feed)
        self.assertIn("git checkout -- wordpress_feed/his-monitor-feed.json", feed)

        commit = self.his_workflow.split("- name: コミット＆プッシュ（HISのパスだけ）", 1)[
            1
        ].split("\n      - name:", 1)[0]
        self.assertIn("if: ${{ !cancelled() }}", commit)
        self.assertIn("git pull --rebase origin main", commit)
        self.assertLess(
            self.his_workflow.index("- name: コミット＆プッシュ（HISのパスだけ）"),
            self.his_workflow.index("- name: 失敗をIssueへ記録"),
        )

    def test_failure_is_recorded_as_a_labelled_issue(self) -> None:
        report = self.his_workflow.split("- name: 失敗をIssueへ記録", 1)[1]
        self.assertIn("if: failure()", report)
        self.assertIn("his-monitor-failure", report)
        self.assertIn("createLabel", report)
        self.assertIn("actions/runs/${context.runId}", report)

    def test_feed_builder_still_requires_a_successful_his_run(self) -> None:
        feed_builder = (ROOT / "wordpress_feed" / "build-his-wordpress-feed.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("latest.status !== \"success\"", feed_builder)
        self.assertIn("HIS公式監視の最新実行が成功状態ではありません。", feed_builder)


if __name__ == "__main__":
    unittest.main()
