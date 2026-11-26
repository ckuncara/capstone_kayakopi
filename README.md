# Kaya Kopi Holdings Pte Ltd — AI Engineering Capstone (team1_kaya_kopi)

Your team is the forward-deployed engineering unit for **Kaya Kopi Holdings Pte Ltd**. Your job: build the
F&B ops copilot described in [CLIENT_BRIEF.md](CLIENT_BRIEF.md) and survive a live demo with the
customer on **Fri 27 Nov 2026**.

## Quick start

Prerequisite: Docker Desktop, or Docker Engine with Docker Compose v2.

```bash
git clone <repository-url>
cd team1_kaya_kopi
cp .env.example .env
# Configure the passwords, ports and OPENAI_API_KEY in .env.
# Copy the client package contents into data/; see data/README.md.
./scripts/bootstrap.sh
./scripts/reload_data.sh
./scripts/test.sh
./scripts/start.sh
```

Open <http://localhost:8501> (or the configured `KAYA_STREAMLIT_HOST_PORT`). Docker images provide
the isolated Python environments; no host virtual environment or manual `pip install` is required.

The commands have deliberately separate responsibilities:

- `bootstrap.sh` validates the environment and builds the images;
- `reload_data.sh` explicitly purges and rebuilds the database from the private inputs;
- `test.sh` runs the parser tests and read-only database health/functional checks; and
- `start.sh` starts MySQL and Streamlit without rerunning ingestion or tests.

The first run is slower because Docker builds the application images and PaddleOCR downloads its
English model. Parsed files remain under the git-ignored `runtime/` directory, the MySQL data
uses a Docker volume, and chat checkpoints use a separate Docker volume.

Useful commands:

```bash
docker compose logs -f copilot  # application logs
docker compose down             # stop services; preserve database volumes
./scripts/reload_data.sh         # explicitly purge and rebuild business data
```

The deterministic test command does not call the LLM. To manually review the non-deterministic
answers after loading the database, run the Ops Copilot or Upload Desk acceptance scenario set:

```bash
# Ops Copilot: 29 scenarios
docker compose --profile test run --rm test python -m scripts.run_llm_acceptance

# Upload Desk: 31 scenarios, including tool traces and source files
docker compose --profile test run --rm test python -m scripts.run_upload_acceptance
```

Both runners accept `--model NAME`, `--all-latest-models`, `--case N` (repeatable) and
`--list-models`. The latest GPT-6 variants are:

```text
gpt-6.2-astra  gpt-6.2-sol  gpt-6.1-terra  gpt-6.1-sol
gpt-6-astra    gpt-6-sol    gpt-6-luna
```

