// OAuth for claude.ai and other MCP clients. Every computer's /pc/<name>/mcp is its own protected
// resource (RFC 8707/9728): a token issued for one computer is refused by every other. One
// authorization server issues tokens for all of them; its list of resources is fixed when it is
// built, so it is built per request from the current list of computers.

import {
  AuthorizationError,
  CimdFetchError,
  type GrantSummary,
  OAuthAuthorizationServer,
  type OAuthHelpers,
  OAuthResourceServer,
  type OAuthResourceHandler,
} from "@cloudflare/workers-oauth-provider";
import { diagnoseClaudeMetadata } from "./cimd-diagnostic";
import { consentPage } from "./pages";
import { LEGACY_PC, secretEquals, validName } from "./pcs";

export const SCOPE = "winhand";
export const OWNER = "owner";
const MAX_FAILURES = 5;
const FAILURE_WINDOW_S = 15 * 60;
// Every sign-in is its own grant, so several Claude accounts and sessions stay connected at once.
// One that goes unused this long expires (each token refresh pushes it out again), and only the
// newest few per computer are kept.
const GRANT_IDLE_S = 30 * 24 * 60 * 60;
const MAX_GRANTS = 20;

export type Props = { user: string; pc?: string };
/** The relay's public origin and the computers it serves. */
export type Directory = { origin: string; pcs: string[]; legacy: boolean };

/** The protected resource of one computer's MCP endpoints; `legacy`: the old /mcp address. */
export function resourceOf(origin: string, pc: string, legacy = false): string {
  return legacy ? `${origin}/mcp` : `${origin}/pc/${pc}/mcp`;
}

/** The computer a resource identifier names, or null. */
export function pcOfResource(origin: string, resource: string | string[] | undefined): string | null {
  const value = Array.isArray(resource) ? resource[0] : resource;
  if (!value) return null;
  if (value === `${origin}/mcp`) return LEGACY_PC;
  const prefix = `${origin}/pc/`;
  if (!value.startsWith(prefix) || !value.endsWith("/mcp")) return null;
  const name = value.slice(prefix.length, -"/mcp".length);
  return validName(name) ? name : null;
}

/** The authorization server for these computers, or null while there is none to authorize for. */
export function authorizationServer(env: Env, dir: Directory): OAuthAuthorizationServer<Env> | null {
  const resources = dir.pcs.map((pc) => resourceOf(dir.origin, pc));
  const legacy = dir.legacy ? resourceOf(dir.origin, LEGACY_PC, true) : undefined;
  if (legacy) resources.push(legacy);
  if (!resources.length) return null;
  return new OAuthAuthorizationServer<Env>({
    issuer: dir.origin,
    resources,
    // A client that names no resource, and the sign-ins made before resources existed, belong to
    // the old single-computer address.
    defaultResource: legacy,
    legacyGrantResource: legacy,
    authorizeEndpoint: "/authorize",
    tokenEndpoint: "/oauth/token",
    clientRegistrationEndpoint: "/oauth/register",
    scopesSupported: [SCOPE],
    // Off unless CIMD=on: with it, /authorize fetches the client's metadata document, and a
    // Worker running in a region Claude does not serve gets an "unavailable in region" page
    // instead (2026-11-13). Without it, clients register dynamically.
    clientIdMetadataDocumentEnabled: env.CIMD === "on",
    accessTokenTTL: 3600,
    refreshTokenTTL: GRANT_IDLE_S,
    refreshTokenIdleTTL: GRANT_IDLE_S,
  });
}

/** One computer's MCP endpoints, behind tokens issued for exactly that computer. */
export function resourceServer(
  server: OAuthAuthorizationServer<Env>,
  origin: string,
  pc: string,
  legacy: boolean,
  handler: OAuthResourceHandler<Env, Props>,
): OAuthResourceServer<Env, Props> {
  return new OAuthResourceServer<Env, Props>({
    resourceMetadata: {
      resource: resourceOf(origin, pc, legacy),
      authorization_servers: [origin],
      scopes_supported: [SCOPE],
      bearer_methods_supported: ["header"],
      resource_name: legacy ? "winhand" : `winhand ${pc}`,
    },
    handler,
    validateToken: (env) => (resource, token) => server.validateToken<Props>(resource, token, env),
  });
}

/** Why a passphrase is refused (counted per IP, shared by every form that asks for it), or null. */
export async function checkPassphrase(
  request: Request,
  env: Env,
  given: string,
): Promise<{ error: string; status: number } | null> {
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  const failKey = `winhand:fail:${ip}`;
  const failures = Number((await env.OAUTH_KV.get(failKey)) ?? "0");
  if (failures >= MAX_FAILURES) return { error: "尝试次数过多，请 15 分钟后再试。", status: 429 };
  if (await secretEquals(given, env.OWNER_PASSPHRASE)) return null;
  await env.OAUTH_KV.put(failKey, String(failures + 1), { expirationTtl: FAILURE_WINDOW_S });
  return { error: "口令不对。", status: 401 };
}

