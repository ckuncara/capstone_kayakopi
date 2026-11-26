CREATE OR REPLACE FUNCTION public.get_openai_usage_summary(
    start_at timestamptz,
    end_at timestamptz
)
RETURNS TABLE (
    total_requests bigint,
    priced_requests bigint,
    unpriced_requests bigint,
    estimated_cost_usd numeric,
    input_tokens bigint,
    output_tokens bigint
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
    SELECT
        count(*),
        count(*) FILTER (WHERE estimated_cost_usd IS NOT NULL),
        count(*) FILTER (WHERE estimated_cost_usd IS NULL),
        coalesce(sum(estimated_cost_usd), 0),
        coalesce(sum(input_tokens), 0),
        coalesce(sum(output_tokens), 0)
    FROM public.openai_usage_events
    WHERE created_at >= start_at AND created_at < end_at;
$$;

REVOKE ALL ON FUNCTION public.get_openai_usage_summary(timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_openai_usage_summary(timestamptz, timestamptz)
    TO service_role;

REVOKE ALL ON TABLE public.openai_usage_events FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.openai_usage_events_id_seq FROM anon, authenticated;
GRANT SELECT, INSERT ON TABLE public.openai_usage_events TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.openai_usage_events_id_seq TO service_role;
