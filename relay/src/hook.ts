// /hook/<event>: lifecycle hooks of a cloud Claude Code session (for now SessionStart) ask the PC
// for its rules. A separate token (HOOK_TOKEN), sent in its own header, opens these paths and
// nothing else: it cannot reach /mcp, the one-time links or /agent.

export const HOOK_PATH = /^\/hook\/([a-z-]{1,40})$/;
export const HOOK_TOKEN_HEADER = "x-winhand-hook-token";
const HOOK_EVENTS = new Set(["session-start"]);
const MAX_HOOK_BODY = 64 * 1024;
const MAX_HOOK_FAILURES = 20;
const HOOK_FAILURE_WINDOW_S = 15 * 60;

type HookEnv = { HOOK_TOKEN?: string; OAUTH_KV: KVNamespace };
type Forward = (request: Request) => Promise<Response>;

async function sameSecret(given: string, expected: string): Promise<boolean> {
  const encode = (value: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  const [a, b] = await Promise.all([encode(given), encode(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function text(body: string, status: number, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", ...headers } });
}

/** What a SessionStart hook gets when the PC cannot answer: one line, so the model knows. */
function unanswered(status: number): Response {
  return Response.json({
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext:
        `winhand on the PC is offline, did not answer in time, or failed (HTTP ${status}), so this machine's ` +
        "rules were not added at session start. Once it is back, call context(repo=...) through the winhand " +
        "tools to read them.",
    },
  });
}

/** The body, or null once it grows past `limit` bytes (a chunked body has no length to check first). */
async function readLimited(request: Request, limit: number): Promise<Uint8Array | null> {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limit) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    body.set(chunk, at);
    at += chunk.length;
  }
  return body;
}

export async function hookRequest(request: Request, env: HookEnv, event: string, forward: Forward): Promise<Response> {
  if (!env.HOOK_TOKEN || !HOOK_EVENTS.has(event)) return text("not found", 404);
  if (request.method !== "POST") return text("method not allowed", 405, { allow: "POST" });
  const given = request.headers.get(HOOK_TOKEN_HEADER);
  // No token at all: a session whose environment was not set up (a clone of the repository, say),
  // not someone guessing. Answered without counting, so such sessions cost no KV writes.
  if (given === null) return text("no hook token", 401);
  if (!(await sameSecret(given, env.HOOK_TOKEN))) {
    // Wrong tokens are counted per address; the right token is never locked out (it is 256 bits:
    // the count only spares the KV store from a flood of guesses).
    const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
    const failKey = `winhand:hfail:${ip}`;
    const failures = Number((await env.OAUTH_KV.get(failKey)) ?? "0");
    if (failures >= MAX_HOOK_FAILURES) return text("too many failed attempts; try later", 429);
    await env.OAUTH_KV.put(failKey, String(failures + 1), { expirationTtl: HOOK_FAILURE_WINDOW_S });
    return text("bad hook token", 401);
  }
  if (Number(request.headers.get("content-length") ?? "0") > MAX_HOOK_BODY) return text("body too large", 413);
  const body = await readLimited(request, MAX_HOOK_BODY);
  if (body === null) return text("body too large", 413);

  const headers = new Headers(request.headers);
  headers.delete(HOOK_TOKEN_HEADER); // the PC does not need it
  headers.set("x-winhand-user", "hook");
  const target = new URL("https://bridge/forward");
  target.searchParams.set("path", `/hook/${event}`);
  const response = await forward(new Request(target, { method: "POST", headers, body, signal: request.signal }));
  return response.status >= 500 ? unanswered(response.status) : response;
}
