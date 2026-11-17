// The computer registry (src/pcs.ts): names, routes, device tokens and one-time pairing codes.
// Real source with an in-memory KV; no network.
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { webcrypto, timingSafeEqual } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';

function load() {
  // Workers add crypto.subtle.timingSafeEqual; Node has the same check as crypto.timingSafeEqual.
  const crypto = {
    getRandomValues: (array) => webcrypto.getRandomValues(array),
    subtle: {
      digest: (...args) => webcrypto.subtle.digest(...args),
      timingSafeEqual: (a, b) => a.byteLength === b.byteLength && timingSafeEqual(Buffer.from(a), Buffer.from(b)),
    },
  };
  const context = { crypto, TextEncoder, btoa, Uint8Array, Array, JSON, Error };
  vm.createContext(context);
  const source = stripTypeScriptTypes(fs.readFileSync(new URL('../src/pcs.ts', import.meta.url), 'utf8')).replace(/^export /gm, '');
  vm.runInContext(source + '\nglobalThis.pcs = { LEGACY_PC, validName, listPcs, deviceTokenFits, pairPc, removePc, newPairCode, pairCodeName, claimPairCode, routeOf, baseOf };', context);
  return context.pcs;
}

function memoryKv() {
  const data = new Map();
  return {
    data,
    async get(key, type) { const v = data.get(key); return v === undefined ? null : type === 'json' ? JSON.parse(v) : v; },
    async put(key, value, options) { data.set(key, value); this.lastOptions = options; },
    async delete(key) { data.delete(key); },
  };
}

const pcs = load();

test('names: lower-case letters, digits and inner hyphens, at most 32', () => {
  for (const ok of ['home', 'a', 'office-2', 'x'.repeat(32)]) assert.ok(pcs.validName(ok), ok);
  for (const bad of ['', 'Home', 'my pc', '-a', 'a-', 'x'.repeat(33), '家里', 'a/b', 'a.b']) assert.ok(!pcs.validName(bad), bad);
});

test('routes: /pc/<name>/... and, only while AGENT_TOKEN is set, the old addresses', () => {
  const plain = (r) => r && { ...r };
  assert.deepEqual(plain(pcs.routeOf('/pc/home/mcp/figma', false)), { pc: 'home', rest: '/mcp/figma', legacy: false });
  assert.deepEqual(plain(pcs.routeOf('/pc/home', false)), { pc: 'home', rest: '/', legacy: false });
  assert.equal(pcs.routeOf('/pc/Bad/mcp', true), null);
  assert.deepEqual(plain(pcs.routeOf('/mcp', true)), { pc: 'default', rest: '/mcp', legacy: true });
  assert.deepEqual(plain(pcs.routeOf('/t/abc', true)), { pc: 'default', rest: '/t/abc', legacy: true });
  assert.equal(pcs.routeOf('/mcp', false), null);
  assert.equal(pcs.routeOf('/agent', false), null);
  assert.equal(pcs.routeOf('/status.json', true), null, 'the top-level status lists every computer');
  assert.equal(pcs.routeOf('/', true), null);
  assert.equal(pcs.baseOf('https://r.example', { pc: 'home', legacy: false }), 'https://r.example/pc/home');
  assert.equal(pcs.baseOf('https://r.example', { pc: 'default', legacy: true }), 'https://r.example');
});

test('device tokens: only the hash is stored; pairing again replaces only that computer', async () => {
  const env = { OAUTH_KV: memoryKv() };
  const home = await pcs.pairPc(env, 'home');
  const office = await pcs.pairPc(env, 'office');
  assert.match(home, /^[A-Za-z0-9_-]{43}$/);
  assert.ok(![...env.OAUTH_KV.data.values()].some((v) => v.includes(home)), 'the token itself is not stored');
  assert.deepEqual([...await pcs.listPcs(env)], ['home', 'office']);
  assert.ok(await pcs.deviceTokenFits(env, 'home', home));
  assert.ok(!(await pcs.deviceTokenFits(env, 'office', home)), "one computer's token does not open another");
  const again = await pcs.pairPc(env, 'home');
  assert.ok(!(await pcs.deviceTokenFits(env, 'home', home)));
  assert.ok(await pcs.deviceTokenFits(env, 'home', again));
  assert.ok(await pcs.deviceTokenFits(env, 'office', office), 'the other computer is untouched');
  await pcs.removePc(env, 'office');
  assert.ok(!(await pcs.deviceTokenFits(env, 'office', office)));
  assert.deepEqual([...await pcs.listPcs(env)], ['home']);
});

test('the old AGENT_TOKEN opens "default" only', async () => {
  const env = { OAUTH_KV: memoryKv(), AGENT_TOKEN: 'a'.repeat(40) };
  assert.deepEqual([...await pcs.listPcs(env)], ['default']);
  assert.ok(await pcs.deviceTokenFits(env, 'default', 'a'.repeat(40)));
  await pcs.pairPc(env, 'home');
  assert.ok(!(await pcs.deviceTokenFits(env, 'home', 'a'.repeat(40))));
  assert.deepEqual([...await pcs.listPcs(env)], ['default', 'home']);
});

test('pairing codes: peeking does not use them up, claiming does, once', async () => {
  const env = { OAUTH_KV: memoryKv() };
  const code = await pcs.newPairCode(env, 'home');
  assert.match(code, /^[A-Za-z0-9_-]{22}$/);
  assert.equal(env.OAUTH_KV.lastOptions.expirationTtl, 600);
  assert.ok(![...env.OAUTH_KV.data.keys()].some((k) => k.includes(code)), 'only the hash of the code is a key');
  assert.equal(await pcs.pairCodeName(env, code), 'home');
  assert.equal(await pcs.pairCodeName(env, code), 'home');
  assert.equal(await pcs.claimPairCode(env, code), 'home');
  assert.equal(await pcs.claimPairCode(env, code), null);
  assert.equal(await pcs.pairCodeName(env, 'not-a-code'), null);
  await assert.rejects(pcs.newPairCode(env, 'Bad Name'));
});
