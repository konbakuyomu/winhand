---
kind: issue
title: "可移植自托管中转与 LAX 验证"
type: feature
status: closed
created: 2026-11-13
---

# 可移植自托管中转与 LAX 验证

## 目标与边界

用户要求按cs重构WinHand中转，让自有服务器能快速部署，目标LAX且电脑须经DMIT接入。保留原Worker/OAuth/CIMD/PKCE与现有诊断补丁，不绕过地区限制、不修改未经确认的网络安全设置。当前先交付可验证的自托管切片，运行时与实际链路通过前不切生产。

## 方案

复用原relay业务，独立 `relay/selfhost/` 使用官方workerd内建KV和Bridge DO。决定及备选见 `.agents/notes/implemented/architecture/2026-11-13-portable-relay-boundary.md`。

质量承诺：原协议兼容（原45测试继续通过）；信息安全性（无秘密输出、固定origin、可信代理边界测试）；可恢复性（持久化/停机备份/回退须实测）；可移植性（单一配置生成，不写死机器地址）。本机穿刺通过，Linux目标环境与跨机回退尚未完整兑现。

## 执行与证据

- 已读取本机cs、repo AGENTS、当前spec及相关notes，保留未提交CIMD补丁。
- 新增独立入口、私有文件配置加载、原生capnp生成、Caddy模板、旧版本拒绝启动、部署/验收/回退文档。
- 原45单测与新增6测试全部通过：origin、secret隔离、持久化配置、代理身份/IP头规范化/拒绝。
- esbuild真实bundle成功，未调用外部OAuth服务/未使用生产秘密。
- 原workerd 1.20260925.1 实测无法解析新配置：`Struct has no field named 'bundled'`。上游KV功能2026-11-07才合入。后续通过明确官方源的权限复核，读取npm官方元数据并在隔离目录安装1.20261113.1，未替换旧依赖。新增独立 `selfhost/runtime` lockfile及SHA512完整性信息固定版本，不在启动时自动下载。
- 本机Node默认mise shim曾触发npm registry请求被工具策略阻断，先改用已安装Node v24.22.1执行本地检查；后续官方npm访问经require_escalated复核通过，没有更换镜像或域名绕过。
- 新版真实运行时通过：入口拒绝/metadata、KV JSON/metadata/list分页/delete/60秒TTL、DCR、consent cookie、PKCE S256、错误redirect/口令/verifier拒绝、refresh与撤销、真实WS传输MCP/SSE、整进程重启后KV与授权继续有效。集成测试仅绑定loopback、synthetic凭据和独立状态。
- `src`及selfhost typecheck、bundle、git diff检查通过。旧版拒绝测试验证start脚本拒绝过旧运行时。
- 停机复制完整SQLite/WAL/blob状态到新目录后启动，KV记录及OAuth refresh仍有效；该隔离备份恢复路径已实测。
- 用户后续明确授权排查SSH后，用原有的跳板机别名和密钥、严格主机校验成功登录，再经跳板机进入LAX；没有改key/sshd/权限，旧认证失败未复现。
- LAX Ubuntu22.04 x86_64的隔离Docker真实集成全部通过（网络none、无端口发布）：KV/OAuth/WS/SSE/TTL/停机备份恢复。首次构建暴露Node slim缺系统CA证书，已在Dockerfile安装ca-certificates修复并复测通过。Node基础镜像按实际官方digest固定。
- LAX直接GET官方Claude CIMD：200/application-json/342 bytes，无redirect。尚未代表Claude实际连接器授权成功。
- 仅创建LAX隔离实验目录及镜像，测试容器运行后自动移除。原Worker、DNS、防火墙、DMIT路由、Windows agent配置未改。

## 已确认发布与剩余验收

用户随后明确确认新域名、LAX公网443、WinHand专用本机SSH代理方案，已执行：新域名DNS-only、有效TLS、LAX443规则、原DMIT→LAX SSH隧道（本机回环端口），仅WinHand进程wss_proxy，系统代理未改。Windows新relay配置/令牌生效并在线；桌面/登录启动入口已添加，旧配置与Worker保留。中转重建后agent自动重连，真实OAuth→实际agent→initialize/tools/list/help通过，测试grant撤销。

实际迁移暴露PowerShell文本管道UTF-8 BOM问题，已规范化秘密文件、补去BOM代码/回归测试并固化新镜像；最终52单测及typecheck通过。具体路径与回退见电脑上的 LAX 部署记录（个人部署细节，不进仓库）。最终部署镜像digest：`sha256:b05b94da545fe5274906bb6cb311ffe834dc6094127a9031ca70e46547e77482`。

Claude网页连接器创建最初遇429，未绕过限流。之后独立UI/主会话已完成真实网页工具调用，父线程转交验收记录：北京时间2026-11-14 00:45:46，WinHand LAX:run成功返回MSI数据，exit0。WS请求处理中断续传有原单测，生产验过重启后自动重连。

用户运行中要求安全/恢复审计，已完成非破坏性核查，无重启/重建。服务及本机边界/持久化权限/自启动正常；54测试通过。日志限额仅仓库候选，Node启动器后备及SSH spawn-error恢复磁盘代码已同步、下一次正常启动生效；Node watchdog和生产定时备份尚未安排；额外不可解析ACL未改。完整证据见电脑上 LAX 部署记录的审计章节。

不自动关闭本票、提交或推送。LAX服务、Windows链路、API及Claude网页调用均有验收记录，常规收尾仍需用户授权；日志/恢复等后续改良保持明确未生效边界。

## 关闭时（2026-11-18）

- 回写：
  - 自托管这种部署形态写进了 `codestable/spec/index.md` 的中转一节：同一份代码，workerd 加 Caddy，`install.sh` 一条命令部署和升级；
  - 部署和升级步骤在 `relay/selfhost/README.md` 和 `codestable/notes/001-发版与部署.md`；
  - 取舍见 `.agents/notes/implemented/architecture/2026-11-13-portable-relay-boundary.md`。
- 关闭判断：
  - LAX 自 11-13 起在生产运行；
  - 代码 11-18 随 Epic 003 进了仓库，并加上一条命令的部署；
  - LAX 11-18 用 `install.sh --cimd on` 升级成功（见 `codestable/epics/003-x-简单部署与多台电脑/issues/005-x-一键部署.md`）。
- 遗留（原票里就写着尚未安排，这次也没做）：
  - 生产的定期备份与恢复演练；
  - 电脑上 SSH 隧道 Node 进程崩溃后的看门狗。
