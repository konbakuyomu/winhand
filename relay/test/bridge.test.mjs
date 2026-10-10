// Real Bridge source; fake DO/WS/timers. No network, credentials or Worker deployment.
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { webcrypto } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = (name) => fs.readFileSync(new URL('../src/' + name, import.meta.url), 'utf8');
const protocol = stripTypeScriptTypes(source('protocol.ts')).replace(/^export /gm, '');
const bridge = stripTypeScriptTypes(source('bridge.ts'))
  .replace(/^import[\s\S]*?from\s+["'][^"']+["'];\s*/gm, '').replace(/^export /gm, '');

function harness() {
  const timers = new Map();
  let nextTimer = 1;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    closes = [];
    attachment = {};
    failSend = false;
    send(data) {
      if (this.readyState !== 1 || this.failSend) throw new Error('fake socket not writable');
      this.sent.push(JSON.parse(data));
    }
    close(code, reason) { this.closes.push({ code, reason }); this.readyState = 2; }
    serializeAttachment(value) { this.attachment = value; }
    deserializeAttachment() { return this.attachment; }
  }
  const ctx = {
    sockets: [],
    getWebSockets() { return this.sockets; },
    acceptWebSocket(ws) { this.sockets.push(ws); },
    setWebSocketAutoResponse(pair) { this.autoResponse = pair; },
  };
  function WorkerResponse(body, options) {
    if (options?.status === 101) return { status: 101, webSocket: options.webSocket };
    return new Response(body, options);
  }
  WorkerResponse.json = Response.json.bind(Response);
  const sandbox = {
    DurableObject: class { constructor(ctx, env) { this.ctx = ctx; this.env = env; } },
    WebSocket: Socket,
    WebSocketPair: class { constructor() { this[0] = new Socket(); this[1] = new Socket(); } },
    WebSocketRequestResponsePair: class { constructor(request, response) { Object.assign(this, {request, response}); } },
    Response: WorkerResponse, Headers, URL, Request, TransformStream, Uint8Array, crypto: webcrypto, atob, btoa,
    setTimeout(fn, ms) { const id = nextTimer++; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(sandbox);
  vm.runInContext(protocol + '\n' + bridge + '\nglobalThis.BridgeClass = Bridge;', sandbox);
  const object = new sandbox.BridgeClass(ctx, {});
  return {ctx, object, Socket, timers, recreate: () => new sandbox.BridgeClass(ctx, {})};
}

const request = (options = {}) => new Request('https://offline.invalid/mcp', {method: 'POST', ...options});
const message = (object, ws, frame) => object.webSocketMessage(ws, JSON.stringify(frame));

async function complete(h, socket, pending, content = 'ok') {
  const id = socket.sent.findLast(f => f.type === 'req').id;
  await message(h.object, socket, { type: 'head', id, status: 200, headers: [] });
  const response = await pending;
  assert.equal(response.status, 200);
  const body = response.text();
  for (let offset = 0; offset < content.length; offset += 256 * 1024) {
    const data = btoa(content.slice(offset, offset + 256 * 1024));
    await message(h.object, socket, {type: 'body', id, data});
  }
  await message(h.object, socket, {type: 'end', id});
  assert.equal(await body, content);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
}

for (const event of ['close', 'error']) {
  test('stale ' + event + ' preserves the new connection request', async () => {
    const h = harness(), old = new h.Socket(), current = new h.Socket();
    h.ctx.sockets = [current];
    const response = h.object.fetch(request());
    if (event === 'close') await h.object.webSocketClose(old, 1006, 'stale');
    else await h.object.webSocketError(old);
    assert.equal(h.object.pending.size, 1);
    assert.equal(current.closes.length, 0);
    await complete(h, current, response);
  });
}

async function replacement() {
  const h = harness(), old = new h.Socket();
  h.ctx.sockets = [old];
  await h.object.fetch(new Request('https://offline.invalid/connect', {headers: {Upgrade: 'websocket'}}));
  return {...h, old, current: h.ctx.sockets[1]};
}

test('CLOSING old socket cannot be selected over its OPEN replacement', async () => {
  const h = await replacement();
  assert.equal(h.old.readyState, 2);
  assert.equal(h.object.agent(), h.current);
  await complete(h, h.current, h.object.fetch(request()));
});

test('late hello cannot reactivate a retired connection, including after hibernation', async () => {
  const h = await replacement();
  h.old.readyState = 1; // closing failed/delayed; attachment must still exclude it
  await message(h.object, h.old, {type: 'hello', hostname: 'old', active: true});
  assert.equal(h.old.attachment.active, false);
  assert.equal(h.recreate().agent(), h.current);
});

test('pre-upgrade OPEN attachment remains usable and CLOSING alone is offline', async () => {
  const h = harness(), socket = new h.Socket();
  h.ctx.sockets = [socket];
  socket.attachment = {type: 'hello', connectedAt: 'old deployment'};
  assert.equal(h.object.agent(), socket);
  socket.readyState = 2;
  assert.equal(h.object.agent(), undefined);
  const status = await (await h.object.fetch(new Request('https://offline.invalid/status'))).json();
  assert.equal(status.online, false);
});

test('a response from another connection cannot satisfy a current request', async () => {
  const h = harness(), old = new h.Socket(), current = new h.Socket();
  h.ctx.sockets = [current];
  const response = h.object.fetch(request());
  const id = current.sent[0].id;
  await message(h.object, old, {type: 'head', id, status: 200, headers: []});
  assert.equal(h.object.pending.get(id).started, false);
  await complete(h, current, response);
});

test('current close fails its own request and clears the head timer', async () => {
  const h = harness(), socket = new h.Socket(); h.ctx.sockets = [socket];
  const response = h.object.fetch(request());
  await h.object.webSocketClose(socket, 1006, 'current');
  assert.equal((await response).status, 502);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
});

test('a request send error does not close the tunnel', async () => {
  const h = harness(), socket = new h.Socket(); socket.failSend = true; h.ctx.sockets = [socket];
  assert.equal((await h.object.fetch(request())).status, 502);
  assert.equal(socket.closes.length, 0);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
});

test('large normal response is streamed in bounded chunks', async () => {
  const h = harness(), socket = new h.Socket(); h.ctx.sockets = [socket];
  await complete(h, socket, h.object.fetch(request()), 'x'.repeat(700_000));
  assert.equal(socket.closes.length, 0);
});

test('old request abort cannot send cancel to the replacement', async () => {
  const h = harness(), old = new h.Socket(); h.ctx.sockets = [old];
  const abort = new AbortController();
  const response = h.object.fetch(request({signal: abort.signal}));
  await h.object.fetch(new Request('https://offline.invalid/connect', {headers: {Upgrade: 'websocket'}}));
  assert.equal((await response).status, 502);
  abort.abort();
  assert.equal(h.ctx.sockets[1].sent.length, 0);
});

test('SSE abort cancels its captured socket even if another socket is selected', async () => {
  const h = harness(), original = new h.Socket(), other = new h.Socket();
  h.ctx.sockets = [original];
  const abort = new AbortController();
  const pending = h.object.fetch(request({signal: abort.signal}));
  const id = original.sent.findLast(f => f.type === 'req').id;
  await message(h.object, original, {type: 'head', id, status: 200, headers: [['content-type', 'text/event-stream']]});
  const response = await pending;
  const body = response.text();
  assert.equal(h.object.pending.size, 1);
  h.ctx.sockets = [other, original];
  abort.abort();
  assert.equal(await body, '');
  assert.equal(original.sent.filter(f => f.type === 'cancel' && f.id === id).length, 1);
  assert.equal(other.sent.length, 0);
  assert.equal(h.object.pending.size, 0);
});

test('head timeout remains per request, with no socket close', async () => {
  const h = harness(), socket = new h.Socket(); h.ctx.sockets = [socket];
  const response = h.object.fetch(request());
  const timer = [...h.timers.values()][0];
  assert.equal(timer.ms, 120_000);
  timer.fn();
  assert.equal((await response).status, 504);
  assert.equal(socket.closes.length, 0);
});
