import path from 'node:path';

const quote = (value) => JSON.stringify(value);
export const normalizeSecretFile = (text) => text.replace(/^\uFEFF/, '').trimEnd();
export function validate(settings) {
  const origin = new URL(settings.PUBLIC_ORIGIN);
  if (origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
    throw new Error('PUBLIC_ORIGIN must be an HTTPS origin without credentials, path, query or fragment');
  }
  // AGENT_TOKEN is optional: only a relay that still serves the computer of the single-computer days
  // (at the old addresses) has it. Computers are otherwise paired from the home page.
  const names = ['OWNER_PASSPHRASE', 'PROXY_TOKEN', ...(settings.AGENT_TOKEN ? ['AGENT_TOKEN'] : [])];
  for (const name of names) {
    const minimum = name === 'OWNER_PASSPHRASE' ? 24 : 32;
    if (typeof settings[name] !== 'string' || settings[name].length < minimum) throw new Error(`${name} must contain at least ${minimum} characters`);
  }
  if (new Set(names.map(k => settings[k])).size !== names.length) {
    throw new Error('Use independent owner, agent and proxy secrets');
  }
  return origin.origin;
}

export function render(settings, stateDir) {
  const origin = validate(settings);
  const bindings = { PUBLIC_ORIGIN: origin, OWNER_PASSPHRASE: settings.OWNER_PASSPHRASE, PROXY_TOKEN: settings.PROXY_TOKEN };
  if (settings.AGENT_TOKEN) bindings.AGENT_TOKEN = settings.AGENT_TOKEN;
  if (settings.HOOK_TOKEN) bindings.HOOK_TOKEN = settings.HOOK_TOKEN;
  if (settings.CIMD) bindings.CIMD = settings.CIMD;
  const vars = Object.entries(bindings).map(([name, value]) => `(name=${quote(name)}, text=${quote(value)})`).join(',\n');
  const directory = (suffix) => quote(path.resolve(stateDir, suffix).replaceAll('\\', '/'));
  // Based on workerd's official kv-test.wd-test. Requires built-in KV merged 2026-11-07.
  return `using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name="relay", worker=(
      modules=[(name="worker.mjs", esModule=embed "worker.mjs")],
      compatibilityDate="2026-09-01",
      compatibilityFlags=["nodejs_compat", "global_fetch_strictly_public"],
      bindings=[${vars},
        (name="BRIDGE", durableObjectNamespace="Bridge"),
        (name="OAUTH_KV", kvNamespace=(name="kv", props=(json="{\\"namespace\\":\\"winhand-oauth-local\\"}")))],
      durableObjectNamespaces=[(className="Bridge", uniqueKey="winhand-bridge-v1", enableSql=true)],
      durableObjectStorage=(localDisk="bridge-storage"))),
    (name="bridge-storage", disk=(path=${directory('bridge')}, writable=true)),
    (name="kv-storage", disk=(path=${directory('kv')}, writable=true)),
    (name="kv", worker=(
      modules=[(name="kv.js", bundled="/workerd/kv.js")],
      compatibilityDate="2026-09-01",
      bindings=[(name="SHARD", durableObjectNamespace="KVNamespace"), (name="BLOBS", service="kv-storage")],
      durableObjectNamespaces=[(className="KVNamespace", uniqueKey="winhand-kv-v1", enableSql=true)],
      durableObjectStorage=(localDisk="kv-storage"))),
    (name="internet", network=(allow=["public"], tlsOptions=(trustBrowserCas=true)))
  ],
  sockets=[(name="http", address="127.0.0.1:8787", http=(), service="relay")]
);
`;
}
