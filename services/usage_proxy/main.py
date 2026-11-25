"""Authenticated OpenAI proxy with minimal Supabase usage logging."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

OPENAI_API_URL = "https://api.openai.com/v1"
PRICING_VERSION = "openai-standard-2026-11-24"
MODEL_PRICING = {
    "gpt-4o": {"input": "2.50", "cached_input": "1.25", "output": "10"},
    "gpt-4o-mini": {"input": "0.15", "cached_input": "0.075", "output": "0.60"},
    "text-embedding-3-small": {"input": "0.02", "cached_input": "0.02", "output": "0"},
    "text-embedding-3-large": {"input": "0.13", "cached_input": "0.13", "output": "0"},
}
MODEL_ALIASES = {
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gpt-4o-2024-08-06": "gpt-4o",
    "gpt-4o-2024-11-20": "gpt-4o",
}
ALLOWED_ENDPOINTS = {"chat/completions", "embeddings"}
ALLOWED_PURPOSES = {"chat", "ocr", "embedding", "other"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        names = {
            "openai_api_key": "OPENAI_API_KEY",
            "supabase_url": "SUPABASE_URL",
            "supabase_anon_key": "SUPABASE_ANON_KEY",
            "supabase_service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
        }
        missing = [env_name for env_name in names.values() if not values.get(env_name)]
        if missing:
            raise RuntimeError(f"Missing required proxy environment variables: {', '.join(missing)}")
        return cls(
            openai_api_key=values["OPENAI_API_KEY"],
            supabase_url=values["SUPABASE_URL"].rstrip("/"),
            supabase_anon_key=values["SUPABASE_ANON_KEY"],
            supabase_service_role_key=values["SUPABASE_SERVICE_ROLE_KEY"],
        )


def extract_usage(response: Mapping[str, Any]) -> dict[str, int | None]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }

    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = (
        usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    )
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
        "cached_input_tokens": (
            prompt_details.get("cached_tokens")
            if isinstance(prompt_details, Mapping)
            and isinstance(prompt_details.get("cached_tokens"), int)
            else None
        ),
        "reasoning_tokens": (
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, Mapping)
            and isinstance(completion_details.get("reasoning_tokens"), int)
            else None
        ),
    }


def estimate_cost(
    model: str, usage: Mapping[str, int | None], service_tier: str = "default"
) -> tuple[str | None, str | None]:
    if service_tier not in ("default", "standard"):
        return None, None
    pricing = MODEL_PRICING.get(MODEL_ALIASES.get(model, model))
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if pricing is None or input_tokens is None or output_tokens is None:
        return None, None

    cached_input = usage.get("cached_input_tokens") or 0
    if cached_input > input_tokens:
        return None, None
    input_cost = (
        Decimal(input_tokens - cached_input) * Decimal(pricing["input"])
        + Decimal(cached_input) * Decimal(pricing["cached_input"])
    ) / Decimal(1_000_000)
    output_cost = Decimal(output_tokens) * Decimal(pricing["output"]) / Decimal(1_000_000)
    return str(input_cost + output_cost), PRICING_VERSION


async def _supabase_user_id(settings: Settings, access_token: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "apikey": settings.supabase_anon_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Unable to verify sign-in with Supabase.") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Sign in again to use the OpenAI proxy.")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Supabase Auth is temporarily unavailable.")
    try:
        user = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Supabase returned an invalid user.") from exc
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str):
        raise HTTPException(status_code=401, detail="Supabase returned an invalid user.")
    return user_id


async def _write_usage(settings: Settings, event: dict[str, Any]) -> None:
    url = f"{settings.supabase_url}/rest/v1/openai_usage_events"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{url}?on_conflict=request_id", headers=headers, json=event
                )
            if response.is_success:
                return
            last_error = RuntimeError(f"Supabase usage insert failed with HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last_error = exc
    raise HTTPException(
        status_code=503,
        detail="OpenAI returned a response, but usage could not be saved. Contact an administrator.",
    ) from last_error


async def _usage_summary(settings: Settings, start_at: str, end_at: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.supabase_url}/rest/v1/rpc/get_openai_usage_summary",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json={"start_at": start_at, "end_at": end_at},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Unable to load team usage from Supabase.") from exc
    if not response.is_success:
        raise HTTPException(status_code=503, detail="Unable to load team usage from Supabase.")
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Supabase returned an invalid usage summary.") from exc
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise HTTPException(status_code=503, detail="Supabase returned an invalid usage summary.")
    return result[0]


async def _record_event(
    settings: Settings,
    *,
    request_id: str,
    user_id: str,
    purpose: str,
    endpoint: str,
    requested_model: str,
    status_code: int,
    response_body: Mapping[str, Any] | None = None,
    provider_request_id: str | None = None,
    error_code: str | None = None,
) -> None:
    body = response_body or {}
    usage = extract_usage(body)
    if endpoint == "embeddings" and usage["input_tokens"] is not None:
        usage["output_tokens"] = 0
    returned_model = body.get("model")
    returned_model = returned_model if isinstance(returned_model, str) else None
    service_tier = body.get("service_tier")
    service_tier = service_tier if isinstance(service_tier, str) else "default"
    cost, pricing_version = estimate_cost(
        returned_model or requested_model, usage, service_tier
    )
    event = {
        "request_id": request_id,
        "user_id": user_id,
        "purpose": purpose,
        "endpoint": endpoint,
        "requested_model": requested_model,
        "returned_model": returned_model,
        **usage,
        "estimated_cost_usd": cost,
        "pricing_version": pricing_version,
        "service_tier": service_tier,
        "provider_response_id": body.get("id") if isinstance(body.get("id"), str) else None,
        "provider_request_id": provider_request_id,
        "http_status": status_code,
        "error_code": error_code,
    }
    await _write_usage(settings, event)


def create_app() -> FastAPI:
    app = FastAPI(title="Kaya Kopi OpenAI Usage Proxy", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        Settings.from_env()
        return {"status": "ok"}

    @app.get("/v1/usage/summary")
    async def usage_summary(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Sign in to view team usage.")
        access_token = authorization.removeprefix("Bearer ").strip()
        if not access_token:
            raise HTTPException(status_code=401, detail="Sign in to view team usage.")
        settings = Settings.from_env()
        await _supabase_user_id(settings, access_token)
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        summary = await _usage_summary(settings, start.isoformat(), now.isoformat())
        return {
            **summary,
            "period_start_utc": start.isoformat(),
            "period_end_utc": now.isoformat(),
        }

    @app.post("/v1/{endpoint:path}")
    async def proxy_openai(
        endpoint: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_usage_purpose: str = Header(default="other"),
    ) -> Response:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise HTTPException(status_code=404, detail="This OpenAI endpoint is not supported.")
        if x_usage_purpose not in ALLOWED_PURPOSES:
            raise HTTPException(status_code=400, detail="Invalid usage purpose.")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Sign in to use the OpenAI proxy.")
        access_token = authorization.removeprefix("Bearer ").strip()
        if not access_token:
            raise HTTPException(status_code=401, detail="Sign in to use the OpenAI proxy.")

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            raise HTTPException(status_code=400, detail="A model is required.")
        if payload.get("stream") is True:
            raise HTTPException(status_code=400, detail="Streaming OpenAI requests are not supported.")

        settings = Settings.from_env()
        user_id = await _supabase_user_id(settings, access_token)
        request_id = str(uuid4())
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                upstream = await client.post(
                    f"{OPENAI_API_URL}/{endpoint}",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            await _record_event(
                settings,
                request_id=request_id,
                user_id=user_id,
                purpose=x_usage_purpose,
                endpoint=endpoint,
                requested_model=payload["model"],
                status_code=502,
                error_code=type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="OpenAI request failed.") from exc

        try:
            response_body = upstream.json()
        except ValueError:
            response_body = {}
        error = response_body.get("error") if isinstance(response_body, dict) else None
        error_code = None
        if isinstance(error, dict):
            error_code = error.get("code") or error.get("type")
        await _record_event(
            settings,
            request_id=request_id,
            user_id=user_id,
            purpose=x_usage_purpose,
            endpoint=endpoint,
            requested_model=payload["model"],
            status_code=upstream.status_code,
            response_body=response_body if isinstance(response_body, dict) else None,
            provider_request_id=upstream.headers.get("x-request-id"),
            error_code=error_code if isinstance(error_code, str) else None,
        )
        headers = {"X-Usage-Request-ID": request_id}
        provider_request_id = upstream.headers.get("x-request-id")
        if provider_request_id:
            headers["X-OpenAI-Request-ID"] = provider_request_id
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=headers,
        )

    return app


app = create_app()
