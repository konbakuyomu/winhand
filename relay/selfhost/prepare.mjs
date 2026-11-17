import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { render, normalizeSecretFile } from './config.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const output = path.resolve(root, 'dist/selfhost');
const state = process.env.WINHAND_STATE_DIR;
if (!state || !path.isAbsolute(state)) throw new Error('WINHAND_STATE_DIR must name a dedicated absolute persistent directory');
const settings = { ...process.env };
for (const key of ['OWNER_PASSPHRASE', 'AGENT_TOKEN', 'PROXY_TOKEN', 'HOOK_TOKEN']) {
  if (settings[`${key}_FILE`]) settings[key] = normalizeSecretFile(await fs.readFile(settings[`${key}_FILE`], 'utf8'));
}
const config = render(settings, state); // Validate before writing anything.
await fs.mkdir(output, { recursive: true, mode: 0o700 });
for (const dir of ['bridge', 'kv']) await fs.mkdir(path.join(state, dir), { recursive: true, mode: 0o700 });
await build({ entryPoints: [path.join(root, 'selfhost/entry.ts')], outfile: path.join(output, 'worker.mjs'),
  bundle: true, format: 'esm', platform: 'neutral', mainFields: ['module', 'main'], target: 'es2022', conditions: ['workerd', 'worker'],
  external: ['cloudflare:*', 'node:*'], logLevel: 'warning' });
await fs.writeFile(path.join(output, 'config.capnp'), config, { mode: 0o600 });
console.log('Prepared self-host bundle and private configuration. No network service started.');
