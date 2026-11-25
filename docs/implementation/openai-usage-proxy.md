# Hosted OpenAI usage tracking

The optional authenticated proxy routes the app's OpenAI chat, vision OCR, and embedding requests
through a Render-hosted Python service. The service keeps the OpenAI key server-side and records
token and cost metadata in Supabase. It does not store prompts, responses, or image payloads.

The current team usage total is a pricing estimate, not the OpenAI invoice. A request that bypasses
the proxy is not recorded; rotate the shared OpenAI key after all clients have moved to proxy mode.
The existing local `BudgetGuard` remains per-computer and does not enforce a team-wide spending cap.

## 1. Configure Supabase

1. Create a Supabase project and save its database password securely.
2. In **SQL Editor**, run the migration files in `supabase/migrations/` in filename order.
   The initial usage-table migration has already been run for this project; run the two newer
   migrations to add the monthly team-summary function and service-tier tracking. For a fresh
   project, run all files in order.
3. Keep Email authentication enabled and disable public sign-ups. Add each teammate individually
   under **Authentication → Users**.
4. Note the project URL, publishable/legacy anon key, and service-role secret. The URL and anon key
   are used by local clients; the service-role secret is server-only.

The `openai_usage_events` table has row-level security enabled and no client policies. Only the
proxy's server-side service role should write or query usage records.

## 2. Deploy the proxy on Render

Deploy after the code is available in the GitHub branch Render will use:

1. In Render, create a **Blueprint** from this repository so it reads the root `render.yaml`.
2. Set the service region close to the Supabase project's region.
3. Add these environment variables to the proxy service:
   - `OPENAI_API_KEY`: the team OpenAI key
   - `SUPABASE_URL`: the project URL
   - `SUPABASE_ANON_KEY`: the publishable/legacy anon key
   - `SUPABASE_SERVICE_ROLE_KEY`: the service-role secret
4. Wait for `/healthz` to report healthy. The health check verifies that all four variables exist.

Never put `OPENAI_API_KEY` or `SUPABASE_SERVICE_ROLE_KEY` in a local `.env`, frontend setting,
repository, or chat message. Anyone with the service-role key can bypass Supabase row-level
security.

## 3. Configure each local app

In the ignored local `.env`, set:

```dotenv
LLM_PROXY_URL=https://<your-render-service>.onrender.com
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_ANON_KEY=<your-publishable-or-legacy-anon-key>
OPENAI_API_KEY=
```

Restart Streamlit. Teammates sign in with their individual Supabase account. The main copilot and
the OCR test harness then route chat, OCR, and embeddings through the proxy. The `.env.example`
contains the variable names but no credentials.

Batch OCR/backfill commands prompt for a Supabase email and password when run interactively. For
non-interactive runs, provide that user's short-lived `KAYA_PROXY_ACCESS_TOKEN` through the process
environment. Do not commit or share the token.

## 4. Verify tracking

1. Sign in to the app and submit a chat question.
2. Upload an invoice to exercise vision OCR. Embedding usage is recorded when the app creates or
   searches Chroma embeddings.
3. Check the **Team OpenAI Usage** sidebar meter.
4. In Supabase SQL Editor, inspect recent metadata:

   ```sql
   SELECT created_at, user_id, purpose, endpoint, requested_model, returned_model,
          input_tokens, output_tokens, estimated_cost_usd, http_status
   FROM public.openai_usage_events
   ORDER BY created_at DESC
   LIMIT 25;
   ```

The proxy rejects unauthenticated requests and unsupported endpoints. It records unknown models or
missing token counts as unpriced rather than claiming they cost zero. After verifying every teammate
and batch job uses the proxy, rotate the old OpenAI key and remove it from local machines.
