import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
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
test('a relay without the old single-computer token is valid and gets no AGENT_TOKEN binding', () => {
  const { AGENT_TOKEN: _unused, ...newer } = settings;
  assert.equal(validate(newer), settings.PUBLIC_ORIGIN);
  assert.ok(!render(newer, '/data').includes('AGENT_TOKEN'));
  assert.match(render({ ...newer, CIMD: 'on' }, '/data'), /name="CIMD", text="on"/);
});
test('the hook token is optional, strong and independent of the other secrets, and bound only when set', () => {
  const hook = 'h'.repeat(32);
  assert.ok(!render(settings, '/data').includes('HOOK_TOKEN'));
  assert.equal(validate({ ...settings, HOOK_TOKEN: hook }), settings.PUBLIC_ORIGIN);
  assert.match(render({ ...settings, HOOK_TOKEN: hook }, '/data'), /name="HOOK_TOKEN", text="h{32}"/);
  assert.throws(() => validate({ ...settings, HOOK_TOKEN: 'short' }));
  assert.throws(() => validate({ ...settings, HOOK_TOKEN: settings.AGENT_TOKEN }));
  assert.throws(() => validate({ ...settings, HOOK_TOKEN: settings.OWNER_PASSPHRASE }));
});
test('the compose file hands the hook token file to the relay (2026-11-25: it did not, so the hooks of cloud sessions answered 404)', () => {
  const compose = fs.readFileSync(new URL('../selfhost/compose.yaml', import.meta.url), 'utf8');
  assert.match(compose, /^\s+HOOK_TOKEN_FILE: \$\{HOOK_TOKEN_FILE:-\}$/m);
});
test('the installer makes the hook token on request, never prints it, keeps it and can remove it', { skip: process.platform === 'win32' }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'winhand-install-'));
  try {
    // A source tree with only what install.sh reads, and a docker and a curl that answer "fine".
    const source = path.join(root, 'src');
    fs.mkdirSync(path.join(source, 'relay', 'selfhost'), { recursive: true });
    for (const name of ['Dockerfile', 'compose.yaml', 'Caddyfile']) {
      fs.copyFileSync(new URL(`../selfhost/${name}`, import.meta.url), path.join(source, 'relay', 'selfhost', name));
    }
    const bin = path.join(root, 'bin');
    fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, 'docker'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    fs.writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\necho \'{"pcs":[{"name":"msi","online":true}]}\'\n', { mode: 0o755 });
    const dir = path.join(root, 'relay');
    const installer = fileURLToPath(new URL('../selfhost/install.sh', import.meta.url));
    const run = (...args) => spawnSync('bash', [installer, 'relay.example.com', '--source', source, '--dir', dir, '--no-build', ...args],
      { encoding: 'utf8', env: { ...process.env, PATH: `${bin}:${process.env.PATH}` } });
    const dotenv = () => fs.readFileSync(path.join(dir, '.env'), 'utf8');
    const tokenFile = path.join(dir, 'secrets', 'hook-token');

    const first = run();
    assert.equal(first.status, 0, first.stderr);
    assert.ok(!fs.existsSync(tokenFile));
    assert.doesNotMatch(dotenv(), /HOOK_TOKEN_FILE/);

    const on = run('--hook', 'on');
    assert.equal(on.status, 0, on.stderr);
    const token = fs.readFileSync(tokenFile, 'utf8');
    assert.ok(token.length >= 32);
    assert.notEqual(token, fs.readFileSync(path.join(dir, 'secrets', 'proxy-token'), 'utf8'));
    assert.match(dotenv(), /^HOOK_TOKEN_FILE=\/run\/secrets\/hook-token$/m);
    assert.ok(!on.stdout.includes(token) && !on.stderr.includes(token), 'the token must not be printed');
    assert.match(on.stdout, /hook token made/);
    assert.equal(fs.statSync(tokenFile).mode & 0o077, 0); // only its owner can read it

    const kept = run(); // not given: kept as it is, and not announced again
    assert.equal(kept.status, 0, kept.stderr);
    assert.equal(fs.readFileSync(tokenFile, 'utf8'), token);
    assert.match(dotenv(), /^HOOK_TOKEN_FILE=/m);
    assert.doesNotMatch(kept.stdout, /hook token made/);

    assert.equal(run('--hook', 'off').status, 0);
    assert.ok(!fs.existsSync(tokenFile));
    assert.doesNotMatch(dotenv(), /HOOK_TOKEN_FILE/);
    assert.equal(run('--hook', 'maybe').status, 1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
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
