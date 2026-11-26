# LangGraph SQL and result completeness implementation

## Slice 1: schema-guided SQL results

The Copilot keeps its existing LangGraph `chat -> tools -> chat` graph. A new
`query_business_data_sql` tool gives the model an approved business schema and lets it write
parameterized MySQL `SELECT` queries, including joins, aggregates, subqueries and non-recursive
CTEs. This is free-form analytics over the catalog, not a set of fixed query templates.

SQLGlot parses and validates each query. The guard allows one `SELECT`, approved tables and public
columns, scalar bound parameters, and a five-second MySQL execution limit. It rejects mutations,
multiple statements, unknown/private columns, unbound text literals, unsafe functions, locking reads,
and unbounded result sets. The Copilot connects with a separate MySQL `SELECT`-only account; ingestion
continues to use the writer account.

The tool returns a short preview to the model and a separate result artifact with all returned rows,
columns, source tables, coverage notes, result mode and truncation status. The Streamlit app renders
the artifact directly and stores it with the chat turn. A complete query is capped at 200 displayed
rows and reports truncation. A bounded `LIMIT` is allowed only when the model marks the request as a
user-requested subset; the UI labels it as a subset. The catalog omits raw OCR, contact information,
staff identities and individual pay.

Relative periods are calculated in Asia/Singapore in Python and included as exact date bounds in the
system message. The model is instructed to use those bounds in SQL. The existing reviewer that
replaced answers without tool evidence has been removed, so direct answers from supplied context
(such as today's date) remain possible.

## Slice 2: LangGraph branching, chaining and follow-ups

The native `ToolNode` remains responsible for execution. The model calls independent tools together
for parallel execution, then can make a dependent call after it receives an earlier result. The
system prompt now says to wait for exact IDs before a dependent lookup. CTE support allows a single
query to rank business records and join detail rows by canonical IDs.

For follow-ups, the graph finds the latest successful structured SQL or specialist report and builds
compact context from its question, effective period, metric/analysis, grouping, filters, coverage
status and returned canonical entity IDs. SQL reports also retain the query, parameters, columns,
source tables and truncation state. It does not resend the full table to the model. The complete
report remains in the checkpoint artifact and is rendered by the app. Failed tool results do not
replace an earlier successful report, and each graph thread reads only its own message history.

## Existing behavior retained

- Dedicated tools still handle business definitions that already have specialized logic, including
  outlet alias resolution, canonical supplier costs, staffing, wastage, margin evidence and Kopi
  price sensitivity.
- ChromaDB implementation is untouched.
- MySQL remains the business database.
- Tool inputs and SQL filters are validated; tool output and model answers retain existing privacy
  redaction.

## Slice 3: direct rendering for specialist reports

Successful results from the existing MySQL reporting tools are retained as structured
`specialist_report` artifacts on their `ToolMessage`. The model receives a bounded summary and a
small preview, while the complete returned report stays outside the model context. The existing
supplier-address privacy exception is preserved when those reports are sanitized.

The Streamlit chat renders these artifacts separately from model prose. It recursively finds
returned record lists, displays their rows with retrieved/displayed counts, shows report metadata,
and collates available source citations. Document-search results use the same structured renderer;
ChromaDB implementation and APIs are unchanged.

## Slice 4: declared SQL query scope

Every schema-guided SQL call must now declare a `query_intent` containing the metric alias,
groupings, filters and date scope. The validator checks that the metric is selected, groupings are
present in `GROUP BY`, declared bound filters match the SQL and parameter values, and no simple
bound SQL filters are omitted from the declaration. For a date-bounded query, it also checks that
the date column is approved, the range is valid, and top-level `>= ?` and `< ?` predicates bind the
declared inclusive start and exclusive end in the correct parameter positions. The result artifact
and UI show the declared intent and applied date range.

This verifies consistency between generated SQL and model-declared intent. It does not independently
prove that the model extracted the user's intent correctly or that a selected metric represents the
business concept requested; representative live questions still need acceptance checks.

## Slice 3: direct rendering for specialist reports

Successful results from the existing MySQL reporting tools are retained as structured
`specialist_report` artifacts on their `ToolMessage`. The model receives a bounded summary and a
small preview, while the complete returned report stays outside the model context. The existing
supplier-address privacy exception is preserved when those reports are sanitized.

The Streamlit chat renders these artifacts separately from model prose. It recursively finds
returned record lists, displays their rows with retrieved/displayed counts, shows report metadata,
and collates available source citations. Document-search results use the same structured renderer;
ChromaDB implementation and APIs are unchanged.

## Limits and next slices

- The specialist renderer displays record lists found in a tool response. It does not yet provide
  field-specific layouts for every report type, and unusual nested list shapes may need a dedicated
  presentation rule.
- Results above 200 rows are disclosed as truncated; there is no UI pagination yet. The user can
  narrow the query or aggregate it in SQL.
- Source coverage is computed for complete, unfiltered outlet-grouped reports that use
  `daily_outlet_summary`: a period-aware check compares outlets with report rows against the
  canonical outlet list and displays missing-report codes. This does not calculate daily coverage,
  and it does not apply to filtered, bounded, POS-only, or specialist reports.
- SQL scope validation verifies consistency against model-declared intent; it cannot independently
  determine whether the declared intent matches the natural-language request.
- Filter validation currently supports direct bound comparisons and `IN` lists joined with `AND`;
  other filter forms such as `BETWEEN`, `IS NULL`, and `OR` are rejected until represented in the
  query-intent schema.
- Existing Kopi price-sensitivity calculations are reused. New margin, staffing, supplier-cost and
  forecast scenario calculators are not added in this slice.
- Live-model acceptance across the client question set and a full database-backed demonstration are
  not covered by the focused tests.

## Focused verification

The added tests cover SQL validation and parameter binding, declared date-scope checks, CTE
ranking/detail joins, complete versus bounded results, the 200-row cap, separate model preview and
result artifact, direct rendering of all 12 returned rows, artifact persistence/redaction, Singapore
calendar bounds, and native LangGraph parallel and sequential tool calls.
