# Kaya Kopi — F&B Ops Copilot

Capstone scaffold for the 12-outlet coffee-chain ops assistant.

## Run
```bash
source .venv/bin/activate
export OPENAI_API_KEY=...          # from the 16 Nov briefing
streamlit run app.py               # chat UI at http://localhost:8501
python evals.py                    # 20-case eval suite -> eval_report.json
python -m kaya_kopi.backend        # (optional) print graph
```

## Architecture
See `docs/architecture.md`. The graph (LangGraph):

```
user -> guard_in -> agent (LLM + tools) -> tools -> agent -> guard_out -> END
```

State is checkpointed to `checkpoints.sqlite` (SqliteSaver) so a
conversation survives a restart — HARNESS_CHECKLIST box 5.

## Harness checklist coverage
| Box | Where |
|---|---|
| 1 grounding | `kaya_kopi/tools.py` — data only from client sources |
| 2 citations | system prompt + tool `source` field + `guard_out` |
| 3 guardrails | `kaya_kopi/guardrails.py` (input + output) |
| 4 honest errors | `_err()` in tools + `app.py` st.error |
| 5 state/checkpointer | `backend.py` SqliteSaver |
| 6 human-in-the-loop | (Phase B: `interrupt()`/`Command(resume=...)`) |
| 7 observability/spend | `kaya_kopi/spend.py` + sidebar tracker |
| 8 eval suite 20+ | `evals.py` |

## Phase A / B
- **Phase A (now → 19 Nov):** this scaffold + architecture doc + trap list.
- **Phase B (after 16 Nov data zip):** real tool bodies in `tools.py`,
  FAISS docs, OCR, MySQL queries, 20+ real eval cases.

## Secrets
`OPENAI_API_KEY` (or compatible) and MySQL creds live in `.env` —
git-ignored. `.env.example` documents the keys.
