from __future__ import annotations

import pytest

from rubric.judge_runtime import JudgeRuntimeError, resolve_judge_runtime_config


def _judge_config(*, base_url: str) -> dict[str, object]:
    return {
        "config_id": "test-judge",
        "model": "gpt-test",
        "base_url": base_url,
        "credential_env": "OPENAI_API_KEY",
        "timeout_seconds": 30,
        "parallelism": 1,
        "reasoning_effort": "high",
    }


def test_base_url_environment_reference_is_resolved() -> None:
    """A frozen config may name the environment variable containing the URL."""

    resolved = resolve_judge_runtime_config(
        _judge_config(base_url="OPENAI_BASE_URL"),
        environ={"OPENAI_BASE_URL": "https://judge.example.test/v1/"},
    )

    assert resolved["base_url"] == "https://judge.example.test/v1"
    assert resolved["runtime_receipt"]["base_url"] == (
        "https://judge.example.test/v1"
    )


def test_literal_base_url_remains_supported() -> None:
    """Existing configs with a literal HTTP(S) URL remain valid."""

    resolved = resolve_judge_runtime_config(
        _judge_config(base_url="https://judge.example.test/v1/"),
        environ={},
    )

    assert resolved["base_url"] == "https://judge.example.test/v1"


def test_missing_base_url_environment_reference_fails_fast() -> None:
    """A missing referenced variable is reported before any Judge request."""

    with pytest.raises(
        JudgeRuntimeError,
        match="Judge base_url environment variable is absent: OPENAI_BASE_URL",
    ):
        resolve_judge_runtime_config(
            _judge_config(base_url="OPENAI_BASE_URL"),
            environ={},
        )


def test_resolved_base_url_must_be_http() -> None:
    """An environment reference cannot bypass the HTTP(S) URL contract."""

    with pytest.raises(
        JudgeRuntimeError,
        match=r"UNITTEST_RL_JUDGE_BASE_URL must be HTTP\(S\)",
    ):
        resolve_judge_runtime_config(
            _judge_config(base_url="OPENAI_BASE_URL"),
            environ={"OPENAI_BASE_URL": "not-a-url"},
        )
