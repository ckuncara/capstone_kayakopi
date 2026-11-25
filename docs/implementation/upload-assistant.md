# Kaya Upload Desk

This independent Streamlit application follows the upload-first approach of the sibling
Outlet Ops AI application. It adds new files only; the existing copilot, ingestion pipeline,
Compose file and environment file are unchanged.

## Start

```bash
bash scripts/start_upload_assistant.sh
```

Open http://localhost:8502. The existing copilot stays on its current port. The launcher
adds an `upload-desk` service to the existing Compose project, using the existing Kaya
MySQL database (`kayakopi` in the current repository) and `kaya_kopi_knowledge` Chroma collection.
It does not run the old ingestion or replace the old copilot container.

For host execution with existing MySQL/Chroma services available:

```bash
PYTHONPATH=src .venv/bin/python -m streamlit run src/kaya_kopi/upload_assistant/app.py --server.port=8502
```

The database user needs permission to create/use the `uploaded_invoices`,
`uploaded_outlet_csvs` and `uploaded_roster_rows` tables in the existing database.
No database or separate Chroma collection is created. For production, provision these tables
using an administrative account and grant only required access. Do not expose this prototype publicly.

## Processing

- Invoice JPEG/PNG/WebP files: OpenAI structured vision extraction into
  `uploaded_invoices` in the existing database, including nullable values, item JSON, review notes and
  `source_file`. These are OCR observations, not the existing audited invoice pipeline.
- Daily outlet CSVs: standard Python parsing into `uploaded_outlet_csvs`, with no OpenAI
  requests, API-key requirement or token cost. All four known client header/date formats
  use the existing tabular parser's schema and decimal rules. Simple ISO-date reports with
  explicit revenue columns are supported too; unknown layouts are rejected, never sent
  to OpenAI. Invalid dates/numbers stay NULL with notes; no silent truncation or unit conversion.
  `reported_volume` and `volume_measure` preserve source counts; the client daily-summary
  counts are receipts, not verified cups sold. `wastage_kg` stays separate from liters.
  Each stored row retains `source_row_number` and the original file path.
  A filename such as `daily_summary_AMK-03.csv` sets the exact outlet code to `AMK-03`,
  not an expanded place name. Reselecting a processed CSV reparses and replaces only that
  file's rows transactionally with Python, correcting older model-normalized values without
  token usage. Processing displays immediate status, parsed-row counts, a progress bar and
  a separate saving phase. Existing tables receive an additive migration for the count,
  unit, wastage and source-row fields at app initialization.
- Excel roster workbooks (`.xlsx`): `openpyxl` reads all worksheets, detects the staff table
  beneath title rows, excludes pay/contact columns from OpenAI requests and replaces names
  with file-local staff keys before normalization. Results go to `uploaded_roster_rows` with
  month, role, outlet, schedule, original worksheet and row number. Staff keys cannot deduplicate
  people across uploads. Actual weekday cells are saved directly as `mon_shift` through
  `sun_shift`: `OFF` remains explicit, while missing cells stay NULL.
  `raw_hourly_rate`, `hourly_rate_sgd` and `rate_status` are also captured directly without
  sending rates to OpenAI. Rate normalization reuses the original roster parser's client rules:
  numeric rates are `CONFIRMED`, `TBC` uses SGD 13.00 with `CLIENT_APPROVED_DEFAULT`, missing
  rates are `MISSING`, and invalid/negative/non-finite rates remain NULL with `REVIEW_REQUIRED`.
  Individual pay is still redacted from chat tool results; storage does not authorize disclosure.
  Existing tables receive an additive column migration at app initialization. Reselecting a
  processed workbook backfills days/rates by file hash, worksheet and row number, without
  rerunning OpenAI or changing roster assignment counts. The DB account needs ALTER permission
  for that one-time upgrade. Existing source workbooks are never modified.
- Text/Markdown/plain-text email: redacted chunks with OpenAI embeddings in the existing
  `kaya_kopi_knowledge` collection. Existing indexed documents are searchable too. New chunks use
  namespaced IDs and include `source_file`, `source_uri`, `source_type` and chunk index.
- Original file bytes: invoice images go under `data/images/supplier_invoices`, CSV and Excel
  workbooks under `data/tabular`, WhatsApp/text under `data/docs/whatsapp_exports`, and
  emails under `data/docs/emails`. Choose Emails for plain-text email exports as well as
  `.eml` files. Identical files reuse
  the original path. A conflicting filename gets a content-hash prefix; no original is overwritten.
  SHA-256 deduplication and the file register remain in `data/upload_assistant/manifest.sqlite3`.
- Chat receives table purposes, columns and query rules before generating SQL. Tool-derived
  file paths are collected separately from model wording. Aggregates request a second
  provenance lookup if needed. The UI offers original-file downloads and image/text previews
  both immediately and when revisiting answers in the current browser session.
- Outlet name-change/history/re-registration questions use one `lookup_outlet_history`
  tool for canonical and alias codes. It requires enabled `outlet_alias`, `outlets` and
  `source_document` tables, joins `evidence_document_id` to the original document, and
  returns effective dates, validation state and file provenance. No recorded end date
  does not establish that an alias remains active. Existing-table queries also request
  provenance follow-up; missing-source warnings are emitted only once.

