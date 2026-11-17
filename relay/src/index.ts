import type { OAuthAuthorizationServer } from "@cloudflare/workers-oauth-provider";
import {
  OWNER,
  type Props,
  authorizationServer,
  authorize,
  checkPassphrase,
  grantPc,
  listGrants,
  resourceOf,
  resourceServer,
  revokeGrants,
} from "./auth";
import { Bridge } from "./bridge";
import { HOOK_PATH, hookRequest } from "./hook";
import { expiredScript, installScript } from "./install";
import { type HomeView, type PcView, homePage } from "./pages";
import {
  LEGACY_PC,
  type Route,
  baseOf,
  claimPairCode,
  deviceTokenFits,
  listPcs,
  newPairCode,
  pairCodeName,
  pairPc,
  removePc,
  routeOf,
  validName,
} from "./pcs";

export { Bridge };

// One-time file links (/t/<token>) are capabilities checked by the agent; an IP that keeps
// presenting unknown tokens is turned away for a while.
const TRANSFER_PATH = /^\/t\/([A-Za-z0-9_-]{43})$/;
const MAX_TRANSFER_FAILURES = 20;
const FAILURE_WINDOW_S = 15 * 60;
const INSTALL_PATH = /^\/i\/([A-Za-z0-9_-]{22})$/;
const PRM = "/.well-known/oauth-protected-resource";

/** Each computer has its own Bridge: its connection, calls in flight and offline time. */
function bridge(env: Env, pc: string) {
  return env.BRIDGE.get(env.BRIDGE.idFromName(`pc:${pc}`));
}

function originOf(env: Env, url: URL): string {
  return env.PUBLIC_ORIGIN?.replace(/\/$/, "") || url.origin;
}

function html(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

function text(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
}

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: { "cache-control": "no-store", "access-control-allow-origin": "*" } });
}

// The authorization server for the current list of computers, kept per isolate: the list changes
// only when a computer is paired or removed, and building it costs CPU on every request otherwise.
let cachedServer: { key: string; server: OAuthAuthorizationServer<Env> | null } | undefined;

/** The computers and the authorization server for them (null while no computer is paired). */
async function directory(env: Env, origin: string) {
  const legacy = Boolean(env.AGENT_TOKEN);
  const pcs = await listPcs(env);
  const key = JSON.stringify([origin, pcs, legacy, env.CIMD ?? ""]);
  if (cachedServer?.key !== key) cachedServer = { key, server: authorizationServer(env, { origin, pcs, legacy }) };
  return { pcs, legacy, server: cachedServer.server };
}

function bridgeRequest(path: string, base: string, request: Request, user: string): Request {
  const target = new URL("https://bridge/forward");
  target.searchParams.set("path", path);
  target.searchParams.set("base", base);
  const headers = new Headers(request.headers);
  headers.set("x-winhand-user", user);
  return new Request(target, { method: request.method, headers, body: request.body, signal: request.signal });
}

async function transferLink(request: Request, env: Env, pc: string, base: string, token: string): Promise<Response> {
  if (!["GET", "PUT", "POST"].includes(request.method)) {
    return new Response("method not allowed", { status: 405, headers: { allow: "GET, PUT, POST" } });
  }
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  const failKey = `winhand:tfail:${ip}`;
  const failures = Number((await env.OAUTH_KV.get(failKey)) ?? "0");
  if (failures >= MAX_TRANSFER_FAILURES) return new Response("too many invalid links; try later", { status: 429 });
  const response = await bridge(env, pc).fetch(bridgeRequest(`/transfer/${token}`, base, request, "transfer-link"));
  if (response.status === 404) {
    await env.OAUTH_KV.put(failKey, String(failures + 1), { expirationTtl: FAILURE_WINDOW_S });
  }
  return response;
}

type PcStatus = { online?: boolean; agent?: { version?: unknown } | null; offlineSince?: number | null };

