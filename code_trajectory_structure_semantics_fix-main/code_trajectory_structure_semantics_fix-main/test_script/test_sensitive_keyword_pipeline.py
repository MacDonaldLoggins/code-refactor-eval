from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from structure_fix_script import fix_sensitive_keywords
from structure_fix_script import remove_internal_model_field


ROOT_DIR = Path(__file__).resolve().parents[1]
STRUCTURE_ENTRY = ROOT_DIR / "structure_check_fix.py"


def encoded_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class SensitiveKeywordFixTests(unittest.TestCase):
    def make_payload(self) -> dict[str, object]:
        return {
            "system": "Claude 3.7 Sonnet",
            "meta": {"model": "claude-opus", "vendor": "Anthropic"},
            "_metadata": {"model": "claude-opus"},
            "tools": [{"description": "claude-opus"}],
            "tool_definitions": [{"description": "Anthropic"}],
            "messages": [
                {
                    "role": "system",
                    "content": "claude-opus from Anthropic",
                    "tool_calls": [
                        {
                            "function": {
                                "arguments": encoded_json(
                                    {"model": "claude-opus", "prompt": "system"}
                                )
                            }
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": "Use Claude 3.7 Sonnet from Anthropic.",
                    "tool_calls": [
                        {
                            "function": {
                                "arguments": encoded_json(
                                    {
                                        "description": "delegate to claude-opus",
                                        "model": "claude-opus",
                                        "prompt": "work",
                                    }
                                )
                            }
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": "opus-4 and haiku",
                    "model": "claude-sonnet",
                    "meta": {"vendor": "Anthropic"},
                },
            ],
        }

    def test_fixers_preserve_excluded_scopes_and_clean_assistant_arguments(self) -> None:
        payload = self.make_payload()
        original_system_message = json.dumps(payload["messages"][0], sort_keys=True)

        _, replacements = fix_sensitive_keywords.replace_payload(payload)
        removals = remove_internal_model_field.remove_model_fields(payload)

        self.assertTrue(replacements)
        self.assertEqual(payload["system"], "Claude 3.7 Sonnet")
        self.assertEqual(payload["meta"]["model"], "claude-opus")
        self.assertEqual(payload["_metadata"]["model"], "claude-opus")
        self.assertEqual(payload["tools"][0]["description"], "claude-opus")
        self.assertEqual(payload["tool_definitions"][0]["description"], "Anthropic")
        self.assertEqual(json.dumps(payload["messages"][0], sort_keys=True), original_system_message)

        assistant = payload["messages"][1]
        self.assertEqual(
            assistant["content"],
            "Use internal-model from Brilliant.",
        )
        assistant_arguments = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
        self.assertNotIn("model", assistant_arguments)
        self.assertEqual(assistant_arguments["description"], "delegate to internal-model")
        self.assertEqual(assistant_arguments["prompt"], "work")

        user = payload["messages"][2]
        self.assertNotIn("model", user)
        self.assertEqual(user["content"], "internal-model and internal-model")
        self.assertEqual(user["meta"]["vendor"], "Anthropic")
        self.assertEqual(len(removals), 2)

class SensitiveKeywordPipelineTests(unittest.TestCase):
    def test_structure_fix_stage_runs_both_steps_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "TASK-TEST"
            sessions = task / "sessions"
            sessions.mkdir(parents=True)
            session_path = sessions / "1-develop-demo.json"
            payload = SensitiveKeywordFixTests().make_payload()
            session_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(STRUCTURE_ENTRY),
                    "--fix",
                    "-r",
                    str(task),
                    "--fix-only",
                    "sensitive_keywords,remove_internal_model_field",
                ],
                cwd=ROOT_DIR,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            fixed = json.loads(session_path.read_text(encoding="utf-8"))
            assistant = fixed["messages"][1]
            arguments = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
            self.assertNotIn("model", arguments)
            self.assertEqual(arguments["description"], "delegate to internal-model")
            self.assertEqual(fixed["messages"][0]["content"], "claude-opus from Anthropic")
            self.assertEqual(fixed["meta"]["model"], "claude-opus")

            keyword_report = json.loads(
                (task / "fix_sensitive_keywords_report.json").read_text(encoding="utf-8")
            )
            removal_report = json.loads(
                (task / "remove_internal_model_field_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(keyword_report["mode"], "write")
            self.assertGreater(keyword_report["summary"]["total_replacements"], 0)
            self.assertEqual(removal_report["mode"], "write")
            self.assertEqual(removal_report["summary"]["fields_removed"], 2)

            keyword_position = result.stdout.index("sensitive_keywords:")
            removal_position = result.stdout.index("remove_internal_model_field:")
            self.assertLess(keyword_position, removal_position)


if __name__ == "__main__":
    unittest.main()
