from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "action_semantics_fix_script" / "ua_detect_and_repair.py"


def load_ua_module():
    spec = importlib.util.spec_from_file_location("ua_detect_and_repair_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ua = load_ua_module()


class UaExcludedLayoutTests(unittest.TestCase):
    def test_excluded_main_stays_at_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "TASK-ONE"
            session = task / "sessions" / "1-develop-main.json"
            session.parent.mkdir(parents=True)
            session.write_text('{"messages": []}\n', encoding="utf-8")
            result = ua.SessionResult(task.name, str(session))
            result.excluded = True
            result.exclude_reason = "unrepairable"

            info = ua.apply_and_write(result, Path(temporary) / "repair-output")

            self.assertEqual(info["outcome"], "excluded")
            self.assertTrue(session.is_file())
            self.assertFalse((task / "ua_excluded").exists())
            self.assertFalse((Path(temporary) / "repair-output" / "excluded").exists())

    def test_excluded_subagent_stays_under_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "TASK-ONE"
            session = task / "sessions" / "1-develop-main" / "subagents" / "agent-a.json"
            session.parent.mkdir(parents=True)
            session.write_text('{"messages": []}\n', encoding="utf-8")
            result = ua.SessionResult(task.name, str(session))
            result.excluded = True
            result.exclude_reason = "unrepairable"

            ua.apply_and_write(result, None)

            self.assertTrue(session.is_file())
            self.assertFalse((task / "sessions" / "1-develop-main" / "ua_excluded").exists())


if __name__ == "__main__":
    unittest.main()