async function pcStatus(env: Env, pc: string): Promise<PcStatus> {
  return (await (await bridge(env, pc).fetch("https://bridge/status")).json()) as PcStatus;
}

/** Whether a computer is online, for scripts: a cloud session can loop on it until the PC is back.
 * Like the page, it shows only online/offline and the winhand version, never the PC's Windows name. */
function statusFields(status: PcStatus) {
  const since = status.online ? null : (status.offlineSince ?? null);
  return {
    online: Boolean(status.online),
    version: typeof status.agent?.version === "string" ? status.agent.version : null,
    offline_since: since ? new Date(since).toISOString() : null,
    offline_for_s: since ? Math.round((Date.now() - since) / 1000) : 0,
  };
}

/** /status.json: every computer; while AGENT_TOKEN is set also the old top-level fields of "default". */
async function statusSummary(env: Env): Promise<Response> {
  const pcs = await listPcs(env);
  const all = await Promise.all(pcs.map(async (name) => ({ name, ...statusFields(await pcStatus(env, name)) })));
  const legacy = env.AGENT_TOKEN ? all.find((pc) => pc.name === LEGACY_PC) : undefined;
  if (!legacy) return json({ pcs: all });
  const { name: _name, ...fields } = legacy;
  return json({ ...fields, pcs: all });
}

async function home(request: Request, env: Env, origin: string, extra: Partial<HomeView> = {}, status = 200): Promise<Response> {
  const { pcs, legacy, server } = await directory(env, origin);
  const views: PcView[] = await Promise.all(
    pcs.map(async (name) => {
      const s = await pcStatus(env, name);
      return {
        name,
        online: Boolean(s.online),
        version: typeof s.agent?.version === "string" ? s.agent.version : null,
        offlineForMs: !s.online && s.offlineSince ? Date.now() - s.offlineSince : null,
        legacy: legacy && name === LEGACY_PC,
      };
    }),
  );
  const signedIn = server ? await listGrants(server.getOAuthApi(env)).then((all) => all.length, () => null) : 0;
  return html(homePage({ origin, host: new URL(origin).host, pcs: views, signedIn, ...extra }), status);
}

/** The owner makes a one-time install command for a computer, new or paired again. */
async function pair(request: Request, env: Env, origin: string): Promise<Response> {
  if (request.method !== "POST") return Response.redirect(`${origin}/`, 303);
  const data = await request.formData();
  const name = String(data.get("name") ?? "").trim().toLowerCase();
  const refused = await checkPassphrase(request, env, String(data.get("passphrase") ?? ""));
  if (refused) return home(request, env, origin, { notice: { text: refused.error, error: true }, openSetup: true }, refused.status);
  if (!validName(name)) {
    const notice = { text: "电脑名称只能用小写字母、数字和中间的连字符，最长 32 个字符。", error: true };
    return home(request, env, origin, { notice, openSetup: true }, 400);
  }
  const code = await newPairCode(env, name);
  return home(request, env, origin, { command: { name, line: `irm ${origin}/i/${code} | iex` } });
}

/** GET: the install script; it does not use the code up, so link previews are harmless. POST: the
 * script uses the code up and gets the computer's new device token and addresses. */
async function install(request: Request, env: Env, origin: string, code: string): Promise<Response> {
  if (request.method === "GET" || request.method === "HEAD") {
    const name = await pairCodeName(env, code);
    return text(name ? installScript(origin, code) : expiredScript(origin));
  }
  if (request.method !== "POST") return new Response("method not allowed", { status: 405, headers: { allow: "GET, POST" } });
  const name = await claimPairCode(env, code);
  if (!name) return json({ error: "this install command was used already or expired" }, 410);
  const token = await pairPc(env, name);
  const base = baseOf(origin, { pc: name, legacy: false });
  return json({
    name,
    token,
    agent_url: `${base.replace(/^http/, "ws")}/agent`,
    mcp_url: `${base}/mcp`,
    status_url: `${base}/status.json`,
  });
}

