import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

// Explicit administrator-provisioned binary: never download an unpinned runtime at startup.
const binary = process.env.WORKERD_BIN;
if (!binary || !path.isAbsolute(binary)) throw new Error('Set WORKERD_BIN to an absolute path to a verified workerd binary with built-in KV support');
const version = spawnSync(binary, ['--version'], { encoding: 'utf8', windowsHide: true });
const match = version.stdout?.replaceAll('-', '').match(/(?:1\.)?(20\d{6})/);
if (version.status !== 0 || !match || Number(match[1]) < 20261108) {
  throw new Error('Runtime rejected: built-in KV requires a verified build containing upstream commit 519e60ed (2026-11-07) or later');
}
const config = fileURLToPath(new URL('../dist/selfhost/config.capnp', import.meta.url));
if (!fs.existsSync(config)) throw new Error('Run selfhost/prepare.mjs first');
const child = spawn(binary, ['serve', config, '--experimental'], { stdio: 'inherit', windowsHide: true });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', () => { console.error('Could not start workerd'); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
