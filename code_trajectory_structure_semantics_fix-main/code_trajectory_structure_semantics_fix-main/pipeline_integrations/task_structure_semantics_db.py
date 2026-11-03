from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


DEFAULT_TABLE = "task_structure_semantics"
STATUS_VALUES = {"已领取", "已分配", "运行中", "已完成", "失败"}
FLAG_COLUMNS = {"is_structure", "is_semantics", "is_detector"}
TIME_COLUMNS = {"download_date", "run_date_start", "run_date_end"}

try:  # pragma: no cover - exercised only when the optional dependency exists.
    import pymysql
except Exception:  # pragma: no cover - keep imports cheap when DB is disabled.
    pymysql = None


def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _positive_int(value: Any, name: str, default: int) -> int:
    candidate = default if value in (None, "") else value
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
        raise ValueError(f"{name} must be a positive integer")
    return candidate


def _safe_identifier(value: str, label: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or candidate[0].isdigit() or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in candidate):
        raise ValueError(f"{label} must be a safe SQL identifier")
    return candidate


def _ordered_ids(task_ids: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in task_ids:
        task_id = str(raw).strip()
        if task_id and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered


@dataclass(frozen=True)
class TaskDatabaseSettings:
    enabled: bool = False
    host: str = ""
    port: int = 3306
    database: str = ""
    user: str = ""
    password: str = ""
    table: str = DEFAULT_TABLE
    charset: str = "utf8mb4"
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 30
    write_timeout_seconds: int = 30
    password_env: str = ""


def load_task_database_settings(config: Mapping[str, Any], env: Mapping[str, str]) -> TaskDatabaseSettings | None:
    enabled = bool(config.get("enabled", False))
    if not enabled:
        return None

    host = str(config.get("host") or "").strip()
    database = str(config.get("database") or "").strip()
    user = str(config.get("user") or "").strip()
    password = str(config.get("password") or "").strip()
    password_env = str(config.get("password_env") or "").strip()
    if not password and password_env:
        password = str(env.get(password_env) or "").strip()
    missing = [
        name
        for name, value in (
            ("host", host),
            ("database", database),
            ("user", user),
            ("password", password),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"database config missing required values: {', '.join(missing)}")

    table = _safe_identifier(str(config.get("table") or DEFAULT_TABLE), "database.table")
    charset = str(config.get("charset") or "utf8mb4").strip() or "utf8mb4"
    port = _positive_int(config.get("port", 3306), "database.port", 3306)
    connect_timeout_seconds = _positive_int(
        config.get("connect_timeout_seconds", 10),
        "database.connect_timeout_seconds",
        10,
    )
    read_timeout_seconds = _positive_int(
        config.get("read_timeout_seconds", 30),
        "database.read_timeout_seconds",
        30,
    )
    write_timeout_seconds = _positive_int(
        config.get("write_timeout_seconds", 30),
        "database.write_timeout_seconds",
        30,
    )
    return TaskDatabaseSettings(
        enabled=True,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        table=table,
        charset=charset,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
        write_timeout_seconds=write_timeout_seconds,
        password_env=password_env,
    )


class TaskStructureSemanticsDatabase:
    def __init__(
        self,
        settings: TaskDatabaseSettings,
        connect_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self._connect_factory = connect_factory
        self._connection: Any | None = None

    def __enter__(self) -> "TaskStructureSemanticsDatabase":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _build_connect_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.settings.host,
            "port": self.settings.port,
            "database": self.settings.database,
            "user": self.settings.user,
            "password": self.settings.password,
            "charset": self.settings.charset,
            "autocommit": True,
            "connect_timeout": self.settings.connect_timeout_seconds,
            "read_timeout": self.settings.read_timeout_seconds,
            "write_timeout": self.settings.write_timeout_seconds,
        }

    def _default_connect_factory(self, **kwargs: Any) -> Any:
        if pymysql is None:
            raise RuntimeError(
                "pymysql is required for database integration; install the package or disable [database]"
            )
        return pymysql.connect(**kwargs)

    def _connect(self) -> Any:
        if self._connection is None:
            factory = self._connect_factory or self._default_connect_factory
            self._connection = factory(**self._build_connect_kwargs())
        else:
            try:
                self._connection.ping(reconnect=True)
            except Exception:
                self.close()
                factory = self._connect_factory or self._default_connect_factory
                self._connection = factory(**self._build_connect_kwargs())
        return self._connection

    def ensure_connected(self) -> None:
        connection = self._connect()
        connection.ping(reconnect=True)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def _assert_task_ids_exist(self, task_ids: list[str]) -> None:
        if not task_ids:
            return
        connection = self._connect()
        placeholders = ", ".join(["%s"] * len(task_ids))
        sql = f"SELECT `task_id` FROM `{self.settings.table}` WHERE `task_id` IN ({placeholders})"
        with connection.cursor() as cursor:
            cursor.execute(sql, task_ids)
            found = {str(row[0]) for row in cursor.fetchall()}
        missing = [task_id for task_id in task_ids if task_id not in found]
        if missing:
            raise RuntimeError(
                f"database table {self.settings.table!r} does not contain task_id(s): {', '.join(missing)}"
            )

    def _execute_many(self, task_ids: Iterable[str], fields: Mapping[str, Any]) -> None:
        task_ids_list = _ordered_ids(task_ids)
        if not task_ids_list:
            return
        if not fields:
            return
        self._assert_task_ids_exist(task_ids_list)
        connection = self._connect()
        set_clause = ", ".join(f"`{column}` = %s" for column in fields)
        sql = f"UPDATE `{self.settings.table}` SET {set_clause} WHERE `task_id` = %s"
        values = [tuple(fields[column] for column in fields) + (task_id,) for task_id in task_ids_list]
        with connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def ensure_claim_columns(self) -> None:
        """Ensure columns used by the QA-claim initializer exist.

        The existing ledger table predates the QA operator fields.  Keep the
        migration idempotent so the initializer can be run against an existing
        installation without requiring a separate manual SQL step.
        """
        connection = self._connect()
        required = {
            "operator_code": "VARCHAR(255) NULL",
            "operator_name": "VARCHAR(255) NULL",
            "task_sub_status": "VARCHAR(255) NULL",
            "task_sub_status_name": "VARCHAR(255) NULL",
        }
        legacy_columns = ("task_status_name",)
        columns_to_check = tuple(required) + legacy_columns
        placeholders = ", ".join(["%s"] * len(columns_to_check))
        sql = (
            "SELECT `COLUMN_NAME` FROM `INFORMATION_SCHEMA`.`COLUMNS` "
            "WHERE `TABLE_SCHEMA` = DATABASE() AND `TABLE_NAME` = %s "
            f"AND `COLUMN_NAME` IN ({placeholders})"
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, (self.settings.table, *columns_to_check))
            existing = {str(row[0]) for row in cursor.fetchall()}
            for column, definition in required.items():
                if column not in existing:
                    cursor.execute(
                        f"ALTER TABLE `{self.settings.table}` "
                        f"ADD COLUMN `{column}` {definition}"
                    )
            if "task_status_name" in existing:
                cursor.execute(
                    f"ALTER TABLE `{self.settings.table}` "
                    "DROP COLUMN `task_status_name`"
                )

    def initialize_claimed_tasks(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        downloaded_at: str | None = None,
        task_status: str = "已领取",
    ) -> int:
        """Insert or update task rows from a QA claim response.

        ``task_status`` is the pipeline ledger state.  The claim operation is
        stored separately in ``task_sub_status`` and
        ``task_sub_status_name``.
        """
        if task_status not in STATUS_VALUES:
            raise ValueError(f"unsupported task status: {task_status}")
        normalized: list[
            tuple[
                str,
                str | None,
                str | None,
                str | None,
                str,
                str,
                str | None,
                str | None,
                str,
                str,
                str,
            ]
        ] = []
        timestamp = downloaded_at or now_string()
        seen: set[str] = set()
        for raw in records:
            task_id = str(raw.get("task_id") or "").strip()
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            operator_id = raw.get("operator_id") or raw.get("operator_code")
            operator_code = raw.get("operator_code")
            operator_name = raw.get("operator_name")
            task_sub_status = raw.get("task_sub_status")
            task_sub_status_name = raw.get("task_sub_status_name")
            normalized.append(
                (
                    task_id,
                    str(operator_id).strip() if operator_id not in (None, "") else None,
                    str(operator_code).strip() if operator_code not in (None, "") else None,
                    str(operator_name).strip() if operator_name is not None else None,
                    timestamp,
                    task_status,
                    str(task_sub_status).strip() if task_sub_status not in (None, "") else None,
                    str(task_sub_status_name).strip()
                    if task_sub_status_name not in (None, "")
                    else None,
                    "否",
                    "否",
                    "否",
                )
            )
        if not normalized:
            return 0

        self.ensure_claim_columns()
        connection = self._connect()
        sql = (
            f"INSERT INTO `{self.settings.table}` "
            "(`task_id`, `operator_id`, `operator_code`, `operator_name`, "
            "`download_date`, `task_status`, `task_sub_status`, "
            "`task_sub_status_name`, `is_structure`, `is_semantics`, `is_detector`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`operator_id` = VALUES(`operator_id`), "
            "`operator_code` = VALUES(`operator_code`), "
            "`operator_name` = VALUES(`operator_name`), "
            "`download_date` = VALUES(`download_date`), "
            "`task_status` = VALUES(`task_status`), "
            "`task_sub_status` = VALUES(`task_sub_status`), "
            "`task_sub_status_name` = VALUES(`task_sub_status_name`)"
        )
        with connection.cursor() as cursor:
            cursor.executemany(sql, normalized)
        return len(normalized)

    def ensure_task_rows(
        self,
        task_ids: Iterable[str],
        *,
        operator_id: str | None = None,
        operator_code: str | None = None,
        operator_name: str | None = None,
        task_status: str = "已领取",
    ) -> int:
        """Insert missing TASK rows without changing existing rows.

        Local or replay runs may skip the QA claim stage, but later pipeline
        stages still need a ledger row to receive their ``success/error``
        status.  ``INSERT IGNORE`` makes this operation safe for rows already
        initialized by a real claim response.
        """
        if task_status not in STATUS_VALUES:
            raise ValueError(f"unsupported task status: {task_status}")
        task_ids_list = _ordered_ids(task_ids)
        if not task_ids_list:
            return 0

        self.ensure_claim_columns()
        timestamp = now_string()
        normalized = [
            (
                task_id,
                str(operator_id).strip() if operator_id not in (None, "") else None,
                str(operator_code).strip() if operator_code not in (None, "") else None,
                str(operator_name).strip() if operator_name not in (None, "") else None,
                timestamp,
                task_status,
                "否",
                "否",
                "否",
            )
            for task_id in task_ids_list
        ]
        connection = self._connect()
        sql = (
            f"INSERT IGNORE INTO `{self.settings.table}` "
            "(`task_id`, `operator_id`, `operator_code`, `operator_name`, "
            "`download_date`, `task_status`, `is_structure`, `is_semantics`, `is_detector`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with connection.cursor() as cursor:
            cursor.executemany(sql, normalized)
        return len(normalized)

    def set_task_sub_status(
        self,
        task_ids: Iterable[str],
        status: str,
        status_name: str,
    ) -> None:
        """Update the current pipeline sub-stage and its display name."""
        self.ensure_claim_columns()
        self._execute_many(
            task_ids,
            {
                "task_sub_status": status,
                "task_sub_status_name": status_name,
            },
        )

    def _execute_status_update(self, task_ids: Iterable[str], status: str, *, preserve_completed: bool) -> None:
        if status not in STATUS_VALUES:
            raise ValueError(f"unsupported task status: {status}")
        task_ids_list = _ordered_ids(task_ids)
        if not task_ids_list:
            return
        self._assert_task_ids_exist(task_ids_list)
        connection = self._connect()
        if preserve_completed:
            sql = (
                f"UPDATE `{self.settings.table}` "
                "SET `task_status` = IF(`task_status` = %s, `task_status`, %s) "
                "WHERE `task_id` = %s"
            )
            values = [("已完成", status, task_id) for task_id in task_ids_list]
        else:
            sql = f"UPDATE `{self.settings.table}` SET `task_status` = %s WHERE `task_id` = %s"
            values = [(status, task_id) for task_id in task_ids_list]
        with connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def _execute_first_touch(self, task_ids: Iterable[str], column: str, value: str) -> None:
        if column not in TIME_COLUMNS:
            raise ValueError(f"unsupported timestamp column: {column}")
        task_ids_list = _ordered_ids(task_ids)
        if not task_ids_list:
            return
        self._assert_task_ids_exist(task_ids_list)
        connection = self._connect()
        sql = (
            f"UPDATE `{self.settings.table}` "
            f"SET `{column}` = IF(`{column}` IS NULL OR `{column}` = '', %s, `{column}`) "
            "WHERE `task_id` = %s"
        )
        values = [(value, task_id) for task_id in task_ids_list]
        with connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def set_task_status(self, task_ids: Iterable[str], status: str, *, preserve_completed: bool = True) -> None:
        self._execute_status_update(task_ids, status, preserve_completed=preserve_completed)

    def mark_claimed(self, task_ids: Iterable[str]) -> None:
        self.set_task_status(task_ids, "已领取")

    def mark_running(self, task_ids: Iterable[str], when: str | None = None) -> None:
        self.set_task_status(task_ids, "运行中")
        self._execute_first_touch(task_ids, "run_date_start", when or now_string())

    def mark_completed(self, task_ids: Iterable[str]) -> None:
        self.set_task_status(task_ids, "已完成", preserve_completed=False)

    def mark_failed(self, task_ids: Iterable[str]) -> None:
        self.set_task_status(task_ids, "失败", preserve_completed=False)

    def mark_finished(self, task_ids: Iterable[str], when: str | None = None) -> None:
        self._execute_first_touch(task_ids, "run_date_end", when or now_string())

    def mark_downloaded(self, task_ids: Iterable[str], when: str | None = None) -> None:
        self._execute_first_touch(task_ids, "download_date", when or now_string())

    def mark_stage_flag(self, task_ids: Iterable[str], column: str, value: str) -> None:
        if column not in FLAG_COLUMNS:
            raise ValueError(f"unsupported stage flag column: {column}")
        if value not in {"是", "否"}:
            raise ValueError(f"unsupported stage flag value: {value}")
        self._execute_many(task_ids, {column: value})

    def mark_stage_started(self, task_ids: Iterable[str], column: str) -> None:
        self.mark_stage_flag(task_ids, column, "是")

    def mark_stage_skipped(self, task_ids: Iterable[str], column: str) -> None:
        self.mark_stage_flag(task_ids, column, "否")
