"""LLM usage tracking and spend meter for the Kaya Kopi operations copilot."""

from __future__ import annotations

import os
import json
import logging
import math
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage

from kaya_kopi.paths import project_root


# Standard USD per 1M tokens, short context (<=272K input tokens).
# Source: https://developers.openai.com/api/docs/pricing (checked 2026-11-26).
# Review against the provider before changing rates. Batch/Fast/Flex/Ultrafast and
# long-context rates are deliberately not used: normal chat calls use Standard.
PRICING_VERSION = "openai-standard-short-context-2026-11-26"


def rate(input_price: str, cached_input: str, output_price: str) -> dict[str, str]:
    """Build one independently editable model rate card."""
    return {"input": input_price, "cached_input": cached_input, "output": output_price}


MODEL_PRICING = {
    "gpt-4o": rate("2.50", "1.25", "10.00"),
    "gpt-4o-mini": rate("0.15", "0.075", "0.60"),
    "gpt-4.1": rate("2.00", "0.50", "8.00"),
    "gpt-4.1-mini": rate("0.40", "0.10", "1.60"),
    # Official Standard pricing for the latest GPT-6 chat models. Each model has
    # its own card; changing one here does not change any other model's estimate.
    "gpt-6.2-astra": rate("8.00", "0.40", "40.00"),
    "gpt-6.2-sol": rate("2.00", "0.10", "10.00"),
    "gpt-6.1-terra": rate("1.00", "0.05", "5.00"),
    "gpt-6.1-sol": rate("2.00", "0.10", "10.00"),
    "gpt-6-astra": rate("10.00", "1.00", "50.00"),
    "gpt-6-sol": rate("2.00", "0.20", "10.00"),
    "gpt-6-luna": rate("0.10", "0.01", "0.50"),
    "text-embedding-3-small": rate("0.02", "0.02", "0"),
    "text-embedding-3-large": rate("0.13", "0.13", "0"),
}
MODEL_ALIASES = {
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gpt-4o-2024-08-06": "gpt-4o",
    "gpt-4o-2024-11-20": "gpt-4o",
    "openai/gpt-4.1": "gpt-4.1",
    "openai/gpt-4.1-mini": "gpt-4.1-mini",
}
logger = logging.getLogger(__name__)


def canonical_model_name(model_name: str) -> str:
    """Strip known aliases and provider prefixes before looking up pricing."""
    name = MODEL_ALIASES.get(model_name, model_name)
    if name.startswith("openai/") or name.startswith("openrouter/"):
        name = name.split("/", 1)[1]
    return name


def pricing_for_model(model_name: str) -> dict[str, str] | None:
    """Return the configured official rate card, or None for an unpriced model."""
    return MODEL_PRICING.get(canonical_model_name(model_name))

BUDGET_USD = 50.0


class TurnUsageSummary(TypedDict):
    models: list[str]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    total_cost: float | None


