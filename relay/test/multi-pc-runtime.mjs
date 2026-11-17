// Real-runtime integration for several computers on one relay (Epic 003 / Issue 002, 003).
// Synthetic secrets, an isolated loopback port and state; never bundled into a deployed worker.
//
//   A. The relay as it was (one computer, AGENT_TOKEN) signs a client in at the old /mcp.
//   B. The new code on the same state: that sign-in still works; two computers are paired from the
//      home page, each reachable only with tokens issued for it; removing one ends its token.
//   C. Without AGENT_TOKEN: the old addresses are gone, the paired computers keep working.
//
// Run: WORKERD_BIN=<absolute path to workerd> node test/multi-pc-runtime.mjs
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { createHash, randomBytes } from 'node:crypto';
import assert from 'node:assert/strict';
import http from 'node:http';
import { build } from 'esbuild';
import WebSocket from 'ws';
import { render } from '../selfhost/config.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const binary = process.env.WORKERD_BIN;
assert.ok(binary && path.isAbsolute(binary), 'Set absolute WORKERD_BIN');
// The relay code before this change: what LAX runs (one computer, OAuthProvider at /mcp).
const OLD_REV = process.env.OLD_RELAY_REV ?? 'ea67358';
await fs.mkdir(path.join(root, 'dist'), { recursive: true });
const dir = await fs.mkdtemp(path.join(root, 'dist/multi-pc-'));
const ORIGIN = 'https://relay.example.com';
const settings = { PUBLIC_ORIGIN: ORIGIN, OWNER_PASSPHRASE: randomBytes(32).toString('hex'), AGENT_TOKEN: randomBytes(32).toString('hex'), PROXY_TOKEN: randomBytes(32).toString('hex') };
for (const part of ['bridge', 'kv']) await fs.mkdir(path.join(dir, 'state', part), { recursive: true });

async function bundle(entryDir, outfile) {
  await build({ entryPoints: [path.join(entryDir, 'selfhost/entry.ts')], outfile, bundle: true, format: 'esm', platform: 'neutral',
    mainFields: ['module', 'main'], target: 'es2022', conditions: ['workerd', 'worker'], external: ['cloudflare:*', 'node:*'], logLevel: 'warning' });
}
const oldDir = path.join(dir, 'old');
for (const file of execFileSync('git', ['ls-tree', '-r', '--name-only', OLD_REV, 'src', 'selfhost/entry.ts'], { cwd: root, encoding: 'utf8' }).split('\n').filter(Boolean)) {
  await fs.mkdir(path.dirname(path.join(oldDir, file)), { recursive: true });
  await fs.writeFile(path.join(oldDir, file), execFileSync('git', ['show', `${OLD_REV}:relay/${file}`], { cwd: root }));
}
await bundle(oldDir, path.join(dir, 'old.mjs'));
await bundle(root, path.join(dir, 'new.mjs'));

