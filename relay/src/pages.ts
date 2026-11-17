const escape = (value: string) =>
  value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

const STYLE = `
  :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#667085; --accent:#2f6fed; --err:#c0362c; --ok:#2e9d57; --off:#b54708; --line:#dfe3ea; --code:#f0f2f5; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1f26; --fg:#e7eaf0; --muted:#98a2b3; --accent:#6a9bff; --err:#ff7a70; --ok:#3fb96f; --off:#f79009; --line:#2b323c; --code:#232a33; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:start center; background:var(--bg); color:var(--fg);
         font: 15px/1.55 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; padding:32px 16px; }
  main { width:100%; max-width:540px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:24px; }
  h1 { font-size:20px; margin:0 0 12px; }
  h2 { font-size:15px; margin:0; }
  p { margin:8px 0; color:var(--muted); }
  code, .cmd { font: 13px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; word-break:break-all; }
  .cmd { display:flex; gap:8px; align-items:flex-start; background:var(--code); border-radius:8px; padding:8px 10px; margin:6px 0; }
  .cmd span { flex:1; user-select:all; }
  label { display:block; margin:12px 0 4px; font-weight:600; font-size:14px; }
  input { width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--line); background:transparent; color:inherit; font:inherit; }
  .row { display:flex; gap:10px; margin-top:14px; }
  button { padding:9px 12px; border-radius:8px; border:1px solid var(--line); background:transparent; color:inherit; font:inherit; cursor:pointer; }
  .row button { flex:1; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button.copy { padding:2px 8px; font-size:13px; flex:none; }
  .err { color:var(--err); font-weight:600; }
  .note { color:var(--fg); font-weight:600; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:1px; }
  .pc { border-top:1px solid var(--line); padding:10px 0; }
  .pc:first-of-type { border-top:none; }
  .pc .state { color:var(--muted); font-size:14px; }
  details { border-top:1px solid var(--line); padding:12px 0 0; margin-top:14px; }
  summary { cursor:pointer; font-weight:600; }
  ol { padding-left:20px; margin:8px 0; }
  ol li { margin:10px 0; }
`;

const COPY_SCRIPT = `<script>
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('button.copy');
  if (!b || !navigator.clipboard) return;
  navigator.clipboard.writeText(b.getAttribute('data-copy')).then(function () {
    var t = b.textContent; b.textContent = '已复制'; setTimeout(function () { b.textContent = t; }, 1500);
  });
});
</script>`;

function page(title: string, body: string): string {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escape(title)}</title><style>${STYLE}</style></head><body><main>
${body}
</main>${COPY_SCRIPT}</body></html>`;
}

function copyable(text: string): string {
  return `<div class="cmd"><span>${escape(text)}</span><button type="button" class="copy" data-copy="${escape(text)}">复制</button></div>`;
}

export function consentPage(opts: { handle: string; client: string; redirect: string; pc: string; error?: string }): string {
  const target = opts.pc ? `电脑 <strong>${escape(opts.pc)}</strong>` : "你的电脑";
  return page(
    "winhand 授权",
    `<h1>允许连接${opts.pc ? "这台" : ""}电脑？</h1>
<p><strong>${escape(opts.client)}</strong> 请求通过 winhand 使用${target}：终端、文件、进程，以及这台电脑上在 winhand 里启用的其他 MCP 服务。</p>
<p>授权后跳转到：<code>${escape(opts.redirect)}</code></p>
${opts.error ? `<p class="err">${escape(opts.error)}</p>` : ""}
<form method="post" action="/authorize">
  <input type="hidden" name="handle" value="${escape(opts.handle)}">
  <input type="hidden" name="client" value="${escape(opts.client)}">
  <input type="hidden" name="redirect" value="${escape(opts.redirect)}">
  <input type="hidden" name="pc" value="${escape(opts.pc)}">
  <label for="passphrase">主人口令</label>
  <input id="passphrase" name="passphrase" type="password" autocomplete="current-password" autofocus required>
  <div class="row">
    <button type="submit" name="action" value="deny" formnovalidate>拒绝</button>
    <button type="submit" name="action" value="approve" class="primary">授权</button>
  </div>
