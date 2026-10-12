// Real hook.ts source; a fake KV and a fake tunnel. No network or Worker deployment.
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { webcrypto } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = stripTypeScriptTypes(fs.readFileSync(new URL('../src/hook.ts', import.meta.url), 'utf8'))
  .replace(/^export /gm, '');
const sandbox = { Response, Request, Headers, URL, TextEncoder, Uint8Array, Number, String, Set, Promise, Infinity,
  crypto: { subtle: Object.assign(Object.create(webcrypto.subtle), {
    digest: (...a) => webcrypto.subtle.digest(...a),
    // the Workers runtime has timingSafeEqual; node's webcrypto does not
    timingSafeEqual(a, b) {
      const x = new Uint8Array(a), y = new Uint8Array(b);
      return x.length === y.length && x.every((v, i) => v === y[i]);
    },
  }) } };
vm.createContext(sandbox);
vm.runInContext(source + '\nglobalThis.exported = { hookRequest, HOOK_PATH };', sandbox);
const { hookRequest, HOOK_PATH } = sandbox.exported;

function kv() {
  const stored = new Map();
  return { stored, async get(k) { return stored.get(k) ?? null; }, async put(k, v) { stored.set(k, v); } };
}

function call(env, { method = 'POST', token = 'right-token', body = '{"source":"startup"}', headers = {}, event = 'session-start' } = {}) {
  const forwarded = [];
  const reply = env.reply ?? (() => new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } }));
  const h = { 'cf-connecting-ip': '203.0.113.9', ...headers };
  if (token !== null) h['x-winhand-hook-token'] = token;
  const request = new Request('https://relay.example/hook/' + event, { method, headers: h, body: method === 'GET' ? undefined : body });
  return hookRequest(request, env, event, async (r) => { forwarded.push(r); return reply(r); }).then((response) => ({ response, forwarded }));
}

test('the path pattern only takes lowercase event names', () => {
  assert.equal(HOOK_PATH.exec('/hook/session-start')[1], 'session-start');
  assert.equal(HOOK_PATH.exec('/hook/../mcp'), null);
  assert.equal(HOOK_PATH.exec('/hook/'), null);
});

test('without HOOK_TOKEN the paths do not exist', async () => {
  const { response, forwarded } = await call({ OAUTH_KV: kv() });
  assert.equal(response.status, 404);
  assert.equal(forwarded.length, 0);
});

test('an event other than session-start is not found', async () => {
  const { response } = await call({ HOOK_TOKEN: 'right-token', OAUTH_KV: kv() }, { event: 'stop' });
  assert.equal(response.status, 404);
});

test('only POST', async () => {
  const { response } = await call({ HOOK_TOKEN: 'right-token', OAUTH_KV: kv() }, { method: 'GET' });
  assert.equal(response.status, 405);
  assert.equal(response.headers.get('allow'), 'POST');
});

test('a missing token is refused without being counted', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv() };
  for (let i = 0; i < 30; i++) assert.equal((await call(env, { token: null })).response.status, 401);
  assert.equal(env.OAUTH_KV.stored.size, 0);
});

test('wrong tokens are counted; after 20 they get 429, the right token still gets in', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv() };
  for (let i = 0; i < 20; i++) assert.equal((await call(env, { token: 'wrong' })).response.status, 401);
  assert.equal((await call(env, { token: 'wrong' })).response.status, 429);
  const owner = await call(env, { token: 'right-token' });
  assert.equal(owner.response.status, 200);
  assert.equal(owner.forwarded.length, 1);
});

test('the right token forwards as the hook identity, without the token', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv() };
  const { response, forwarded } = await call(env, { headers: { 'x-winhand-user': 'owner', 'x-winhand-repo': 'o/r' } });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  const [sent] = forwarded;
  assert.equal(new URL(sent.url).searchParams.get('path'), '/hook/session-start');
  assert.equal(sent.headers.get('x-winhand-user'), 'hook');
  assert.equal(sent.headers.get('x-winhand-hook-token'), null);
  assert.equal(sent.headers.get('x-winhand-repo'), 'o/r');
  assert.equal(await sent.text(), '{"source":"startup"}');
});

test('a body over 64 KiB is refused', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv() };
  const { response, forwarded } = await call(env, { body: 'x'.repeat(64 * 1024 + 1) });
  assert.equal(response.status, 413);
  assert.equal(forwarded.length, 0);
});

test('a chunked body over 64 KiB is refused while reading', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv() };
  let sent = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (sent > 200 * 1024) return controller.close();
      sent += 16 * 1024;
      controller.enqueue(new Uint8Array(16 * 1024));
    },
  });
  const request = new Request('https://relay.example/hook/session-start', {
    method: 'POST', body: stream, duplex: 'half', headers: { 'x-winhand-hook-token': 'right-token' },
  });
  const forwarded = [];
  const response = await hookRequest(request, env, 'session-start', async (r) => { forwarded.push(r); return new Response('x'); });
  assert.equal(response.status, 413);
  assert.equal(forwarded.length, 0);
  assert.ok(sent < 200 * 1024, 'stopped reading early');
});

test('when the PC cannot answer, the session gets one line saying so', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv(), reply: () => new Response('offline', { status: 503 }) };
  const { response } = await call(env);
  assert.equal(response.status, 200);
  const out = await response.json();
  assert.equal(out.hookSpecificOutput.hookEventName, 'SessionStart');
  assert.match(out.hookSpecificOutput.additionalContext, /offline, did not answer in time, or failed \(HTTP 503\)/);
});

test('a refusal from the PC passes through', async () => {
  const env = { HOOK_TOKEN: 'right-token', OAUTH_KV: kv(), reply: () => new Response('forbidden', { status: 403 }) };
  assert.equal((await call(env)).response.status, 403);
});
