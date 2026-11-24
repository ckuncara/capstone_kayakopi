from pathlib import Path

from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker


class BudgetExceededError(RuntimeError):
    """Raised when current month's OpenAI spend reaches MONTHLY_BUDGET_USD."""


class BudgetGuard:
    def __init__(self, sqlite_path: Path, monthly_limit_usd: float = 50.0):
        self.sqlite_path = sqlite_path
        self.monthly_limit_usd = monthly_limit_usd
        self.usage_tracker = LLMUsageTracker(db_path=sqlite_path)

    def get_current_month_spend(self) -> float:
        return self.usage_tracker.get_current_month_spend()

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
        thread_id: str,
        prompt: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        return self.usage_tracker.log_usage(
            thread_id, prompt, model_name, input_tokens, output_tokens
        )