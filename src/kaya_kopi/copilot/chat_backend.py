"""LangGraph tool-calling backend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from time import perf_counter
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Iterator, TypedDict
from zoneinfo import ZoneInfo
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker  # Added for LLM spend meter (llm_usage_tracker.py)
from kaya_kopi.copilot.tools import ALL_TOOLS
from kaya_kopi.config import AppConfig
from kaya_kopi.security.openai_proxy import openai_client_options
from kaya_kopi.paths import project_root
from kaya_kopi.security.guardrails import protect_answer, protect_text, refuse_request
from kaya_kopi.copilot.timing import log_timing
from kaya_kopi.copilot.result_renderer import report_scope_alignment_warnings


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Kaya Kopi operations copilot.

Use the available tools to answer current business questions. Choose the tool or combination of tools
that best supplies the evidence the question needs. Tool descriptions define their inputs and output;
the capability guide below explains their scope and limitations. Do not choose a tool solely because a
question contains a particular keyword. Begin with a sufficient tool call; combine tools when the
question needs different evidence or a result needs follow-up. Run independent calls together. For
dependent calls, wait for the earlier result and pass its exact identifiers to the next tool.

## Tool capabilities and evidence limits

- `query_business_data_sql`: the primary tool for MySQL business questions, including outlet sales and
  comparisons, custom POS queries, wastage, data-quality issues, and source coverage. It
  accepts one validated read-only query over the approved schema, returns structured rows and source
  metadata, and supports SQL aggregation. Follow the tool's grain, join, invoice-validation, and
  source-coverage notes. POS exports and daily reports may be incomplete; no matching observations are
  not zero activity. The catalog has no recipe cost per cup or observed customer demand response.
- `analyze_outlets_sql`: outlet performance, measured wastage, and roster headcounts. Use it for
  outlet wastage rankings and measured outlet performance; it reports only observed measurements and
  their coverage, so unmeasured outlets must not be treated as zero. Use `analyze_staffing_sql` for
  paid-hour and labour-cost analysis.
- `analyze_pos_sql`: POS product mix, observed prices, payment modes, time patterns, and Kopi margin
  evidence for an outlet type or selected outlets. The margin mode returns observed Kopi sales and
  quantity by outlet, checks source coverage, and identifies missing recipe-cost inputs; it cannot
  establish full sales from partial POS exports or infer per-cup costs from supplier invoice prices.
  Choose it when its focused POS evidence fits; use `query_business_data_sql` for custom POS groupings
  or measures.
- `assess_kopi_price_change_sql`: deterministic revenue sensitivity and revenue break-even scenarios
  for a proposed Kopi price increase, based on observed POS volume. Use it for what-if questions about
  a price change; partial POS coverage limits the conclusion, and missing recipe costs mean profit or
  margin impact cannot be established.
- `get_outlet_sales_sql`: validated sales totals and coverage for one canonical outlet or verified
  outlet alias. Use it for a single outlet; use `query_business_data_sql` for all-outlet lists or custom
  comparisons. Missing daily reports are not zero sales.
- `get_data_coverage_sql`: source periods, counts, and completeness notes for a requested business area.
- `get_invoice_sql`: structured MySQL scan details, line items, review state, reconciliation, and source
  citation for one invoice number. It is entity lookup, not a full invoice search or spend aggregate.
- `analyze_suppliers_sql`: validated scanned-invoice spend, reconciliation, item-level price changes, and
  average scanned line prices by supplier and exact item description. Use `item_price_summary` for
  all-supplier or supplier-filtered average-price reports; it returns valid-line counts and paged
  review-excluded SGD line records.
  Prefer it for invoice-cost totals or ranking supplier price increases because it applies the canonical
  invoice-cost rules and returns comparable first/latest invoice-line evidence. Invoice unit prices are
  purchase prices, not recipe costs per cup without a verified ingredient-to-product mapping. Use
  schema-guided SQL for supplier questions outside those validated analyses or for custom groupings.
- `get_data_quality_issues_sql`: list citable quality issues by status, severity, or rule code. Prefer it
  for direct issue-list requests; use schema-guided SQL for custom quality summaries or joins.
- `analyze_staffing_sql`: aggregated planned staffing hours and labour costs by outlet. It is retained
  because the SQL catalog excludes individual pay fields. Roster patterns are not attendance, and an
  overstaffing verdict needs an approved benchmark. For a multi-month request, totals and aligned sales
  include only months with roster records; report the per-outlet roster-month coverage and do not label
  those partial totals as covering the full requested period.
- `get_supplier_addresses_sql`: up to 10 invoice-backed address records for an exact supplier name; it
  is not a complete address history and does not establish which address is current.
- `search_documents_sql`: relevant citable email and WhatsApp passages, with optional entity filters.
  Search results are relevance-based and are not proof that no other messages exist.
- `lookup_invoice_sqlite`: local uploaded-invoice records, audit details, and supported invoice analysis.
- `search_invoice_chroma`: semantic retrieval over shared invoice OCR chunks. Results are a limited
  search set, not a complete invoice ledger or an authoritative financial aggregate.

## Evidence and reasoning

Base factual claims and calculations on tool results, not model memory. Never invent sales, costs,
recipe yields, demand response, records, or source coverage. A tool's insufficient-evidence status
describes what that tool could establish; consider whether another available tool can provide useful
additional facts, and combine results when appropriate. Do not override a tool's status or turn missing
evidence into zero, no change, or a negative finding. If required evidence is absent, state what is
known, what is missing, and which conclusions remain uncertain. The LLM decides what the combined
evidence supports; it cannot fill gaps in the database.

Treat each tool result's effective period, filters, source, grain, coverage, and explicit classification
or cause field as authoritative. Keep evidence from different periods or scopes separate; do not combine
it into one comparison or ratio unless the periods and entity scope align. If they do not align, state
the periods separately and say the comparison is unavailable. Do not replace a tool's explicit
classification or `UNDETERMINED` cause with a stronger conclusion. A wage rate used to calculate labour
cost is not an overstaffing benchmark; do not use it as one.
When a result's requested period is wider than its recorded source coverage, describe the measured
periods and missing coverage; do not present the requested bounds as the period represented by its totals.

For business advice questions, provide an evidence-based assessment and quantify supported trade-offs
or scenarios. Distinguish observed facts, assumptions, scenarios, and forecasts. Do not claim to make
management's decision. Do not present revenue sensitivity as profit impact when costs are unavailable.
If a question needs legal or tax evidence outside the available data, explain that the connected tools
cannot establish it.

Scanned supplier invoices are authoritative for supplier costs when they conflict with the legacy
purchase table. If a scanned invoice header is internally inconsistent, use validated line arithmetic
and disclose its review status. MATCHED reconciliation means records were linked, not that amounts
agree. A difference between pre-GST subtotals is not explained by GST. Do not rank non-comparable records.
Local SQLite invoice records, Chroma OCR search results, and MySQL business data are separate sources;
identify which source supports each claim and do not treat one as another.

Use only categorical or status values documented by a tool or schema; never invent a status such as
`VALID` when the schema documents `OK`. If a query unexpectedly returns no rows, inspect its filters,
correct unsupported values, and check relevant alternate sources before concluding data is unavailable
or asking the user to choose a retry. Preserve the requested period and label any alternate source or
broader period clearly.

Use the supplied Asia/Singapore date for relative periods. “This quarter” and “this year” start at the
current calendar quarter or year and end today. When a business question has no stated period, use
year-to-date through today as the default; use a longer available source period when it materially
improves a trend or comparison, and state the period used. Preserve explicitly requested dates even when
source coverage ends earlier. No observations in a period are not zero activity. If a requested or
default period has no usable rows, check a broader available period as a separately labeled reference;
never silently replace the requested period. Carry forward the last report's scope only for a clear
follow-up (for example, “same period”, “break that down”, or “show their monthly sales”). Do not apply
the previous period to a new standalone question. Always retrieve fresh business facts.

## Response composition

For a final answer, return one valid JSON object with exactly these fields:
{"finding":"direct answer","key_evidence":[],"limitations":[],"recommendation":null}
`finding` is required. Use short strings in the optional evidence and limitation arrays. Set
`recommendation` to one concise evidence-supported action or null. Do not wrap the JSON in markdown.

Put each fact in one place. Lead with the conclusion; use key evidence for only the figures needed to
explain it; state each decision-relevant limitation once and say what it affects. Do not repeat scenario
rows, record counts, or source lists already displayed by the interface. State scenario thresholds
conditionally; do not call them predictions. Do not confuse no matching observations with zero activity.
Render internal status enums as consistent, human-readable labels such as “Insufficient evidence” or
“No observations”; do not show raw all-caps status codes or bold them. Preserve the status and its
distinction from related statuses, but do not add a separate explanation of the label itself.

Offer a recommendation only when the user asks for advice or a practical action follows from evidence.
Separate revenue effects from profit or margin effects. Label proposals and assumptions, and do not
invent pilot sizes, durations, decision rules, or rollout advice. Avoid generic follow-up menus. If a
report has no rows, explain the evidence status in the finding or limitations.

If a response cannot be represented in this format, answer in ordinary prose; the interface will retain
the text as a compatibility fallback.

Use SGD for monetary amounts. The tools are read-only; do not claim to change client data. Personal and
contact identifiers, contact details, addresses, and individual roster fields are redacted by the
application; do not infer or reconstruct redacted values.
"""


