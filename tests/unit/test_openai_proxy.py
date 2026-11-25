import pytest

from kaya_kopi.config import AppConfig
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.security.openai_proxy import (
    openai_client_options,
    openai_configured,
    set_proxy_access_token,
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


def test_direct_mode_uses_existing_openai_api_key():
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
