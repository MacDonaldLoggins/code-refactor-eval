import json
import tempfile
import unittest
from pathlib import Path

from structure_fix_script.fix_corrupted_content import fix_one_file


class FixCorruptedContentTests(unittest.TestCase):
    @staticmethod
    def corrupted_content() -> str:
        return "ʶ𲢼¼޸ģδִκ"

    def write_session(self, path: Path, messages: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")

    def test_only_deletes_corrupted_assistant_and_its_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "TASK-20260519-F702AF"
            session_path = project_dir / "sessions" / "subagents" / "agent.json"
            messages = [
                {"role": "user", "content": self.corrupted_content()},
                {
                    "role": "assistant",
                    "content": self.corrupted_content(),
                    "tool_calls": [{"id": "call-1", "function": {"name": "Bash"}}],
                    "_metadata": {"tokens": {"output": 0}, "timestamp": None},
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
                {"role": "tool", "tool_call_id": "other-call", "content": "keep me"},
                {"role": "assistant", "content": "valid response"},
            ]
            self.write_session(session_path, messages)

            result, fixed_data = fix_one_file(session_path, project_dir)

            self.assertEqual(result.project_dir, project_dir.name)
            self.assertEqual(result.deleted_count, 1)
            self.assertEqual(result.deleted_tool_count, 1)
            self.assertEqual(result.deleted_indices, [1])
            self.assertIsNotNone(fixed_data)
            assert fixed_data is not None
            self.assertEqual(
                fixed_data["messages"],
                [messages[0], messages[3], messages[4]],
            )

    def test_keeps_valid_assistant_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "TASK-20260519-F702AF"
            session_path = project_dir / "sessions" / "agent.json"
            messages = [{"role": "assistant", "content": "正常内容"}]
            self.write_session(session_path, messages)

            result, fixed_data = fix_one_file(session_path, project_dir)

            self.assertEqual(result.deleted_count, 0)
            self.assertEqual(result.deleted_tool_count, 0)
            self.assertIsNone(fixed_data)


if __name__ == "__main__":
    unittest.main()
