from pathlib import Path
import sqlite3

MODEL_PRICING_PER_1M = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.00},
    "text-embedding-3-large": {"input": 0.13, "output": 0.00},
}


class BudgetExceededError(RuntimeError):
    """Raised when current month's OpenAI spend reaches MONTHLY_BUDGET_USD."""


class BudgetGuard:
    def __init__(self, sqlite_path: Path, monthly_limit_usd: float = 50.0):
        self.sqlite_path = sqlite_path
        self.monthly_limit_usd = monthly_limit_usd

    def _connect(self) -> sqlite3.Connection:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.sqlite_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def get_current_month_spend(self) -> float:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    branch_code TEXT DEFAULT 'HQ',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            row = conn.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0) AS spend
                FROM api_usage_logs
                WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
                """
            ).fetchone()
            return float(row["spend"])

    def check_budget_or_raise(self) -> float:
        current_spend = self.get_current_month_spend()
        if current_spend >= self.monthly_limit_usd:
            raise BudgetExceededError(
                f"Monthly OpenAI budget cap reached (${current_spend:.4f} / "
                f"${self.monthly_limit_usd:.2f}). OpenAI calls are paused."
            )
        return current_spend

    def log_usage(
        self,
        operation: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
        branch_code: str = "HQ",
    ) -> float:
        rates = MODEL_PRICING_PER_1M.get(model, {"input": 0.15, "output": 0.60})
        cost = (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_usage_logs (
                    operation, model, prompt_tokens, completion_tokens, cost_usd, branch_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (operation, model, prompt_tokens, completion_tokens, round(cost, 6), branch_code),
            )
        return cost