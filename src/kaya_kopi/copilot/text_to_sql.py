"""Schema-guided, read-only SQL access for questions beyond fixed reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, Literal

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from sqlglot import exp, parse, tokenize
from sqlglot.errors import ParseError
from sqlglot.tokens import TokenType

from kaya_kopi.repositories.mysql import MySQLRepository
from kaya_kopi.copilot.timing import log_timing


# The catalog is intentionally limited to business fields. Raw OCR, contact details,
# staff identities and individual pay are excluded from both model context and SQL access.
BUSINESS_SCHEMA: dict[str, tuple[str, ...]] = {
    "source_document": ("source_document_id", "source_uri", "document_type", "extraction_status"),
    "outlets": ("outlet_id", "outlet_code", "outlet_name", "outlet_type", "opened_date"),
    "outlet_alias": (
        "outlet_alias_id", "outlet_id", "alias_code", "effective_from", "effective_to",
        "validation_status",
    ),
    "daily_outlet_summary": (
        "daily_summary_id", "source_document_id", "source_row_number", "source_locator",
        "outlet_id", "business_date", "sales_amount_sgd", "reported_volume",
        "volume_measure", "wastage_kg", "validation_status",
    ),
    "pos_transactions": (
        "tx_id", "outlet_code", "tx_time", "item_id", "qty", "unit_price", "total", "payment_mode",
    ),
    "menu_items": ("item_id", "item_name", "base_price", "category"),
    "supplier": ("supplier_id", "supplier_code", "canonical_name", "validation_status"),
    "supplier_invoice": (
        "supplier_invoice_id", "source_document_id", "supplier_name", "invoice_number", "invoice_date",
        "supplier_purchase_id", "currency", "is_gst_inclusive", "subtotal", "gst_rate_percent",
        "gst_amount", "total_amount", "arithmetic_valid", "extraction_status",
    ),
    "supplier_invoice_item": (
        "supplier_invoice_item_id", "supplier_invoice_id", "line_number",
        "description", "quantity", "unit_price", "amount", "line_total_valid",
        "extraction_status",
    ),
    "purchase_invoice_reconciliation": (
        "reconciliation_id", "supplier_purchase_id", "supplier_invoice_id",
        "reconciliation_status", "match_method", "legacy_subtotal_sgd", "scanned_subtotal_sgd",
        "subtotal_difference_sgd", "validation_status",
    ),
    "staff_roster": ("staff_roster_id", "roster_month", "validation_status"),
    "staff_assignment": ("staff_assignment_id", "staff_roster_id", "outlet_id", "role"),
    "staff_shift": ("staff_shift_id", "staff_assignment_id", "day_of_week", "paid_hours"),
    "data_quality_issue": (
        "issue_id", "source_document_id", "source_locator", "record_type", "record_key", "rule_code",
        "severity", "details", "resolution_status", "detected_at",
    ),
    "document_chunk": (
        "document_chunk_id", "source_document_id", "source_type", "occurred_at",
        "title", "source_locator",
    ),
}

KNOWN_ENUM_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("daily_outlet_summary", "validation_status"): frozenset({"OK", "REVIEW_REQUIRED"}),
}

SCHEMA_GUIDE = "\n".join(
    f"{table}({', '.join(columns)})" for table, columns in BUSINESS_SCHEMA.items()
)
SCHEMA_GUIDE += """
Business grain and join notes:
- outlets: one row per canonical outlet; join daily_outlet_summary.outlet_id to outlets.outlet_id.
- source_document: source URI and document type for citations; do not select document contents or OCR locations.
- outlet_alias: join verified historical aliases by alias_code, effective dates, and validation_status='VERIFIED'.
- daily_outlet_summary: one outlet/day report; sales_amount_sgd is gross takings. Interpret reported_volume using volume_measure and validation_status.
- daily_outlet_summary.validation_status is one of 'OK' (accepted row) or 'REVIEW_REQUIRED' (row needs review); do not invent other status values.
- pos_transactions: one item row from a POS export; total is item-level sales, and COUNT(DISTINCT tx_id) counts receipts. Join outlet_code to outlets.outlet_code and item_id to menu_items.item_id.
- supplier_invoice: one scanned invoice header; supplier_invoice_item: one scanned invoice line, joined by supplier_invoice_id.
- staff_roster: one roster month; staff_assignment joins by staff_roster_id and staff_shift by staff_assignment_id.
- Supplier spend must aggregate headers before joining invoice lines to avoid counting invoice totals more than once.
- POS exports may be partial; daily summaries can have missing dates. Do not claim complete coverage without checking dates/source coverage.
- daily summaries expose source_document_id, source_row_number, and source_locator for traceability. Scanned invoices expose source_document_id and line_number.
- For scanned invoice costs, keep one header per invoice when aggregating spend. Use SGD only. If arithmetic_valid is true, use the header subtotal. Otherwise derive subtotal from invoice lines only when every line is valid and extracted successfully; account for is_gst_inclusive and gst_rate_percent. Flag or exclude foreign-currency, invalid, or incomplete invoices. Compare price changes only for the same supplier and exact item description, with dates and invoice evidence.
- Staff rosters describe planned shifts, not attendance. Do not expose staff names or individual pay.
- Invoice extraction_status, line_total_valid, and arithmetic_valid identify evidence needing review.
"""

SOURCE_COVERAGE_NOTES = {
    "daily_outlet_summary": "Daily outlet reports can have missing dates.",
    "pos_transactions": "POS transaction exports are partial and do not establish complete revenue or volume.",
    "supplier_invoice": "Scanned invoice headers may be incomplete or require arithmetic/extraction review.",
    "supplier_invoice_item": "Scanned invoice lines may be incomplete or require line validation.",
    "staff_roster": "Roster records describe planned staffing, not attendance.",
    "staff_assignment": "Roster assignments describe planned staffing, not attendance.",
    "staff_shift": "Roster shifts describe planned hours, not attendance.",
}
UNSAFE_FUNCTIONS = {
    "BENCHMARK", "GET_LOCK", "IS_FREE_LOCK", "IS_USED_LOCK", "LOAD_FILE",
    "MASTER_POS_WAIT", "RELEASE_LOCK", "SLEEP", "WAIT_FOR_EXECUTED_GTID_SET",
}


class UnsafeSQLQuery(ValueError):
    """Raised when generated SQL falls outside the approved read-only contract."""


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    parameters: tuple[Any, ...]
    truncated_by_limit: bool
    source_tables: tuple[str, ...]
    date_scope: dict[str, str] | None


class DateScope(BaseModel):
    """Date range the model says the user requested; end date is exclusive."""

    column: str = Field(description="Approved business date column used by the query.")
    start: date = Field(description="Inclusive start date for the requested period.")
    end_exclusive: date = Field(description="Exclusive end date for the requested period.")


class QueryFilter(BaseModel):
    column: str = Field(description="Business column filtered by the query.")
    operator: Literal["=", "IN", ">", ">=", "<", "<=", "<>"] = Field(
        description="Comparison or membership filter operator."
    )
    values: list[str | int | float | Decimal | date | datetime] = Field(
        min_length=1, description="Exact values bound to this filter in SQL."
    )


class QueryIntent(BaseModel):
    """Model-declared request scope, checked against the SQL before execution."""

    metric_alias: str | None = Field(
        description="Selected output alias representing the requested measure, or null if there is no measure."
    )
    group_by: list[str] = Field(description="Requested grouping output columns or aliases; empty if not applicable.")
    filters: list[QueryFilter] = Field(description="Requested filters and bound values; empty if not applicable.")
    date_scope: DateScope | None = Field(description="Date bounds, or null for an undated request.")


class ReadOnlySQLValidator:
    def __init__(self, schema: dict[str, tuple[str, ...]], max_rows: int = 200) -> None:
        self.schema = {name.lower(): {column.lower() for column in columns} for name, columns in schema.items()}
        self.max_rows = max_rows

    def validate(
        self,
        sql: str,
        parameters: list[Any] | tuple[Any, ...],
        result_mode: Literal["complete", "bounded"] = "complete",
        date_scope: DateScope | dict[str, Any] | None = None,
        query_intent: QueryIntent | dict[str, Any] | None = None,
    ) -> ValidatedQuery:
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 12_000:
            raise UnsafeSQLQuery("Provide one non-empty SQL query under 12,000 characters.")
        try:
            statements = parse(sql, read="mysql")
        except ParseError as error:
            raise UnsafeSQLQuery("The SQL query could not be parsed as MySQL.") from error
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise UnsafeSQLQuery("Only one read-only SELECT statement is allowed.")
        tree = statements[0]
        if any(next(tree.find_all(node_type), None) is not None for node_type in (exp.Into, exp.Lock)):
            raise UnsafeSQLQuery("SELECT INTO and locking reads are not allowed.")
        with_clause = tree.args.get("with_")
        if with_clause is not None and with_clause.args.get("recursive"):
            raise UnsafeSQLQuery("Recursive CTEs are not allowed.")
        for function in tree.find_all(exp.Func):
            name = function.name.upper()
            if name in UNSAFE_FUNCTIONS:
                raise UnsafeSQLQuery(f"The SQL function '{name}' is not allowed.")

        def output_columns(select: exp.Select) -> set[str]:
            columns = set()
            for projection in select.expressions:
                if isinstance(projection, exp.Alias):
                    columns.add(projection.alias.lower())
                elif isinstance(projection, exp.Column) and not projection.is_star:
                    columns.add(projection.name.lower())
            return columns

        cte_columns = {
            cte.alias.lower(): output_columns(cte.this)
            for cte in tree.find_all(exp.CTE)
            if isinstance(cte.this, exp.Select)
        }
        aliases: dict[str, tuple[str | None, set[str]]] = {}
        referenced: set[str] = set()
        for table in tree.find_all(exp.Table):
            if table.db or table.catalog:
                raise UnsafeSQLQuery("Database-qualified table names are not allowed.")
            name = table.name.lower()
            if name in self.schema:
                referenced.add(name)
                aliases[table.alias_or_name.lower()] = (name, self.schema[name])
            elif name in cte_columns:
                aliases[table.alias_or_name.lower()] = (None, cte_columns[name])
            else:
                raise UnsafeSQLQuery(f"Table '{name}' is not in the business schema catalog.")
        for subquery in tree.find_all(exp.Subquery):
            alias = subquery.args.get("alias")
            if alias and isinstance(subquery.this, exp.Select):
                aliases[alias.name.lower()] = (None, output_columns(subquery.this))
        if not referenced:
            raise UnsafeSQLQuery("The query must read from an approved business table.")

        selected_aliases = {
            item.alias.lower() for item in tree.expressions if isinstance(item, exp.Alias)
        }
        selected_columns = output_columns(tree)
        for column in tree.find_all(exp.Column):
            name = column.name.lower()
            if column.is_star and isinstance(column.parent, exp.Count):
                continue
            if column.is_star:
                raise UnsafeSQLQuery("Select explicit business columns; SELECT * is not allowed.")
            if name in selected_aliases and not column.table:
                continue
            qualifier = column.table.lower()
            if qualifier:
                available = aliases.get(qualifier)
                if available is None or name not in available[1]:
                    raise UnsafeSQLQuery(f"Column '{column.sql()}' is not in the business schema catalog.")
            elif not any(name in self.schema[table_name] for table_name in referenced) and not any(
                name in columns for table_name, columns in aliases.values() if table_name is None
            ):
                raise UnsafeSQLQuery(f"Column '{name}' is not in the business schema catalog.")

        for literal in tree.find_all(exp.Literal):
            if literal.is_string:
                if _is_filter_text_literal(literal):
                    raise UnsafeSQLQuery(
                        "Bind text values used in WHERE, HAVING, JOIN, or CASE conditions as parameters."
                    )
                if not _is_column_derived_text_literal(literal):
                    raise UnsafeSQLQuery(
                        "String literals must be part of a column-based expression; do not select invented text values."
                    )
            # Numeric constants remain useful in aggregates and calculations; filter values
            # that identify business records should be supplied as bound parameters.
        placeholders = list(tree.find_all(exp.Placeholder))
        if not isinstance(parameters, (list, tuple)) or len(parameters) > 100:
            raise UnsafeSQLQuery("Provide at most 100 scalar query parameters.")
        if len(placeholders) != len(parameters):
            raise UnsafeSQLQuery("The number of bound values must match the SQL placeholders.")
        if any(not isinstance(value, (str, int, float, Decimal, date, datetime, type(None))) for value in parameters):
            raise UnsafeSQLQuery("Query parameters must be scalar values.")

        intent: QueryIntent | None = None
        if query_intent is not None:
            try:
                intent = QueryIntent.model_validate(query_intent)
            except Exception as error:
                raise UnsafeSQLQuery("Provide a valid query intent for metric, grouping, filters, and dates.") from error
            if intent.metric_alias and intent.metric_alias.lower() not in selected_columns:
                raise UnsafeSQLQuery(
                    f"The declared metric '{intent.metric_alias}' is not a selected output column or alias."
                )
            if intent.group_by:
                actual_grouping = _grouping_output_names(tree)
                missing_grouping = {name.lower() for name in intent.group_by} - actual_grouping
                if missing_grouping:
                    raise UnsafeSQLQuery(
                        "The declared grouping is not present in GROUP BY: "
                        + ", ".join(sorted(missing_grouping))
                    )
            for requested_filter in intent.filters:
                self._validate_known_enum_filter(requested_filter, aliases, referenced)
                _validate_query_filter(tree, sql, parameters, requested_filter)
            unsupported_filter = next(
                (condition for condition in _where_conjuncts(tree) if not _is_bound_filter(condition)),
                None,
            )
            if unsupported_filter is not None:
                raise UnsafeSQLQuery(
                    "Declare filters as bound column comparisons or IN lists so their scope can be checked."
                )
            declared_filters = {
                (item.column.rsplit(".", 1)[-1].lower(), item.operator)
                for item in intent.filters
            }
            declared_date_column = (
                intent.date_scope.column.rsplit(".", 1)[-1].lower()
                if intent.date_scope is not None else None
            )
            undeclared_filters = {
                (column, operator)
                for column, operator in _bound_where_filters(tree)
                if column != declared_date_column and (column, operator) not in declared_filters
            }
            if undeclared_filters:
                details = ", ".join(f"{column} {operator}" for column, operator in sorted(undeclared_filters))
                raise UnsafeSQLQuery(f"Declare each SQL filter in query_intent.filters; undeclared: {details}.")
            if date_scope is not None and intent.date_scope is not None:
                if DateScope.model_validate(date_scope) != intent.date_scope:
                    raise UnsafeSQLQuery("date_scope must match query_intent.date_scope.")
            date_scope = intent.date_scope if intent.date_scope is not None else date_scope

        normalized_scope: dict[str, str] | None = None
        if date_scope is not None:
            try:
                scope = DateScope.model_validate(date_scope)
            except Exception as error:
                raise UnsafeSQLQuery("Provide a valid inclusive-start, exclusive-end date scope.") from error
            scope_column = scope.column.rsplit(".", 1)[-1].lower()
            available_date_columns = {
                column
                for table_name in referenced
                for column in self.schema[table_name]
                if column in {
                    "business_date", "tx_time", "invoice_date", "roster_month",
                    "occurred_at", "detected_at", "effective_from", "effective_to",
                    "opened_date",
                }
            }
            if scope_column not in available_date_columns:
                raise UnsafeSQLQuery("The date scope column must be an approved date column in a referenced table.")
            if scope.start >= scope.end_exclusive:
                raise UnsafeSQLQuery("The date scope start must be earlier than its exclusive end.")
            start_value = scope.start.isoformat()
            end_value = scope.end_exclusive.isoformat()
            start_indexes = _date_scope_parameter_indexes(sql, scope_column, TokenType.GTE)
            end_indexes = _date_scope_parameter_indexes(sql, scope_column, TokenType.LT)
            if not _has_conjunctive_date_comparison(tree, scope_column, exp.GTE) or not start_indexes:
                raise UnsafeSQLQuery(
                    f"The SQL must constrain {scope.column} with >= ? as a top-level date filter."
                )
            if not _has_conjunctive_date_comparison(tree, scope_column, exp.LT) or not end_indexes:
                raise UnsafeSQLQuery(
                    f"The SQL must constrain {scope.column} with < ? as a top-level date filter."
                )
            if not any(_date_parameter(parameters[index]) == start_value for index in start_indexes) or not any(
                _date_parameter(parameters[index]) == end_value for index in end_indexes
            ):
                raise UnsafeSQLQuery("Bind the declared date start and exclusive end to their matching SQL filters.")
            normalized_scope = {
                "column": scope.column,
                "start": start_value,
                "end_exclusive": end_value,
            }

        limits = list(tree.find_all(exp.Limit))
        if result_mode == "complete" and limits:
            raise UnsafeSQLQuery(
                "A complete result cannot use a SQL LIMIT. Use result_mode='bounded' only when the user asked for a top-N subset."
            )
        if result_mode == "bounded" and not limits:
            raise UnsafeSQLQuery("A bounded result must include a SQL LIMIT.")
        for limit in limits:
            limit_value = limit.expression
            if not isinstance(limit_value, exp.Literal) or not limit_value.is_int:
                raise UnsafeSQLQuery("LIMIT must be a fixed integer.")
            if int(limit_value.this) > self.max_rows:
                raise UnsafeSQLQuery(f"LIMIT cannot exceed {self.max_rows} rows.")
        limit = tree.args.get("limit")
        truncated = limit is None
        if result_mode == "complete":
            tree = tree.limit(self.max_rows + 1)
        elif limit is None:
            tree = tree.limit(self.max_rows + 1)
            truncated = True

        normalized = _prepare_pymysql_sql(tree.sql(dialect="mysql"))
        return ValidatedQuery(
            normalized, tuple(parameters), truncated, tuple(sorted(referenced)), normalized_scope
        )

    @staticmethod
    def _validate_known_enum_filter(
        requested_filter: QueryFilter,
        aliases: dict[str, tuple[str | None, set[str]]],
        referenced: set[str],
    ) -> None:
        if requested_filter.operator not in {"=", "IN"}:
            return
        parts = requested_filter.column.lower().rsplit(".", 1)
        column = parts[-1]
        candidate_tables: set[str] = set()
        if len(parts) == 2:
            qualifier = parts[0]
            if qualifier in aliases and aliases[qualifier][0] is not None:
                candidate_tables.add(aliases[qualifier][0])
            elif qualifier in BUSINESS_SCHEMA:
                candidate_tables.add(qualifier)
        else:
            candidate_tables = {
                table for table in referenced if (table, column) in KNOWN_ENUM_VALUES
            }
        for table in candidate_tables:
            allowed = KNOWN_ENUM_VALUES.get((table, column))
            if not allowed:
                continue
            invalid = [
                str(value) for value in requested_filter.values
                if str(value).upper() not in allowed
            ]
            if invalid:
                allowed_values = ", ".join(sorted(allowed))
                raise UnsafeSQLQuery(
                    f"Unsupported value for {table}.{column}. "
                    f"Use a documented value: {allowed_values}."
                )


def _date_parameter(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _date_scope_parameter_indexes(sql: str, column_name: str, operator: TokenType) -> list[int]:
    """Find positional parameter indexes for direct column/operator/placeholder filters."""
    tokens = tokenize(sql, read="mysql")
    indexes: list[int] = []
    placeholders_seen = 0
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.PLACEHOLDER:
            placeholders_seen += 1
            continue
        if token.token_type != operator or index == 0 or index + 1 >= len(tokens):
            continue
        previous, following = tokens[index - 1], tokens[index + 1]
        if (
            previous.token_type in {TokenType.VAR, TokenType.IDENTIFIER}
            and previous.text.strip("`").lower() == column_name
            and following.token_type == TokenType.PLACEHOLDER
        ):
            indexes.append(placeholders_seen)
    return indexes


def _has_conjunctive_date_comparison(
    tree: exp.Select, column_name: str, operator: type[exp.Expression]
) -> bool:
    for condition in _where_conjuncts(tree):
        if (
            isinstance(condition, operator)
            and isinstance(condition.this, exp.Column)
            and condition.this.name.lower() == column_name
            and isinstance(condition.expression, exp.Placeholder)
        ):
            return True
    return False


def _where_conjuncts(tree: exp.Select) -> list[exp.Expression]:
    where = tree.args.get("where")
    if where is None:
        return []

    def conjuncts(expression: exp.Expression) -> list[exp.Expression]:
        while isinstance(expression, exp.Paren):
            expression = expression.this
        if isinstance(expression, exp.And):
            return conjuncts(expression.this) + conjuncts(expression.expression)
        return [expression]

    return conjuncts(where.this)


def _is_bound_filter(condition: exp.Expression) -> bool:
    comparison_types = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)
    if isinstance(condition, comparison_types):
        return (
            isinstance(condition.this, exp.Column)
            and isinstance(condition.expression, exp.Placeholder)
        )
    return (
        isinstance(condition, exp.In)
        and isinstance(condition.this, exp.Column)
        and bool(condition.expressions)
        and all(isinstance(value, exp.Placeholder) for value in condition.expressions)
    )


def _grouping_output_names(tree: exp.Select) -> set[str]:
    grouping = tree.args.get("group")
    if grouping is None:
        return set()
    names: set[str] = set()
    for expression in grouping.expressions:
        if isinstance(expression, exp.Column):
            names.add(expression.name.lower())
        for projection in tree.expressions:
            if isinstance(projection, exp.Alias) and projection.this.sql().lower() == expression.sql().lower():
                names.add(projection.alias.lower())
    return names


def _is_filter_text_literal(literal: exp.Literal) -> bool:
    """Return whether a text literal participates in a row or aggregate filter."""
    node = literal.parent
    while node is not None:
        if isinstance(node, (exp.Where, exp.Having, exp.Join)):
            return True
        if isinstance(node, exp.If):
            condition = node.args.get("this")
            if condition is not None and any(child is literal for child in condition.walk()):
                return True
        node = node.parent
    return False


def _is_column_derived_text_literal(literal: exp.Literal) -> bool:
    """Allow display constants only when the same expression is grounded in a column."""
    node = literal.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.Func) and any(isinstance(child, exp.Column) for child in node.walk()):
            return True
        if isinstance(node, exp.If) and isinstance(node.parent, exp.Case):
            if any(isinstance(child, exp.Column) for child in node.parent.walk()):
                return True
        node = node.parent
    return False


def _filter_parameter_indexes(sql: str, column_name: str, operator: str) -> list[int]:
    tokens = tokenize(sql, read="mysql")
    indexes: list[int] = []
    placeholders_seen = 0
    operator_tokens = {
        "=": TokenType.EQ,
        "IN": TokenType.IN,
        ">": TokenType.GT,
        ">=": TokenType.GTE,
        "<": TokenType.LT,
        "<=": TokenType.LTE,
        "<>": TokenType.NEQ,
    }
    operator_token = operator_tokens[operator]
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.PLACEHOLDER:
            placeholders_seen += 1
            continue
        if token.token_type != operator_token or index == 0 or index + 1 >= len(tokens):
            continue
        previous = tokens[index - 1]
        if previous.token_type not in {TokenType.VAR, TokenType.IDENTIFIER}:
            continue
        if previous.text.strip("`").lower() != column_name:
            continue
        if operator != "IN":
            if tokens[index + 1].token_type == TokenType.PLACEHOLDER:
                indexes.append(placeholders_seen)
            continue
        if tokens[index + 1].token_type != TokenType.L_PAREN:
            continue
        local_placeholders = 0
        for value_token in tokens[index + 2:]:
            if value_token.token_type == TokenType.R_PAREN:
                break
            if value_token.token_type == TokenType.PLACEHOLDER:
                indexes.append(placeholders_seen + local_placeholders)
                local_placeholders += 1
    return indexes


def _has_conjunctive_filter(tree: exp.Select, column_name: str, operator: str) -> bool:
    for condition in _where_conjuncts(tree):
        comparison_types = {
            "=": exp.EQ,
            ">": exp.GT,
            ">=": exp.GTE,
            "<": exp.LT,
            "<=": exp.LTE,
            "<>": exp.NEQ,
        }
        if operator in comparison_types and isinstance(condition, comparison_types[operator]):
            left, right = condition.this, condition.expression
            if (
                isinstance(left, exp.Column)
                and left.name.lower() == column_name
                and isinstance(right, exp.Placeholder)
            ):
                return True
        if operator == "IN" and isinstance(condition, exp.In):
            if (
                isinstance(condition.this, exp.Column)
                and condition.this.name.lower() == column_name
                and condition.expressions
                and all(isinstance(value, exp.Placeholder) for value in condition.expressions)
            ):
                return True
    return False


def _bound_where_filters(tree: exp.Select) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    comparisons = (
        (exp.EQ, "="), (exp.GT, ">"), (exp.GTE, ">="), (exp.LT, "<"),
        (exp.LTE, "<="), (exp.NEQ, "<>"),
    )
    for condition in _where_conjuncts(tree):
        for expression_type, operator in comparisons:
            if (
                isinstance(condition, expression_type)
                and isinstance(condition.this, exp.Column)
                and isinstance(condition.expression, exp.Placeholder)
            ):
                found.add((condition.this.name.lower(), operator))
        if (
            isinstance(condition, exp.In)
            and isinstance(condition.this, exp.Column)
            and condition.expressions
            and all(isinstance(value, exp.Placeholder) for value in condition.expressions)
        ):
            found.add((condition.this.name.lower(), "IN"))
    return found


def _filter_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    return value


def _validate_query_filter(
    tree: exp.Select,
    sql: str,
    parameters: list[Any] | tuple[Any, ...],
    requested_filter: QueryFilter,
) -> None:
    column_name = requested_filter.column.rsplit(".", 1)[-1].lower()
    if not _has_conjunctive_filter(tree, column_name, requested_filter.operator):
        raise UnsafeSQLQuery(
            f"The declared filter on '{requested_filter.column}' is not a top-level bound SQL filter."
        )
    indexes = _filter_parameter_indexes(sql, column_name, requested_filter.operator)
    if len(indexes) != len(requested_filter.values):
        raise UnsafeSQLQuery(
            f"The declared filter on '{requested_filter.column}' does not match its SQL placeholders."
        )
    actual_values = [_filter_value(parameters[index]) for index in indexes]
    expected_values = [_filter_value(value) for value in requested_filter.values]
    if actual_values != expected_values:
        raise UnsafeSQLQuery(
            f"The bound values for filter '{requested_filter.column}' do not match query_intent."
        )


class BusinessSQLInput(BaseModel):
    sql: str = Field(description="One MySQL SELECT over the approved business schema; use ? placeholders for text filters.")
    parameters: list[str | int | float | Decimal | date | datetime | None] = Field(default_factory=list, max_length=100)
    query_intent: QueryIntent = Field(
        description=(
            "Declare the requested metric alias, grouping output names, equality or IN filters with their "
            "exact values, and date_scope. Use null for metric_alias/date_scope when not applicable, and "
            "empty arrays when there are no requested groups or filters."
        )
    )
    result_mode: Literal["complete", "bounded"] = Field(
        default="complete",
        description="Use complete for all matching records; use bounded only when the user explicitly asks for top N or another subset.",
    )


_validator = ReadOnlySQLValidator(BUSINESS_SCHEMA)
BUSINESS_QUERY_DESCRIPTION = (
    "Use this as the general MySQL tool for business data and custom analytics. Focused tools are available for one outlet's sales, source coverage by business area, one scanned invoice's details, POS product mix/prices/payments/time patterns and Kopi margin evidence, and validated supplier reports; use custom SQL for cross-entity lists, comparisons, or custom groupings. A supplier analysis tool is available for validated scanned-invoice spend, reconciliation, comparable item-price changes, and average line prices by supplier and exact item description with review-excluded line evidence. Supplier unit prices are purchase costs and do not establish product costs per cup without verified recipe mapping. Craft a scoped query from the approved schema for other questions and custom groupings. Use only documented values for status or other categorical filters; do not invent enum values. When a query unexpectedly returns no rows, check whether an unsupported or unnecessarily restrictive filter caused it, then correct the query or check a relevant alternate source before concluding no data is available or asking the user to choose a retry. Unless the user specifies dates or clearly continues a prior report, use year-to-date through today. Use a longer available period for trends when it adds useful evidence. If the selected scope has no rows, query a broader period separately and label it; never silently widen the requested scope. The staffing report tool remains available only for aggregated labour-cost analysis because individual pay fields are excluded. "
    "Approved schema (table(columns)): " + SCHEMA_GUIDE + " "
    "Use one SELECT, explicit columns, and ? placeholders for text filters. Do not use SELECT *. "
    "Always include query_intent to declare the requested measure, grouping, filters, and date scope. "
    "Use metric_alias for the selected output representing the requested measure; group_by values must "
    "be selected columns or aliases; filters must list each requested equality or IN column and its exact values. "
    "Set date_scope to the exact date column and inclusive start/exclusive end, or null when no date range was requested. "
    "Apply declared bounds in SQL with >= ? and < ? and include both bounds in parameters. "
    "Set result_mode='complete' for all matching records; set it to 'bounded' with a LIMIT only when the user asks for top N or a defined subset. "
    "Results are capped at 200 rows and the complete structured result is rendered in the app."
)


def _prepare_pymysql_sql(sql: str) -> str:
    """Convert SQLGlot placeholders without rewriting quoted text.

    PyMySQL applies Python percent formatting to every execution, so literal percent
    characters must be doubled even when they are inside SQL strings or comments.
    """
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == "\\" and quote != "`" and index + 1 < len(sql):
                output.append(char)
                next_char = sql[index + 1]
                output.append("%%" if next_char == "%" else next_char)
                index += 2
                continue
            if char == quote:
                output.append(char)
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            elif char == "%":
                output.append("%%")
            else:
                output.append(char)
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        elif char == "%":
            output.append("%%")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _outlet_source_coverage(
    query: ValidatedQuery,
    result_mode: Literal["complete", "bounded"],
    query_intent: QueryIntent | dict[str, Any] | None,
    timing_id: str | None = None,
) -> dict[str, Any] | None:
    """Measure source coverage for complete, unfiltered reports grouped by outlet."""
    if result_mode != "complete" or "daily_outlet_summary" not in query.source_tables or query_intent is None:
        return None
    intent = QueryIntent.model_validate(query_intent)
    outlet_groups = {"outlet_code", "outlet_name", "outlet_id"}
    if not outlet_groups.intersection(
        column.rsplit(".", 1)[-1].lower() for column in intent.group_by
    ) or intent.filters:
        return None

    period_column = None
    coverage_parameters: list[str] = []
    if query.date_scope is not None:
        period_column = query.date_scope["column"].rsplit(".", 1)[-1].lower()
        if period_column != "business_date":
            return None
        coverage_parameters = [
            query.date_scope["start"], query.date_scope["end_exclusive"],
        ]

    period_filter = (
        " AND d.business_date >= %s AND d.business_date < %s"
        if period_column else ""
    )
    started = perf_counter()
    try:
        rows = MySQLRepository()._query(
            "SELECT o.outlet_code, COUNT(d.daily_summary_id) AS source_rows "
            "FROM outlets o LEFT JOIN daily_outlet_summary d "
            f"ON d.outlet_id = o.outlet_id{period_filter} "
            "GROUP BY o.outlet_id, o.outlet_code ORDER BY o.outlet_code",
            coverage_parameters,
        )
    except Exception as error:
        log_timing("mysql_coverage_query", (perf_counter() - started) * 1000, timing_id,
                   outcome="error", error_type=type(error).__name__)
        raise
    log_timing("mysql_coverage_query", (perf_counter() - started) * 1000, timing_id,
               row_count=len(rows), outcome="ok")
    missing = [row["outlet_code"] for row in rows if not int(row["source_rows"] or 0)]
    total = len(rows)
    return {
        "source_table": "daily_outlet_summary",
        "period": query.date_scope,
        "expected_outlet_count": total,
        "outlets_with_source_records_count": total - len(missing),
        "outlets_without_source_records": missing,
        "complete": not missing,
    }


def execute_business_query(
    sql: str,
    parameters: list[Any],
    result_mode: Literal["complete", "bounded"] = "complete",
    query_intent: QueryIntent | dict[str, Any] | None = None,
    timing_id: str | None = None,
) -> dict[str, Any]:
    """Execute validated SQL and return bounded rows plus completeness metadata."""
    started = perf_counter()
    try:
        query = _validator.validate(sql, parameters, result_mode, query_intent=query_intent)
    except Exception as error:
        details: dict[str, object] = {
            "outcome": "error",
            "error_type": type(error).__name__,
        }
        if isinstance(error, UnsafeSQLQuery):
            # Rejection messages identify the violated rule and schema field without
            # including SQL text or bound parameter values.
            details["rejection_reason"] = " ".join(str(error).split())[:300]
        log_timing("sql_validation", (perf_counter() - started) * 1000, timing_id, **details)
        raise
    log_timing("sql_validation", (perf_counter() - started) * 1000, timing_id,
               outcome="ok", table_count=len(query.source_tables))
    started = perf_counter()
    try:
        with MySQLRepository()._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION max_execution_time = 5000")
                cursor.execute(query.sql, query.parameters)
                rows = cursor.fetchmany(201)
                columns = [item[0] for item in cursor.description or ()]
    except Exception as error:
        log_timing("mysql_query", (perf_counter() - started) * 1000, timing_id,
                   outcome="error", error_type=type(error).__name__)
        raise
    log_timing("mysql_query", (perf_counter() - started) * 1000, timing_id,
               outcome="ok", row_count=len(rows))
    truncated = len(rows) > 200 or query.truncated_by_limit and len(rows) >= 201
    visible = rows[:200]
    intent = QueryIntent.model_validate(query_intent) if query_intent is not None else None
    outlet_coverage = _outlet_source_coverage(query, result_mode, intent, timing_id)
    return {
        "result_type": "business_data",
        "columns": columns,
        "rows": [{key: _json_value(value) for key, value in row.items()} for row in visible],
        "retrieved_record_count": len(visible),
        "expected_record_count": None,
        "truncated": truncated,
        "requested_result_mode": result_mode,
        "sources": list(query.source_tables),
        "source_coverage_notes": [
            SOURCE_COVERAGE_NOTES[table]
            for table in query.source_tables if table in SOURCE_COVERAGE_NOTES
        ],
        "outlet_source_coverage": outlet_coverage,
        "date_scope": query.date_scope,
        "query_intent": intent.model_dump(mode="json") if intent is not None else None,
    }


@tool(
    "query_business_data_sql",
    args_schema=BusinessSQLInput,
    response_format="content_and_artifact",
    description=BUSINESS_QUERY_DESCRIPTION,
)
def query_business_data_sql(
    sql: str,
    parameters: list[Any] | None = None,
    result_mode: Literal["complete", "bounded"] = "complete",
    query_intent: QueryIntent | None = None,
    config: RunnableConfig = None,
) -> tuple[str, dict[str, Any]]:
    try:
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        timing_id = configurable.get("copilot_timing_id")
        result = execute_business_query(sql, parameters or [], result_mode, query_intent, timing_id)
    except UnsafeSQLQuery as error:
        return (
            f"Query rejected: {error} Revise it as one SELECT using only the schema and a matching "
            "query_intent from the tool description.",
            {"result_type": "query_error", "error": str(error)},
        )
    except Exception as error:
        # Keep driver details, credentials, and table internals out of the model response.
        if error.__class__.__module__.startswith("pymysql"):
            return (
                "The read-only business query failed. Check database availability and the query's table and column names.",
                {"result_type": "query_error", "error": "database_query_failed"},
            )
        raise
    preview = result["rows"][:8]
    outlet_coverage = result.get("outlet_source_coverage")
    if outlet_coverage:
        missing = outlet_coverage["outlets_without_source_records"]
        coverage_note = (
            f"Daily Z-report source coverage: {outlet_coverage['outlets_with_source_records_count']} of "
            f"{outlet_coverage['expected_outlet_count']} canonical outlets have report rows in scope. "
            f"No report rows for: {', '.join(missing) if missing else 'none'}. "
            "No report rows means missing evidence, not zero sales."
        )
        result["source_coverage_notes"].append(coverage_note)
    content = (
        f"Read-only business query returned {result['retrieved_record_count']} rows"
        f"{' (truncated to 200)' if result['truncated'] else ''}. "
        f"Result mode: {result['requested_result_mode']}. "
        f"Columns: {', '.join(result['columns'])}. Preview: {preview}. "
        "Use the full rows in the attached result artifact for the answer. "
        f"Coverage notes: {result['source_coverage_notes']}"
    )
    return content, result