const base = 'http://127.0.0.1:18788';
const headers = { host: 'relay.example.com', 'x-winhand-proxy-token': settings.PROXY_TOKEN, 'x-winhand-client-ip': '192.0.2.10' };
const call = (url, options = {}) => new Promise((resolve, reject) => {
  const req = http.request(base + url, { method: options.method ?? 'GET', headers: { ...headers, ...options.headers } }, (res) => {
    const chunks = []; res.on('data', (b) => chunks.push(b));
    res.on('end', () => { const h = new Headers(); for (const [k, v] of Object.entries(res.headers)) for (const item of Array.isArray(v) ? v : [v]) if (item !== undefined) h.append(k, item); resolve(new Response(Buffer.concat(chunks), { status: res.statusCode, headers: h })); });
  });
  req.setTimeout(15000, () => req.destroy(new Error('isolated request timeout'))); req.on('error', reject);
  if (options.body) req.write(options.body.toString()); req.end();
});
const formPost = (url, fields, extra = {}) => call(url, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded', origin: ORIGIN, ...extra }, body: new URLSearchParams(fields) });

let child; let runtimeError = '';
async function start(code, withAgentToken = true) {
  await fs.copyFile(path.join(dir, `${code}.mjs`), path.join(dir, 'worker.mjs'));
  const current = { ...settings }; if (!withAgentToken) delete current.AGENT_TOKEN;
  await fs.writeFile(path.join(dir, 'config.capnp'), render(current, path.join(dir, 'state')), { mode: 0o600 });
  child = spawn(binary, ['serve', path.join(dir, 'config.capnp'), '--experimental', '--socket-addr=http=127.0.0.1:18788'], { stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
  child.stderr.on('data', (b) => { runtimeError += b.toString(); });
  for (let i = 0; i < 80; i++) { if (child.exitCode !== null) throw Error('workerd exited: ' + runtimeError); try { if ((await call('/status.json')).status === 200) return; } catch {} await new Promise((r) => setTimeout(r, 100)); }
  throw Error('runtime readiness timeout');
}
async function stop() { if (child && child.exitCode === null) { child.kill(); await once(child, 'exit'); } }

async function register() {
  const r = await call('/oauth/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ client_name: 'multi-pc test', redirect_uris: ['https://client.example/callback'], token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'] }) });
  assert.equal(r.status, 201, 'dynamic client registration'); return r.json();
}
/** Sign a client in for one resource; returns the token response and what the consent page said. */
async function signIn(client, resource) {
  const verifier = randomBytes(32).toString('base64url');
  const params = new URLSearchParams({ client_id: client.client_id, redirect_uri: 'https://client.example/callback', response_type: 'code', scope: 'winhand', state: 's', code_challenge: createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256', resource });
  const consent = await call('/authorize?' + params); assert.equal(consent.status, 200, 'consent page');
  const page = await consent.text(); const handle = page.match(/name="handle" value="([^"]+)"/)[1];
  const cookie = consent.headers.getSetCookie().map((s) => s.split(';')[0]).join('; ');
  const approved = await formPost('/authorize', { handle, client: 'multi-pc test', redirect: 'https://client.example/callback', passphrase: settings.OWNER_PASSPHRASE, action: 'approve' }, { cookie });
  assert.equal(approved.status, 302, 'consent approved');
  const code = new URL(approved.headers.get('location')).searchParams.get('code');
  const token = await formPost('/oauth/token', { grant_type: 'authorization_code', client_id: client.client_id, redirect_uri: 'https://client.example/callback', code, code_verifier: verifier, resource });
  assert.equal(token.status, 200, 'token exchange'); return { tokens: await token.json(), page };
}
/** A fake winhand: answers every call with its own name, and records the paths it was asked for. */
async function agent(url, token, name) {
  const ws = new WebSocket(base.replace('http:', 'ws:') + url, { headers: { ...headers, authorization: 'Bearer ' + token } });
  ws.paths = [];
  ws.on('message', (b) => {
    const m = JSON.parse(b);
    if (m.type === 'welcome') ws.send(JSON.stringify({ type: 'hello', version: '0.0-' + name, hostname: 'synthetic', platform: 'test', resume: 1, pending: [] }));
    if (m.type === 'req') ws.paths.push(m.path);
    if (m.type === 'req_end') {
      ws.send(JSON.stringify({ type: 'head', id: m.id, status: 200, headers: [['content-type', 'application/json']] }));
      ws.send(JSON.stringify({ type: 'body', id: m.id, data: Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { pc: name } })).toString('base64') }));
      ws.send(JSON.stringify({ type: 'end', id: m.id }));
    }
  });
  const opened = await Promise.race([once(ws, 'open').then(() => true), once(ws, 'unexpected-response').then(([, res]) => res.statusCode)]);
  if (opened !== true) return { status: opened };
  await new Promise((r) => setTimeout(r, 150)); // hello reaches the relay
  return ws;
}
const rpc = (url, token) => call(url, { method: 'POST', headers: { authorization: 'Bearer ' + token, 'content-type': 'application/json', accept: 'application/json, text/event-stream' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }) });
async function pairOne(name) {
  const page = await formPost('/pair', { name, passphrase: settings.OWNER_PASSPHRASE });
  assert.equal(page.status, 200, 'pair form');
  const code = (await page.text()).match(/\/i\/([A-Za-z0-9_-]{22}) \| iex/)[1];
  const script = await call('/i/' + code); assert.equal(script.status, 200);
  assert.match(await script.text(), new RegExp(code), 'GET returns the install script');
  assert.match(await (await call('/i/' + code)).text(), /Invoke-RestMethod -Method Post/, 'a second GET still gets it: GET does not use the code up');
  const claimed = await call('/i/' + code, { method: 'POST' }); assert.equal(claimed.status, 200, 'claim');
  assert.equal((await call('/i/' + code, { method: 'POST' })).status, 410, 'a code works once');
  assert.match(await (await call('/i/' + code)).text(), /用过或过期/, 'a used code gets the expired script');
  return claimed.json();
}

const sockets = [];
try {
  // A. The relay as it was.
  await start('old');
  const oldClient = await register();
  const old = (await signIn(oldClient, ORIGIN + '/mcp')).tokens;
  await stop();
  console.log('PASS old relay signed a client in at /mcp');

  // B. The new code on the same state.
  await start('new');
  const legacyAgent = await agent('/agent', settings.AGENT_TOKEN, 'default'); sockets.push(legacyAgent);
  let r = await rpc('/mcp', old.access_token); assert.equal(r.status, 200, 'old access token at /mcp'); assert.equal((await r.json()).result.pc, 'default');
  const refreshed = await formPost('/oauth/token', { grant_type: 'refresh_token', client_id: oldClient.client_id, refresh_token: old.refresh_token, resource: ORIGIN + '/mcp' });
  assert.equal(refreshed.status, 200, 'old refresh token after the upgrade');
  console.log('PASS sign-ins made before the upgrade keep working at the old address');

  const alpha = await pairOne('alpha');
  const beta = await pairOne('beta');
  assert.equal(alpha.agent_url, 'wss://relay.example.com/pc/alpha/agent');
  assert.equal(alpha.mcp_url, ORIGIN + '/pc/alpha/mcp');
  assert.equal((await agent('/pc/beta/agent', alpha.token, 'beta')).status, 401, "alpha's token does not open beta");
  assert.equal((await agent('/agent', alpha.token, 'default')).status, 401, "alpha's token does not open the old address");
  const alphaAgent = await agent('/pc/alpha/agent', alpha.token, 'alpha'); sockets.push(alphaAgent);
  const betaAgent = await agent('/pc/beta/agent', beta.token, 'beta'); sockets.push(betaAgent);
  console.log('PASS two computers paired from the home page, each with its own device token; the install code works once');

  const client = await register();
  const signedA = await signIn(client, ORIGIN + '/pc/alpha/mcp');
  assert.match(signedA.page, /电脑 <strong>alpha<\/strong>/, 'the consent page names the computer');
  const tokenA = signedA.tokens.access_token;
  const tokenB = (await signIn(client, ORIGIN + '/pc/beta/mcp')).tokens.access_token;
  r = await rpc('/pc/alpha/mcp', tokenA); assert.equal(r.status, 200); assert.equal((await r.json()).result.pc, 'alpha');
  r = await rpc('/pc/beta/mcp', tokenB); assert.equal(r.status, 200); assert.equal((await r.json()).result.pc, 'beta');
  assert.equal((await rpc('/pc/beta/mcp', tokenA)).status, 401, "alpha's token is refused by beta");
  assert.equal((await rpc('/mcp', tokenA)).status, 401, "alpha's token is refused at the old address");
  assert.equal((await rpc('/pc/alpha/mcp', old.access_token)).status, 401, 'the old token is refused by alpha');
  r = await rpc('/pc/alpha/mcp/figma', tokenA); assert.equal(r.status, 200, "one sign-in covers the computer's other MCP servers");
  assert.deepEqual(alphaAgent.paths, ['/mcp', '/mcp/figma'], 'the PC sees its own paths, without /pc/<name>');
  const prm = await (await call('/.well-known/oauth-protected-resource/pc/alpha/mcp/figma')).json();
  assert.equal(prm.resource, ORIGIN + '/pc/alpha/mcp');
  const challenge = (await rpc('/pc/beta/mcp', 'nope')).headers.get('www-authenticate');
  assert.match(challenge, /resource_metadata="https:\/\/relay\.example\.com\/\.well-known\/oauth-protected-resource\/pc\/beta\/mcp"/);
  console.log('PASS each computer is its own OAuth resource; tokens do not cross between computers');

  const one = await (await call('/pc/alpha/status.json')).json();
  assert.equal(one.online, true); assert.equal(one.version, '0.0-alpha');
  const all = await (await call('/status.json')).json();
  assert.deepEqual(all.pcs.map((p) => p.name).sort(), ['alpha', 'beta', 'default']);
  assert.equal(all.online, true, 'the old top-level fields describe "default" while AGENT_TOKEN is set');
  const home = await (await call('/')).text();
  for (const name of ['alpha', 'beta', 'default']) assert.match(home, new RegExp(`${ORIGIN}/pc/${name}/mcp`));
  assert.equal((await call('/pc/nobody/status.json')).status, 404);
  assert.equal((await call('/pc/Bad_Name/status.json')).status, 404);
  console.log('PASS status per computer and the home page list');

  const closed = once(betaAgent, 'close');
  r = await formPost('/remove', { name: 'beta', passphrase: settings.OWNER_PASSPHRASE }); assert.equal(r.status, 200, 'remove');
  const [code] = await closed; assert.equal(code, 1008, 'its connection is closed');
  assert.equal((await agent('/pc/beta/agent', beta.token, 'beta')).status, 401, 'its token no longer opens it');
  assert.equal((await call('/pc/beta/status.json')).status, 404);
  assert.equal((await rpc('/pc/beta/mcp', tokenB)).status, 404, 'its address is gone');
  r = await rpc('/pc/alpha/mcp', tokenA); assert.equal(r.status, 200, 'the other computer is untouched');
  const again = await pairOne('alpha');
  assert.equal((await agent('/pc/alpha/agent', alpha.token, 'alpha')).status, 401, 'pairing again replaces the token');
  const alphaAgain = await agent('/pc/alpha/agent', again.token, 'alpha'); sockets.push(alphaAgain);
  assert.equal((await formPost('/pair', { name: 'x', passphrase: 'wrong' })).status, 401, 'pairing needs the passphrase');
  console.log('PASS removing a computer ends its token, sign-ins and connection; pairing again replaces the token');

  for (const ws of sockets) ws.terminate?.();
  sockets.length = 0;
  await stop();

  // C. No AGENT_TOKEN: the paired computers only.
  await start('new', false);
  assert.equal((await call('/mcp')).status, 404, 'old /mcp is gone');
  assert.equal((await call('/agent')).status, 404, 'old /agent is gone');
  const summary = await (await call('/status.json')).json();
  assert.equal(summary.online, undefined); assert.deepEqual(summary.pcs.map((p) => p.name), ['alpha']);
  const alphaC = await agent('/pc/alpha/agent', again.token, 'alpha'); sockets.push(alphaC);
  r = await rpc('/pc/alpha/mcp', tokenA); assert.equal(r.status, 200, 'sign-ins of paired computers survive dropping AGENT_TOKEN');
  console.log('PASS without AGENT_TOKEN only the paired computers remain');
} finally {
  for (const ws of sockets) ws.terminate?.();
  await stop();
}
