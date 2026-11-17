// Self-host boundary only. Cloudflare deployment continues to use src/index.ts.
import worker, { Bridge } from '../src/index';
export { Bridge };

export async function proxyRequest(request: Request, env: { PUBLIC_ORIGIN?: string; PROXY_TOKEN: string }): Promise<Request | Response> {
  const supplied = request.headers.get('x-winhand-proxy-token') ?? '';
  const hash = (s: string) => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  if (!env.PROXY_TOKEN || !crypto.subtle.timingSafeEqual(await hash(supplied), await hash(env.PROXY_TOKEN))) {
    return new Response('untrusted ingress', { status: 403 });
  }
  if (!env.PUBLIC_ORIGIN) return new Response('PUBLIC_ORIGIN is not configured', { status: 500 });
  const url = new URL(request.url);
  const origin = new URL(env.PUBLIC_ORIGIN);
  if (url.host !== origin.host) return new Response('unexpected host', { status: 421 });
  const headers = new Headers(request.headers);
  // Only our private reverse proxy can supply the client IP. Never trust a public CF header.
  headers.set('cf-connecting-ip', headers.get('x-winhand-client-ip') || 'unknown');
  headers.delete('x-winhand-client-ip');
  headers.delete('x-winhand-proxy-token');
  headers.delete('forwarded');
  headers.delete('x-forwarded-for');
  url.protocol = origin.protocol;
  return new Request(url, new Request(request, { headers }));
}

export default {
  async fetch(request: Request, env: Env & { PROXY_TOKEN: string }, ctx: ExecutionContext) {
    const normalized = await proxyRequest(request, env);
    if (normalized instanceof Response) return normalized;
    return worker.fetch(normalized, env, ctx);
  },
};
