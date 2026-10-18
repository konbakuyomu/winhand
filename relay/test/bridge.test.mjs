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

function harness(env = {}) {
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
    stored: new Map(),
    storage: {
      async get(key) { return ctx.stored.get(key); },
      async put(key, value) { ctx.stored.set(key, value); },
      async delete(key) { return ctx.stored.delete(key); },
    },
    waitUntil(promise) { promise.catch(() => {}); },
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
    Response: WorkerResponse, Headers, URL, Request, TransformStream, Uint8Array, TextDecoder, TextEncoder, crypto: webcrypto, atob, btoa,
    setTimeout(fn, ms) { const id = nextTimer++; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(sandbox);
  vm.runInContext(protocol + '\n' + bridge + '\nglobalThis.BridgeClass = Bridge;', sandbox);
  const object = new sandbox.BridgeClass(ctx, env);
  return {ctx, object, Socket, timers, recreate: () => new sandbox.BridgeClass(ctx, env)};
}

// The request reaches the socket after the body is read and the agent is found.
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };

const request = (options = {}) => new Request('https://offline.invalid/mcp', {method: 'POST', ...options});
const message = (object, ws, frame) => object.webSocketMessage(ws, JSON.stringify(frame));

async function complete(h, socket, pending, content = 'ok') {
  await settle();
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
  await settle();
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
  assert.deepEqual(h.ctx.sockets[1].sent.filter(f => f.type !== 'welcome'), []);
});

test('SSE abort cancels its captured socket even if another socket is selected', async () => {
  const h = harness(), original = new h.Socket(), other = new h.Socket();
  h.ctx.sockets = [original];
  const abort = new AbortController();
  const pending = h.object.fetch(request({signal: abort.signal}));
  await settle();
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
  await settle();
  const timer = [...h.timers.values()][0];
  assert.equal(timer.ms, 120_000);
  timer.fn();
  assert.equal((await response).status, 504);
  assert.equal(socket.closes.length, 0);
});

const rpc = (id, signal) => request({body: JSON.stringify({jsonrpc: '2.0', id, method: 'tools/call', params: {}}), signal});

test('a call made while the PC is away waits for it to reconnect', async () => {
  const h = harness();
  const response = h.object.fetch(rpc(7));
  await settle();
  const grace = [...h.timers.values()][0];
  assert.equal(grace.ms, 20_000);
  await h.object.fetch(new Request('https://offline.invalid/connect', {headers: {Upgrade: 'websocket'}}));
  const socket = h.ctx.sockets[0];
  await message(h.object, socket, {type: 'hello', hostname: 'pc'});
  assert.equal(socket.attachment.active, true);
  assert.equal(socket.attachment.hostname, 'pc');
  await complete(h, socket, response);
});

test('a call that outlasts the grace period gets a JSON-RPC error and the offline time', async () => {
  const h = harness({AGENT_GRACE_S: '5'}), socket = new h.Socket();
  h.ctx.sockets = [socket];
  await h.object.webSocketClose(socket, 1006, 'gone');
  socket.readyState = 3;
  const since = h.ctx.stored.get('offlineSince');
  assert.equal(typeof since, 'number');
  const response = h.object.fetch(rpc(7));
  await settle();
  const grace = [...h.timers.values()][0];
  assert.equal(grace.ms, 5_000);
  grace.fn();
  const answer = await response;
  assert.equal(answer.status, 200);
  const body = await answer.json();
  assert.equal(body.id, 7);
  assert.match(body.error.message, /offline since .* waited 5 s/);
  const status = await (await h.object.fetch(new Request('https://offline.invalid/status'))).json();
  assert.equal(status.offlineSince, since);
});

test('a session-start hook waits at most 5 s for the PC, then gets a 5xx, not a JSON-RPC answer', async () => {
  const h = harness({AGENT_GRACE_S: '20'});
  const target = new URL('https://bridge/forward');
  target.searchParams.set('path', '/hook/session-start');
  const response = h.object.fetch(new Request(target, {method: 'POST', body: '{"source":"startup"}', headers: {'x-winhand-user': 'hook'}}));
  await settle();
  const grace = [...h.timers.values()][0];
  assert.equal(grace.ms, 5_000);
  grace.fn();
  const answer = await response;
  assert.equal(answer.status, 503);
});