On the Upload tab, choose files from the browser or select existing data-folder files. In
particular, `staff_roster_jun2026.xlsx` and daily CSVs can be selected from `data/tabular`
without uploading another copy. Existing files are not automatically processed at startup.
The WhatsApp/text and Email choices list existing files from their respective document
folders. Previously archived files are not moved automatically, so saved citations remain valid.

CSV input is limited to 500 data rows and 200,000 decoded characters. Excel input is limited
to 20 worksheets, 2,000 source rows total, 50 columns per sheet and 50 MB uncompressed.
Every normalized roster row must retain its source row number; invalid output is rejected
before MySQL writes. `.xls` and HTML-only workbooks must be exported to `.xlsx` first.
Text input is limited
to 200,000 characters, and all uploads to 10 MB. Larger inputs are rejected rather than
silently shortened. HTML-only emails must be exported as plain text. File previews are
limited in length but downloads retain the complete original bytes.

Uploads use new tables in the same Kaya database; they do not overwrite existing audited
supplier invoices, CSV summaries or staff roster tables. Other Kaya MySQL and SQLite tables
can optionally be queried read-only through their existing `.env` table lists.
Their schemas are included in context, and source paths must be returned for citations.
The client tables `outlets`, `pos_transactions`, `supplier` and `menu_items` are queried
through `query_existing` with `database=mysql`, not the upload-only query tool. Include their
names in `KAYA_LLM_MYSQL_TABLES`; preserve any other enabled tables already configured.
Their business descriptions and join rules appear only when schema discovery exposes them.
POS joins to menu items by `item_id` and outlets by `outlet_code`; exact export duplicates
must be removed before aggregation. Supplier master records are not supplier spending.
Do not sum uploaded and existing copies of the same invoice or staff roster.
Old isolated Upload Desk stores from the earlier prototype are not read and are not deleted
automatically. Reprocess their originals using the Upload tab to write shared tables and correct
type-based file paths; old manifest paths under `data/upload_assistant` do not block reprocessing.

## Optional Settings

No new settings are required. These may be added to the existing environment by the user:

```dotenv
OPENAI_CHAT_MODEL=gpt-4o-mini
UPLOAD_AI_EXISTING_DATA=true
UPLOAD_AI_PORT=8502
```

`OPENAI_API_KEY`, `OPENAI_OCR_MODEL`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBED_MODEL`,
`MONTHLY_BUDGET_USD`, and the existing MySQL/Chroma settings are reused. Calls go directly
to OpenAI. `UPLOAD_AI_DATABASE` and `UPLOAD_AI_COLLECTION` from the earlier prototype are
ignored. Database selection follows the existing repository configuration; collection selection
uses `CHROMA_COLLECTION` (Docker fixes it to `kaya_kopi_knowledge`). The shared collection must
already exist and use the same embedding model/dimension as `OPENAI_EMBED_MODEL`.
Usage is logged separately in `data/upload_assistant/usage.sqlite3`; its budget
is independent of the old app and is not a combined organization cap. The budget check
runs before every request, but cannot reserve concurrent calls or predict their final cost.

Conversations persist in the `upload_conversations` table in
`data/upload_assistant/manifest.sqlite3`, independently of the original copilot's chat store.
The sidebar lists saved chats with reopen, rename and confirmed deletion controls. A new
conversation is saved on its first question; existing browser-session history is saved when
first loaded by this version. Sources, query activity and per-answer usage captions are
restored when reopening a chat. User text and answers are protected before storage.
Deleting a conversation does not remove file records, extracted data, embeddings or usage.

Tabs are ordered Assistant, Upload, Usage Log, Files. Every chat request has a conversation
and turn ID; chat calls and search embeddings made during the same turn share those IDs.
Captions beneath answers sum provider-reported input/output tokens and estimated USD across
all calls for that turn. Cached input pricing is applied and missing/unsupported usage is
reported as unavailable, never guessed as zero. The Usage Log tab refreshes local SQLite data
every five seconds and lists call models, prompts/operations, conversation/turn IDs, token
breakdowns, cost status and estimated totals. It makes no OpenAI requests. OCR/uploads
outside a chat appear as separate operations. Older logs without turn IDs cannot be
retroactively attributed to individual answers.

File records, extracted data, embeddings and usage persist. Sources validate
against actual files under `data` before display. Citations show which files were retrieved;
they do not independently prove every sentence or establish which business record is correct.
Original-file downloads are unredacted and available to every user of this app. Use only
authorized, non-sensitive uploads; pattern-based text redaction is not access control.

## Tests

Use offline mocked tests first. Do not run live OpenAI verification or rebuild/restart
Docker for testing without explicit approval; live chat, extraction and embedding checks
consume the team's API budget. The focused tests mock external services, including the
Streamlit progress test, and do not require Docker or an API key.

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_upload_assistant.py -q
```

Stop only this new service:

```bash
docker compose -f docker-compose.yml -f docker-compose.upload-assistant.yml stop upload-desk
```