def _relative_date_bounds(today: date) -> dict[str, tuple[date, date]]:
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    this_quarter_start = date(today.year, quarter_month, 1)
    last_quarter_end = this_quarter_start - timedelta(days=1)
    last_quarter_month = ((last_quarter_end.month - 1) // 3) * 3 + 1
    last_quarter_start = date(last_quarter_end.year, last_quarter_month, 1)
    return {
        "this quarter": (this_quarter_start, today),
        "last quarter": (last_quarter_start, last_quarter_end),
        "this year": (date(today.year, 1, 1), today),
        "last year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    }


def _current_system_message(
    report_context: str = "", today: date | None = None,
    scope_warnings: list[str] | None = None,
) -> SystemMessage:
    today = today or datetime.now(ZoneInfo("Asia/Singapore")).date()
    period_context = "\n".join(
        f"{name}: {start.isoformat()} through {end.isoformat()}"
        for name, (start, end) in _relative_date_bounds(today).items()
    )
    system_prompt = SYSTEM_PROMPT
    if _query_business_data_only_mode():
        before_tools, remainder = SYSTEM_PROMPT.split(
            "## Tool capabilities and evidence limits\n", 1
        )
        _, after_tools = remainder.split("\n## Evidence and reasoning", 1)
        system_prompt = (
            before_tools
            + "## Tool capabilities and evidence limits\n"
            + "- `query_business_data_sql`: the only MySQL/SQL tool exposed in this temporary test. "
              "Use it for all MySQL business questions, including focused outlet, POS, supplier, "
              "invoice, staffing, document, and data-quality questions. It accepts one validated "
              "read-only query over the approved schema and returns structured rows and source metadata. "
              "Follow its schema, grain, join, parameter, and source-coverage rules.\n"
            + "- `search_invoice_chroma`: search shared invoice OCR chunks. Results are a limited "
              "search set, not a complete invoice ledger or authoritative financial aggregate.\n"
            + "- `lookup_invoice_sqlite`: look up uploaded invoices or analyze local invoice spending, "
              "audits, and price trends.\n"
            + "\n## Evidence and reasoning"
            + after_tools
            + "\n\nTemporary SQL-tool isolation is active: do not call other SQL tools. "
              "Chroma and SQLite invoice tools remain available."
        )
    context = f"\n\nRecent successful report context (use only for follow-ups):\n{report_context}" if report_context else ""
    scope_context = ""
    if scope_warnings:
        scope_context = (
            "\n\nCurrent-turn evidence has scope mismatches:\n"
            + "\n".join(f"- {warning}" for warning in scope_warnings)
            + "\nKeep those results separate in the answer; do not combine their figures."
        )
    return SystemMessage(
        content=(
            f"Today's date in Asia/Singapore is {today.isoformat()}.\n"
            f"Relative calendar period bounds (inclusive dates):\n{period_context}\n\n"
            f"{system_prompt}{context}{scope_context}"
        )
    )


def _query_business_data_only_mode() -> bool:
    return os.getenv("KAYA_QUERY_BUSINESS_DATA_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _active_tools() -> list:
    if not _query_business_data_only_mode():
        return ALL_TOOLS
    return [
        tool for tool in ALL_TOOLS
        if not tool.name.endswith("_sql") or tool.name == "query_business_data_sql"
    ]


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _safe_tool_value(value: object, key: str = "", allow_supplier_address: bool = False) -> object:
    if key in {"staff_name", "hourly_rate_sgd", "raw_hourly_rate", "hourly_rate", "pay_rate"}:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            name: _safe_tool_value(item, name, allow_supplier_address)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_tool_value(item, allow_supplier_address=allow_supplier_address) for item in value]
    if isinstance(value, str):
        if key == "supplier_address" and allow_supplier_address:
            return protect_text(value, [value])
        return protect_answer(value)
    return value


def _safe_tool_message(message: ToolMessage) -> ToolMessage:
    content = message.content
    if not isinstance(content, str):
        return message.model_copy(update={"content": "Tool output withheld: unsupported format."})
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        content = json.dumps(
            _safe_tool_value(
                value,
                allow_supplier_address=message.name in {
                    "analyze_suppliers_sql", "get_supplier_addresses_sql",
                },
            ),
            ensure_ascii=False,
        )
    else:
        content = protect_answer(content)
    artifact = message.artifact
    if isinstance(artifact, dict):
        artifact = _safe_tool_value(
            artifact,
            allow_supplier_address=message.name in {
                "analyze_suppliers_sql", "get_supplier_addresses_sql",
            },
        )
    else:
        artifact = None
    return message.model_copy(update={"content": content, "artifact": artifact})


SPECIALIST_REPORT_TOOLS = {
    "get_outlet_sales_sql", "compare_outlets_sql", "analyze_outlets_sql",
    "analyze_pos_sql", "analyze_suppliers_sql", "get_supplier_addresses_sql",
    "get_data_coverage_sql", "assess_kopi_price_change_sql", "get_invoice_sql",
    "get_supplier_cost_trend_sql", "search_documents_sql", "get_data_quality_issues_sql",
    "analyze_staffing_sql",
}


def _compact_report_value(value: object, depth: int = 0) -> object:
    if depth >= 6:
        return "[nested details omitted]"
    if isinstance(value, dict):
        return {
            str(key): _compact_report_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return {
                "record_count": len(value),
                "preview": [_compact_report_value(item, depth + 1) for item in value[:3]],
            }
        if len(value) > 8:
            return {
                "record_count": len(value),
                "preview": [_compact_report_value(item, depth + 1) for item in value[:8]],
            }
        return [_compact_report_value(item, depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "… [shortened]"
    return value


def _normalize_specialist_tool_result(message: ToolMessage) -> ToolMessage:
    """Keep full MySQL report data for the UI; send a bounded preview back to the LLM."""
    if message.name not in SPECIALIST_REPORT_TOOLS or not isinstance(message.content, str):
        return message
    try:
        report = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return message
    if not isinstance(report, dict) or report.get("ok") is not True:
        return message

    compact = _compact_report_value(report)
    content = json.dumps(
        {"ok": True, "result_type": "specialist_report", "tool": message.name, "summary": compact},
        ensure_ascii=False,
    )
    if len(content) > 12_000:
        counts = _report_record_counts(report)
        content = json.dumps({
            "ok": True,
            "result_type": "specialist_report",
            "tool": message.name,
            "record_counts": counts,
            "note": "Report detail is extensive; the complete returned data is available in the displayed tables.",
        }, ensure_ascii=False)
    return message.model_copy(update={
        "content": content,
        "artifact": {
            "result_type": "specialist_report",
            "tool_name": message.name,
            "report": report,
        },
    })


def _report_record_counts(value: object, path: tuple[str, ...] = ()) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            counts.update(_report_record_counts(item, (*path, str(key))))
    elif isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            counts[".".join(path)] = len(value)
        for index, item in enumerate(value[:3]):
            if isinstance(item, dict):
                counts.update(_report_record_counts(item, (*path, str(index))))
    return counts


def _safe_ai_message(message: AIMessage, addresses: list[str]) -> AIMessage:
    content = message.content
    if isinstance(content, list):
        # Some model/API combinations return text as content blocks instead of a
        # single string. Normalize only text blocks so JSON answer validation and
        # the existing redaction path still run on the actual answer.
        text_blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") in {"text", "output_text"}
            and isinstance(block.get("text", ""), str)
        ]
        content = "".join(text_blocks) if text_blocks else content
    if isinstance(content, str):
        try:
            structured = json.loads(content)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, dict) and {
            "finding", "key_evidence", "limitations", "recommendation"
        }.issubset(structured):
            # Apply the existing text guard to each field so words in separate JSON fields
            # cannot combine into a false tax/business-advice match.
            for field in ("finding", "recommendation"):
                if isinstance(structured[field], str):
                    structured[field] = protect_answer(structured[field], addresses)
            for field in ("key_evidence", "limitations"):
                if isinstance(structured[field], list):
                    structured[field] = [
                        protect_answer(item, addresses) if isinstance(item, str) else item
                        for item in structured[field]
                    ]
            content = json.dumps(structured, ensure_ascii=False)
        else:
            content = protect_answer(content, addresses)
    elif content:
        content = "The model returned an unsupported answer format."
    calls = [
        {**call, "args": _safe_tool_value(call["args"])}
        for call in message.tool_calls
    ]
    extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
    return message.model_copy(
        update={"content": content, "tool_calls": calls, "additional_kwargs": extra}
    )


def _supplier_addresses(messages: list[BaseMessage]) -> list[str]:
    addresses = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {
            "analyze_suppliers_sql", "get_supplier_addresses_sql",
        }:
            continue
        if isinstance(message.artifact, dict) and message.artifact.get("result_type") == "specialist_report":
            result = message.artifact.get("report")
        else:
            try:
                result = json.loads(message.content)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(result, dict):
            continue
        if message.name == "analyze_suppliers_sql":
            summary = result.get("summary")
            records = [result.get("invoice")]
            if isinstance(summary, dict):
                records.append(summary.get("invoice"))
        else:
            records = result.get("results", [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            address = record.get("supplier_address")
            if isinstance(address, str) and address:
                addresses.append(address)
    return addresses


def _report_entity_ids(report: dict[str, object]) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {
        "outlet_codes": [], "supplier_names": [], "supplier_codes": [],
        "item_names": [], "invoice_numbers": [],
    }
    key_map = {
        "outlet_code": "outlet_codes",
        "canonical_outlet_code": "outlet_codes",
        "supplier_name": "supplier_names",
        "canonical_supplier_name": "supplier_names",
        "supplier_code": "supplier_codes",
        "item_name": "item_names",
        "description": "item_names",
        "invoice_number": "invoice_numbers",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                target = key_map.get(str(key).lower())
                if target and isinstance(item, str) and item.strip():
                    if item not in collected[target]:
                        collected[target].append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(report)
    return {key: values[:30] for key, values in collected.items() if values}


def _latest_business_report_context(messages: list[BaseMessage]) -> str:
    """Carry compact effective scope from the latest successful structured report."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, ToolMessage) or not isinstance(message.artifact, dict):
            continue
        report = message.artifact
        result_type = report.get("result_type")
        if result_type not in {"business_data", "specialist_report"}:
            continue
        question = next(
            (str(previous.content) for previous in reversed(messages[:index]) if previous.type == "human"),
            "",
        )
        call = next(
            (
                tool_call
                for previous in reversed(messages[:index])
                if isinstance(previous, AIMessage)
                for tool_call in previous.tool_calls
                if tool_call.get("id") == message.tool_call_id
            ),
            None,
        )
        arguments = call.get("args", {}) if call else {}
        if not isinstance(arguments, dict):
            arguments = {}
        details: dict[str, object] = {"previous_question": question[:500]}
        if result_type == "business_data":
            details.update({
                "report_type": "schema_guided_sql",
                "query": arguments.get("sql", "")[:1500],
                "query_parameters": arguments.get("parameters", [])[:20],
                "source_tables": report.get("sources", []),
                "query_intent": report.get("query_intent"),
                "result_columns": report.get("columns", []),
                "retrieved_rows": report.get("retrieved_record_count"),
                "truncated": report.get("truncated", False),
                "returned_entity_ids": _report_entity_ids({"rows": report.get("rows", [])}),
            })
        else:
            data = report.get("report", {})
            if not isinstance(data, dict):
                continue
            tool_name = str(report.get("tool_name", message.name or ""))
            report_scope = data.get("scope")
            report_filters = data.get("filters")
            if not isinstance(report_scope, dict):
                report_scope = {}
            if not isinstance(report_filters, dict):
                report_filters = {}
            effective_period = data.get("period") or report_scope.get("period")
            if not isinstance(effective_period, dict):
                effective_period = {
                    "start": arguments.get("start_date"),
                    "end": arguments.get("end_date"),
                }
            fields = (
                "analysis", "group_by", "outlet_type", "supplier_query", "item", "menu_item",
                "outlet_code", "outlet_codes", "supplier_name", "supplier_code", "invoice_number",
                "area",
            )
            effective_filters = {
                key: data[key]
                for key in fields
                if key in data and data[key] not in (None, [], "")
            }
            effective_filters.update({
                key: value for key, value in report_filters.items()
                if key in {"outlet_code", "supplier_name"} and value not in (None, "")
            })
            effective_filters.update({
                key: report_scope[key]
                for key in ("outlet_type", "default_period")
                if key in report_scope
            })
            details.update({
                "report_type": "specialist",
                "tool": tool_name,
                "analysis": data.get("analysis") or arguments.get("analysis"),
                "period": effective_period,
                "filters_and_grouping": effective_filters,
                "status": data.get("status"),
                "authority": data.get("authority"),
                "coverage_warning": data.get("coverage_warning") or data.get("limitation"),
                "returned_entity_ids": _report_entity_ids(data),
                "result_was_partial": data.get("status") == "PARTIAL",
            })
        return json.dumps(details, ensure_ascii=False, default=str)[:4000]
    return ""


def _repair_interrupted_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Supply error results for calls interrupted before their tool results were saved."""
    repaired: list[BaseMessage] = []
    pending: dict[str, str] = {}

    def finish_pending() -> None:
        for call_id, name in pending.items():
            repaired.append(ToolMessage(
                content="This tool call was interrupted; no result is available.",
                tool_call_id=call_id,
                name=name,
                status="error",
            ))
        pending.clear()

    for message in messages:
        if not isinstance(message, ToolMessage):
            finish_pending()
        repaired.append(message)
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if not call["id"]:
                    raise ValueError("A saved tool call is missing its ID.")
                pending[call["id"]] = call["name"]
        elif isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
    finish_pending()
    return repaired


def _chat_database_path() -> Path:
    path = Path(os.getenv("KAYA_CHAT_DB", ".kaya_chat.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=1)
def get_chatbot(access_token: str | None = None):
    """Build and cache the persistent tool-calling graph."""
    build_started = perf_counter()
    load_dotenv(project_root() / ".env")
    cfg = AppConfig()
    if not cfg.llm_proxy_url and not cfg.api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add the team API key."
        )
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    model_options = {}
    if model_name.casefold().startswith("gpt-6"):
        # GPT-6 Chat Completions requires reasoning_effort=none when function tools are bound.
        model_options["reasoning_effort"] = "none"
    model = ChatOpenAI(
        model=model_name,
        temperature=0,
        timeout=30,
        max_retries=2,
        **model_options,
        **openai_client_options(cfg, "chat", access_token=access_token),
    )
    active_tools = _active_tools()
    model_with_tools = model.bind_tools(active_tools)

    tool_node = ToolNode(active_tools)

    def safe_tools(state: ChatState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        started = perf_counter()
        last_message = state["messages"][-1] if state.get("messages") else None
        tool_names = [call.get("name", "unknown") for call in getattr(last_message, "tool_calls", [])]
        turn_id = (config.get("configurable") or {}).get("copilot_timing_id")
        try:
            result = tool_node.invoke(state, config=config)
        except Exception as error:
            log_timing("tool_batch", (perf_counter() - started) * 1000, turn_id,
                       tools=",".join(tool_names), outcome="error", error_type=type(error).__name__)
            raise
        log_timing("tool_batch", (perf_counter() - started) * 1000, turn_id,
                   tools=",".join(tool_names), outcome="ok")
        return {
            "messages": [
                _safe_tool_message(_normalize_specialist_tool_result(message))
                if isinstance(message, ToolMessage) else message
                for message in result["messages"]
            ]
        }

    def chat_node(state: ChatState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        from langchain_core.messages import HumanMessage

        messages = []
        for message in _repair_interrupted_tool_calls(state["messages"]):
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, ToolMessage):
                message = _safe_tool_message(message)
            elif isinstance(message, HumanMessage) and isinstance(message.content, str):
                message = message.model_copy(update={"content": protect_text(message.content)})
            elif isinstance(message, AIMessage):
                message = _safe_ai_message(message, _supplier_addresses(messages))
            messages.append(message)
        last_user_index = max(
            (index for index, message in enumerate(messages) if message.type == "human"),
            default=-1,
        )
        current_turn_reports = [
            message.artifact
            for message in messages[last_user_index + 1:]
            if isinstance(message, ToolMessage)
            and isinstance(message.artifact, dict)
            and message.artifact.get("result_type") in {"business_data", "specialist_report"}
        ]
        scope_warnings = report_scope_alignment_warnings(current_turn_reports)
        messages.insert(0, _current_system_message(
            _latest_business_report_context(messages), scope_warnings=scope_warnings,
        ))
        
        # Invoke model and capture response with token usage (llm_usage_tracker.py)
        started = perf_counter()
        turn_id = (config.get("configurable") or {}).get("copilot_timing_id")
        try:
            response = model_with_tools.invoke(messages)
        except Exception as error:
            log_timing("model_call", (perf_counter() - started) * 1000, turn_id,
                       outcome="error", error_type=type(error).__name__, message_count=len(messages))
            raise
        usage = response.usage_metadata or {}
        log_timing(
            "model_call", (perf_counter() - started) * 1000, turn_id,
            outcome="ok", tool_calls=len(response.tool_calls), message_count=len(messages),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        )
        response = _safe_ai_message(response, _supplier_addresses(messages))
        
        # Store response metadata with tokens in state for later capture (llm_usage_tracker.py)
        if hasattr(response, 'response_metadata') and response.response_metadata:
            state['_last_response_metadata'] = response.response_metadata
        
        return {"messages": [response]}

    connection = sqlite3.connect(_chat_database_path(), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    graph = StateGraph(ChatState)
    graph.add_node("chat", chat_node)
    graph.add_node("tools", safe_tools)
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "chat")
    compiled = graph.compile(checkpointer=checkpointer)
    log_timing("graph_build", (perf_counter() - build_started) * 1000, outcome="ok")
    return compiled


def retrieve_all_threads(access_token: str | None = None) -> list[str]:
    """Return saved conversation IDs, newest checkpoint first."""
    chatbot = get_chatbot(access_token)
    seen: set[str] = set()
    thread_ids: list[str] = []
    for checkpoint in chatbot.checkpointer.list(None):
        thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            thread_ids.append(thread_id)
    return thread_ids


def get_thread_names(access_token: str | None = None) -> dict[str, str]:
    """Return saved conversation names keyed by thread ID."""
    with get_chatbot(access_token).checkpointer.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS conversation_names (thread_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        return dict(cursor.execute("SELECT thread_id, name FROM conversation_names").fetchall())


def rename_thread(thread_id: str, name: str, access_token: str | None = None) -> None:
    """Save a name for an existing conversation."""
    name = protect_text(name.strip())
    if not name:
        raise ValueError("Conversation name cannot be empty.")
    if len(name) > 80:
        raise ValueError("Conversation name cannot exceed 80 characters.")
    if thread_id not in retrieve_all_threads(access_token):
        raise ValueError("Conversation no longer exists.")
    get_thread_names(access_token)
    with get_chatbot(access_token).checkpointer.cursor() as cursor:
        cursor.execute(
            "INSERT INTO conversation_names (thread_id, name) VALUES (?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET name=excluded.name",
            (thread_id, name),
        )


def delete_thread(thread_id: str, access_token: str | None = None) -> None:
    """Delete one conversation's checkpoints and name."""
    checkpointer = get_chatbot(access_token).checkpointer
    checkpointer.delete_thread(thread_id)
    get_thread_names(access_token)
    with checkpointer.cursor() as cursor:
        cursor.execute("DELETE FROM conversation_names WHERE thread_id=?", (thread_id,))


def stream_chat(
    thread_id: str, message: str, access_token: str | None = None
) -> Iterator[tuple[BaseMessage, dict]]:
    """Stream one user turn through the LLM/tool loop, capturing token usage."""
    from langchain_core.messages import HumanMessage, AIMessage
    import streamlit as st

    refusal = refuse_request(message)
    if refusal:
        yield AIMessage(content=refusal), {}
        return
    message = protect_text(message)
    turn_id = str(uuid4())
    started = perf_counter()
    config = {"configurable": {"thread_id": thread_id, "copilot_timing_id": turn_id}}
    outcome = "ok"
    
    # Get the shared tracker from Streamlit session state (llm_usage_tracker.py, llm_ui_components.py)
    if "usage_tracker" not in st.session_state:
        st.session_state.usage_tracker = LLMUsageTracker()
    tracker = st.session_state["usage_tracker"]
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    
    # Stream with stream_mode="updates" to capture token data
    # In "updates" mode, we get deltas like {"chat": state} or {"tools": state}
    try:
        for update in get_chatbot(access_token).stream(
            {"messages": [HumanMessage(content=message, id=turn_id)]},
            config=config,
            stream_mode="updates",
        ):
            # In "updates" mode, each update is a dict: {"node_name": state_update}
            for node_name, node_state in update.items():
                if "messages" in node_state:
                    messages = node_state["messages"]
                    if messages:
                        for msg in messages:
                            if isinstance(msg, AIMessage):
                                tracker.log_response(
                                    thread_id, message, msg, model_name, turn_id=turn_id,
                                )
                            yield (msg, {"turn_id": turn_id})
    except Exception as error:
        outcome = "error"
        logger.error("Copilot turn failed [turn_id=%s error_type=%s]", turn_id, type(error).__name__)
        raise
    finally:
        log_timing("turn_stream", (perf_counter() - started) * 1000, turn_id,
                   outcome=outcome, model=model_name)