The spend meter shows the rate card for the configured model, and usage logging uses the same
per-model rate. Each listed model has its own entry in `MODEL_PRICING`, so a business rate change
can be applied to one model without affecting the others. The current rates are the **Standard,
short-context** values from [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
(checked 2026-11-26); Batch/Fast/Flex/Ultrafast and long-context rates are not used by default.

For example, to check only scenarios 1, 10 and 11 with one model:

```bash
docker compose --profile test run --rm test python -m scripts.run_upload_acceptance \
  --model gpt-6.2-astra --case 1 --case 10 --case 11
```

The runners use the configured OpenAI-compatible endpoint/key and print each answer beside its
expected evidence checklist; they are intentionally not part of the automated test suite.

### Remembered sign-in

When the hosted proxy is enabled, select **Remember me for 14 days** at sign-in to
reopen the normal app URL in the same browser without signing in again. No bookmark
or URL parameters are needed. A refresh token and the original 14-day deadline are
stored in browser localStorage; access tokens stay in Streamlit session memory and
are refreshed using Supabase's actual expiry. Refreshing does not extend that deadline.
Signing out removes the remembered login. With the box unchecked, login stays in the
current Streamlit session for up to 10 minutes and clears any previously remembered login.

This requires Streamlit 1.51 or newer and browser site storage. Clearing site data,
private browsing, changing the app's origin (including its port), or a revoked Supabase
session can require signing in sooner. LocalStorage is accessible to scripts on the
app's origin, so use HTTPS for hosted deployments and trusted frontend components.
The 14-day deadline is an app persistence policy; configure Supabase session lifetime
limits separately if a server-enforced maximum is required. Existing token-bearing
bookmarks should be deleted; sign in again using the normal URL.

### LLM spend tracking

The spend meter and usage log refresh every five seconds while their page is open. Without `LLM_PROXY_URL`, usage is stored locally in the `llm_usage` table of `LOCAL_SQLITE_PATH`
(Docker: `/workspace/data/kaya_ops.db`, mounted from `data/kaya_ops.db`). This local log is not a
shared team ledger or a provider billing total. To route chat, vision OCR and embedding calls through
the authenticated hosted proxy and store token/cost metadata in Supabase, follow
[the hosted usage proxy setup](docs/implementation/openai-usage-proxy.md).
With the proxy enabled, the sidebar shows the shared team estimate; the detailed local usage log
and existing budget guard remain per-computer. The hosted total is an estimate, not a provider
invoice or a global hard spending cap.
Each model response in a streamed chat turn is logged, including tool-selection and final-answer
calls. OCR and invoice chat use the same response-metadata logger; embeddings use provider counts.
Log timestamps display in Singapore time (UTC+8); stored timestamps, monthly budgets, and billing
comparison periods remain UTC. Each new chat answer has a subtle grey footer with the model(s),
combined input/output tokens, and estimated USD cost across all model calls for that question,
including context and tool results. The footer survives reruns and reopening a saved conversation.
Older turns without a saved usage association do not show a reconstructed estimate.

Costs are **estimates** using the explicit standard USD-per-million-token rates and exact snapshot
aliases in `src/kaya_kopi/copilot/llm_usage_tracker.py`. Review the linked provider pricing before
updating the rate table and its version; each new priced row retains the pricing version used.
Cached input is charged separately; reasoning tokens are recorded but are already included in
output tokens. The returned model, requested model, response ID, and provider token details are
retained where available. Unknown models, unsupported tiers/audio pricing, or missing token counts
are saved as **unpriced**, not charged as a different model or counted as free. Totals exclude
unpriced calls and warn that the budget is incomplete; budget-guarded OCR/embedding calls pause
until missing usage/pricing is resolved. Existing rows remain unchanged and labeled legacy estimates.

In **LLM Usage Log → Compare with provider billing**, enter a provider USD usage total before
credits/tax, a matching UTC date range (start inclusive, end exclusive), and the matching project/
API-key scope. Confirm that the filters cover only usage recorded in this local database. The saved
comparison shows provider cost minus the local estimate and flags incomplete periods; it never
rewrites historical usage or the budget. Provider reporting may lag, and usage on teammates' laptops
or other clients will not be captured here. No organization admin API key is required.

See [docs/implementation/ingestion.md](docs/implementation/ingestion.md) for individual parser,
schema and loader commands.

## Python development

The installable package uses the standard `src/` layout. For local development outside Docker:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests scripts
```

Install the OCR extra with `.[ocr]` when working on invoice-image ingestion. Application code lives
under `src/kaya_kopi/`; private inputs, generated runtime files, operational scripts, and tests stay
outside the package.

## Rules of engagement

1. **No client data in git.** `data/` is git-ignored; keep it that way. Same for `.env` and your API key.
2. **Every member contributes.** Team-process marks are graded per person from commit history — every member commits real work under their own GitHub account (set `git config user.email` to the e-mail on your GitHub profile, or your commits are credited to nobody). Pair-programming is fine; say so in commit messages (`Co-authored-by:`).
3. **Budget.** Your API key has a hard budget. Watch your spend; a burned key is not replaced.
4. **Cite everything.** Answers your app gives must say where the fact came from. See [HARNESS_CHECKLIST.md](HARNESS_CHECKLIST.md) — it is the grading rubric in checklist form.
5. **Ask the client, by email.** The client is reachable only by email to the course director (subject `Client question — Team N`), replies within two working days and does not take calls. If the brief mentions something that is not in the package, ask — do not guess and do not quietly work around it. Keep every answer in [`docs/client_qa.md`](docs/client_qa.md).

## Deliverables

- Working application (live demo; a judging panel plays your customer and asks unscripted questions)
- Architecture documentation with **at least two Mermaid diagrams** in `docs/`
- Data quality report: every trap you found and how you handled it
- 5-minute pitch
- Honest post-mortem (what you'd do differently)

## Timeline

Briefing Mon 16 Nov (Zoom) · **Checkpoint 1 Fri 20 Nov, 17:00 (repo review; no meeting)** · Checkpoint 2 Mon 23 Nov · Consults Mon 23 and Tue 24 Nov (Zoom) · Final sprint Thu 26 Nov · **Demo Fri 27 Nov 09:00–13:00, SR902** (slot order announced Tue 24 Nov). Details: [CHECKPOINTS.md](CHECKPOINTS.md).

## Real commit
- Chandra
- Andri
