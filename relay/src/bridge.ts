import { DurableObject } from "cloudflare:workers";
import {
  type AgentFrame,
  CHUNK_BYTES,
  forwardableRequestHeaders,
  forwardableResponseHeaders,
  fromBase64,
  type RelayFrame,
  toBase64,
} from "./protocol";

type Pending = {
  socket: WebSocket;
  head: (response: Response) => void;
  fail: (message: string, status?: number) => void;
  writer?: WritableStreamDefaultWriter<Uint8Array>;
  started: boolean;
};

const HEAD_TIMEOUT_MS = 120_000;
// The agent drops off for seconds at a time (winhand restarting or updating, the PC's network
// switching). A request arriving then waits this long for it to come back before failing.
const DEFAULT_GRACE_S = 20;

// One Bridge instance per device. The agent keeps a single WebSocket open to it;
// every authenticated MCP request is pushed down that socket and the agent's
// response is streamed back. The socket uses the hibernation API, so an idle
// connection costs nothing and survives Durable Object eviction.
export class Bridge extends DurableObject<Env> {
  private pending = new Map<string, Pending>();
  private waiters: Array<() => void> = [];

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Keep-alive pings are answered without waking the object.
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  private agent(): WebSocket | undefined {
    return this.ctx.getWebSockets("agent").find((ws) =>
      ws.readyState === WebSocket.OPEN &&
      (ws.deserializeAttachment() as { active?: boolean } | null)?.active !== false
    );
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/connect") return this.acceptAgent(request);
    if (url.pathname === "/status") return Response.json(await this.status());
    return this.forward(request, url.searchParams.get("path") ?? "/mcp", request.headers.get("x-winhand-user") ?? "");
  }

  private async status() {
    const socket = this.agent();
    const hello = socket ? (socket.deserializeAttachment() as Record<string, unknown> | null) : null;
    const offlineSince = socket ? null : ((await this.ctx.storage.get<number>("offlineSince")) ?? null);
    return { online: Boolean(socket), agent: hello, inFlight: this.pending.size, offlineSince };
  }

  private graceMs(): number {
    const configured = Number(this.env.AGENT_GRACE_S);
    return (Number.isFinite(configured) && configured >= 0 ? configured : DEFAULT_GRACE_S) * 1000;
  }

