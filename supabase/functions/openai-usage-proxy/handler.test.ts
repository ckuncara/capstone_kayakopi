// deno-lint-ignore-file require-await
import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  estimateCost,
  extractUsage,
  handleRequest,
  type Settings,
} from "./handler.ts";

const settings: Settings = {
  openaiApiKey: "test-openai-key",
  supabaseUrl: "https://project.example",
  supabaseAnonKey: "test-anon-key",
  supabaseServiceRoleKey: "test-service-key",
};
const baseUrl = "https://project.example/functions/v1/openai-usage-proxy/v1";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

function request(
  endpoint: string,
  payload: unknown,
  headers: Record<string, string> = {},
) {
  return new Request(`${baseUrl}/${endpoint}`, {
    method: "POST",
    headers: {
      authorization: "Bearer user-token",
      "content-type": "application/json",
      ...headers,
    },
    body: JSON.stringify(payload),
  });
}

test("records OCR usage while forwarding the provider response", async () => {
  const events: Record<string, unknown>[] = [];
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) {
      assert.equal(
        new Headers(init?.headers).get("authorization"),
        "Bearer user-token",
      );
      return json({ id: "user-uuid" });
    }
    if (url === "https://api.openai.com/v1/chat/completions") {
      assert.equal(
        new Headers(init?.headers).get("authorization"),
        "Bearer test-openai-key",
      );
      return new Response(
        JSON.stringify({
          id: "response-id",
          model: "gpt-4o-mini-2024-07-18",
          usage: {
            prompt_tokens: 1000,
            completion_tokens: 100,
            prompt_tokens_details: { cached_tokens: 200 },
          },
        }),
        {
          headers: {
            "content-type": "application/json",
            "x-request-id": "provider-id",
          },
        },
      );
    }
    assert.match(url, /openai_usage_events\?on_conflict=request_id$/);
    assert.equal(new Headers(init?.headers).get("apikey"), "test-service-key");
    events.push(JSON.parse(String(init?.body)));
    return new Response(null, { status: 201 });
  };
  const response = await handleRequest(
    request("chat/completions", {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "private input" }],
    }, { "x-usage-purpose": "ocr" }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 200);
  assert.equal((await response.json()).id, "response-id");
  assert.equal(response.headers.get("x-openai-request-id"), "provider-id");
  assert.equal(events.length, 1);
  assert.equal(events[0].user_id, "user-uuid");
  assert.equal(events[0].purpose, "ocr");
  assert.equal(events[0].estimated_cost_usd, "0.0001950000");
  assert.equal(events[0].provider_request_id, "provider-id");
  assert.equal(JSON.stringify(events).includes("private input"), false);
});

test("rejects absent or invalid user tokens without calling OpenAI", async () => {
  let calls = 0;
  const mockFetch: typeof fetch = async () => {
    calls++;
    return json({ message: "unauthorized" }, 401);
  };
  const missing = await handleRequest(
    request("embeddings", { model: "text-embedding-3-small" }, {
      authorization: "",
    }),
    settings,
    mockFetch,
  );
  assert.equal(missing.status, 401);
  assert.equal(calls, 0);
  const invalid = await handleRequest(
    request("embeddings", { model: "text-embedding-3-small" }),
    settings,
    mockFetch,
  );
  assert.equal(invalid.status, 401);
  assert.equal(calls, 1);
});

test("records embeddings with zero output and supports function-relative paths", async () => {
  let event: Record<string, unknown> | undefined;
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return json({ id: "user-uuid" });
    if (url.endsWith("/v1/embeddings")) {
      return json({
        model: "text-embedding-3-small",
        usage: { prompt_tokens: 500, total_tokens: 500 },
      });
    }
    event = JSON.parse(String(init?.body));
    return new Response(null, { status: 201 });
  };
  const response = await handleRequest(
    new Request("https://project.example/openai-usage-proxy/v1/embeddings", {
      method: "POST",
      headers: {
        authorization: "Bearer user-token",
        "x-usage-purpose": "embedding",
      },
      body: JSON.stringify({
        model: "text-embedding-3-small",
        input: ["text"],
      }),
    }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 200);
  assert.equal(event?.output_tokens, 0);
  assert.equal(event?.estimated_cost_usd, "0.0000100000");
});

