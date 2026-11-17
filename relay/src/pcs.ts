// The computers this relay serves. Each has a name, its own device token (the relay keeps only
// its SHA-256) and its own address space /pc/<name>/. A relay that still has the AGENT_TOKEN
// secret also serves the computer of the single-computer days, named "default", at the old
// addresses (/mcp, /agent, /t/..., /hook/..., /status.json).

export const LEGACY_PC = "default";

// Lower-case letters, digits and inner hyphens, at most 32: it goes into connector URLs as is.
const NAME = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;
const LIST_KEY = "winhand:pcs";
const pcKey = (name: string) => `winhand:pc:${name}`;
const pairKey = (codeHash: string) => `winhand:pair:${codeHash}`;
export const PAIR_CODE_TTL_S = 600;

type PcEnv = { OAUTH_KV: KVNamespace; AGENT_TOKEN?: string };
type PcRecord = { tokenHash: string; paired: number };

export function validName(name: string): boolean {
  return NAME.test(name);
}

function base64url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomToken(bytes: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

async function sha256(value: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
}

async function sha256Hex(value: string): Promise<string> {
  return [...new Uint8Array(await sha256(value))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison of two secrets of any length (compares their digests). */
export async function secretEquals(given: string, expected: string | undefined): Promise<boolean> {
  if (!expected) return false;
  const [a, b] = await Promise.all([sha256(given), sha256(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

async function storedNames(env: PcEnv): Promise<string[]> {
  const stored = await env.OAUTH_KV.get<unknown>(LIST_KEY, "json");
  return Array.isArray(stored) ? stored.filter((name): name is string => typeof name === "string" && validName(name)) : [];
}

/** Every computer, in the order they were first paired; "default" first while AGENT_TOKEN is set. */
export async function listPcs(env: PcEnv): Promise<string[]> {
  const names = await storedNames(env);
  if (env.AGENT_TOKEN && !names.includes(LEGACY_PC)) names.unshift(LEGACY_PC);
  return names;
}

/** Whether `token` is this computer's device token. */
export async function deviceTokenFits(env: PcEnv, name: string, token: string): Promise<boolean> {
  if (!token || !validName(name)) return false;
  if (name === LEGACY_PC && env.AGENT_TOKEN && (await secretEquals(token, env.AGENT_TOKEN))) return true;
  const record = await env.OAUTH_KV.get<PcRecord>(pcKey(name), "json");
  if (!record?.tokenHash) return false;
  return secretEquals(await sha256Hex(token), record.tokenHash);
}

/** Give a computer a new device token (adding it when new) and return the token, shown only now. */
export async function pairPc(env: PcEnv, name: string): Promise<string> {
  if (!validName(name)) throw new Error(`invalid computer name: ${name}`);
  const token = randomToken(32);
  const record: PcRecord = { tokenHash: await sha256Hex(token), paired: Date.now() };
  await env.OAUTH_KV.put(pcKey(name), JSON.stringify(record));
  const names = await storedNames(env);
  if (!names.includes(name)) await env.OAUTH_KV.put(LIST_KEY, JSON.stringify([...names, name]));
  return token;
}

/** Forget a computer: its token stops working. "default" stays while AGENT_TOKEN is set. */
export async function removePc(env: PcEnv, name: string): Promise<void> {
  await env.OAUTH_KV.delete(pcKey(name));
  const names = await storedNames(env);
  if (names.includes(name)) await env.OAUTH_KV.put(LIST_KEY, JSON.stringify(names.filter((n) => n !== name)));
}

/** A one-time pairing code for `name`, valid PAIR_CODE_TTL_S. Only its hash is stored. */
export async function newPairCode(env: PcEnv, name: string): Promise<string> {
  if (!validName(name)) throw new Error(`invalid computer name: ${name}`);
  const code = randomToken(16);
  await env.OAUTH_KV.put(pairKey(await sha256Hex(code)), name, { expirationTtl: PAIR_CODE_TTL_S });
  return code;
}

/** The computer a pairing code is for, without using the code up (null: unknown or expired). */
export async function pairCodeName(env: PcEnv, code: string): Promise<string | null> {
  if (!/^[A-Za-z0-9_-]{22}$/.test(code)) return null;
  return env.OAUTH_KV.get(pairKey(await sha256Hex(code)));
}

/** Use a pairing code up: the computer it is for, or null. */
export async function claimPairCode(env: PcEnv, code: string): Promise<string | null> {
  const name = await pairCodeName(env, code);
  if (name === null) return null;
  await env.OAUTH_KV.delete(pairKey(await sha256Hex(code)));
  return validName(name) ? name : null;
}

export type Route = { pc: string; rest: string; legacy: boolean };

const PC_PATH = /^\/pc\/([^/]+)(\/.*)?$/;
// /status.json is not here: the top-level one lists every computer (and keeps the old fields).
const LEGACY_PATH = /^\/(?:mcp(?:\/.*)?|agent|t\/[^/]+|hook\/[^/]+)$/;

/**
 * Which computer a path belongs to and the rest of the path below its address space:
 * /pc/home/mcp/figma -> { pc: "home", rest: "/mcp/figma" }. The old single-computer paths (/mcp,
 * /agent, /t/..., /hook/...) map to "default" only while `legacy` (AGENT_TOKEN is set). null: not a
 * computer's path.
 */
export function routeOf(pathname: string, legacy: boolean): Route | null {
  const match = pathname.match(PC_PATH);
  if (match) return validName(match[1]) ? { pc: match[1], rest: match[2] ?? "/", legacy: false } : null;
  if (legacy && LEGACY_PATH.test(pathname)) return { pc: LEGACY_PC, rest: pathname, legacy: true };
  return null;
}

/** The public base of a computer's address space: links and hooks hang below it. */
export function baseOf(origin: string, route: Pick<Route, "pc" | "legacy">): string {
  return route.legacy ? origin : `${origin}/pc/${route.pc}`;
}
