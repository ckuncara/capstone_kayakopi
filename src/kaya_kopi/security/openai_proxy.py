"""Shared OpenAI client configuration for direct and authenticated proxy modes."""

from __future__ import annotations

import os
import re
from contextvars import ContextVar
from typing import Any

from langchain_openai import ChatOpenAI

from kaya_kopi.config import AppConfig

_access_token: ContextVar[str | None] = ContextVar("openai_proxy_access_token", default=None)

# GPT-6 models only accept the provider's default temperature and reject explicit
# 0/0.1 values with HTTP 400, so omit the parameter entirely for the whole family.
GPT6_MODEL_PATTERN = re.compile(r"^gpt-6(?:[-._:/]|$)", re.IGNORECASE)


def is_gpt6_model(model: str) -> bool:
    """Match gpt-6 and every gpt-6-* variant, including provider prefixes."""
    name = model.strip()
    if name.startswith("openai/") or name.startswith("openrouter/"):
        name = name.split("/", 1)[1]
    return bool(GPT6_MODEL_PATTERN.match(name))


def model_temperature_kwargs(model: str, temperature: float | None = 0.0) -> dict[str, float]:
    """Return temperature keyword arguments only when the model supports them."""
    if temperature is None or is_gpt6_model(model):
        return {}
    return {"temperature": temperature}


def reasoning_effort_kwargs(model: str, effort: str = "none") -> dict[str, str]:
    """Return reasoning_effort only when the model requires it for function tools."""
    if is_gpt6_model(model):
        return {"reasoning_effort": effort}
    return {}


def tool_request_kwargs(model: str, choice: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a tool-call request that is compatible with the selected model."""
    request: dict[str, Any] = {"tool_choice": choice, "tools": tools}
    if not is_gpt6_model(model):
        return request
    # Keep tools enabled, but let the provider choose them. The caller adds a
    # system instruction whenever a tool call is required.
    request.pop("tool_choice")
    if choice == "none":
        request.pop("tools")
    return request


def message_text(content: Any) -> str:
    """Normalise plain or block-form model output to one text string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def set_proxy_access_token(access_token: str | None) -> None:
    _access_token.set(access_token)


def openai_configured(cfg: AppConfig) -> bool:
    return bool(cfg.llm_proxy_url or cfg.api_key)


def openai_client_options(
    cfg: AppConfig,
    purpose: str,
    *,
    api_key: str | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    if cfg.llm_proxy_url:
        token = (
            access_token
            or _access_token.get()
            or os.getenv("KAYA_PROXY_ACCESS_TOKEN")
        )
        if not token:
            raise RuntimeError(
                "LLM_PROXY_URL is enabled, but no signed-in Supabase session is available."
            )
        return {
            "api_key": token,
            "base_url": f"{cfg.llm_proxy_url.rstrip('/')}/v1",
            "default_headers": {"x-usage-purpose": purpose},
        }

    options: dict[str, Any] = {"api_key": api_key or cfg.api_key or None}
    # Honour the same OpenAI-compatible endpoint used by the raw Upload Desk
    # client. This is required for custom chat/vision models such as gpt-6-luna.
    base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    if base_url:
        options["base_url"] = base_url
    return options


def create_chat_model(
    cfg: AppConfig,
    model: str,
    *,
    purpose: str,
    temperature: float = 0.0,
    api_key: str | None = None,
    access_token: str | None = None,
    **kwargs: Any,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        **model_temperature_kwargs(model, temperature),
        **reasoning_effort_kwargs(model),
        **openai_client_options(
            cfg, purpose, api_key=api_key, access_token=access_token
        ),
        **kwargs,
    )