  /** The agent's socket, waiting up to the grace period for it to (re)connect. */
  private async waitForAgent(): Promise<WebSocket | undefined> {
    const socket = this.agent();
    if (socket) return socket;
    await new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== done);
        resolve();
      }, this.graceMs());
      this.waiters.push(done);
    });
    return this.agent();
  }

  private wakeWaiters() {
    const waiting = this.waiters;
    this.waiters = [];
    for (const wake of waiting) wake();
  }

  private async offlineMessage(): Promise<string> {
    const since = await this.ctx.storage.get<number>("offlineSince");
    const when = since ? ` since ${new Date(since).toISOString()} (${describe(Date.now() - since)} ago)` : "";
    return (
      `winhand on the PC is offline${when}; waited ${Math.round(this.graceMs() / 1000)} s for it to reconnect. ` +
      "Likely the PC is asleep or off, winhand is restarting or updating, or its network changed. " +
      "Try again in a minute; once it is back, call history() to see whether earlier calls ran before resending them."
    );
  }

  private acceptAgent(request: Request): Response {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("expected a WebSocket upgrade", { status: 426 });
    }
    // A newer connection from the agent replaces any stale one.
    for (const old of this.ctx.getWebSockets("agent")) {
      try {
        old.serializeAttachment({ ...(old.deserializeAttachment() as object | null), active: false });
        old.close(4000, "replaced by a new agent connection");
      } catch {
        // already closed
      }
    }
    this.failAll("winhand reconnected to the relay while this call was in flight; check history() before resending");
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1], ["agent"]);
    pair[1].serializeAttachment({ connectedAt: new Date().toISOString(), active: true });
    this.ctx.waitUntil(this.ctx.storage.delete("offlineSince"));
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  private send(socket: WebSocket, frame: RelayFrame) {
    socket.send(JSON.stringify(frame));
  }

  private async forward(request: Request, path: string, user: string): Promise<Response> {
    // MCP calls are small JSON-RPC bodies: read them, so a failure can be answered as a JSON-RPC
    // error for the same id, which every client shows to the model. File transfers stream.
    const mcp = !path.startsWith("/transfer/");
    const body = mcp && request.body ? new Uint8Array(await request.arrayBuffer()) : null;
    const reply = errorReply(request.method, mcp ? body : null);

    const socket = await this.waitForAgent();
    if (!socket) return reply(await this.offlineMessage(), 503);

    const id = crypto.randomUUID();
    const headers = forwardableRequestHeaders(request.headers);
    headers.push(["x-winhand-user", user]);
    const response = new Promise<Response>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve(
          reply(
            `winhand on the PC did not answer within ${HEAD_TIMEOUT_MS / 1000} s; the call may still be running there. ` +
              "Check history() before resending it.",
            504,
          ),
        );
      }, HEAD_TIMEOUT_MS);
      this.pending.set(id, {
        socket,
        started: false,
        head: (r) => {
          clearTimeout(timer);
          resolve(r);
        },
        fail: (message, status = 502) => {
          clearTimeout(timer);
          resolve(reply(message, status));
        },
      });
    });

    try {
      this.send(socket, { type: "req", id, method: request.method, path, headers });
      if (body) {
        for (let at = 0; at < body.length; at += CHUNK_BYTES) {
          this.send(socket, { type: "req_body", id, data: toBase64(body.subarray(at, at + CHUNK_BYTES)) });
        }
      } else if (request.body) {
        const reader = request.body.getReader();
        let buffered: Uint8Array = new Uint8Array(0);
        for (;;) {
          const { done, value } = await reader.read();
          if (value) buffered = concat(buffered, value);
          while (buffered.length >= CHUNK_BYTES || (done && buffered.length > 0)) {
            const part = buffered.subarray(0, CHUNK_BYTES);
            buffered = buffered.subarray(part.length);
            this.send(socket, { type: "req_body", id, data: toBase64(part) });
          }
          if (done) break;
        }
      }
      this.send(socket, { type: "req_end", id });
    } catch (error) {
      this.pending.get(id)?.fail(`could not reach winhand on the PC: ${String(error)}`);
      this.pending.delete(id);
    }

    request.signal?.addEventListener("abort", () => {
      if (this.pending.has(id)) {
        this.pending.get(id)?.writer?.close().catch(() => {});
        this.pending.delete(id);
        if (socket.readyState === WebSocket.OPEN) this.send(socket, { type: "cancel", id });
      }
    });
    return response;
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (typeof message !== "string") return;
    let frame: AgentFrame;
    try {
      frame = JSON.parse(message) as AgentFrame;
    } catch {
      return;
    }
    if (frame.type === "hello") {
      ws.serializeAttachment({ ...frame, ...(ws.deserializeAttachment() as object | null) });
      this.wakeWaiters();
      return;
    }
    const entry = this.pending.get(frame.id);
    if (!entry || entry.socket !== ws) return;
    switch (frame.type) {
      case "head": {
        const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
        entry.writer = writable.getWriter();
        entry.started = true;
        entry.head(new Response(readable, { status: frame.status, headers: forwardableResponseHeaders(frame.headers) }));
        break;
      }
      case "body":
        await entry.writer?.write(fromBase64(frame.data)).catch(() => this.pending.delete(frame.id));
        break;
      case "end":
        await entry.writer?.close().catch(() => {});
        this.pending.delete(frame.id);
        break;
      case "error":
        if (entry.started) await entry.writer?.abort(frame.message).catch(() => {});
        else entry.fail(frame.message);
        this.pending.delete(frame.id);
        break;
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    // A socket closed because a newer agent connection replaced it: the PC is not offline.
    // Only the calls sent down this socket fail; calls on the new one carry on.
    if (!this.replaced(ws)) await this.markOffline();
    this.failAll(
      `winhand on the PC disconnected while this call was in flight (${code} ${reason}); ` +
        "check history() once it is back before resending",
      ws,
    );
    try {
      ws.close(code, reason);
    } catch {
      // already closed
    }
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    if (!this.replaced(ws)) await this.markOffline();
    this.failAll(
      "the connection to winhand on the PC failed while this call was in flight; check history() before resending",
      ws,
    );
  }

  /** Whether another agent connection, not yet replaced itself, has taken over from this one. */
  private replaced(closed: WebSocket): boolean {
    return this.ctx.getWebSockets("agent").some(
      (s) => s !== closed && (s.deserializeAttachment() as { active?: boolean } | null)?.active !== false,
    );
  }

  private async markOffline() {
    if ((await this.ctx.storage.get<number>("offlineSince")) === undefined) {
      await this.ctx.storage.put("offlineSince", Date.now());
    }
  }

  private failAll(message: string, socket?: WebSocket) {
    for (const [id, entry] of this.pending) {
      if (socket && entry.socket !== socket) continue;
      if (entry.started) entry.writer?.abort(message).catch(() => {});
      else entry.fail(message);
      this.pending.delete(id);
    }
  }
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  if (a.length === 0) return b;
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

function describe(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

/**
 * How to answer a request the agent could not serve. For a JSON-RPC request the answer is a
 * JSON-RPC error with the request's own id over HTTP 200: MCP clients hand that message to the
 * model, while a bare 5xx often reaches it only as "Service Unavailable".
 */
function errorReply(method: string, body: Uint8Array | null): (message: string, status: number) => Response {
  let ids: unknown[] | null = null;
  let batch = false;
  if (method === "POST" && body && body.length) {
    try {
      const parsed = JSON.parse(new TextDecoder().decode(body)) as unknown;
      batch = Array.isArray(parsed);
      const messages = (batch ? parsed : [parsed]) as Array<Record<string, unknown>>;
      ids = messages
        .filter((m) => m && typeof m === "object" && "method" in m && "id" in m && m.id !== null)
        .map((m) => m.id);
    } catch {
      ids = null;
    }
  }
  return (message, status) => {
    const error = (id: unknown) => ({ jsonrpc: "2.0", id, error: { code: -32000, message } });
    if (ids === null) return Response.json(error(null), { status });
    if (ids.length === 0) return new Response(null, { status: 202 }); // only notifications
    return Response.json(batch ? ids.map(error) : error(ids[0]), { status: 200 });
  };
}
