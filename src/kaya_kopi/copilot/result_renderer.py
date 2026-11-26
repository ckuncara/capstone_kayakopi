"""UI rendering for bounded structured Copilot business results."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


def render_business_results(
    ui: Any, results: list[dict[str, Any]], question: str | None = None,
) -> None:
    """Render all rows retained in each result artifact without model summarization."""
    for result in results:
        rows = result.get("rows", [])
        columns = result.get("columns", [])
        ui.markdown("**Query results**")
        if rows:
            intent = result.get("query_intent", {})
            if not _chart_rows_match_declared_scope(rows, columns, intent):
                ui.caption(
                    "Chart omitted because returned rows conflict with the declared query scope; "
                    "the complete result table is shown below."
                )
            elif not _business_chart_scope_is_clear(rows, columns, question):
                ui.caption(
                    "Chart omitted because this result contains an unrequested product breakdown; "
                    "the complete result table is shown below."
                )
            else:
                _render_chart(ui, rows, columns, intent)
            ui.dataframe(rows, column_order=columns, use_container_width=True, hide_index=True)
        else:
            ui.caption("The query returned no rows.")
        row_count = result.get("retrieved_record_count", len(rows))
        displayed_count = len(rows)
        if result.get("requested_result_mode") == "bounded":
            status = (
                f"{row_count} retrieved · {displayed_count} displayed for the requested subset; "
                "this is not the full matching set"
            )
        else:
            status = f"{row_count} retrieved · {displayed_count} displayed"
        if result.get("truncated"):
            status += " (result capped; narrow the query for more detail)"
        ui.caption(status)
        date_scope = result.get("date_scope")
        if isinstance(date_scope, dict):
            column = date_scope.get("column", "date")
            start = date_scope.get("start")
            end_exclusive = date_scope.get("end_exclusive")
            if start and end_exclusive:
                ui.caption(f"Applied date scope: {column}, {start} inclusive to {end_exclusive} exclusive")
        intent = result.get("query_intent")
        if isinstance(intent, dict):
            details = []
            if intent.get("metric_alias"):
                details.append(f"metric {intent['metric_alias']}")
            if intent.get("group_by"):
                details.append("grouped by " + ", ".join(intent["group_by"]))
            if intent.get("filters"):
                filters = [
                    f"{item.get('column')} {item.get('operator')} {', '.join(map(str, item.get('values', [])))}"
                    for item in intent["filters"]
                    if isinstance(item, dict)
                ]
                details.append("filters " + "; ".join(filters))
            if details:
                ui.caption("Query intent: " + "; ".join(details))
        sources = result.get("sources", [])
        if sources:
            ui.caption("MySQL tables: " + ", ".join(sources))
        for note in result.get("source_coverage_notes", []):
            ui.caption(note)
        outlet_coverage = result.get("outlet_source_coverage")
        if isinstance(outlet_coverage, dict):
            covered = outlet_coverage.get("outlets_with_source_records_count", 0)
            expected = outlet_coverage.get("expected_outlet_count", 0)
            ui.caption(f"Daily Z-report source coverage: {covered} of {expected} canonical outlets")
            missing = outlet_coverage.get("outlets_without_source_records", [])
            if missing:
                ui.caption(
                    "No daily Z-report rows in this period for: " + ", ".join(map(str, missing))
                    + ". Missing evidence is not zero sales."
                )


def render_scope_alignment(ui: Any, results: list[dict[str, Any]]) -> None:
    """Warn when same-scope business reports use different effective periods."""
    for warning in report_scope_alignment_warnings(results):
        ui.warning(warning)


def report_scope_alignment_warnings(results: list[dict[str, Any]]) -> list[str]:
    """Describe date or source-coverage mismatches in structured results."""
    scoped = [
        scope for result in results
        if (scope := _result_scope(result)) is not None
    ]
    warnings: list[str] = []
    for index, left in enumerate(scoped):
        for right in scoped[index + 1:]:
            shared_scope = set(left["dimensions"]) & set(right["dimensions"])
            if not shared_scope or not all(
                left["dimensions"][key] == right["dimensions"][key]
                for key in shared_scope
            ):
                continue
            if left["start"] == right["start"] and left["end_exclusive"] == right["end_exclusive"]:
                continue
            warnings.append(
                "These results use different reporting periods and are displayed separately; "
                "do not compare or combine their figures as if the periods matched. "
                f"{left['label']}: {_period_label(left['start'], left['end_exclusive'])}; "
                f"{right['label']}: {_period_label(right['start'], right['end_exclusive'])}."
            )
    for result in results:
        report = result.get("report") if result.get("result_type") == "specialist_report" else None
        if not isinstance(report, dict):
            continue
        partial_rows = []
        for row in report.get("results", []):
            if not isinstance(row, dict) or not isinstance(row.get("coverage"), dict):
                continue
            coverage = row["coverage"]
            requested = coverage.get("requested_roster_months", [])
            observed = coverage.get("roster_months", [])
            missing = coverage.get("missing_roster_months", [])
            if requested and missing:
                outlet = row.get("outlet_code", "outlet")
                partial_rows.append(
                    f"{outlet}: {len(observed)} of {len(requested)} roster months"
                    + (f" ({', '.join(observed)})" if observed else " (none)")
                )
        if partial_rows:
            label = str(result.get("tool_name") or "Specialist report")
            warnings.append(
                f"{label} has incomplete roster-month coverage. Scheduled totals and aligned sales "
                "include only months with roster records. " + "; ".join(partial_rows) + "."
            )
    return warnings


def _result_scope(result: dict[str, Any]) -> dict[str, Any] | None:
    result_type = result.get("result_type")
    if result_type == "business_data":
        date_scope = result.get("date_scope")
        intent = result.get("query_intent")
        if not isinstance(date_scope, dict) or not isinstance(intent, dict):
            return None
        start = date_scope.get("start")
        end_exclusive = date_scope.get("end_exclusive")
        dimensions: dict[str, str] = {}
        for item in intent.get("filters", []):
            if not isinstance(item, dict) or str(item.get("operator", "")).upper() != "=":
                continue
            column = _column_leaf(item.get("column", ""))
            values = item.get("values", [])
            if column in {"outlet_type", "outlet_code", "supplier_name", "supplier_code"} and len(values) == 1:
                dimensions[column] = str(values[0]).strip().casefold()
        label = "schema-guided query"
    elif result_type == "specialist_report":
        report = result.get("report")
        if not isinstance(report, dict):
            return None
        period = report.get("period")
        scope = report.get("scope")
        if not isinstance(period, dict) and isinstance(scope, dict):
            period = scope.get("period")
        if not isinstance(period, dict):
            return None
        start = period.get("start")
        inclusive_end = period.get("end")
        try:
            end_exclusive = (date.fromisoformat(str(inclusive_end)) + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            return None
        dimensions = {}
        for source in (scope, report):
            if not isinstance(source, dict):
                continue
            for key in ("outlet_type", "outlet_code", "supplier_name", "supplier_code"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    dimensions[key] = value.strip().casefold()
        label = str(result.get("tool_name") or "specialist report")
    else:
        return None

    try:
        start = date.fromisoformat(str(start)).isoformat()
        end_exclusive = date.fromisoformat(str(end_exclusive)).isoformat()
    except (TypeError, ValueError):
        return None
    if not dimensions:
        return None
    return {
        "start": start,
        "end_exclusive": end_exclusive,
        "dimensions": dimensions,
        "label": label,
    }


def _period_label(start: str, end_exclusive: str) -> str:
    inclusive_end = (date.fromisoformat(end_exclusive) - timedelta(days=1)).isoformat()
    return f"{start} through {inclusive_end}"


def _title(value: str) -> str:
    return value.replace("_", " ").replace(".", " · ").title()


SPECIALIST_REPORT_TITLES = {
    "get_outlet_sales_sql": "Outlet sales",
    "compare_outlets_sql": "Outlet comparison",
    "analyze_outlets_sql": "Outlet analysis",
    "analyze_staffing_sql": "Staffing analysis",
    "analyze_pos_sql": "POS analysis",
    "analyze_suppliers_sql": "Supplier analysis",
    "get_supplier_addresses_sql": "Supplier address records",
    "get_data_coverage_sql": "Data coverage",
    "assess_kopi_price_change_sql": "Kopi price sensitivity",
    "get_invoice_sql": "Invoice details",
    "get_supplier_cost_trend_sql": "Supplier cost trend",
    "search_documents_sql": "Document search results",
    "get_data_quality_issues_sql": "Data quality issues",
}


def _report_tables(value: Any, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"citation", "citations", "sources", "source_uris"}:
                continue
            tables.extend(_report_tables(item, (*path, str(key))))
    elif isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        columns = list(dict.fromkeys(column for row in value for column in row))
        tables.append({"title": _title(".".join(path) or "Results"), "columns": columns, "rows": value})
    return tables


def _display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        }
        for row in rows
    ]


_TIME_COLUMNS = {"date", "business_date", "month", "period", "week", "year"}
_LABEL_PRIORITY = (
    "outlet_name", "outlet_code", "supplier_name", "supplier", "item_description",
    "item_name", "product_name", "month", "period", "business_date", "date",
)


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return float(number) if number.is_finite() else None


_ENTITY_SCOPE_COLUMN_PARTS = (
    "outlet", "branch", "item", "menu", "product", "supplier", "category",
)


def _column_leaf(column: Any) -> str:
    return str(column).rsplit(".", 1)[-1].strip(" `").casefold()


def _chart_rows_match_declared_scope(
    rows: list[dict[str, Any]], columns: list[str], intent: Any,
) -> bool:
    """Suppress charts when returned entity values contradict declared query filters."""
    if not isinstance(intent, dict):
        return True
    filters = intent.get("filters", [])
    if not isinstance(filters, list):
        return False

    output_columns = {_column_leaf(column): column for column in columns}
    for item in filters:
        if not isinstance(item, dict):
            continue
        filter_column = _column_leaf(item.get("column", ""))
        if not any(part in filter_column for part in _ENTITY_SCOPE_COLUMN_PARTS):
            continue

        output_column = output_columns.get(filter_column)
        if output_column is None:
            # The validated SQL still applies this filter; the result does not expose
            # the field needed for a row-level cross-check.
            continue

        operator = str(item.get("operator", "")).upper()
        values = item.get("values", [])
        if operator not in {"=", "IN"} or not isinstance(values, list) or not values:
            return False
        allowed = {str(value).strip().casefold() for value in values}
        if any(
            row.get(output_column) is None
            or str(row.get(output_column)).strip().casefold() not in allowed
            for row in rows
        ):
            return False
    return True


_PRODUCT_COLUMN_PARTS = ("item", "menu", "product", "food", "category")


def _business_chart_scope_is_clear(
    rows: list[dict[str, Any]], columns: list[str], question: str | None,
) -> bool:
    """Avoid plotting product categories unless the user asked for that breakdown."""
    product_columns = [
        column for column in columns
        if any(part in _column_leaf(column) for part in _PRODUCT_COLUMN_PARTS)
    ]
    for column in product_columns:
        values = list(dict.fromkeys(
            str(row[column]).strip() for row in rows if row.get(column) is not None
        ))
        if len(values) < 2:
            continue
        if _requests_product_mix(question):
            continue
        if all(_value_is_mentioned(value, question) for value in values):
            continue
        return False
    return True


def _value_is_mentioned(value: str, question: str | None) -> bool:
    if not question:
        return False
    tokens = [
        token for token in "".join(
            char.casefold() if char.isalnum() else " " for char in value
        ).split()
        if len(token) > 2
    ]
    question_tokens = set("".join(
        char.casefold() if char.isalnum() else " " for char in question
    ).split())
    return bool(tokens) and set(tokens).issubset(question_tokens)


def _chart_metric(columns: list[str], preferred: str | None = None) -> str | None:
    if preferred in columns:
        return preferred
    for column in columns:
        name = column.lower()
        if any(token in name for token in ("sales", "takings", "revenue", "cost", "wastage", "hours", "change", "margin", "count", "quantity", "volume", "price")):
            return column
    return None


def _chart_label(columns: list[str], rows: list[dict[str, Any]], group_by: list[str] | None = None) -> str | None:
    candidates = [str(item) for item in (group_by or []) if item in columns]
    candidates.extend(column for column in _LABEL_PRIORITY if column in columns and column not in candidates)
    candidates.extend(column for column in columns if column not in candidates)
    for column in candidates:
        values = [row.get(column) for row in rows]
        if len(rows) > 1 and all(value is not None and _numeric_value(value) is None for value in values):
            return column
    return None


def _render_chart(
    ui: Any,
    rows: list[dict[str, Any]],
    columns: list[str],
    intent: Any = None,
    *,
    preferred_metric: str | None = None,
    preferred_label: str | None = None,
    title: str = "Visual comparison",
) -> None:
    """Draw a chart only when the result has an unambiguous label and business metric."""
    if len(rows) < 2:
        return
    intent = intent if isinstance(intent, dict) else {}
    metric = _chart_metric(columns, preferred_metric or intent.get("metric_alias"))
    group_by = intent.get("group_by", [])
    label = preferred_label if preferred_label in columns else _chart_label(
        columns, rows, group_by if isinstance(group_by, list) else []
    )
    if not metric or not label or label == metric:
        return
    chart_rows = []
    series = next((
        str(column) for column in group_by
        if column in columns and column not in {label, metric}
        and any(row.get(column) is not None and _numeric_value(row.get(column)) is None for row in rows)
    ), None) if isinstance(group_by, list) else None
    for row in rows:
        value = _numeric_value(row.get(metric))
        category = row.get(label)
        if value is None or category is None:
            continue
        point = {label: str(category), metric: value}
        if series and row.get(series) is not None:
            point[series] = str(row[series])
        chart_rows.append(point)
    if len(chart_rows) < 2:
        return
    ui.markdown(f"**{title} · {metric.replace('_', ' ')} by {label.replace('_', ' ')}**")
    is_time_series = label.lower() in _TIME_COLUMNS
    chart_method = ui.line_chart if is_time_series else ui.bar_chart
    chart_options = {"x": label, "y": metric, "use_container_width": True}
    if is_time_series and series:
        chart_options["color"] = series
    chart_method(chart_rows, **chart_options)
    ui.caption(f"Chart shows {len(chart_rows)} returned rows; the table below retains the exact values.")


_SPECIALIST_CHART_METRICS = {
    "performance": ("gross_takings_sgd", "total_sales_sgd", "sales_sgd"),
    "sales": ("gross_takings_sgd", "total_sales_sgd", "sales_sgd"),
    "wastage": ("total_wastage_kg", "wastage_kg", "wastage_value_sgd"),
    "staffing": ("gross_takings_per_paid_hour_sgd", "labour_cost_sgd", "paid_hours"),
    "price_change": ("percentage_change", "percent_change", "absolute_change_sgd"),
    "kopi_price_change": ("incremental_gross_sales_sgd", "projected_gross_sales_sgd"),
    "spend": ("total_sgd", "total_amount_sgd", "subtotal_sgd"),
    "supplier_spend": ("total_sgd", "total_amount_sgd", "subtotal_sgd"),
    "product_mix": ("sales_sgd", "quantity_sold", "units_sold"),
}


def _specialist_chart_metric(analysis: Any, columns: list[str]) -> str | None:
    preferences = _SPECIALIST_CHART_METRICS.get(str(analysis), ())
    for candidate in preferences:
        if candidate in columns:
            return candidate
    return None


def _format_citation(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [citation for item in value for citation in _format_citation(item)]
    if isinstance(value, dict):
        uri = value.get("source_uri")
        locator = value.get("source_locator")
        if isinstance(uri, str) and isinstance(locator, str):
            return [f"{uri}#{locator}"]
        if isinstance(uri, str):
            return [uri]
        if isinstance(locator, str):
            return [locator]
        for key in ("citation", "citations", "source_uris", "records"):
            if key in value:
                return _format_citation(value[key])
    return []


def _report_citations(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"citation", "citations"}:
                found.extend(_format_citation(item))
            else:
                found.extend(_report_citations(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_report_citations(item))
    return list(dict.fromkeys(found))


def render_specialist_reports(
    ui: Any, results: list[dict[str, Any]], question: str | None = None,
) -> None:
    """Render specialist data without repeating the LLM's narrative summary."""
    rendered_reports: set[str] = set()
    for artifact in results:
        report = artifact.get("report", {})
        tool_name = artifact.get("tool_name", "")
        report_key = json.dumps(
            {"tool_name": tool_name, "report": report},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if report_key in rendered_reports:
            continue
        rendered_reports.add(report_key)

        title = SPECIALIST_REPORT_TITLES.get(tool_name, _title(tool_name or "Business report"))
        ui.markdown(f"### {title}")
        if not isinstance(report, dict):
            ui.caption("The report data could not be displayed as structured rows.")
            continue

        period = report.get("period")
        scope = report.get("scope")
        if not isinstance(period, dict) and isinstance(scope, dict):
            period = scope.get("period")
        if isinstance(period, dict) and period.get("start") and period.get("end"):
            ui.caption(f"Requested period: {period['start']} through {period['end']}")
        classification = report.get("classification")
        if classification:
            ui.caption(f"Tool classification: {classification}")
        causes = _report_values(report, "cause")
        for cause in causes:
            ui.caption(f"Tool-recorded cause: {cause}")

        tables = _report_tables(report)
        analysis = report.get("analysis")
        for table in tables:
            rows = table["rows"]
            ui.markdown(f"**{table['title']}**")
            metric = _specialist_chart_metric(analysis, table["columns"])
            chart_label = None
            chart_rows = rows
            chart_columns = table["columns"]
            if analysis == "price_change" and "supplier_name" in chart_columns:
                item_column = next(
                    (column for column in ("description", "item_description") if column in chart_columns),
                    None,
                )
                if item_column:
                    chart_label = "supplier_item"
                    chart_columns = [*chart_columns, chart_label]
                    chart_rows = [
                        {
                            **row,
                            chart_label: " — ".join(
                                str(row.get(column, "")).strip()
                                for column in ("supplier_name", item_column)
                                if row.get(column) is not None and str(row[column]).strip()
                            ),
                        }
                        for row in rows
                    ]
            if "demand_change_percent" in table["columns"]:
                metric = metric or _chart_metric(table["columns"], "incremental_gross_sales_sgd")
                chart_label = "demand_change_percent"
            if analysis == "product_mix" and not _requests_product_mix(question):
                if metric:
                    ui.caption(
                        "Chart omitted because a product-mix breakdown was not requested; "
                        "the complete report table is shown below."
                    )
                metric = None
            if metric:
                _render_chart(
                    ui, chart_rows, chart_columns, preferred_metric=metric,
                    preferred_label=chart_label, title=title,
                )
            ui.dataframe(
                _display_rows(rows),
                column_order=table["columns"],
                use_container_width=True,
                hide_index=True,
            )
            ui.caption(f"{len(rows)} retrieved · {len(rows)} displayed")

        citations = _report_citations(report)
        if citations:
            visible = citations[:20]
            suffix = f"; {len(citations) - 20} more citations are included in the rows" if len(citations) > 20 else ""
            ui.caption("Sources: " + "; ".join(visible) + suffix)


def _report_values(value: Any, field_name: str) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() == field_name.casefold() and child not in (None, ""):
                    rendered = str(child)
                    if rendered not in found:
                        found.append(rendered)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found[:5]


def _requests_product_mix(question: str | None) -> bool:
    if not question:
        return False
    normalized = " ".join(
        "".join(char.casefold() if char.isalnum() else " " for char in question).split()
    )
    requested_breakdowns = (
        "product mix", "sales mix", "item mix", "menu item mix",
        "by product", "by item", "by menu item",
        "product breakdown", "item breakdown", "mix of products", "mix of items",
        "all items", "each item", "every item", "items sold",
        "all products", "each product", "every product", "products sold",
    )
    return any(phrase in normalized for phrase in requested_breakdowns)