export async function listGrants(oauth: OAuthHelpers): Promise<GrantSummary[]> {
  const all: GrantSummary[] = [];
  let cursor: string | undefined;
  do {
    const page = await oauth.listUserGrants(OWNER, { cursor, limit: 1000 });
    all.push(...page.items);
    cursor = page.cursor;
  } while (cursor);
  return all;
}

/** The computer a sign-in is for ("default" for sign-ins made before resources existed). */
export function grantPc(origin: string, grant: GrantSummary): string | null {
  return grant.resource === undefined ? LEGACY_PC : pcOfResource(origin, grant.resource);
}

/** Revoke these sign-ins; counts the revocations that went through and failed. */
export async function revokeGrants(oauth: OAuthHelpers, grants: GrantSummary[]): Promise<{ revoked: number; failed: number }> {
  const results = await Promise.allSettled(grants.map((grant) => oauth.revokeGrant(grant.id, OWNER)));
  const failed = results.filter((result) => result.status === "rejected").length;
  return { revoked: grants.length - failed, failed };
}

/** Keep the newest MAX_GRANTS sign-ins for one resource and revoke the older ones. */
async function trimGrants(oauth: OAuthHelpers, resource: string, legacy: boolean) {
  const matches = (grant: GrantSummary) =>
    grant.resource === undefined ? legacy : (Array.isArray(grant.resource) ? grant.resource : [grant.resource]).includes(resource);
  const old = (await listGrants(oauth)).filter(matches).sort((a, b) => b.createdAt - a.createdAt).slice(MAX_GRANTS);
  return revokeGrants(oauth, old);
}

export function authError(error: unknown): Response {
  if (!(error instanceof AuthorizationError)) throw error;
  if (!error.redirectUri) return new Response(error.description, { status: 400 });
  const redirect = new URL(error.redirectUri);
  redirect.searchParams.set("error", error.code);
  redirect.searchParams.set("error_description", error.description);
  if (error.state) redirect.searchParams.set("state", error.state);
  if (error.issuer) redirect.searchParams.set("iss", error.issuer);
  return Response.redirect(redirect.href, 302);
}

function html(body: string, status = 200, headers?: Headers): Response {
  const out = new Headers(headers);
  out.set("content-type", "text/html; charset=utf-8");
  return new Response(body, { status, headers: out });
}

/** The consent page (GET) and its answer (POST): the owner's passphrase signs a client in. */
export async function authorize(request: Request, env: Env, server: OAuthAuthorizationServer<Env>, origin: string): Promise<Response> {
  try {
    return await authorizeInner(request, env, server, origin);
  } catch (error) {
    if (!(error instanceof CimdFetchError)) throw error;
    // No credentials, state, request URL or upstream response body in diagnostics.
    await diagnoseClaudeMetadata(error.metadataUrl);
    return new Response("OAuth client metadata is temporarily unavailable. Please retry signing in later.", {
      status: 503,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store", "retry-after": "60" },
    });
  }
}

async function authorizeInner(request: Request, env: Env, server: OAuthAuthorizationServer<Env>, origin: string): Promise<Response> {
  const oauth = server.getOAuthApi(env);
  if (request.method === "GET") {
    let authRequest;
    try {
      authRequest = await oauth.parseAuthRequest(request);
    } catch (error) {
      return authError(error);
    }
    const client = await oauth.lookupClient(authRequest.clientId);
    if (!client) return new Response("Unknown OAuth client", { status: 400 });
    const pc = pcOfResource(origin, authRequest.resource) ?? "";
    const consent = await oauth.beginConsent(authRequest);
    return html(
      consentPage({ handle: consent.handle, client: client.clientName ?? authRequest.clientId, redirect: authRequest.redirectUri, pc }),
      200,
      consent.headers,
    );
  }

  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");
  const client = String(form.get("client") ?? "");
  const redirect = String(form.get("redirect") ?? "");
  const shownPc = String(form.get("pc") ?? ""); // only to show the page again; the grant uses the request's resource
  try {
    if (form.get("action") === "deny") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }
    const refused = await checkPassphrase(request, env, String(form.get("passphrase") ?? ""));
    if (refused) return html(consentPage({ handle, client, redirect, pc: shownPc, error: refused.error }), refused.status);
    const approved = await oauth.approveConsent(request, handle, { scope: [SCOPE] });
    const pc = pcOfResource(origin, approved.request.resource) ?? LEGACY_PC;
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: OWNER,
      metadata: { client, pc },
      scope: [SCOPE],
      props: { user: OWNER, pc } satisfies Props,
      // claude.ai signs every account in as the same client (one metadata document, one redirect
      // URI): replacing that client's earlier grants would sign the other accounts out.
      revokeExistingGrants: false,
    });
    try {
      const resource = approved.request.resource ?? resourceOf(origin, LEGACY_PC, true);
      const { failed } = await trimGrants(oauth, resource, pc === LEGACY_PC && resource === resourceOf(origin, LEGACY_PC, true));
      if (failed) console.warn(`could not trim ${failed} old sign-ins`); // the next sign-in tries again
    } catch (error) {
      console.warn("could not trim old sign-ins:", error); // the next sign-in tries again
    }
    const headers = new Headers(approved.headers);
    headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers });
  } catch (error) {
    return authError(error);
  }
}