test('a session-start hook waits 15 s for the answer, not 120', async () => {
  const h = harness(), socket = new h.Socket();
  h.ctx.sockets = [socket];
  const target = new URL('https://bridge/forward');
  target.searchParams.set('path', '/hook/session-start');
  const response = h.object.fetch(new Request(target, {method: 'POST', body: '{}', headers: {'x-winhand-user': 'hook'}}));
  await settle();
  const head = [...h.timers.values()].find(t => t.ms === 15_000);
  assert.ok(head, 'a 15 s head timer');
  head.fn();
  assert.equal((await response).status, 504);
});

test('a replaced connection closing does not mark the PC offline', async () => {
  const h = await replacement();
  await h.object.webSocketClose(h.old, 4000, 'replaced by a new agent connection');
  assert.equal(h.ctx.stored.has('offlineSince'), false);
  await h.object.webSocketClose(h.current, 1006, 'gone');
  assert.equal(typeof h.ctx.stored.get('offlineSince'), 'number');
});

async function streaming(contentType, chunk) {
  const h = harness(), socket = new h.Socket();
  h.ctx.sockets = [socket];
  const pending = h.object.fetch(rpc(9));
  await settle();
  const id = socket.sent.findLast(f => f.type === 'req').id;
  await message(h.object, socket, {type: 'head', id, status: 200, headers: [['content-type', contentType]]});
  const response = await pending;
  const body = response.text();
  if (chunk) await message(h.object, socket, {type: 'body', id, data: btoa(chunk)});
  await h.object.webSocketClose(socket, 1006, 'gone');
  return {h, body};
}

