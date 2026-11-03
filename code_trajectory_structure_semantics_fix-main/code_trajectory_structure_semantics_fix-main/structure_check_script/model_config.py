#!/usr/bin/env python3
"""模型配置。

模型参数统一从仓库根目录 .env 读取；外部传入的环境变量/CLI 参数仍可覆盖。

支持的根目录 .env 配置：
  CLAUDE_OPENAI_MODEL_PROFILE=MindFlow_deepseek
  MODEL_NAME=...
  MODEL_BASE_URL=...
  MODEL_API_KEY=...

  CLAUDE_OPENAI_MODEL_PROFILE=LongCat
  M_MODEL_NAME=...
  M_MODEL_BASE_URL=...
  M_MODEL_API_KEY=...
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"


# ============================================================
# 可手动修改的通用模型配置
# ============================================================

# 可选值：openai / longcat。留空时根据 .env 中的 CLAUDE_OPENAI_MODEL_PROFILE 推断。
MODEL_CHANNEL = ""

# 通用覆盖项。留空时使用对应渠道的默认配置。
MODEL_NAME = ""
MODEL_BASE_URL = ""
MODEL_API_KEY = ""
MODEL_TEMPERATURE = 0.1
MODEL_TIMEOUT: float | None = None


# ============================================================
# OpenAI 渠道
# ============================================================

OPENAI_MODEL_NAME = "gpt-5.5"
OPENAI_BASE_URL = ""
OPENAI_API_KEY = ""


# ============================================================
# LongCat 渠道
# ============================================================

M_MODEL_BASE_URL = "https://api.longcat.chat/openai/v1"
M_MODEL_NAME = "LongCat-Flash-Chat"
M_MODEL_API_KEY = ""

# 兼容更直观的 LongCat 命名。实际默认值以上面的 M_MODEL_* 为准。
LONGCAT_MODEL_NAME = M_MODEL_NAME
LONGCAT_BASE_URL = M_MODEL_BASE_URL
LONGCAT_API_KEY = M_MODEL_API_KEY


@dataclass(frozen=True)
class ChannelDefaults:
    model: str
    base_url: str | None = None
    api_key: str | None = None


@dataclass(frozen=True)
class ModelConfig:
    channel: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.1
    timeout: float | None = None
    max_retries: int | None = None


def load_env_file(path: Path = ENV_FILE_PATH) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


load_env_file()


def env_value(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return fallback
    return value


def first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def profile_channel(profile: str | None) -> str | None:
    if not profile:
        return None
    normalized = profile.lower().strip()
    if normalized == "longcat":
        return "longcat"
    if normalized in {"mindflow", "mindflow_deepseek", "openai"}:
        return "openai"
    return None


def float_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def channel_defaults(channel: str) -> ChannelDefaults:
    normalized = channel.lower().strip()
    if normalized == "longcat":
        return ChannelDefaults(
            model=LONGCAT_MODEL_NAME,
            base_url=LONGCAT_BASE_URL,
            api_key=LONGCAT_API_KEY,
        )

    return ChannelDefaults(
        model=OPENAI_MODEL_NAME,
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
    )


def resolve_model_config(
    *,
    channel: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> ModelConfig:
    selected_channel = str(
        first_value(
            channel,
            env_value("MODEL_CHANNEL"),
            env_value("OPENAI_MODEL_CHANNEL"),
            profile_channel(env_value("CLAUDE_OPENAI_MODEL_PROFILE")),
            MODEL_CHANNEL,
            "openai",
        )
    ).lower().strip()
    defaults = channel_defaults(selected_channel)

    if selected_channel == "longcat":
        env_model = first_value(env_value("M_MODEL_NAME"), env_value("LONGCAT_MODEL"))
        env_base_url = first_value(env_value("M_MODEL_BASE_URL"), env_value("LONGCAT_BASE_URL"))
        env_api_key = first_value(env_value("M_MODEL_API_KEY"), env_value("LONGCAT_API_KEY"))
    else:
        env_model = first_value(env_value("MODEL_NAME"), env_value("OPENAI_MODEL"))
        env_base_url = first_value(env_value("MODEL_BASE_URL"), env_value("OPENAI_BASE_URL"), env_value("OPENAI_API_BASE"))
        env_api_key = first_value(env_value("MODEL_API_KEY"), env_value("OPENAI_API_KEY"))

    return ModelConfig(
        channel=selected_channel,
        model=first_value(model, env_model, MODEL_NAME, defaults.model),
        base_url=first_value(base_url, env_base_url, MODEL_BASE_URL, defaults.base_url),
        api_key=first_value(api_key, env_api_key, MODEL_API_KEY, defaults.api_key),
        temperature=float(first_value(temperature, env_value("MODEL_TEMPERATURE"), MODEL_TEMPERATURE, 0.1)),
        timeout=float_value(first_value(timeout, env_value("MODEL_TIMEOUT"), MODEL_TIMEOUT)),
        max_retries=int_value(first_value(max_retries, env_value("MODEL_MAX_RETRIES"))),
    )


def build_chat_model(config: ModelConfig):
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "langchain-openai is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": config.model,
        "temperature": config.temperature,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.api_key:
        kwargs["api_key"] = config.api_key
    elif config.base_url:
        kwargs["api_key"] = "EMPTY"
    if config.timeout is not None:
        kwargs["timeout"] = config.timeout
    if config.max_retries is not None:
        kwargs["max_retries"] = config.max_retries

    return ChatOpenAI(**kwargs)
