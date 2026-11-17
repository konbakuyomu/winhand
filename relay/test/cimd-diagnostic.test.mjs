import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import assert from 'node:assert/strict';
import test from 'node:test';

const url = 'https://claude.ai/oauth/mcp-oauth-client-metadata';
function load(fetch) {
  const logs = [];
  const context = { fetch, Response, Request, Headers, URL, AbortController, setTimeout, clearTimeout,
    console: { warn: (...args) => logs.push(args) } };
  vm.createContext(context);
  const source = stripTypeScriptTypes(fs.readFileSync(new URL('../src/cimd-diagnostic.ts', import.meta.url), 'utf8')).replace(/^export /gm, '');
  vm.runInContext(source + '\nglobalThis.probe = diagnoseClaudeMetadata;', context);
  return { context, logs, probe: context.probe };
}
test('only fixed public Claude endpoint is probed; cooldown avoids repeated traffic', async () => {
  let calls = 0;
  const { probe, logs } = load(async (target, init) => {
    calls++;
    assert.equal(target, url);
    assert.equal(init.redirect, 'manual');
    assert.equal(init.headers.Accept, 'application/json');
    return new Response('{}', { headers: { 'content-type': 'application/json' } });
  });
  await probe('https://untrusted.example/metadata?secret=never');
  assert.equal(calls, 0);
  await probe(url); await probe(url);
  assert.equal(calls, 1);
  assert.equal(logs[0][1].contentType, 'json');
});
test('oversized HTML is classified without logging body or arbitrary headers', async () => {
  const { probe, logs } = load(async () => new Response('PRIVATE'.repeat(2000), { headers: { 'content-type': 'text/html; secret=PRIVATE' } }));
  await probe(url);
  assert.equal(logs[0][1].contentType, 'html');
  assert.equal(logs[0][1].bytesObserved, 5121);
  assert.equal(logs[0][1].exceeded5120, true);
  assert.ok(!JSON.stringify(logs).includes('PRIVATE'));
});
test('network error does not leak exception or escape', async () => {
  const { probe, logs } = load(async () => { throw Error('PRIVATE'); });
  await probe(url);
  assert.equal(logs[0][1].failed, true);
  assert.ok(!JSON.stringify(logs).includes('PRIVATE'));
});
test('redirect is classified without following it or exposing query values', async () => {
  const { probe, logs } = load(async () => new Response(null, { status: 302, headers: { location: 'https://claude.ai/unsupported-country?secret=PRIVATE' } }));
  await probe(url);
  assert.equal(logs[0][1].status, 302);
  assert.equal(logs[0][1].redirectClass, 'unsupported-country');
  assert.ok(!JSON.stringify(logs).includes('PRIVATE'));
});
test('CIMD exception becomes no-store 503, without issuing credentials', async () => {
  const { context } = load(async () => new Response('{}'));
  context.CimdFetchError = class extends Error { constructor() { super('PRIVATE'); this.metadataUrl = url; } };
  context.AuthorizationError = class extends Error {};
  Object.assign(context, { OAuthAuthorizationServer: class {}, OAuthResourceServer: class {}, consentPage: () => '',
    LEGACY_PC: 'default', secretEquals: async () => false, validName: () => true });
  const source = stripTypeScriptTypes(fs.readFileSync(new URL('../src/auth.ts', import.meta.url), 'utf8'))
    .replace(/^import[\s\S]*?from [^;]+;\s*/gm, '').replace(/^export /gm, '');
  vm.runInContext(source + '\nglobalThis.authorizeFn = authorize;', context);
  const server = { getOAuthApi: () => ({ parseAuthRequest() { throw new context.CimdFetchError(); } }) };
  const response = await context.authorizeFn(new Request('https://relay.example/authorize'), {}, server, 'https://relay.example');
  assert.equal(response.status, 503);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('retry-after'), '60');
  assert.ok(!(await response.text()).includes('PRIVATE'));
  assert.equal(response.headers.get('location'), null);
});
