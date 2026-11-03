"""Runtime-selectable Judge transports used inside the Harbor verifier."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


RUNNERS = ("api", "codex-cli", "claude-code")
PROTOCOL_BY_RUNNER = {
    "api": "responses",
    "codex-cli": "responses",
    "claude-code": "anthropic-messages",
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class JudgeRuntimeError(RuntimeError):
    """A Judge transport failed before producing a contract-valid object."""


def _bounded_redacted(value: object, credential: str, limit: int = 32768) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text.replace(credential, "<REDACTED>")[-limit:]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required_env(environ: Mapping[str, str], key: str) -> str:
    value = environ.get(key, "").strip()
    if not value:
        raise JudgeRuntimeError(f"Judge runtime environment variable is absent: {key}")
    return value


def _positive_env_int(
    environ: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = environ.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise JudgeRuntimeError(f"{key} must be an integer") from exc
    if value < 1:
        raise JudgeRuntimeError(f"{key} must be positive")
    return value


def _resolve_base_url(value: str, environ: Mapping[str, str]) -> str:
    """Resolve a literal URL or an environment-variable reference."""

    source = value.strip()
    if _ENV_NAME.fullmatch(source):
        resolved = environ.get(source, "").strip()
        if not resolved:
            raise JudgeRuntimeError(
                f"Judge base_url environment variable is absent: {source}"
            )
        source = resolved
    return source.rstrip("/")


def resolve_judge_runtime_config(
    frozen_config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Overlay non-secret runtime identity while preserving frozen policy fields."""

    env = os.environ if environ is None else environ
    resolved = dict(frozen_config)
    runner = env.get("UNITTEST_RL_JUDGE_RUNNER", "api").strip()
    if runner not in RUNNERS:
        raise JudgeRuntimeError(
            f"UNITTEST_RL_JUDGE_RUNNER must be one of {', '.join(RUNNERS)}"
        )
    protocol = env.get(
        "UNITTEST_RL_JUDGE_PROTOCOL", PROTOCOL_BY_RUNNER[runner]
    ).strip()
    if protocol != PROTOCOL_BY_RUNNER[runner]:
        raise JudgeRuntimeError(
            f"Judge runner {runner!r} requires protocol "
            f"{PROTOCOL_BY_RUNNER[runner]!r}"
        )
    model = env.get("UNITTEST_RL_JUDGE_MODEL", str(resolved.get("model", ""))).strip()
    base_url = _resolve_base_url(
        env.get("UNITTEST_RL_JUDGE_BASE_URL", str(resolved.get("base_url", ""))),
        env,
    )
    auth_mode = env.get(
        "UNITTEST_RL_JUDGE_AUTH_MODE",
        "api-key" if runner == "claude-code" else "bearer",
    ).strip()
    if not model:
        raise JudgeRuntimeError("UNITTEST_RL_JUDGE_MODEL is required")
    if not base_url.startswith(("https://", "http://")):
        raise JudgeRuntimeError("UNITTEST_RL_JUDGE_BASE_URL must be HTTP(S)")
    if auth_mode not in {"api-key", "bearer"}:
        raise JudgeRuntimeError(
            "UNITTEST_RL_JUDGE_AUTH_MODE must be 'api-key' or 'bearer'"
        )
    if runner == "codex-cli" and auth_mode != "bearer":
        raise JudgeRuntimeError("codex-cli Judge requires bearer authentication")

    timeout_seconds = _positive_env_int(
        env,
        "UNITTEST_RL_JUDGE_TIMEOUT_SECONDS",
        int(resolved.get("timeout_seconds", 900)),
    )
    parallelism = _positive_env_int(
        env,
        "UNITTEST_RL_JUDGE_PARALLELISM",
        int(resolved.get("parallelism", 1)),
    )
    if parallelism > 5:
        raise JudgeRuntimeError("UNITTEST_RL_JUDGE_PARALLELISM must be <= 5")
    reasoning_effort = env.get(
        "UNITTEST_RL_JUDGE_REASONING_EFFORT",
        str(resolved.get("reasoning_effort", "high")),
    ).strip()
    if not reasoning_effort:
        raise JudgeRuntimeError("UNITTEST_RL_JUDGE_REASONING_EFFORT is required")
    runner_version = env.get("UNITTEST_RL_JUDGE_RUNNER_VERSION", "").strip() or None
    credential_env = str(resolved.get("credential_env", "")).strip()
    if not credential_env:
        raise JudgeRuntimeError("frozen Judge credential_env is required")

    runtime_receipt = {
        "runner": runner,
        "protocol": protocol,
        "model": model,
        "base_url": base_url,
        "auth_mode": auth_mode,
        "credential_env": credential_env,
        "timeout_seconds": timeout_seconds,
        "parallelism": parallelism,
        "reasoning_effort": reasoning_effort,
        "runner_version": runner_version,
    }
    runtime_sha = hashlib.sha256(_canonical_bytes(runtime_receipt)).hexdigest()
    frozen_id = str(resolved.get("config_id", "judge"))
    resolved.update(
        {
            "config_id": f"{frozen_id}--runtime-{runtime_sha[:12]}",
            "frozen_config_id": frozen_id,
            "model_key": f"runtime--{runner}--{model}--judge",
            "provider": "openai",
            "model": model,
            "base_url": base_url,
            "credential_env": credential_env,
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": timeout_seconds,
            "parallelism": parallelism,
            "runner": runner,
            "protocol": protocol,
            "auth_mode": auth_mode,
            "runner_version": runner_version,
            "runtime_config_sha256": runtime_sha,
            "runtime_receipt": runtime_receipt,
        }
    )
    return resolved


