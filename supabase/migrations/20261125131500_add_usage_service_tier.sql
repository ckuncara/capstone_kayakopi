ALTER TABLE public.openai_usage_events
    ADD COLUMN IF NOT EXISTS service_tier text;
