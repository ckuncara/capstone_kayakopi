# Kaya Kopi chatbot tools

The LangChain tools in `kaya_kopi.copilot.tools` expose specialist business queries, schema
discovery, model-written read-only SQL, and Chroma retrieval. OpenAI selects tools and writes
queries from chat requests; users do not need to supply SQL.

## General database access

Configure explicit, comma-separated table names in `.env`:

```dotenv
KAYA_LLM_MYSQL_TABLES=source_document,supplier_invoice,supplier_invoice_item
KAYA_LLM_SQLITE_TABLES=supplier_invoices,supplier_invoice_items
KAYA_LLM_CHROMA_COLLECTIONS=kaya_kopi_knowledge
LOCAL_SQLITE_PATH=data/kaya_ops.db
```

`discover_database_schema` exposes only enabled tables and columns. `query_database` accepts
one read-only SELECT/CTE query, rejects unapproved or qualified tables, and limits results to
200 rows. SQLite uses a read-only connection and an authorizer, with an instruction budget;
MySQL uses a read-only transaction and a five-second SELECT timeout. General MySQL queries
reject views. Custom/server-side functions and write statements are denied.

Unset or empty SQL lists disable general SQL access. When a list is present, existing MySQL
specialists also enforce it, and the old SQLite question-pattern tool directs the model to
the restricted query tool. Specialists may need more tables than the invoice-only example.
An empty Chroma list disables retrieval; when unset, only the configured default collection
is enabled. `search_knowledge_chroma` searches enabled collections, not only invoice chunks.
Restart the application after changing `.env`. Use a dedicated SELECT-only MySQL account
with per-table grants; application allowlists are not a replacement for database permissions.

Queries should join enabled provenance tables and return `source_uri` or `image_path`.
The general query tool checks returned file paths against files in `data`. Aggregate queries
may need a follow-up provenance lookup over contributing records. Citations from structured
reports are displayed separately from the answer when present. The main chat does not append
a `Sources:` suffix or withhold an answer solely because source metadata is missing, and it does
not prove that a citation supports every claim. Local invoice tools identify the SQLite records
or Chroma invoice number.

Existing privacy checks remain pattern-based. Only allow data appropriate for every user
of the chat, preferably pre-redacted tables; arbitrary SQL aliases can weaken field-name
redaction. Chroma collection access is independent of SQL table access.

```python
from kaya_kopi.copilot.tools import ALL_TOOLS
```

Tools:

- `analyze_outlets_sql`: authoritative performance, measured wastage, roster-based staffing,
  and monthly headcounts by outlet and role (`analysis="headcount"`);
- `analyze_pos_sql`: observed product mix, prices, payment modes, and time-of-day patterns from the explicitly partial POS export;
- `analyze_suppliers_sql`: scanned invoices, canonical spend, cost trends, item price changes, and purchase reconciliation;
- `get_supplier_addresses_sql`: recorded business addresses and supporting invoice references
  for an exact supplier name, ignoring case and surrounding spaces;
- `get_data_coverage_sql`: objective source counts and completeness limitations;
- `search_documents_sql`: MySQL full-text search with substring fallback and entity filters;
- `assess_kopi_price_change_sql`: transparent price sensitivity scenarios with an insufficient-evidence decision state; and
- `get_data_quality_issues_sql`: bounded review queue with source citations.

The older narrow Python functions remain importable as compatibility wrappers, but are not exposed
to the model in `ALL_TOOLS`. Sales analysis resolves effective aliases such as `AMK-3A` to `AMK-03`.
Daily Z-report summaries are authoritative for revenue where present; POS analysis is always marked
as partial. Structured facts come from MySQL and document results include source locators.

Install dependencies and run the database ingestion before importing the tools:

```bash
.venv/bin/python -m pip install -e ".[dev]"
./scripts/run_compose_ingest.sh
```

Run the live MySQL smoke tests with:

```bash
.venv/bin/python -m unittest tests.integration.test_query_tools -v
```

## Streamlit copilot

The Streamlit application follows the `llm-rag-basic` tool-calling pattern: an OpenAI chat model
selects a tool, LangGraph executes the read-only database call, and the model turns the cited result
into an answer. Conversations persist in a local SQLite checkpoint database.

Set `OPENAI_API_KEY` in `.env`, then run from the repository root:

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/streamlit run src/kaya_kopi/copilot/streamlit_app.py
```

The database tools themselves do not need an API key. The natural-language agent does, because the
LLM decides which tool and arguments to use. `KAYA_OPENAI_MODEL` optionally overrides the default
`gpt-4o-mini` model, and standard `OPENAI_BASE_URL` configuration is supported by LangChain.

## Chat privacy checks

Staff-count questions use monthly roster headcounts, counting distinct staff keys within each outlet
and role without returning names or pay rates. Undated questions use the latest roster month.
Counts describe the monthly roster, not attendance or a particular shift; missing roster data is not
zero staff, and counts must not be summed across months or roles to infer unique staff.
Outlet-qualified payroll and staff-pay summaries are allowed; individual-pay checks still apply
separately to each sentence or line, so an outlet total does not exempt nearby personal-pay details.

The supplier-address lookup reads only supplier names, recorded addresses, and invoice references
from MySQL. It returns at most 10 distinct invoice-backed records, not a complete address history.
An empty result means no recorded address matched that exact name. Differing addresses should be
reported with their references, without assuming which is current. The structured `supplier_address`
field from this tool has the same privacy exception as supplier invoice details, including when
reopening a chat. This trusts the source field's classification; it does not independently verify
that an address is a business address. Other address fields retain the normal checks.

The main copilot checks recognised requests for individual pay, staff discipline or
performance, private contact details, legal or tax advice, bulk exports, and external actions
before calling the model. Tool results and answers are checked for common contact details and
confidential HR data. Citations are rendered in the structured report area when available; missing
source metadata alone does not block the final answer. Text accompanying a tool call is not
displayed as an answer, including when reopening an older conversation. Malformed structured
model answers are replaced with an explicit format error rather than displayed as raw JSON.
These checks do not prove that every statement in an answer is supported by the source.
The copilot continues to allow aggregate labour costs and user-initiated invoice uploads.

These are pattern-based checks, not a guarantee that every wording, personal address, or
indirect disclosure will be detected. Review access controls and existing saved data before
deployment. Existing conversation checkpoints and source records are not deleted or rewritten
by these changes; saved answers are checked again when displayed.
