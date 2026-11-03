from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> dict[str, str]:
    values = dict(os.environ)
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def require_values(values: dict[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise RuntimeError(f"missing required configuration: {', '.join(missing)}")


def resolve_context_path(explicit: Path | None) -> Path:
    raw = explicit or (Path(os.environ["PIPELINE_CONTEXT_PATH"]) if os.environ.get("PIPELINE_CONTEXT_PATH") else None)
    if raw is None:
        raise RuntimeError("missing --context or PIPELINE_CONTEXT_PATH")
    path = raw.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"pipeline context not found: {path}")
    return path


def load_context(explicit: Path | None) -> tuple[Path, dict[str, Any]]:
    path = resolve_context_path(explicit)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"pipeline context must be a JSON object: {path}")
    return path, payload


def resolve_result_path(explicit: Path | None) -> Path:
    raw = explicit or (Path(os.environ["PIPELINE_HOOK_RESULT_PATH"]) if os.environ.get("PIPELINE_HOOK_RESULT_PATH") else None)
    if raw is None:
        raise RuntimeError("missing --result or PIPELINE_HOOK_RESULT_PATH")
    return raw.expanduser().resolve()


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def context_task_ids(context: dict[str, Any]) -> list[str]:
    raw = context.get("task_ids") or []
    if not isinstance(raw, list):
        raise ValueError("pipeline context task_ids must be an array")
    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw:
        task_id = Path(str(item)).name.strip()
        if task_id and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered
