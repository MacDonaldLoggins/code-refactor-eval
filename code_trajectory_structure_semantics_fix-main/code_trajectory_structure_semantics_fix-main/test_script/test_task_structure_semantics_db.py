from __future__ import annotations

import unittest

from pipeline_integrations.task_structure_semantics_db import (
    TaskDatabaseSettings,
    TaskStructureSemanticsDatabase,
    load_task_database_settings,
)


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self._rows: list[tuple[str, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        self.connection.statements.append(("execute", sql, tuple(params or ())))
        if sql.startswith("SELECT `task_id`"):
            self._rows = [(task_id,) for task_id in self.connection.existing_task_ids]

    def executemany(self, sql: str, params_seq) -> None:
        normalized = [tuple(params) for params in params_seq]
        self.connection.statements.append(("executemany", sql, normalized))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, existing_task_ids: list[str] | None = None) -> None:
        self.existing_task_ids = existing_task_ids or ["TASK-001"]
        self.statements: list[tuple[str, str, tuple]] = []
        self.pings = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        self.pings += 1

    def close(self) -> None:
        self.closed = True


class TaskStructureSemanticsDatabaseTests(unittest.TestCase):
    def test_loader_returns_none_when_disabled(self) -> None:
        self.assertIsNone(load_task_database_settings({}, {}))

    def test_loader_requires_connection_values_when_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "database config missing required values"):
            load_task_database_settings({"enabled": True}, {})

    def test_tracker_emits_expected_updates(self) -> None:
        fake_connection = _FakeConnection()
        settings = TaskDatabaseSettings(
            enabled=True,
            host="127.0.0.1",
            port=3306,
            database="demo",
            user="demo",
            password="secret",
        )
        tracker = TaskStructureSemanticsDatabase(settings, connect_factory=lambda **kwargs: fake_connection)

        tracker.mark_claimed(["TASK-001"])
        tracker.mark_running(["TASK-001"])
        tracker.mark_downloaded(["TASK-001"])
        tracker.mark_stage_started(["TASK-001"], "is_semantics")
        tracker.mark_stage_skipped(["TASK-001"], "is_detector")
        tracker.set_task_sub_status(
            ["TASK-001"],
            "fetch_from_oss-error",
            "从 OSS 下载并组装 TASK 数据失败",
        )
        tracker.ensure_task_rows(["TASK-001"], operator_id="operator-1")
        tracker.mark_completed(["TASK-001"])
        tracker.mark_failed(["TASK-001"])
        tracker.mark_finished(["TASK-001"])

        rendered = "\n".join(item[1] for item in fake_connection.statements)
        self.assertIn("SET `task_status` = IF(`task_status` = %s, `task_status`, %s)", rendered)
        self.assertIn("SET `run_date_start` = IF(`run_date_start` IS NULL OR `run_date_start` = '', %s, `run_date_start`)", rendered)
        self.assertIn("SET `download_date` = IF(`download_date` IS NULL OR `download_date` = '', %s, `download_date`)", rendered)
        self.assertIn("SET `is_semantics` = %s", rendered)
        self.assertIn("SET `is_detector` = %s", rendered)
        self.assertIn("SET `task_sub_status` = %s, `task_sub_status_name` = %s", rendered)
        self.assertIn("INSERT IGNORE INTO `task_structure_semantics`", rendered)
        self.assertIn("SET `task_status` = %s WHERE `task_id` = %s", rendered)
        self.assertTrue(
            any(
                "失败" in values
                for operation, _sql, rows in fake_connection.statements
                if operation == "executemany"
                for values in rows
            )
        )
        tracker.close()
        self.assertTrue(fake_connection.closed)


if __name__ == "__main__":
    unittest.main()
