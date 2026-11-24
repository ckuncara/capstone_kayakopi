"""LLM usage tracking and spend meter for the Kaya Kopi operations copilot."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from kaya_kopi.paths import project_root


# Token pricing per model (USD per 1K tokens)
MODEL_PRICING = {
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
    "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
}

BUDGET_USD = 50.0


class LLMUsageTracker:
    """Track LLM token usage and spending."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the usage tracker with SQLite database."""
        load_dotenv(project_root() / ".env")
        
        if db_path is None:
            db_path = Path(os.getenv("KAYA_CHAT_DB", ".kaya_chat.sqlite3")).parent / "llm_usage.sqlite3"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database schema."""
        with sqlite3.connect(self.db_path) as conn:
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
            conn.commit()

    def log_usage(
        self,
        thread_id: str,
        prompt: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Log LLM usage and return the total cost.
        
        Args:
            thread_id: The conversation thread ID
            prompt: The user prompt
            model_name: The LLM model name
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            
        Returns:
            Total cost in USD for this usage
        """
        # Get pricing for the model
        pricing = MODEL_PRICING.get(model_name, MODEL_PRICING["gpt-4o-mini"])
        
        # Calculate costs
        input_cost = (input_tokens / 1000) * pricing["input"]
        output_cost = (output_tokens / 1000) * pricing["output"]
        total_cost = input_cost + output_cost
        total_tokens = input_tokens + output_tokens
        
        # Store in database
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO llm_usage (
                    thread_id, prompt, model_name, input_tokens, output_tokens,
                    total_tokens, input_cost, output_cost, total_cost
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                thread_id, prompt, model_name, input_tokens, output_tokens,
                total_tokens, input_cost, output_cost, total_cost
            ))
            conn.commit()
        
        return total_cost

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
                SELECT COALESCE(SUM(total_cost), 0.0) FROM llm_usage
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
                ORDER BY timestamp DESC
                LIMIT 1
            """).fetchone()
            return dict(result) if result else None

    def get_all_usage_history(self) -> list[dict]:
        """Get all usage history sorted by timestamp descending."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            results = conn.execute("""
                SELECT 
                    timestamp,
                    prompt,
                    model_name,
                    total_tokens,
                    total_cost
                FROM llm_usage
                ORDER BY timestamp DESC
            """).fetchall()
            return [dict(row) for row in results]

    def get_current_model(self) -> str:
        """Get the currently configured LLM model."""
        return os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")

    def get_budget_info(self) -> dict:
        """Get budget information."""
        total_spent = self.get_total_spend()
        budget_left = max(0, BUDGET_USD - total_spent)
        percentage_used = (total_spent / BUDGET_USD) * 100 if BUDGET_USD > 0 else 0
        
        return {
            "total_budget": BUDGET_USD,
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