def _response_text(response: Mapping[str, Any]) -> str:
    if response.get("status") != "completed":
        detail = response.get("incomplete_details") or response.get("error")
        raise JudgeRuntimeError(f"Responses API did not complete: {detail!r}")
    for output in response.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise JudgeRuntimeError(
                    f"Judge refused request: {content.get('refusal')}"
                )
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return text
    raise JudgeRuntimeError("Responses API returned no output_text")


def _api_result(
    *,
    config: Mapping[str, Any],
    credential: str,
    instructions: str,
    user_input: str,
    output_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request_payload = {
        "model": config["model"],
        "instructions": instructions,
        "input": user_input,
        "reasoning": {"effort": config["reasoning_effort"]},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "unittest_rl_judge_result",
                "strict": True,
                "schema": output_schema,
            }
        },
        "max_output_tokens": config["max_output_tokens"],
        "store": False,
    }
    url = str(config["base_url"]).rstrip("/") + "/responses"
    headers = {"Content-Type": "application/json"}
    if config.get("auth_mode") == "api-key":
        headers["x-api-key"] = credential
    else:
        headers["Authorization"] = f"Bearer {credential}"
    last_error: Exception | None = None
    raw_response: dict[str, Any] | None = None
    for attempt in range(1, int(config["max_attempts"]) + 1):
        request = urllib.request.Request(
            url,
            data=_canonical_bytes(request_payload),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=int(config["timeout_seconds"]),
            ) as handle:
                decoded = json.loads(handle.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise JudgeRuntimeError("Responses API body must be an object")
                raw_response = decoded
            break
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            last_error = JudgeRuntimeError(
                f"Responses API HTTP {exc.code}: {body}"
            )
            fallback_tokens = config.get("fallback_max_output_tokens")
            if (
                exc.code == 400
                and isinstance(fallback_tokens, int)
                and request_payload["max_output_tokens"] != fallback_tokens
            ):
                request_payload["max_output_tokens"] = fallback_tokens
                continue
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except (OSError, ValueError, JudgeRuntimeError) as exc:
            last_error = exc
        if attempt < int(config["max_attempts"]):
            time.sleep(min(2 ** (attempt - 1), 4))
    if raw_response is None:
        raise JudgeRuntimeError(f"Responses API request failed: {last_error}")
    try:
        model_result = json.loads(_response_text(raw_response))
    except json.JSONDecodeError as exc:
        raise JudgeRuntimeError("Judge output_text is not JSON") from exc
    if not isinstance(model_result, dict):
        raise JudgeRuntimeError("Judge model result must be an object")
    return (
        model_result,
        {
            "runner": "api",
            "url": url,
            "payload": request_payload,
            "credential_env": config["credential_env"],
            "credential_injected": True,
            "auth_mode": config.get("auth_mode"),
            "runtime_config_sha256": config.get("runtime_config_sha256"),
        },
        raw_response,
    )


def _parse_json_object(text: str, *, location: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeRuntimeError(f"{location} is not JSON") from exc
    if not isinstance(value, dict):
        raise JudgeRuntimeError(f"{location} must be a JSON object")
    return value


def _claude_model_result(raw: dict[str, Any]) -> dict[str, Any]:
    structured = raw.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = raw.get("result")
    if isinstance(result, str):
        return _parse_json_object(result, location="Claude Code result")
    if isinstance(raw.get("schema_version"), str):
        return raw
    raise JudgeRuntimeError("Claude Code returned no structured_output")


def _cli_result(
    *,
    config: Mapping[str, Any],
    credential: str,
    instructions: str,
    user_input: str,
    output_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    runner = str(config["runner"])
    prompt = (
        instructions
        + " Do not use tools or inspect the host filesystem. The JSON snapshot "
        + "below is the complete and exclusive evidence set.\n\n"
        + user_input
    )
    last_error: str | None = None
    attempts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="unittest-rl-judge-") as raw_dir:
        workdir = Path(raw_dir)
        schema_path = workdir / "output-schema.json"
        result_path = workdir / "model-result.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False), encoding="utf-8"
        )
        for attempt in range(1, int(config["max_attempts"]) + 1):
            environment = dict(os.environ)
            environment.pop("OPENAI_API_KEY", None)
            environment.pop("ANTHROPIC_API_KEY", None)
            environment.pop("ANTHROPIC_AUTH_TOKEN", None)
            if runner == "codex-cli":
                environment.update(
                    {
                        "OPENAI_API_KEY": credential,
                        "OPENAI_BASE_URL": str(config["base_url"]),
                    }
                )
                command = [
                    "codex",
                    "exec",
                    "--model",
                    str(config["model"]),
                    "--config",
                    f'model_reasoning_effort="{config["reasoning_effort"]}"',
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(result_path),
                    "-",
                ]
            else:
                if config.get("auth_mode") == "bearer":
                    environment["ANTHROPIC_AUTH_TOKEN"] = credential
                else:
                    environment["ANTHROPIC_API_KEY"] = credential
                environment["ANTHROPIC_BASE_URL"] = str(config["base_url"])
                command = [
                    "claude",
                    "--print",
                    "--model",
                    str(config["model"]),
                    "--effort",
                    str(config["reasoning_effort"]),
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(output_schema, ensure_ascii=False),
                    "--tools",
                    "",
                    "--no-session-persistence",
                ]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    cwd=workdir,
                    env=environment,
                    timeout=int(config["timeout_seconds"]),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = (
                    f"attempt {attempt} timed out after "
                    f"{config['timeout_seconds']}s"
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "timeout",
                        "stdout": _bounded_redacted(exc.stdout, credential),
                        "stderr": _bounded_redacted(exc.stderr, credential),
                    }
                )
            except OSError as exc:
                last_error = f"attempt {attempt} could not start {runner}: {exc}"
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "start_error",
                        "stdout": "",
                        "stderr": _bounded_redacted(exc, credential),
                    }
                )
            else:
                receipt = {
                    "attempt": attempt,
                    "returncode": completed.returncode,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stdout": _bounded_redacted(completed.stdout, credential),
                    "stderr": _bounded_redacted(completed.stderr, credential),
                }
                attempts.append(receipt)
                if completed.returncode == 0:
                    try:
                        if runner == "codex-cli":
                            model_result = _parse_json_object(
                                result_path.read_text(encoding="utf-8"),
                                location="Codex CLI result",
                            )
                        else:
                            model_result = _claude_model_result(
                                _parse_json_object(
                                    completed.stdout,
                                    location="Claude Code output",
                                )
                            )
                    except (OSError, UnicodeDecodeError, JudgeRuntimeError) as exc:
                        last_error = f"attempt {attempt} returned invalid JSON: {exc}"
                    else:
                        request_record = {
                            "runner": runner,
                            "model": config["model"],
                            "runner_version": config.get("runner_version"),
                            "reasoning_effort": config["reasoning_effort"],
                            "credential_env": config["credential_env"],
                            "credential_injected": True,
                            "auth_mode": config.get("auth_mode"),
                            "runtime_config_sha256": config.get(
                                "runtime_config_sha256"
                            ),
                            "prompt": prompt,
                            "output_schema": output_schema,
                        }
                        raw_response = {
                            "runner": runner,
                            "attempts": attempts,
                            "model_result": model_result,
                        }
                        return model_result, request_record, raw_response
                else:
                    last_error = (
                        f"attempt {attempt} exited {completed.returncode}: "
                        f"{_bounded_redacted(completed.stderr, credential, 4096)}"
                    )
            if attempt < int(config["max_attempts"]):
                time.sleep(min(2 ** (attempt - 1), 4))
    raise JudgeRuntimeError(last_error or f"{runner} Judge failed")


def invoke_judge(
    *,
    config: Mapping[str, Any],
    instructions: str,
    user_input: str,
    output_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Invoke the selected transport without exposing the credential in artifacts."""

    credential = _required_env(os.environ, str(config["credential_env"]))
    runner = str(config.get("runner", "api"))
    if runner == "api":
        return _api_result(
            config=config,
            credential=credential,
            instructions=instructions,
            user_input=user_input,
            output_schema=output_schema,
        )
    if runner in {"codex-cli", "claude-code"}:
        return _cli_result(
            config=config,
            credential=credential,
            instructions=instructions,
            user_input=user_input,
            output_schema=output_schema,
        )
    raise JudgeRuntimeError(f"unsupported Judge runner: {runner!r}")
