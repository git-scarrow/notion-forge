import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))

import mcp_server  # noqa: E402


class MCPServerTests(unittest.TestCase):
    def test_update_agent_uses_shared_impl(self) -> None:
        with mock.patch.object(mcp_server, "_update_agent_impl", return_value="ok") as impl:
            result = mcp_server.update_agent("librarian", "# Hello", publish=False)

        self.assertEqual(result, "ok")
        impl.assert_called_once_with("librarian", "# Hello", False)

    def test_update_agent_from_file_reads_markdown_and_uses_shared_impl(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# Title\n\nBody")
            path = f.name

        try:
            with mock.patch.object(mcp_server, "_update_agent_impl", return_value="ok") as impl:
                result = mcp_server.update_agent_from_file("librarian", path, publish=True)
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(result, "ok")
        impl.assert_called_once_with("librarian", "# Title\n\nBody", True)

    def test_build_update_message_formats_counts(self) -> None:
        msg = mcp_server._build_update_message(
            "librarian",
            {
                "unchanged": 10,
                "updated": 2,
                "inserted": 1,
                "deleted": 3,
                "ops": 17,
            },
        )

        self.assertEqual(
            msg,
            "Updated librarian (10 unchanged, 2 updated, 1 inserted, 3 deleted, 17 ops in 1 tx).",
        )


if __name__ == "__main__":
    unittest.main()