/** The owner forgets a computer: its token, its sign-ins and its connection go. */
async function remove(request: Request, env: Env, origin: string): Promise<Response> {
  if (request.method !== "POST") return Response.redirect(`${origin}/`, 303);
  const data = await request.formData();
  const name = String(data.get("name") ?? "").trim().toLowerCase();
  const refused = await checkPassphrase(request, env, String(data.get("passphrase") ?? ""));
  if (refused) return home(request, env, origin, { notice: { text: refused.error, error: true } }, refused.status);
  const { pcs, legacy, server } = await directory(env, origin);
  if (!pcs.includes(name)) return home(request, env, origin, { notice: { text: `没有叫 ${name} 的电脑。`, error: true } }, 404);
  let failed = 0;
  if (server) {
    const oauth = server.getOAuthApi(env);
    const mine = (await listGrants(oauth)).filter((grant) => grantPc(origin, grant) === name);
    failed = (await revokeGrants(oauth, mine)).failed;
  }
  await removePc(env, name);
  await bridge(env, name).fetch("https://bridge/disconnect", { method: "POST" });
  const kept = legacy && name === LEGACY_PC ? "它的旧令牌 AGENT_TOKEN 还在，删掉这个密钥才算完全移除。" : "";
  if (failed) {
    return home(request, env, origin, { notice: { text: `已移除 ${name}，但有 ${failed} 个登录没退出成功，请再试一次。${kept}`, error: true } }, 500);
  }
  return home(request, env, origin, { notice: { text: `已移除 ${name}。${kept}` } });
}

/** The owner signs every client out of every computer (a leaked or forgotten sign-in). */
async function signOutAll(request: Request, env: Env, origin: string): Promise<Response> {
  if (request.method !== "POST") return Response.redirect(`${origin}/`, 303);
  const data = await request.formData();
  const refused = await checkPassphrase(request, env, String(data.get("passphrase") ?? ""));
  if (refused) return home(request, env, origin, { notice: { text: refused.error, error: true } }, refused.status);
  const { server } = await directory(env, origin);
  if (!server) return home(request, env, origin, { notice: { text: "现在没有登录。" } });
  const oauth = server.getOAuthApi(env);
  const { revoked, failed } = await revokeGrants(oauth, await listGrants(oauth));
  if (failed) {
    return home(request, env, origin, { notice: { text: `已退出 ${revoked} 个登录，${failed} 个没退出成功，请再试一次。`, error: true } }, 500);
  }
  return home(request, env, origin, { notice: { text: `已退出 ${revoked} 个登录。各处再用时，重新输入主人口令登录。` } });
}

/** A computer's MCP endpoints, behind its own OAuth resource; they reach its PC as /mcp/... */
function mcp(request: Request, env: Env, ctx: ExecutionContext, origin: string, route: Route, server: OAuthAuthorizationServer<Env>) {
  const base = baseOf(origin, route);
  const handler = {
    async fetch(inner: Request, innerEnv: Env, innerCtx: ExecutionContext & { props?: Props }) {
      const url = new URL(inner.url);
      const rest = routeOf(url.pathname, route.legacy)?.rest ?? route.rest;
      return bridge(innerEnv, route.pc).fetch(bridgeRequest(rest + url.search, base, inner, innerCtx.props?.user ?? OWNER));
    },
  };
  return resourceServer(server, origin, route.pc, route.legacy, handler).fetch(request, env, ctx);
}

