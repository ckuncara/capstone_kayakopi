import pytest

from kaya_kopi.config import AppConfig
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.security.openai_proxy import (
    message_text,
    model_temperature_kwargs,
    openai_client_options,
    openai_configured,
    reasoning_effort_kwargs,
    set_proxy_access_token,
    tool_request_kwargs,
)


def test_proxy_mode_requires_a_user_token_instead_of_falling_back_to_openai_key():
    cfg = AppConfig(api_key="not-used-on-client", llm_proxy_url="https://proxy.example")
    set_proxy_access_token(None)

    with pytest.raises(RuntimeError, match="no signed-in Supabase session"):
        openai_client_options(cfg, "chat")


def test_proxy_mode_uses_supabase_bearer_token_and_purpose_header():
    cfg = AppConfig(
        api_key="not-used-on-client",
        llm_proxy_url="https://proxy.example/",
    )
    set_proxy_access_token("supabase-user-jwt")

    try:
        options = openai_client_options(cfg, "ocr")
    finally:
        set_proxy_access_token(None)

    assert options == {
        "api_key": "supabase-user-jwt",
        "base_url": "https://proxy.example/v1",
        "default_headers": {"x-usage-purpose": "ocr"},
    }


def test_temperature_is_omitted_only_for_models_that_reject_it():
    assert model_temperature_kwargs("gpt-6-luna", 0.0) == {}
    assert model_temperature_kwargs("gpt-6-pro", 0.0) == {}
    assert model_temperature_kwargs("gpt-6-luna", None) == {}
    assert model_temperature_kwargs("gpt-4o-mini", 0.0) == {"temperature": 0.0}


def test_reasoning_effort_is_set_only_for_models_that_require_it():
    assert reasoning_effort_kwargs("gpt-6-luna") == {"reasoning_effort": "none"}
    assert reasoning_effort_kwargs("gpt-6-pro") == {"reasoning_effort": "none"}
    assert reasoning_effort_kwargs("gpt-4o-mini") == {}


def test_message_text_normalises_content_blocks():
    assert message_text("Our suppliers are A and B.") == "Our suppliers are A and B."
    assert message_text([
        {"type": "text", "text": "Our suppliers are "},
        {"type": "text", "text": "A and B."},
    ]) == "Our suppliers are A and B."
    assert message_text([{"type": "image_url", "image_url": {"url": "x"}}]) == ""


def test_tool_request_kwargs_omit_unsupported_forced_choice():
    tools = [{"type": "function", "function": {"name": "query_existing"}}]
    named = {"type": "function", "function": {"name": "query_existing"}}

    assert tool_request_kwargs("gpt-6-luna", "required", tools) == {"tools": tools}
    assert tool_request_kwargs("gpt-6-pro", "required", tools) == {"tools": tools}
    assert tool_request_kwargs("gpt-6-luna", named, tools) == {"tools": tools}
    assert tool_request_kwargs("gpt-6-luna", "none", tools) == {}
    assert tool_request_kwargs("gpt-4o-mini", "required", tools) == {
        "tool_choice": "required",
        "tools": tools,
    }


def test_direct_mode_honors_custom_openai_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example/v1/")
    cfg = AppConfig(api_key="direct-key")

    assert openai_client_options(cfg, "chat") == {
        "api_key": "direct-key",
        "base_url": "https://llm.example/v1",
    }


def test_direct_mode_uses_existing_openai_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    cfg = AppConfig(api_key="direct-key", llm_proxy_url="")

    assert openai_client_options(cfg, "chat") == {"api_key": "direct-key"}
    assert openai_configured(cfg)


def test_chroma_embedding_client_uses_authenticated_proxy(tmp_path):
    cfg = AppConfig(
        api_key="not-used-on-client",
        llm_proxy_url="https://proxy.example",
        chroma_dir=tmp_path / "chroma",
    )
    set_proxy_access_token("supabase-user-jwt")
    try:
        repository = SharedChromaRepository(cfg, budget_guard=object())
    finally:
        set_proxy_access_token(None)

    assert str(repository.embed_fn.client.base_url).rstrip("/") == "https://proxy.example/v1"
    assert repository.embed_fn.client.default_headers["x-usage-purpose"] == "embedding"
