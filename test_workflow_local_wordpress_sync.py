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
            ["python wordpress_feed/push_his_wordpress_feed.py --dry-run"],
            calls,
        )

    def test_wordpress_credentials_are_not_exposed_to_the_workflow(self) -> None:
        for variable in ("YF_WP_URL", "YF_WP_USER", "YF_WP_APP_PASSWORD"):
            with self.subTest(variable=variable):
                self.assertNotIn(variable, self.workflow)

    def test_validated_feed_is_still_committed(self) -> None:
        self.assertIn("wordpress_feed/his-monitor-feed.json", self.workflow)


if __name__ == "__main__":
    unittest.main()
