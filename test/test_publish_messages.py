import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))

import mcp_server  # noqa: E402


class PublishMessageTests(unittest.TestCase):
    def test_build_publish_message_includes_cleanup_on_success(self) -> None:
        msg = mcp_server._build_publish_message(
            "librarian",
            {
                "version": 42,
                "workflowArtifactId": "artifact-42",
                "archivedThreadCount": 2,
            },
            standalone=True,
        )

        self.assertEqual(
            msg,
            "Published librarian — version: 42, artifact: artifact-42. Archived 2 stale chats.",
        )

    def test_build_publish_message_includes_publish_and_cleanup_warnings(self) -> None:
        msg = mcp_server._build_publish_message(
            "librarian",
            {
                "warning": "incomplete_ancestor_path",
                "detail": "publish snapshot stale",
                "archivedThreadCount": 1,
                "threadCleanupWarning": "cleanup failed",
            },
        )

        self.assertIn("Publish librarian: incomplete_ancestor_path.", msg)
        self.assertIn("publish snapshot stale", msg)
        self.assertIn("Archived 1 stale chat.", msg)
        self.assertIn("Thread cleanup warning: cleanup failed", msg)


if __name__ == "__main__":
    unittest.main()
