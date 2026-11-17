// Diagnostic only: never supplies metadata to the OAuth provider or changes validation.
const CLAUDE_METADATA = "https://claude.ai/oauth/mcp-oauth-client-metadata";
let lastProbe = 0;

export async function diagnoseClaudeMetadata(metadataUrl: string): Promise<void> {
  if (metadataUrl !== CLAUDE_METADATA || Date.now() - lastProbe < 60_000) return;
  lastProbe = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3_000);
  try {
    const response = await fetch(CLAUDE_METADATA, {
      headers: { Accept: "application/json", "Cache-Control": "no-store" },
      cache: "no-store", redirect: "manual", signal: controller.signal,
    });
    const reader = response.body?.getReader();
    let bytes = 0;
    try {
      while (reader && bytes <= 5120) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
      }
    } finally {
      await reader?.cancel();
    }
    // Headers are classified, not printed, since an upstream value is untrusted.
    const type = response.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
    const location = response.headers.get("location");
    let redirectClass = "none";
    let redirectHost = "none";
    let redirectPathClass = "none";
    let publicRedirectPath = "none";
    if (location) {
      const target = new URL(location, CLAUDE_METADATA);
      // This fixed public request sends no credentials. Record the hostname and
      // a bounded alphabetic path on official hosts, never query or raw Location.
      redirectHost = target.hostname.slice(0, 253);
      publicRedirectPath = ["claude.ai", "claude.com"].includes(target.hostname)
        && /^\/[a-zA-Z/_-]{0,120}$/.test(target.pathname) ? target.pathname : "redacted";
      redirectPathClass = target.pathname === "/unsupported-country" ? "unsupported-country"
        : target.pathname === "/login" ? "login" : "other";
      redirectClass = target.hostname !== "claude.ai" ? "other-origin"
        : target.pathname === "/unsupported-country" ? "unsupported-country"
        : target.pathname === "/login" ? "login" : "same-origin-other";
    }
    console.warn("winhand_cimd_probe", {
      status: response.status,
      contentType: type === "application/json" ? "json" : type === "text/html" ? "html" : "other",
      redirectClass,
      redirectHost, redirectPathClass,
      publicRedirectPath,
      bytesObserved: Math.min(bytes, 5121), exceeded5120: bytes > 5120,
    });
  } catch {
    console.warn("winhand_cimd_probe", { failed: true, timedOut: controller.signal.aborted });
  } finally {
    clearTimeout(timeout);
  }
}