</form>`,
  );
}

/** One computer as the page shows it: anyone with the address sees this, so no Windows name or build. */
export type PcView = {
  name: string;
  online: boolean;
  version: string | null;
  /** ms since the PC went offline; null when online or never seen. */
  offlineForMs: number | null;
  /** It is the computer of the old single-computer addresses (AGENT_TOKEN). */
  legacy: boolean;
};

export type HomeView = {
  origin: string;
  host: string;
  pcs: PcView[];
  signedIn: number | null;
  /** A freshly made install command, with the computer it is for. */
  command?: { name: string; line: string };
  notice?: { text: string; error?: boolean };
  /** Open the setup steps (also when no computer has connected yet). */
  openSetup?: boolean;
};

function ago(ms: number): string {
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function pcRow(view: HomeView, pc: PcView): string {
  const state = pc.online
    ? `<span class="dot" style="background:var(--ok)"></span>在线${pc.version ? ` · winhand ${escape(pc.version)}` : ""}`
    : pc.offlineForMs === null
      ? `<span class="dot" style="background:var(--muted)"></span>还没连上过`
      : `<span class="dot" style="background:var(--off)"></span>离线 · ${ago(pc.offlineForMs)}断开`;
  const mcp = `${view.origin}/pc/${pc.name}/mcp`;
  return `<div class="pc"><h2>${escape(pc.name)}</h2>
<p class="state">${state}</p>
${copyable(mcp)}${pc.legacy ? `<p>旧地址 <code>${escape(view.origin)}/mcp</code> 也指向这台，迁移完成后删掉 AGENT_TOKEN 即停用。</p>` : ""}</div>`;
}

export function homePage(view: HomeView): string {
  const names = view.pcs.map((pc) => pc.name);
  const anyOnline = view.pcs.some((pc) => pc.online);
  const open = view.openSetup || !anyOnline || Boolean(view.command);
  const pcs = view.pcs.length
    ? view.pcs.map((pc) => pcRow(view, pc)).join("\n")
    : `<p>还没有电脑。按下面的步骤接一台。</p>`;
  const datalist = `<datalist id="pc-names">${names.map((n) => `<option value="${escape(n)}">`).join("")}</datalist>`;
  const command = view.command
    ? `<p class="note">在电脑「${escape(view.command.name)}」上打开 PowerShell，运行：</p>${copyable(view.command.line)}
<p>10 分钟内有效，只能用一次。它会下载并安装 winhand、存好设备令牌、开机自启，连上以后打印连接器地址。</p>`
    : "";
  const notice = view.notice ? `<p${view.notice.error ? ' class="err"' : ' class="note"'}>${escape(view.notice.text)}</p>` : "";
  return page(
    "winhand 中转",
    `<h1>winhand 中转</h1>
${notice}
${pcs}
<p>Claude 登录：${view.signedIn === null ? "读不到" : `${view.signedIn} 个`}</p>
<details${open ? " open" : ""}>
<summary>${view.pcs.length ? "接一台电脑 / 连 Claude" : "开始接入"}</summary>
<ol>
<li><strong>连电脑</strong>：填一个名字（小写字母、数字、连字符，例如 home）。用已有的名字就是给那台重新配对。
<form method="post" action="/pair">
  <label for="name">电脑名称</label>
  <input id="name" name="name" list="pc-names" required pattern="[a-z0-9]([a-z0-9\\-]{0,30}[a-z0-9])?" maxlength="32" autocomplete="off" value="${escape(view.command?.name ?? "")}">
  ${datalist}
  <label for="pair-passphrase">主人口令</label>
  <input id="pair-passphrase" name="passphrase" type="password" autocomplete="current-password" required>
  <div class="row"><button type="submit" class="primary">生成安装命令</button></div>
</form>
${command}</li>
<li><strong>连 Claude</strong>：在 claude.ai 的「设置 → 连接器 → 添加自定义连接器」里填上面那台电脑的地址，授权时输入主人口令。每台电脑一个连接器。</li>
<li><strong>云端 Claude Code（可选）</strong>：在云环境的网络设置里放行 <code>${escape(view.host)}</code>，一次性文件链接和任务结束叫醒才能用。</li>
</ol>
</details>
<details>
<summary>安全</summary>
<p>每次授权是一个独立的登录。30 天不用的登录自动失效，每台电脑最多保留最新的 20 个。</p>
<form method="post" action="/signout">
  <label for="out-passphrase">主人口令</label>
  <input id="out-passphrase" name="passphrase" type="password" autocomplete="current-password" required>
  <div class="row"><button type="submit">退出所有登录</button></div>
</form>
${
  view.pcs.length
    ? `<form method="post" action="/remove">
  <label for="remove-name">移除一台电脑（它的令牌和登录随即作废）</label>
  <input id="remove-name" name="name" list="pc-names" required autocomplete="off">
  <label for="remove-passphrase">主人口令</label>
  <input id="remove-passphrase" name="passphrase" type="password" autocomplete="current-password" required>
  <div class="row"><button type="submit">移除</button></div>
</form>`
    : ""
}
</details>`,
  );
}
