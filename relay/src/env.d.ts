import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { Bridge } from "./bridge";

declare global {
  interface Env {
    OAUTH_KV: KVNamespace;
    BRIDGE: DurableObjectNamespace<Bridge>;
    OAUTH_PROVIDER: OAuthHelpers;
    PUBLIC_ORIGIN: string;
    OWNER_PASSPHRASE: string;
    AGENT_TOKEN: string;
  }
}
