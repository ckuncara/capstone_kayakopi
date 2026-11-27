"""Shared command-line options for the manual LLM acceptance runners."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from typing import Any

from kaya_kopi.config import (
    LATEST_CHAT_MODELS,
    MODEL_NAME_PATTERN,
    suggested_chat_models,
)


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--model", action="append", metavar="NAME",
        help="Run the scenarios with NAME; repeat to test several models.",
    )
    parser.add_argument(
        "--all-latest-models", action="store_true",
        help="Run the scenarios once for each of the seven latest GPT-6 models.",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="Print suggested chat models and exit without calling the LLM.",
    )
    parser.add_argument(
        "--case", type=int, action="append", nargs="+", metavar="N",
        help="Run only the listed 1-based scenario numbers; repeat as needed.",
    )
    return parser


def parse_acceptance_args(
    argv: Sequence[str] | None,
    cases: Sequence[tuple[str, str]],
    description: str,
) -> argparse.Namespace:
    """Validate runner options; any syntactically valid model name is accepted."""
    parser = build_parser(description)
    args = parser.parse_args(argv)

    if args.list_models:
        args.models = []
        return args
    if args.all_latest_models and args.model:
        parser.error("--model and --all-latest-models cannot be used together")

    requested = (
        list(LATEST_CHAT_MODELS)
        if args.all_latest_models
        else (args.model or [os.getenv("KAYA_OPENAI_MODEL", "gpt-6-luna")])
    )
    models = []
    for name in requested:
        cleaned = name.strip()
        if not MODEL_NAME_PATTERN.fullmatch(cleaned):
            parser.error(f"Unsafe model name: {name!r}")
        models.append(cleaned)
    args.models = models

    if args.case:
        numbers = [number for group in args.case for number in group]
        invalid = [number for number in numbers if not 1 <= number <= len(cases)]
        if invalid:
            parser.error(f"Case numbers must be between 1 and {len(cases)}: {invalid}")
        args.case = list(dict.fromkeys(numbers))
    return args


def select_acceptance_cases(
    args: argparse.Namespace,
    cases: Sequence[tuple[str, str]],
) -> list[tuple[int, tuple[str, str]]]:
    """Return the requested cases in scenario order, or every case by default."""
    numbers = args.case or list(range(1, len(cases) + 1))
    return [(number, cases[number - 1]) for number in numbers]


def print_suggested_models() -> None:
    print("Suggested chat models:")
    for model in suggested_chat_models():
        print(f"  {model}")


def print_case(number: int, question: str, expected: str, answer: Any) -> None:
    """Render one scenario in the same format for both acceptance runners."""
    print(f"\n=== {number}. QUESTION ===\n{question}")
    print(f"\nEXPECTED CHECK\n{expected}")
    print(f"\nANSWER\n{answer}")