test("refuses unsupported streaming requests before calling OpenAI", async () => {
  let calls = 0;
  const mockFetch: typeof fetch = async () => {
    calls++;
    return json({ id: "user-uuid" });
  };
  const response = await handleRequest(
    request("chat/completions", { model: "gpt-4o-mini", stream: true }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 400);
  assert.equal(calls, 1);
});

test("marks missing or unsupported prices and invalid cached counts as unpriced", () => {
  const usage = extractUsage({
    usage: { prompt_tokens: 100, completion_tokens: 20 },
  });
  assert.equal(estimateCost("unknown-model", usage, "default").cost, null);
  assert.equal(estimateCost("gpt-4o-mini", usage, "priority").cost, null);
  assert.equal(
    estimateCost(
      "gpt-4o-mini",
      { ...usage, cached_input_tokens: 101 },
      "default",
    ).cost,
    null,
  );
  assert.equal(
    estimateCost("gpt-4o-mini", { ...usage, output_tokens: null }, "default")
      .cost,
    null,
  );
});

test("records malformed cached token counts as unpriced", async () => {
  let event: Record<string, unknown> | undefined;
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return json({ id: "user-uuid" });
    if (url.endsWith("/v1/chat/completions")) {
      return json({
        model: "gpt-4o-mini",
        usage: {
          prompt_tokens: 100,
          completion_tokens: 20,
          prompt_tokens_details: { cached_tokens: -1 },
        },
      });
    }
    event = JSON.parse(String(init?.body));
    return new Response(null, { status: 201 });
  };
  const response = await handleRequest(
    request("chat/completions", { model: "gpt-4o-mini" }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 200);
  assert.equal(event?.estimated_cost_usd, null);
  assert.equal(event?.cached_input_tokens, null);
});

test("records an unpriced event when the provider request fails", async () => {
  let event: Record<string, unknown> | undefined;
  const mockFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return json({ id: "user-uuid" });
    if (url.endsWith("/v1/chat/completions")) {
      throw new TypeError("network failure");
    }
    event = JSON.parse(String(init?.body));
    return new Response(null, { status: 201 });
  };
  const response = await handleRequest(
    request("chat/completions", { model: "gpt-4o-mini" }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 502);
  assert.equal(event?.http_status, 502);
  assert.equal(event?.estimated_cost_usd, null);
  assert.equal(event?.error_code, "TypeError");
});

test("reports a ledger failure after retrying without retrying OpenAI", async () => {
  let openaiCalls = 0;
  let insertCalls = 0;
  const mockFetch: typeof fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return json({ id: "user-uuid" });
    if (url.endsWith("/v1/embeddings")) {
      openaiCalls++;
      return json({
        model: "text-embedding-3-small",
        usage: { prompt_tokens: 10 },
      });
    }
    insertCalls++;
    return json({ message: "unavailable" }, 503);
  };
  const response = await handleRequest(
    request("embeddings", { model: "text-embedding-3-small", input: ["text"] }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 503);
  assert.match((await response.json()).detail, /usage could not be saved/);
  assert.equal(openaiCalls, 1);
  assert.equal(insertCalls, 3);
});

test("returns authenticated monthly team summary", async () => {
  const mockFetch: typeof fetch = async (input, init) => {
    if (String(input).endsWith("/auth/v1/user")) {
      return json({ id: "user-uuid" });
    }
    assert.match(String(input), /rpc\/get_openai_usage_summary$/);
    const payload = JSON.parse(String(init?.body));
    assert.match(payload.start_at, /-01T00:00:00\.000Z$/);
    return json([{ total_requests: 2, unpriced_requests: 1 }]);
  };
  const response = await handleRequest(
    new Request(`${baseUrl}/usage/summary`, {
      headers: { authorization: "Bearer user-token" },
    }),
    settings,
    mockFetch,
  );
  assert.equal(response.status, 200);
  assert.equal((await response.json()).unpriced_requests, 1);
});
