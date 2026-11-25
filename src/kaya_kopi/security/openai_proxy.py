"""Shared OpenAI client configuration for direct and authenticated proxy modes."""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

from langchain_openai import ChatOpenAI

from kaya_kopi.config import AppConfig

_access_token: ContextVar[str | None] = ContextVar("openai_proxy_access_token", default=None)


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

    direct_key = api_key or cfg.api_key
    return {"api_key": direct_key or None}


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
        temperature=temperature,
        **openai_client_options(
            cfg, purpose, api_key=api_key, access_token=access_token
        ),
        **kwargs,
    )
