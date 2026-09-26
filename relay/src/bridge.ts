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
  head: (response: Response) => void;
  fail: (message: string, status?: number) => void;
  writer?: WritableStreamDefaultWriter<Uint8Array>;
  started: boolean;
};

const HEAD_TIMEOUT_MS = 120_000;

// One Bridge instance per device. The agent keeps a single WebSocket open to it;
// every authenticated MCP request is pushed down that socket and the agent's
// response is streamed back. The socket uses the hibernation API, so an idle
// connection costs nothing and survives Durable Object eviction.
export class Bridge extends DurableObject<Env> {
  private pending = new Map<string, Pending>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Keep-alive pings are answered without waking the object.
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  private agent(): WebSocket | undefined {
    return this.ctx.getWebSockets("agent")[0];
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
    return { online: Boolean(socket), agent: hello, inFlight: this.pending.size };
  }

  private acceptAgent(request: Request): Response {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("expected a WebSocket upgrade", { status: 426 });
    }
    // A newer connection from the agent replaces any stale one.
    for (const old of this.ctx.getWebSockets("agent")) {
      try {
        old.close(4000, "replaced by a new agent connection");
      } catch {
        // already closed
      }
    }
    this.failAll("agent reconnected");
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1], ["agent"]);
    pair[1].serializeAttachment({ connectedAt: new Date().toISOString() });
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  private send(socket: WebSocket, frame: RelayFrame) {
    socket.send(JSON.stringify(frame));
  }

  private async forward(request: Request, path: string, user: string): Promise<Response> {
    const socket = this.agent();
    if (!socket) return offline("winhand agent is not connected; start `winhand connect` on the machine");

    const id = crypto.randomUUID();
    const headers = forwardableRequestHeaders(request.headers);
    headers.push(["x-winhand-user", user]);
    const response = new Promise<Response>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve(offline("winhand agent did not answer in time", 504));
      }, HEAD_TIMEOUT_MS);
      this.pending.set(id, {
        started: false,
        head: (r) => {
          clearTimeout(timer);
          resolve(r);
        },
        fail: (message, status = 502) => {
          clearTimeout(timer);
          resolve(offline(message, status));
        },
      });
    });

    try {
      this.send(socket, { type: "req", id, method: request.method, path, headers });
      if (request.body) {
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
      this.pending.get(id)?.fail(`could not reach the agent: ${String(error)}`);
      this.pending.delete(id);
    }

    request.signal?.addEventListener("abort", () => {
      if (this.pending.has(id)) {
        this.pending.get(id)?.writer?.close().catch(() => {});
        this.pending.delete(id);
        const live = this.agent();
        if (live) this.send(live, { type: "cancel", id });
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
      ws.serializeAttachment({ ...frame, connectedAt: new Date().toISOString() });
      return;
    }
    const entry = this.pending.get(frame.id);
    if (!entry) return;
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
    this.failAll(`agent disconnected (${code} ${reason})`);
    try {
      ws.close(code, reason);
    } catch {
      // already closed
    }
  }

  async webSocketError(): Promise<void> {
    this.failAll("agent connection error");
  }

  private failAll(message: string) {
    for (const [id, entry] of this.pending) {
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

// MCP clients show JSON-RPC errors to the model, which is more useful than a bare 502.
function offline(message: string, status = 503): Response {
  return Response.json({ jsonrpc: "2.0", id: null, error: { code: -32000, message } }, { status });
}
