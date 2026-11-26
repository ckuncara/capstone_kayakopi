"""Safe, correlated timing logs for Copilot request stages."""

from __future__ import annotations

import logging
import sys


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


def log_timing(stage: str, elapsed_ms: float, turn_id: str | None = None, **details: object) -> None:
    """Log durations and non-sensitive stage metadata without query or user data."""
    fields = {"stage": stage, "elapsed_ms": round(elapsed_ms, 1)}
    if turn_id:
        fields["turn_id"] = turn_id
    fields.update(details)
    logger.info("[COPILOT_TIMING] %s", " ".join(f"{key}={value}" for key, value in fields.items()))