/** Everything under one computer's address space. */
async function computer(request: Request, env: Env, ctx: ExecutionContext, origin: string, route: Route): Promise<Response> {
  if (route.rest === "/agent") {
    const auth = request.headers.get("authorization") ?? "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!(await deviceTokenFits(env, route.pc, token))) return new Response("bad agent token", { status: 401 });
    return bridge(env, route.pc).fetch(new Request("https://bridge/connect", request));
  }
  const { pcs, server } = await directory(env, origin);
  if (!pcs.includes(route.pc)) return new Response("not found", { status: 404 });
  const base = baseOf(origin, route);
  if (route.rest === "/status.json") return json({ name: route.pc, ...statusFields(await pcStatus(env, route.pc)) });
  const link = route.rest.match(TRANSFER_PATH);
  if (link) return transferLink(request, env, route.pc, base, link[1]);
  const hook = route.rest.match(HOOK_PATH);
  if (hook) {
    return hookRequest(request, env, hook[1], (forwarded) => {
      const target = new URL(forwarded.url);
      target.searchParams.set("base", base);
      return bridge(env, route.pc).fetch(new Request(target, forwarded));
    });
  }
  if ((route.rest === "/mcp" || route.rest.startsWith("/mcp/")) && server) return mcp(request, env, ctx, origin, route, server);
  return new Response("not found", { status: 404 });
}

/**
 * Protected-resource metadata. A client that looks for it at the path-specific location (RFC 9728
 * path insertion) of one of a computer's other MCP servers (/pc/<name>/mcp/<server>) gets that
 * computer's resource: one sign-in covers all of a computer's MCP servers.
 */
async function resourceMetadata(request: Request, env: Env, ctx: ExecutionContext, origin: string, below: string): Promise<Response> {
  const legacy = Boolean(env.AGENT_TOKEN);
  const route = below === "" ? (legacy ? { pc: LEGACY_PC, rest: "/mcp", legacy: true } : null) : routeOf(below, legacy);
  if (!route || !(route.rest === "/mcp" || route.rest.startsWith("/mcp/"))) return new Response(null, { status: 404 });
  const { pcs, server } = await directory(env, origin);
  if (!server || !pcs.includes(route.pc)) return new Response(null, { status: 404 });
  const canonical = `${origin}${PRM}${new URL(resourceOf(origin, route.pc, route.legacy)).pathname}`;
  const metadataOnly = { fetch: () => new Response(null, { status: 404 }) };
  return resourceServer(server, origin, route.pc, route.legacy, metadataOnly).fetch(new Request(canonical, request), env, ctx);
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

function isAuthorizationServerPath(pathname: string): boolean {
  return pathname.startsWith("/.well-known/oauth-authorization-server") || pathname === "/oauth/token" || pathname === "/oauth/register";
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const origin = originOf(env, url);
    const route = routeOf(url.pathname, Boolean(env.AGENT_TOKEN));
    if (route) return computer(request, env, ctx, origin, route);
    if (url.pathname === PRM || url.pathname.startsWith(`${PRM}/`)) {
      return resourceMetadata(request, env, ctx, origin, url.pathname.slice(PRM.length));
    }
    if (isAuthorizationServerPath(url.pathname)) {
      const { server } = await directory(env, origin);
      if (!server) {
        return json({ error: "temporarily_unavailable", error_description: `no computer is paired yet; pair one at ${origin}/` }, 503);
      }
      const forwarded = url.pathname === "/oauth/token" && request.method === "POST" ? await singleClientAuth(request) : request;
      return server.fetch(forwarded, env, ctx);
    }
    if (url.pathname === "/authorize") {
      const { server } = await directory(env, origin);
      if (!server) return text(`还没有配对任何电脑。先到 ${origin}/ 接一台电脑。`, 503);
      return authorize(request, env, server, origin);
    }
    const code = url.pathname.match(INSTALL_PATH);
    if (code) return install(request, env, origin, code[1]);
    if (url.pathname === "/pair") return pair(request, env, origin);
    if (url.pathname === "/remove") return remove(request, env, origin);
    if (url.pathname === "/signout") return signOutAll(request, env, origin);
    if (url.pathname === "/") return home(request, env, origin);
    if (url.pathname === "/status.json") return statusSummary(env);
    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
