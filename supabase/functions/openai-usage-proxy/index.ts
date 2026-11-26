import { handleRequest } from "./handler.ts";

Deno.serve((request) => {
  // Get secrets from environment
  const openaiApiKey = Deno.env.get("OPENAI_API_KEY")?.trim() || "";
  
  // Log what we're getting (for debugging)
  console.log("Environment variable check:", {
    hasOpenAiKey: !!openaiApiKey,
    openaiKeyLength: openaiApiKey.length,
    openaiKeyStart: openaiApiKey.substring(0, 10),
  });
  
  // Supabase injects these automatically; try multiple ways to get them
  let supabaseUrl = Deno.env.get("SUPABASE_URL") || 
                    Deno.env.get("SUPABASE_URL_ENV") ||
                    Deno.env.get("SUPABASE_HOST");
  let supabaseAnonKey = Deno.env.get("SUPABASE_ANON_KEY") ||
                        Deno.env.get("SUPABASE_PUBLISHABLE_KEY") ||
                        Deno.env.get("SUPABASE_PUBLIC_KEY");
  let supabaseServiceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ||
                               Deno.env.get("SUPABASE_SECRET_KEY");
  
  // If SUPABASE_URL is still not found, construct it from the request
  if (!supabaseUrl && request.headers.has("x-forwarded-proto")) {
    const host = request.headers.get("x-forwarded-host") || 
                 new URL(request.url).host;
    if (host.includes("supabase.co")) {
      const projectId = host.split(".")[0];
      supabaseUrl = `https://${projectId}.supabase.co`;
      console.log("Constructed Supabase URL from request:", supabaseUrl);
    }
  }
  
  if (
    !openaiApiKey || !supabaseUrl || !supabaseAnonKey || !supabaseServiceRoleKey
  ) {
    console.error("Missing required proxy environment variables:", {
      hasOpenAiKey: !!openaiApiKey,
      hasSupabaseUrl: !!supabaseUrl,
      hasAnonKey: !!supabaseAnonKey,
      hasServiceRoleKey: !!supabaseServiceRoleKey,
      openaiKeyStart: openaiApiKey.substring(0, 10),
    });
    return Response.json({ detail: "The usage proxy is not configured." }, {
      status: 503,
    });
  }
  return handleRequest(request, {
    openaiApiKey,
    supabaseUrl: supabaseUrl.replace(/\/+$/, ""),
    supabaseAnonKey,
    supabaseServiceRoleKey,
  });
});
