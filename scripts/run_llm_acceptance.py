#!/usr/bin/env python3
"""Run manual, non-deterministic acceptance questions through the real copilot."""

from __future__ import annotations

from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from kaya_kopi.copilot.chat_backend import get_chatbot
from kaya_kopi.config import AppConfig
from kaya_kopi.security.supabase_auth import ensure_cli_sign_in


CASES = [
    (
        "what's our margin on Kopi at mall outlets this quarter",
        "Must use the current Singapore calendar quarter, resolve the four mall outlets, and explain the missing sales coverage and cost per cup without reporting a zero margin.",
    ),
    (
        "Based on the available sales data, should we increase the price of Kopi by S$0.10?",
        "Must show deterministic revenue sensitivity and any revenue-only break-even threshold from observed volume, disclose partial POS coverage, and not claim profit impact without recipe cost per cup.",
    ),
    (
        "Which supplier raised prices the most this year?",
        "Must use the Singapore calendar year to date, state actual scan coverage, and cite the first and latest invoice lines for the leading comparable increase.",
    ),
    (
        "Are the mall stalls over-staffed for what they sell?",
        "Must resolve the mall outlets, show scheduled cost alongside partial POS takings for the latest roster month, and explain what is missing for a verdict.",
    ),
    (
        "Retrieve sales for legacy outlet code AMK-3A and explain which canonical outlet it belongs to.",
        "Must resolve AMK-3A to AMK-03, return sales, and cite the alias evidence.",
    ),
    (
        "Which outlet has the highest wastage, and can we make that comparison across all twelve outlets?",
        "Must name BDK-01 among measured outlets, include 640.400 kg, and refuse a twelve-outlet ranking.",
    ),
    (
        "Which outlets appear overstaffed in June 2026 based on paid roster hours and authoritative gross takings?",
        "Must not classify any outlet without an approved threshold; must exclude partial-POS outlets from productivity comparison.",
    ),
    (
        "How much did we spend with each supplier by month, and which invoices were excluded or require review?",
        "Must provide supplier-month figures and identify excluded invoice INV-022602-661 as REVIEW_REQUIRED. INV-012601-458 is retained using validated invoice lines.",
    ),
    (
        "Why does purchase record INV-012508-182 not match the scanned invoice? Is GST responsible for the difference?",
        "Must distinguish identity match from amount mismatch, state the S$9.33 subtotal difference, reject GST as the cause, and leave cause undetermined.",
    ),
    (
        "Which supplier increased its prices the most over the past year? Show evidence from the scanned invoice lines.",
        "Must identify Mewah/SUP-04 Condensed milk ctn as the largest comparable increase (8.03%) with first/latest invoice evidence, and disclose the observed invoice coverage period.",
    ),
]


def main() -> None:
    cfg = AppConfig()
    if cfg.llm_proxy_url:
        ensure_cli_sign_in(cfg)
    chatbot = get_chatbot()
    for number, (question, expected) in enumerate(CASES, start=1):
        result = chatbot.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"configurable": {"thread_id": f"acceptance-{uuid4()}"}},
        )
        answers = [message.content for message in result["messages"] if isinstance(message, AIMessage) and message.content]
        answer = answers[-1] if answers else "<NO FINAL ANSWER>"
        print(f"\n=== {number}. QUESTION ===\n{question}")
        print(f"\nEXPECTED CHECK\n{expected}")
        print(f"\nANSWER\n{answer}")


if __name__ == "__main__":
    main()
