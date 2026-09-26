const escape = (value: string) =>
  value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

const STYLE = `
  :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#667085; --accent:#2f6fed; --err:#c0362c; --line:#dfe3ea; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1f26; --fg:#e7eaf0; --muted:#98a2b3; --accent:#6a9bff; --err:#ff7a70; --line:#2b323c; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
         font: 15px/1.55 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; padding:16px; }
  main { width:100%; max-width:420px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:28px; }
  h1 { font-size:20px; margin:0 0 6px; }
  p { margin:8px 0; color:var(--muted); }
  code { font-size:13px; word-break:break-all; }
  label { display:block; margin:18px 0 6px; font-weight:600; }
  input[type=password] { width:100%; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:transparent; color:inherit; font:inherit; }
  .row { display:flex; gap:10px; margin-top:18px; }
  button { flex:1; padding:10px; border-radius:8px; border:1px solid var(--line); background:transparent; color:inherit; font:inherit; cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  .err { color:var(--err); font-weight:600; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
`;

export function consentPage(opts: { handle: string; client: string; redirect: string; error?: string }): string {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>winhand 授权</title><style>${STYLE}</style></head><body><main>
<h1>允许连接这台电脑？</h1>
<p><strong>${escape(opts.client)}</strong> 请求通过 winhand 使用你的电脑（终端、文件、进程，以及在 winhand 里启用的本机 MCP 服务）。</p>
<p>授权后跳转到：<code>${escape(opts.redirect)}</code></p>
${opts.error ? `<p class="err">${escape(opts.error)}</p>` : ""}
<form method="post" action="/authorize">
  <input type="hidden" name="handle" value="${escape(opts.handle)}">
  <input type="hidden" name="client" value="${escape(opts.client)}">
  <input type="hidden" name="redirect" value="${escape(opts.redirect)}">
  <label for="passphrase">主人口令</label>
  <input id="passphrase" name="passphrase" type="password" autocomplete="current-password" autofocus required>
  <div class="row">
    <button type="submit" name="action" value="deny" formnovalidate>拒绝</button>
    <button type="submit" name="action" value="approve" class="primary">授权</button>
  </div>
</form></main></body></html>`;
}

export function statusPage(status: Record<string, unknown>, mcpUrl: string): string {
  const online = Boolean(status.online);
  const agent = (status.agent ?? {}) as Record<string, string>;
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>winhand 中转</title><style>${STYLE}</style></head><body><main>
<h1>winhand 中转</h1>
<p><span class="dot" style="background:${online ? "#2e9d57" : "#b54708"}"></span>${online ? "电脑在线" : "电脑离线"}${
    online && agent.hostname ? `：${escape(agent.hostname)}（${escape(agent.platform ?? "")}，winhand ${escape(agent.version ?? "")}）` : ""
  }</p>
<p>在 claude.ai 添加自定义连接器，地址填：</p>
<p><code>${escape(mcpUrl)}</code></p>
<p>本机的其他 MCP 服务（在 winhand 应用的“MCP 服务”页启用）各有自己的地址：<code>${escape(mcpUrl)}/&lt;名称&gt;</code>，分别添加为连接器。</p>
</main></body></html>`;
}