class LLMUsageTracker:
    """Track LLM token usage and spending."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the usage tracker with SQLite database."""
        load_dotenv(project_root() / ".env")
        
        if db_path is None:
            from kaya_kopi.config import AppConfig

            db_path = AppConfig().sqlite_db_path
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database schema."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    thread_id TEXT,
                    prompt TEXT,
                    model_name TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    input_cost REAL,
                    output_cost REAL,
                    total_cost REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON llm_usage(timestamp DESC)
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
            for name, definition in {
                "cached_input_tokens": "INTEGER",
                "reasoning_tokens": "INTEGER",
                "usage_source": "TEXT NOT NULL DEFAULT 'legacy'",
                "usage_details_json": "TEXT",
                "response_id": "TEXT",
                "requested_model": "TEXT",
                "service_tier": "TEXT",
                "pricing_version": "TEXT",
                "cost_status": "TEXT NOT NULL DEFAULT 'legacy_estimate'",
                "turn_id": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_turn ON llm_usage(thread_id, turn_id)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS billing_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    provider_cost_usd REAL NOT NULL,
                    scope TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def log_response(
        self, thread_id: str, prompt: str, response: AIMessage, requested_model: str,
        *, turn_id: str | None = None,
    ) -> float | None:
        """Record provider usage without substituting guessed token counts."""
        usage = response.usage_metadata or {}
        metadata = getattr(response, "response_metadata", None) or {}
        return self.log_usage(
            thread_id, prompt, metadata.get("model_name") or requested_model,
            usage.get("input_tokens"), usage.get("output_tokens"),
            cached_input_tokens=usage.get("input_token_details", {}).get("cache_read", 0),
            reasoning_tokens=usage.get("output_token_details", {}).get("reasoning", 0),
            usage_source="provider" if usage else "missing",
            usage_details_json=json.dumps(usage),
            response_id=getattr(response, "id", None),
            requested_model=requested_model,
            service_tier=metadata.get("service_tier") or "default",
            turn_id=turn_id,
        )

    def log_usage(
        self,
        thread_id: str,
        prompt: str,
        model_name: str,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
        usage_source: str = "provider",
        usage_details_json: str | None = None,
        response_id: str | None = None,
        requested_model: str | None = None,
        service_tier: str = "default",
        turn_id: str | None = None,
    ) -> float | None:
        """
        Log LLM usage and return the total cost.
        
        Args:
            thread_id: The conversation thread ID
            prompt: The user prompt
            model_name: The LLM model name
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            
        Returns:
            Estimated USD cost, or None when usage or applicable pricing is unavailable.
        """
        for value in (input_tokens, output_tokens, cached_input_tokens, reasoning_tokens):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError("Token counts must be non-negative integers.")
        if input_tokens is not None and cached_input_tokens > input_tokens:
            raise ValueError("Cached input cannot exceed total input tokens.")
        if output_tokens is not None and reasoning_tokens > output_tokens:
            raise ValueError("Reasoning tokens cannot exceed total output tokens.")
        pricing = pricing_for_model(model_name)
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None else None
        )
        if total_tokens is None:
            usage_source = "missing"
        details = json.loads(usage_details_json) if usage_details_json else {}
        audio_tokens = (
            details.get("input_token_details", {}).get("audio", 0)
            + details.get("output_token_details", {}).get("audio", 0)
        )
        priceable = (
            pricing is not None and total_tokens is not None
            and service_tier in ("default", "standard") and not audio_tokens
        )
        input_cost = output_cost = total_cost = None
        if priceable and pricing is not None and input_tokens is not None and output_tokens is not None:
            input_cost = float((
                Decimal(input_tokens - cached_input_tokens) * Decimal(pricing["input"])
                + Decimal(cached_input_tokens) * Decimal(pricing["cached_input"])
            ) / Decimal(1_000_000))
            # Reasoning is already included in output_tokens; never charge it twice.
            output_cost = float(
                Decimal(output_tokens) * Decimal(pricing["output"]) / Decimal(1_000_000)
            )
            total_cost = input_cost + output_cost
        else:
            logger.warning("Usage saved without a price: model=%s, tier=%s, source=%s",
                           model_name, service_tier, usage_source)
        
        # Store in database
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("""
                INSERT INTO llm_usage (
                    thread_id, prompt, model_name, input_tokens, output_tokens,
                    total_tokens, input_cost, output_cost, total_cost,
                    cached_input_tokens, reasoning_tokens, usage_source, usage_details_json,
                    response_id, requested_model, service_tier, pricing_version, cost_status,
                    turn_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                thread_id, prompt, model_name, input_tokens, output_tokens,
                total_tokens, input_cost, output_cost, total_cost,
                cached_input_tokens, reasoning_tokens, usage_source, usage_details_json,
                response_id, requested_model, service_tier,
                PRICING_VERSION if priceable else None,
                "estimated" if priceable else "unpriced",
                turn_id,
            ))
            conn.commit()
        
        return total_cost

    def get_turn_usage(self, thread_id: str, turn_id: str) -> TurnUsageSummary | None:
        """Sum only the model calls associated with this saved human message."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT model_name, input_tokens, output_tokens, total_tokens, total_cost
                   FROM llm_usage WHERE thread_id=? AND turn_id=? ORDER BY id""",
                (thread_id, turn_id),
            ).fetchall()
        if not rows:
            return None
        return {
            "models": sorted({row["model_name"] for row in rows}),
            "input_tokens": (
                sum(row["input_tokens"] for row in rows)
                if all(row["input_tokens"] is not None for row in rows) else None
            ),
            "output_tokens": (
                sum(row["output_tokens"] for row in rows)
                if all(row["output_tokens"] is not None for row in rows) else None
            ),
            "total_tokens": (
                sum(row["total_tokens"] for row in rows)
                if all(row["total_tokens"] is not None for row in rows) else None
            ),
            "total_cost": (
                sum(row["total_cost"] for row in rows)
                if all(row["total_cost"] is not None for row in rows) else None
            ),
        }

    def get_unpriced_count(self, *, current_month: bool = False) -> int:
        where = "WHERE total_cost IS NULL"
        if current_month:
            where += """ AND timestamp >= datetime('now', 'start of month')
                         AND timestamp < datetime('now', 'start of month', '+1 month')"""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM llm_usage {where}").fetchone()[0]

    def save_reconciliation(
        self, start_date: date, end_date: date, provider_cost_usd: float, scope: str,
    ) -> None:
        """Save a manual provider total for [start_date, end_date) in UTC."""
        if start_date >= end_date:
            raise ValueError("End date must be after start date (exclusive, UTC).")
        if not math.isfinite(provider_cost_usd) or provider_cost_usd < 0:
            raise ValueError("Provider cost must be a finite non-negative USD amount.")
        if not scope.strip():
            raise ValueError("Describe the matching provider project/API-key scope.")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO billing_reconciliations
                   (start_date, end_date, provider_cost_usd, scope) VALUES (?, ?, ?, ?)""",
                (start_date.isoformat(), end_date.isoformat(), provider_cost_usd, scope.strip()),
            )

    def get_latest_reconciliation(self) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM billing_reconciliations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            totals = conn.execute(
                """SELECT COALESCE(SUM(total_cost), 0) AS estimated_cost,
                          COUNT(*) AS calls, SUM(total_cost IS NULL) AS unpriced_calls,
                          SUM(cost_status='legacy_estimate') AS legacy_calls
                   FROM llm_usage WHERE timestamp >= ? AND timestamp < ?""",
                (result["start_date"], result["end_date"]),
            ).fetchone()
            result.update(dict(totals))
            result["difference_usd"] = result["provider_cost_usd"] - result["estimated_cost"]
            return result

    def get_total_spend(self) -> float:
        """Get total spending across all usages."""
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "SELECT COALESCE(SUM(total_cost), 0) FROM llm_usage"
            ).fetchone()
            return result[0] if result else 0.0

    def get_current_month_spend(self) -> float:
        """Get spending for the current UTC month."""
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                """
                SELECT COALESCE(SUM(total_cost), 0.00) FROM llm_usage
                WHERE timestamp >= datetime('now', 'start of month')
                  AND timestamp < datetime('now', 'start of month', '+1 month')
                """
            ).fetchone()
            return float(result[0]) if result else 0.0

    def get_latest_usage(self) -> Optional[dict]:
        """Get the most recent usage entry."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            result = conn.execute("""
                SELECT * FROM llm_usage
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
            """).fetchone()
            return dict(result) if result else None

    def get_all_usage_history(self) -> list[dict]:
        """Get all usage history sorted by timestamp descending."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            results = conn.execute("""
                SELECT * FROM llm_usage
                ORDER BY timestamp DESC, id DESC
            """).fetchall()
            return [dict(row) for row in results]

    def get_current_model(self) -> str:
        """Get the currently configured LLM model."""
        return os.getenv("KAYA_OPENAI_MODEL", "gpt-6-luna")

    def get_budget_info(self) -> dict:
        """Get budget information."""
        budget = float(os.getenv("MONTHLY_BUDGET_USD", str(BUDGET_USD)))
        total_spent = self.get_current_month_spend()
        budget_left = max(0, budget - total_spent)
        percentage_used = (total_spent / budget) * 100 if budget > 0 else 0
        
        return {
            "total_budget": budget,
            "total_spent": total_spent,
            "budget_left": budget_left,
            "percentage_used": min(100, percentage_used),
        }

    def get_color_for_spending(self, percentage: float) -> str:
        """Get color indicator based on spending percentage."""
        if percentage <= 50:
            return "green"
        elif percentage <= 80:
            return "orange"
        else:
            return "red"
