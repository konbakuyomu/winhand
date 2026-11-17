import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto, timingSafeEqual } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import { render, validate, normalizeSecretFile } from '../selfhost/config.mjs';

const settings = { PUBLIC_ORIGIN: 'https://relay.example.com', OWNER_PASSPHRASE: 'o'.repeat(32), AGENT_TOKEN: 'a'.repeat(32), PROXY_TOKEN: 'p'.repeat(32) };
test('secret files tolerate Windows UTF-8 BOM and CRLF without changing secret', () => {
  assert.equal(normalizeSecretFile('\uFEFFsecret-value\r\n'), 'secret-value');
});
test('origin validation rejects insecure and non-origin values', () => {
  for (const origin of ['http://relay.example.com', 'https://user:pass@relay.example.com', 'https://relay.example.com/path', 'https://relay.example.com/?secret=1']) {
    assert.throws(() => validate({ ...settings, PUBLIC_ORIGIN: origin }));
  }
  assert.equal(validate(settings), settings.PUBLIC_ORIGIN);
});
test('independent strong secrets are mandatory', () => {
  assert.throws(() => validate({ ...settings, AGENT_TOKEN: 'short' }));
  assert.throws(() => validate({ ...settings, AGENT_TOKEN: settings.PROXY_TOKEN }));
});
test('generated native config has isolated persistence and loopback-only ingress', () => {
  const config = render(settings, '/data');
  assert.match(config, /127\.0\.0\.1:8787/);
  assert.match(config, /winhand-oauth-local/);
  assert.match(config, /winhand-bridge-v1/);
  assert.match(config, /winhand-kv-v1/);
  assert.match(config, /allow=\["public"\]/);
  assert.match(config, /bundled="\/workerd\/kv\.js"/);
  assert.doesNotMatch(config, /inMemory|ephemeralLocal/);
});
const raw = fs.readFileSync(new URL('../selfhost/entry.ts', import.meta.url), 'utf8');
const fn = stripTypeScriptTypes(raw.slice(raw.indexOf('export async function'), raw.indexOf('\nexport default'))).replace('export ', '');
const context = vm.createContext({ Request, Response, URL, Headers, TextEncoder,
  crypto: { subtle: { digest: (...args) => webcrypto.subtle.digest(...args), timingSafeEqual: (a,b) => timingSafeEqual(Buffer.from(a), Buffer.from(b)) } } });
vm.runInContext(fn + '\nthis.normalize = proxyRequest;', context);
test('untrusted ingress is rejected even with spoofed Cloudflare headers', async () => {
  const result = await context.normalize(new Request('http://relay.example.com/mcp', { headers: { 'cf-connecting-ip': 'attacker' } }), settings);
  assert.equal(result.status, 403);
});
test('trusted proxy normalizes origin and removes spoofable headers', async () => {
  const result = await context.normalize(new Request('http://relay.example.com/mcp?a=1', { headers: {
    'x-winhand-proxy-token': settings.PROXY_TOKEN, 'x-winhand-client-ip': '192.0.2.1', 'cf-connecting-ip': 'attacker', 'x-forwarded-for': 'attacker'
  } }), settings);
  assert.equal(result.url, 'https://relay.example.com/mcp?a=1');
  assert.equal(result.headers.get('cf-connecting-ip'), '192.0.2.1');
  assert.equal(result.headers.get('x-winhand-proxy-token'), null);
  assert.equal(result.headers.get('x-forwarded-for'), null);
});
test('trusted ingress still rejects unexpected host', async () => {
  const result = await context.normalize(new Request('http://evil.example/mcp', { headers: { 'x-winhand-proxy-token': settings.PROXY_TOKEN } }), settings);
  assert.equal(result.status, 421);
});
