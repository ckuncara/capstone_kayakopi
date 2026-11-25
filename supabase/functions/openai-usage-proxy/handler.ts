type JsonObject = Record<string, unknown>;

export interface Settings {
  openaiApiKey: string;
  supabaseUrl: string;
  supabaseAnonKey: string;
  supabaseServiceRoleKey: string;
}

type Usage = {
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
};

const PRICING_VERSION = "openai-standard-2026-11-24";
const PRICING: Record<
  string,
  { input: number; cached: number; output: number }
> = {
  "gpt-4o": { input: 2.5, cached: 1.25, output: 10 },
  "gpt-4o-mini": { input: 0.15, cached: 0.075, output: 0.6 },
  "text-embedding-3-small": { input: 0.02, cached: 0.02, output: 0 },
  "text-embedding-3-large": { input: 0.13, cached: 0.13, output: 0 },
};
const ALIASES: Record<string, string> = {
  "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
  "gpt-4o-2024-07-18": "gpt-4o",
  "gpt-4o-2024-11-20": "gpt-4o",
};
const ENDPOINTS = new Set(["chat/completions", "embeddings"]);
const PURPOSES = new Set(["chat", "ocr", "embedding", "other"]);

function object(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

function tokenCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

export function extractUsage(body: JsonObject): Usage {
  const usage = object(body.usage);
  const inputDetails = object(
    usage.prompt_tokens_details ?? usage.input_tokens_details,
  );
  const outputDetails = object(
    usage.completion_tokens_details ?? usage.output_tokens_details,
  );
  return {
    input_tokens: tokenCount(usage.prompt_tokens ?? usage.input_tokens),
    output_tokens: tokenCount(usage.completion_tokens ?? usage.output_tokens),
    cached_input_tokens: tokenCount(inputDetails.cached_tokens),
    reasoning_tokens: tokenCount(outputDetails.reasoning_tokens),
  };
}

export function estimateCost(
  model: string,
  usage: Usage,
  tier: string,
): { cost: string | null; version: string | null } {
  const rates = PRICING[ALIASES[model] ?? model];
  const input = usage.input_tokens;
  const output = usage.output_tokens;
  const cached = usage.cached_input_tokens ?? 0;
  if (
    !rates || !["default", "standard"].includes(tier) || input === null ||
    output === null || cached > input
  ) {
    return { cost: null, version: null };
  }
  const cost = ((input - cached) * rates.input + cached * rates.cached +
    output * rates.output) /
    1_000_000;
  return { cost: cost.toFixed(10), version: PRICING_VERSION };
}

class ProxyError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function serviceHeaders(settings: Settings): Record<string, string> {
  return {
    apikey: settings.supabaseServiceRoleKey,
    Authorization: `Bearer ${settings.supabaseServiceRoleKey}`,
    "Content-Type": "application/json",
  };
}

async function authenticate(
  request: Request,
  settings: Settings,
  fetcher: typeof fetch,
): Promise<string> {
  const authorization = request.headers.get("authorization") ?? "";
  if (!authorization.startsWith("Bearer ") || !authorization.slice(7).trim()) {
    throw new ProxyError(401, "Sign in to use the OpenAI proxy.");
  }
  let response: Response;
  try {
    response = await fetcher(`${settings.supabaseUrl}/auth/v1/user`, {
      headers: {
        apikey: settings.supabaseAnonKey,
        Authorization: authorization,
      },
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new ProxyError(503, "Unable to verify sign-in with Supabase.");
  }
  if (response.status === 401 || response.status === 403) {
    throw new ProxyError(401, "Sign in again to use the OpenAI proxy.");
  }
  if (!response.ok) {
    throw new ProxyError(503, "Supabase Auth is temporarily unavailable.");
  }
  let user: JsonObject;
  try {
    user = object(await response.json());
  } catch {
    throw new ProxyError(503, "Supabase returned an invalid user.");
  }
  if (typeof user.id !== "string" || !user.id) {
    throw new ProxyError(401, "Supabase returned an invalid user.");
  }
  return user.id;
}

async function writeUsage(
  settings: Settings,
  event: JsonObject,
  fetcher: typeof fetch,
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await fetcher(
        `${settings.supabaseUrl}/rest/v1/openai_usage_events?on_conflict=request_id`,
        {
          method: "POST",
          headers: {
            ...serviceHeaders(settings),
            Prefer: "resolution=ignore-duplicates,return=minimal",
          },
          body: JSON.stringify(event),
          signal: AbortSignal.timeout(10_000),
        },
      );
      if (response.ok) return;
      console.error(`Usage insert failed with HTTP ${response.status}`);
    } catch (error) {
      console.error(
        "Usage insert could not reach Supabase:",
        error instanceof Error ? error.name : "unknown error",
      );
    }
  }
  throw new ProxyError(
    503,
    "OpenAI returned a response, but usage could not be saved. Contact an administrator.",
  );
}

async function summary(
  settings: Settings,
  fetcher: typeof fetch,
): Promise<Response> {
  const now = new Date();
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
  let response: Response;
  try {
    response = await fetcher(
      `${settings.supabaseUrl}/rest/v1/rpc/get_openai_usage_summary`,
      {
        method: "POST",
        headers: serviceHeaders(settings),
        body: JSON.stringify({
          start_at: start.toISOString(),
          end_at: now.toISOString(),
        }),
        signal: AbortSignal.timeout(10_000),
      },
    );
  } catch {
    throw new ProxyError(503, "Unable to load team usage from Supabase.");
  }
  if (!response.ok) {
    throw new ProxyError(503, "Unable to load team usage from Supabase.");
  }
  let result: unknown;
  try {
    result = await response.json();
  } catch {
    throw new ProxyError(503, "Supabase returned an invalid usage summary.");
  }
  if (
    !Array.isArray(result) || !result.length ||
    !Object.keys(object(result[0])).length
  ) {
    throw new ProxyError(503, "Supabase returned an invalid usage summary.");
  }
  return Response.json({
    ...object(result[0]),
    period_start_utc: start.toISOString(),
    period_end_utc: now.toISOString(),
  });
}

async function recordEvent(
  settings: Settings,
  fetcher: typeof fetch,
  details: {
    requestId: string;
    userId: string;
    purpose: string;
    endpoint: string;
    requestedModel: string;
    status: number;
    body?: JsonObject;
    providerRequestId?: string | null;
    errorCode?: string | null;
  },
): Promise<void> {
  const body = details.body ?? {};
  const usage = extractUsage(body);
  if (details.endpoint === "embeddings" && usage.input_tokens !== null) {
    usage.output_tokens = 0;
  }
  const returnedModel = typeof body.model === "string" ? body.model : null;
  const tier = typeof body.service_tier === "string"
    ? body.service_tier
    : "default";
  const rawUsage = object(body.usage);
  const inputDetails = object(
    rawUsage.prompt_tokens_details ?? rawUsage.input_tokens_details,
  );
  const rawCached = inputDetails.cached_tokens;
  const invalidCached = rawCached != null && tokenCount(rawCached) === null;
  const { cost, version } = invalidCached
    ? { cost: null, version: null }
    : estimateCost(returnedModel ?? details.requestedModel, usage, tier);
  await writeUsage(settings, {
    request_id: details.requestId,
    user_id: details.userId,
    purpose: details.purpose,
    endpoint: details.endpoint,
    requested_model: details.requestedModel,
    returned_model: returnedModel,
    ...usage,
    estimated_cost_usd: cost,
    pricing_version: version,
    service_tier: tier,
    provider_response_id: typeof body.id === "string" ? body.id : null,
    provider_request_id: details.providerRequestId ?? null,
    http_status: details.status,
    error_code: details.errorCode ?? null,
  }, fetcher);
}

export async function handleRequest(
  request: Request,
  settings: Settings,
  fetcher: typeof fetch = fetch,
): Promise<Response> {
  try {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path.endsWith("/healthz")) {
      return Response.json({ status: "ok" });
    }
    const prefix = "/openai-usage-proxy/v1/";
    const index = path.indexOf(prefix);
    if (
      index < 0 || (index !== 0 && path.slice(0, index) !== "/functions/v1")
    ) {
      throw new ProxyError(404, "This OpenAI endpoint is not supported.");
    }
    const endpoint = path.slice(index + prefix.length);
    if (request.method === "GET" && endpoint === "usage/summary") {
      await authenticate(request, settings, fetcher);
      return await summary(settings, fetcher);
    }
    if (request.method !== "POST" || !ENDPOINTS.has(endpoint)) {
      throw new ProxyError(404, "This OpenAI endpoint is not supported.");
    }
    const purpose = request.headers.get("x-usage-purpose") ?? "other";
    if (!PURPOSES.has(purpose)) {
      throw new ProxyError(400, "Invalid usage purpose.");
    }
    const userId = await authenticate(request, settings, fetcher);
    let payload: JsonObject;
    try {
      payload = object(await request.json());
    } catch {
      throw new ProxyError(400, "Request body must be valid JSON.");
    }
    if (typeof payload.model !== "string" || !payload.model) {
      throw new ProxyError(400, "A model is required.");
    }
    if (payload.stream === true) {
      throw new ProxyError(400, "Streaming OpenAI requests are not supported.");
    }
    const requestId = crypto.randomUUID();
    const event = {
      requestId,
      userId,
      purpose,
      endpoint,
      requestedModel: payload.model,
    };
    let upstream: Response;
    try {
      upstream = await fetcher(`https://api.openai.com/v1/${endpoint}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${settings.openaiApiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(120_000),
      });
    } catch (error) {
      await recordEvent(settings, fetcher, {
        ...event,
        status: 502,
        errorCode: error instanceof Error ? error.name : "upstream_error",
      });
      throw new ProxyError(502, "OpenAI request failed.");
    }
    let responseText: string;
    try {
      responseText = await upstream.text();
    } catch (error) {
      await recordEvent(settings, fetcher, {
        ...event,
        status: 502,
        providerRequestId: upstream.headers.get("x-request-id"),
        errorCode: error instanceof Error
          ? error.name
          : "upstream_response_error",
      });
      throw new ProxyError(502, "Unable to read the OpenAI response.");
    }
    let body: JsonObject = {};
    try {
      body = object(JSON.parse(responseText));
    } catch {
      console.error(
        "OpenAI returned a non-JSON response; usage will be marked unpriced.",
      );
    }
    const providerError = object(body.error);
    const providerErrorCode = providerError.code ?? providerError.type;
    const providerRequestId = upstream.headers.get("x-request-id");
    await recordEvent(settings, fetcher, {
      ...event,
      status: upstream.status,
      body,
      providerRequestId,
      errorCode: typeof providerErrorCode === "string"
        ? providerErrorCode
        : null,
    });
    const headers = new Headers({
      "Content-Type": upstream.headers.get("content-type") ??
        "application/json",
      "X-Usage-Request-ID": requestId,
    });
    if (providerRequestId) {
      headers.set("X-OpenAI-Request-ID", providerRequestId);
    }
    const responseBody = [204, 205, 304].includes(upstream.status)
      ? null
      : responseText;
    return new Response(responseBody, { status: upstream.status, headers });
  } catch (error) {
    if (error instanceof ProxyError) {
      return Response.json({ detail: error.message }, { status: error.status });
    }
    console.error(
      "Unexpected proxy error:",
      error instanceof Error ? error.name : "unknown error",
    );
    return Response.json({
      detail: "The usage proxy encountered an unexpected error.",
    }, { status: 500 });
  }
}
