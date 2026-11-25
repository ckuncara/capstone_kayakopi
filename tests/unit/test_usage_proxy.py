from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from services.usage_proxy import main as usage_proxy
from services.usage_proxy.main import Settings, estimate_cost, extract_usage


def test_extract_chat_usage_includes_cached_and_reasoning_tokens():
    usage = extract_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 200},
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        }
    )

    assert usage == {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cached_input_tokens": 200,
        "reasoning_tokens": 25,
    }


def test_extract_embedding_usage_sets_no_output_tokens():
    usage = extract_usage({"usage": {"prompt_tokens": 500}})

    assert usage["input_tokens"] == 500
    assert usage["output_tokens"] is None


def test_estimate_cost_uses_cached_input_rate():
    cost, version = estimate_cost(
        "gpt-4o-mini-2024-07-18",
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cached_input_tokens": 200,
            "reasoning_tokens": 25,
        },
    )

    assert Decimal(cost) == Decimal("0.000195")
    assert version == "openai-standard-2026-11-24"


@pytest.mark.parametrize(
    ("model", "usage"),
    [
        ("future-model", {"input_tokens": 100, "output_tokens": 20}),
        ("gpt-4o-mini", {"input_tokens": None, "output_tokens": 20}),
        (
            "gpt-4o-mini",
            {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 101},
        ),
    ],
)
def test_estimate_cost_does_not_invent_pricing(model, usage):
    assert estimate_cost(model, usage) == (None, None)


def test_estimate_cost_does_not_apply_standard_prices_to_other_service_tiers():
    assert estimate_cost(
        "gpt-4o-mini",
        {"input_tokens": 100, "output_tokens": 20},
        service_tier="priority",
    ) == (None, None)


def test_proxy_settings_name_missing_required_variables():
    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        Settings.from_env(
            {
                "OPENAI_API_KEY": "server-only",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_ANON_KEY": "public",
            }
        )


def test_proxy_records_usage_and_returns_openai_response(monkeypatch):
    settings = Settings(
        openai_api_key="server-openai-key",
        supabase_url="https://project.supabase.co",
        supabase_anon_key="public-key",
        supabase_service_role_key="server-db-key",
    )
    recorded = []

    async def auth_user(_settings, token):
        assert token == "supabase-user-jwt"
        return "user-uuid"

    async def record_event(_settings, **event):
        recorded.append(event)

    body = {
        "id": "chatcmpl-123",
        "model": "gpt-4o-mini-2024-07-18",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
    }

    class UpstreamResponse:
        status_code = 200
        headers = {"x-request-id": "openai-request-123", "content-type": "application/json"}
        content = json.dumps(body).encode()

        @staticmethod
        def json():
            return body

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            assert url == "https://api.openai.com/v1/chat/completions"
            assert headers["Authorization"] == "Bearer server-openai-key"
            assert json["model"] == "gpt-4o-mini"
            return UpstreamResponse()

    monkeypatch.setattr(usage_proxy.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(usage_proxy, "_supabase_user_id", auth_user)
    monkeypatch.setattr(usage_proxy, "_record_event", record_event)
    monkeypatch.setattr(usage_proxy.httpx, "AsyncClient", AsyncClient)

    response = TestClient(usage_proxy.create_app()).post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer supabase-user-jwt",
            "x-usage-purpose": "ocr",
        },
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "test"}]},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-123"
    assert response.headers["x-usage-request-id"]
    assert recorded[0]["user_id"] == "user-uuid"
    assert recorded[0]["purpose"] == "ocr"
    assert recorded[0]["endpoint"] == "chat/completions"
    assert recorded[0]["response_body"]["usage"]["prompt_tokens"] == 1000


def test_proxy_rejects_missing_authentication():
    response = TestClient(usage_proxy.create_app()).post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": ["test"]},
    )

    assert response.status_code == 401


def test_usage_summary_requires_auth_and_returns_team_aggregate(monkeypatch):
    settings = Settings(
        openai_api_key="server-openai-key",
        supabase_url="https://project.supabase.co",
        supabase_anon_key="public-key",
        supabase_service_role_key="server-db-key",
    )

    async def auth_user(_settings, token):
        assert token == "supabase-user-jwt"
        return "user-uuid"

    async def summary(_settings, start_at, end_at):
        assert start_at.endswith("T00:00:00+00:00")
        assert end_at.endswith("+00:00")
        return {
            "total_requests": 2,
            "priced_requests": 1,
            "unpriced_requests": 1,
            "estimated_cost_usd": "0.001",
            "input_tokens": 10,
            "output_tokens": 5,
        }

    monkeypatch.setattr(usage_proxy.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(usage_proxy, "_supabase_user_id", auth_user)
    monkeypatch.setattr(usage_proxy, "_usage_summary", summary)

    response = TestClient(usage_proxy.create_app()).get(
        "/v1/usage/summary",
        headers={"Authorization": "Bearer supabase-user-jwt"},
    )

    assert response.status_code == 200
    assert response.json()["total_requests"] == 2
    assert response.json()["unpriced_requests"] == 1