test('an event stream cut by a disconnect ends with a JSON-RPC error for the call', async () => {
  const {h, body} = await streaming('text/event-stream', 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n');
  const text = await body;
  const events = text.split('\n\n').filter(Boolean);
  assert.equal(events.length, 2);
  assert.match(events[1], /^event: message\ndata: /);
  const error = JSON.parse(events[1].split('data: ')[1]);
  assert.equal(error.id, 9);
  assert.match(error.error.message, /disconnected while this call was in flight/);
  assert.equal(h.object.pending.size, 0);
});

test('a stream cut inside an event closes that event before the error', async () => {
  const {body} = await streaming('text/event-stream', 'event: message\ndata: {"partial');
  const text = await body;
  assert.match(text, /\{"partial\n\nevent: message\ndata: \{"jsonrpc":"2.0","id":9,"error"/);
});

test('a plain streamed response is still cut off', async () => {
  const {body} = await streaming('application/octet-stream', 'abc');
  await assert.rejects(body);
});

// ---------------------------------------------------------------- resuming calls across a drop

async function resumable(contentType = 'text/event-stream') {
  const h = harness(), first = new h.Socket(); h.ctx.sockets = [first];
  await message(h.object, first, {type: 'hello', hostname: 'pc', resume: 1, pending: []});
  const pending = h.object.fetch(rpc(9));
  await settle();
  const id = first.sent.findLast(f => f.type === 'req').id;
  await message(h.object, first, {type: 'head', id, status: 200, headers: [['content-type', contentType]]});
  const response = await pending;
  const body = response.text();
  await message(h.object, first, {type: 'body', id, data: btoa('event: message\ndata: one\n\n')});
  await h.object.webSocketClose(first, 1006, 'gone');
  return {h, id, body, first};
}

async function reconnect(h) {
  await h.object.fetch(new Request('https://offline.invalid/connect', {headers: {Upgrade: 'websocket'}}));
  return h.ctx.sockets.at(-1);
}

test('welcome is the first frame on every agent connection', async () => {
  const h = harness();
  const socket = await reconnect(h);
  assert.deepEqual(socket.sent[0], {type: 'welcome', resume: 1});
});

test('a call cut off by a drop waits, then resumes where the relay stopped', async () => {
  const {h, id, body} = await resumable();
  assert.equal(h.object.pending.size, 1, 'kept, not failed');
  assert.equal(h.object.pending.get(id).socket, null);
  const second = await reconnect(h);
  await message(h.object, second, {type: 'hello', hostname: 'pc', resume: 1, pending: [id]});
  assert.deepEqual(second.sent.filter(f => f.type === 'resume'), [{type: 'resume', id, from: 2}]);
  await message(h.object, second, {type: 'body', id, data: btoa('event: message\ndata: two\n\n')});
  await message(h.object, second, {type: 'end', id});
  assert.equal(await body, 'event: message\ndata: one\n\nevent: message\ndata: two\n\n');
  assert.deepEqual(second.sent.filter(f => f.type === 'ack'), [{type: 'ack', id}]);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0, 'the orphan timer was cleared');
});

test('an orphaned call fails when the agent does not come back in the grace period', async () => {
  const {h, body} = await resumable();
  const [timer] = [...h.timers.values()];
  assert.equal(timer.ms, 20_000);
  timer.fn();
  const text = await body;
  assert.match(text, /disconnected while this call was in flight/);
  assert.equal(h.object.pending.size, 0);
});

test('a returning agent that no longer has the call fails it, and cancels calls nobody waits for', async () => {
  const {h, body} = await resumable();
  const second = await reconnect(h);
  await message(h.object, second, {type: 'hello', hostname: 'pc', resume: 1, pending: ['ghost']});
  assert.match(await body, /lost this call while it was disconnected/);
  assert.deepEqual(second.sent.filter(f => f.type === 'cancel'), [{type: 'cancel', id: 'ghost'}]);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
});

test('an agent without resume support still fails its calls at once', async () => {
  const h = harness(), socket = new h.Socket(); h.ctx.sockets = [socket];
  await message(h.object, socket, {type: 'hello', hostname: 'old agent'});
  const response = h.object.fetch(rpc(3));
  await settle();
  await h.object.webSocketClose(socket, 1006, 'gone');
  const answer = await (await response).json();
  assert.match(answer.error.message, /disconnected while this call was in flight/);
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
});

test('a replacement connection keeps the calls of a resuming agent for it to resume', async () => {
  const h = harness(), first = new h.Socket(); h.ctx.sockets = [first];
  await message(h.object, first, {type: 'hello', hostname: 'pc', resume: 1, pending: []});
  const pending = h.object.fetch(rpc(4));
  await settle();
  const id = first.sent.findLast(f => f.type === 'req').id;
  const second = await reconnect(h);  // the old socket is replaced before it closed
  assert.equal(h.object.pending.get(id).socket, null);
  await message(h.object, first, {type: 'head', id, status: 200, headers: []});  // late frame from the old one
  assert.equal(h.object.pending.get(id).frames, 0, 'frames from a replaced connection do not count');
  await message(h.object, second, {type: 'hello', hostname: 'pc', resume: 1, pending: [id]});
  assert.deepEqual(second.sent.filter(f => f.type === 'resume'), [{type: 'resume', id, from: 0}]);
  await message(h.object, second, {type: 'head', id, status: 200, headers: []});
  const response = await pending;
  const body = response.text();
  await message(h.object, second, {type: 'body', id, data: btoa('done')});
  await message(h.object, second, {type: 'end', id});
  assert.equal(await body, 'done');
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 0);
});

test('a caller giving up on an orphaned call gets it cancelled when the agent is back', async () => {
  const h = harness(), first = new h.Socket(); h.ctx.sockets = [first];
  await message(h.object, first, {type: 'hello', hostname: 'pc', resume: 1, pending: []});
  const abort = new AbortController();
  const pending = h.object.fetch(rpc(5, abort.signal));
  await settle();
  const id = first.sent.findLast(f => f.type === 'req').id;
  await h.object.webSocketClose(first, 1006, 'gone');
  abort.abort();
  assert.equal(h.object.pending.size, 0);
  assert.equal(h.timers.size, 1, 'only the head timeout of the abandoned call is left');
  const second = await reconnect(h);
  await message(h.object, second, {type: 'hello', hostname: 'pc', resume: 1, pending: [id]});
  assert.deepEqual(second.sent.filter(f => f.type === 'cancel'), [{type: 'cancel', id}]);
  void pending;
});
