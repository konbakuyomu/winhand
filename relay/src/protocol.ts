// Frames exchanged between the relay (Durable Object) and the winhand agent over
// one WebSocket. The relay is a transparent HTTP tunnel: it never parses MCP, it
// forwards each HTTP request to the agent's local MCP endpoint and streams the
// response (including SSE) back. Bodies travel base64-encoded in chunks that
// stay well below the 1 MiB WebSocket message limit.

export const CHUNK_BYTES = 256 * 1024;

// relay -> agent
export type RequestHead = {
  type: "req";
  id: string;
  method: string;
  path: string; // path + query, e.g. "/mcp"
  headers: [string, string][];
};
export type RequestBody = { type: "req_body"; id: string; data: string };
export type RequestEnd = { type: "req_end"; id: string };
export type Cancel = { type: "cancel"; id: string };
// Resuming calls across a dropped agent connection. `welcome` goes out first on every connection:
// this relay can resume. A `hello` with resume: 1 lists in `pending` the calls the agent still
// serves; each gets `resume` (send again from frame `from`, counting the head as frame 0) or
// `cancel` (the relay gave up on it). `ack` confirms a finished answer, so the agent can drop it.
export type Welcome = { type: "welcome"; resume: 1 };
export type Resume = { type: "resume"; id: string; from: number };
export type Ack = { type: "ack"; id: string };

// agent -> relay
export type ResponseHead = { type: "head"; id: string; status: number; headers: [string, string][] };
export type ResponseBody = { type: "body"; id: string; data: string };
export type ResponseEnd = { type: "end"; id: string };
export type ResponseError = { type: "error"; id: string; message: string };
export type Hello = {
  type: "hello";
  version: string;
  hostname: string;
  platform: string;
  resume?: number;
  pending?: string[];
};

export type AgentFrame = ResponseHead | ResponseBody | ResponseEnd | ResponseError | Hello;
export type RelayFrame = RequestHead | RequestBody | RequestEnd | Cancel | Welcome | Resume | Ack;

export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

export function fromBase64(data: string): Uint8Array {
  const binary = atob(data);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

// Headers that must not cross the tunnel: hop-by-hop, the caller's OAuth token
// (already verified by the relay) and Cloudflare/proxy metadata.
const DROP_REQUEST = new Set([
  "authorization",
  "host",
  "origin",
  "connection",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
  "content-length",
  "cookie",
  "x-forwarded-for",
  "x-forwarded-proto",
  "x-real-ip",
  "cf-connecting-ip",
  "cf-ipcountry",
  "cf-ray",
  "cf-visitor",
  "cf-worker",
]);
const DROP_RESPONSE = new Set(["connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"]);

export function forwardableRequestHeaders(headers: Headers): [string, string][] {
  const out: [string, string][] = [];
  headers.forEach((value, key) => {
    if (!DROP_REQUEST.has(key.toLowerCase())) out.push([key, value]);
  });
  return out;
}

export function forwardableResponseHeaders(pairs: [string, string][]): Headers {
  const headers = new Headers();
  for (const [key, value] of pairs) {
    if (!DROP_RESPONSE.has(key.toLowerCase())) headers.append(key, value);
  }
  return headers;
}
