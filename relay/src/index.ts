import { AuthorizationError, type AuthRequest, OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { Bridge } from "./bridge";
import { consentPage, statusPage } from "./pages";

export { Bridge };

const SCOPE = "winhand";
const MAX_FAILURES = 5;
const FAILURE_WINDOW_S = 15 * 60;

function bridge(env: Env) {
  return env.BRIDGE.get(env.BRIDGE.idFromName("default"));
}

async function digest(value: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
}

async function secretEquals(given: string, expected: string | undefined): Promise<boolean> {
  if (!expected) return false;
  const [a, b] = await Promise.all([digest(given), digest(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

// Authenticated MCP traffic: tunnel it to the machine.
const mcpHandler = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext & { props?: { user?: string } }) {
    const url = new URL(request.url);
    const target = new URL("https://bridge/forward");
    target.searchParams.set("path", url.pathname + url.search);
    const headers = new Headers(request.headers);
    headers.set("x-winhand-user", ctx.props?.user ?? "owner");
    return bridge(env).fetch(new Request(target, { method: request.method, headers, body: request.body, signal: request.signal }));
  },
};

async function authorize(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  if (request.method === "GET") {
    let authRequest: AuthRequest;
    try {
      authRequest = await oauth.parseAuthRequest(request);
    } catch (error) {
      return authError(error);
    }
    const client = await oauth.lookupClient(authRequest.clientId);
    if (!client) return new Response("Unknown OAuth client", { status: 400 });
    const consent = await oauth.beginConsent(authRequest);
    const headers = new Headers(consent.headers);
    headers.set("content-type", "text/html; charset=utf-8");
    return new Response(consentPage({ handle: consent.handle, client: client.clientName ?? authRequest.clientId, redirect: authRequest.redirectUri }), { headers });
  }

  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");
  const client = String(form.get("client") ?? "");
  const redirect = String(form.get("redirect") ?? "");
  try {
    if (form.get("action") === "deny") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }
    const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
    const failKey = `winhand:fail:${ip}`;
    const failures = Number((await env.OAUTH_KV.get(failKey)) ?? "0");
    if (failures >= MAX_FAILURES) {
      return html(consentPage({ handle, client, redirect, error: "尝试次数过多，请 15 分钟后再试。" }), 429);
    }
    if (!(await secretEquals(String(form.get("passphrase") ?? ""), env.OWNER_PASSPHRASE))) {
      await env.OAUTH_KV.put(failKey, String(failures + 1), { expirationTtl: FAILURE_WINDOW_S });
      return html(consentPage({ handle, client, redirect, error: "口令不对。" }), 401);
    }
    const approved = await oauth.approveConsent(request, handle, { scope: [SCOPE] });
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: "owner",
      metadata: { client },
      scope: [SCOPE],
      props: { user: "owner" },
    });
    const headers = new Headers(approved.headers);
    headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers });
  } catch (error) {
    return authError(error);
  }
}

function authError(error: unknown): Response {
  if (!(error instanceof AuthorizationError)) throw error;
  if (!error.redirectUri) return new Response(error.description, { status: 400 });
  const redirect = new URL(error.redirectUri);
  redirect.searchParams.set("error", error.code);
  redirect.searchParams.set("error_description", error.description);
  if (error.state) redirect.searchParams.set("state", error.state);
  if (error.issuer) redirect.searchParams.set("iss", error.issuer);
  return Response.redirect(redirect.href, 302);
}

function html(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}

const PRM_PREFIX = "/.well-known/oauth-protected-resource";

function protectedResourceMetadata(requestOrigin: string, env: Env): Response {
  const origin = env.PUBLIC_ORIGIN?.replace(/\/$/, "") || requestOrigin;
  return Response.json(
    {
      resource: `${origin}/mcp`,
      authorization_servers: [origin],
      scopes_supported: [SCOPE],
      bearer_methods_supported: ["header"],
      resource_name: "winhand",
    },
    { headers: { "access-control-allow-origin": "*", "cache-control": "max-age=3600" } },
  );
}

const defaultHandler = {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/authorize") return authorize(request, env);
    if (url.pathname === "/") {
      const status = (await (await bridge(env).fetch("https://bridge/status")).json()) as Record<string, unknown>;
      return html(statusPage(status, `${url.origin}/mcp`));
    }
    return new Response("not found", { status: 404 });
  },
};

let provider: OAuthProvider<Env> | undefined;
let providerOrigin = "";

function getProvider(origin: string): OAuthProvider<Env> {
  if (!provider || providerOrigin !== origin) {
    providerOrigin = origin;
    provider = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: mcpHandler as never,
      defaultHandler: defaultHandler as never,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/oauth/token",
      clientRegistrationEndpoint: "/oauth/register",
      scopesSupported: [SCOPE],
      clientIdMetadataDocumentEnabled: true,
      accessTokenTTL: 3600,
      resourceMetadata: {
        resource: `${origin}/mcp`,
        authorization_servers: [origin],
        scopes_supported: [SCOPE],
        bearer_methods_supported: ["header"],
        resource_name: "winhand",
      },
    });
  }
  return provider;
}

/**
 * Some clients (the Python MCP SDK among them) send a confidential client's credentials both in
 * the Authorization header and in the form body. RFC 6749 says to use one method, and the OAuth
 * library rejects the request outright; when both copies agree, keep only the header.
 */
async function singleClientAuth(request: Request): Promise<Request> {
  const auth = request.headers.get("authorization") ?? "";
  const type = request.headers.get("content-type") ?? "";
  if (!auth.startsWith("Basic ") || !type.includes("application/x-www-form-urlencoded")) return request;
  const form = new URLSearchParams(await request.clone().text());
  if (!form.has("client_secret")) return request;
  let id = "";
  let secret = "";
  try {
    const decoded = atob(auth.slice(6).trim());
    const colon = decoded.indexOf(":");
    id = decodeURIComponent(decoded.slice(0, colon));
    secret = decodeURIComponent(decoded.slice(colon + 1));
  } catch {
    return request;
  }
  if (form.get("client_secret") !== secret || (form.has("client_id") && form.get("client_id") !== id)) return request;
  form.delete("client_secret");
  form.delete("client_id");
  const headers = new Headers(request.headers);
  headers.delete("content-length");
  return new Request(request.url, { method: "POST", headers, body: form.toString() });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/oauth/token" && request.method === "POST") request = await singleClientAuth(request);
    if (url.pathname === "/agent") {
      const auth = request.headers.get("authorization") ?? "";
      const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
      if (!(await secretEquals(token, env.AGENT_TOKEN))) return new Response("bad agent token", { status: 401 });
      return bridge(env).fetch(new Request("https://bridge/connect", request));
    }
    // The machine's local MCP servers live at /mcp/<name> and share the /mcp resource (one
    // grant covers them all). Clients that look for metadata at the path-specific location
    // (RFC 9728 path insertion) are pointed at that shared resource.
    if (url.pathname.startsWith(`${PRM_PREFIX}/mcp/`) && request.method === "GET") {
      return protectedResourceMetadata(url.origin, env);
    }
    const origin = env.PUBLIC_ORIGIN?.replace(/\/$/, "") || url.origin;
    return getProvider(origin).fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
