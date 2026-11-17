import type { Bridge } from "./bridge";

declare global {
  interface Env {
    OAUTH_KV: KVNamespace;
    BRIDGE: DurableObjectNamespace<Bridge>;
    /** The public origin, e.g. https://mcp.example.com; unset: the origin of each request. */
    PUBLIC_ORIGIN?: string;
    OWNER_PASSPHRASE: string;
    /** Device token of the computer of the single-computer days ("default", at the old addresses).
     * Unset on new deployments: computers are paired from the home page instead. */
    AGENT_TOKEN?: string;
    /** Opens /hook/<event> for cloud sessions' lifecycle hooks; unset: those paths do not exist. */
    HOOK_TOKEN?: string;
    /** Seconds a request waits for the agent to reconnect before failing (default 20). */
    AGENT_GRACE_S?: string;
    /** "on": accept OAuth clients identified by a metadata document URL (CIMD). Default off. */
    CIMD?: string;
  }
}
