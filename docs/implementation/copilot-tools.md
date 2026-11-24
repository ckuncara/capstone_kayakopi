# Kaya Kopi chatbot tools

The seven LangChain tools in `app.tools` expose fixed, read-only MySQL queries. They do not accept SQL
from the model and do not write to the database.

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
performance, legal or tax advice, bulk exports, and external actions before calling the
model. Tool results and answers are checked for common contact details and confidential HR data.
An answer is withheld if no tool returned a result in the current turn; this does not prove that
every statement in an answer is supported by the result.
The copilot continues to allow aggregate labour costs and user-initiated invoice uploads.

These are pattern-based checks, not a guarantee that every wording, personal address, or
indirect disclosure will be detected. Review access controls and existing saved data before
deployment. Existing conversation checkpoints and source records are not deleted or rewritten
by these changes.
