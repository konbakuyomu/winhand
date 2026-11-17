---
kind: issue
title: "CIMD 上游故障受限诊断与错误处理"
type: ff
status: open
created: 2026-11-13
---

# CIMD 上游故障受限诊断与错误处理

Claude 原 OAuth/CIMD 登录在 14:35:39 UTC 到达 /authorize，Worker 抛出 CimdFetchError：官方客户端元数据响应超过 5120 字节；同刻本机取公开 JSON 仅 342 字节。尚未认定是 HTML、出口异常或其他内容差异。

- 改动：relay/src/index.ts 捕获 CIMD 元数据异常并返回可重试、no-store 的 503，不再冒泡为 1101；cimd-diagnostic.ts 仅探测固定 Claude 公开元数据 URL，每 isolate 每分钟最多一次、3 秒超时、拒绝重定向，读到超过 5120 字节即停止，仅日志 status/内容类型分类/有界长度，不记录正文、请求 URL、OAuth state 或凭据。
- 验证：新增 5 项离线测试通过，原有 40 项通过，TypeScript noEmit 通过。线上由 1101 改为明确 503。Worker 对官方公开 CIMD 请求收到 302、text/html、143 字节，目标主机 claude.com；本机同请求为 200 JSON 342 字节。不得提高 5KB 上限掩盖不同响应。
- 线上：诊断版本 9b208534-8841-408c-9433-18480bb83287。探针不跟随重定向，仅记录主机和官方主机的有界字母路径，查询参数、片段、正文均不记录。暂未恢复成功登录。
- 根因证据：14:51 UTC 的 Worker 探针 Location 分类确认落地页为 `https://claude.com/app-unavailable-in-region`。当前 Worker 出口受到官方地区限制，库跟随该 HTML 后触发大小限制。不得伪造地区、增加代理、硬编码或代取 CIMD 绕过此限制；需服务商纠正可用地区判定，或按官方允许的部署方式处理。
- 边界：不使用诊断返回值鉴权；保留原库 5KB 限长、超时、URL/PKCE/OAuth 校验。未撤销旧授权，未修改密钥、访问策略或路由。
- 回滚：此前线上 Worker 版本 8fadec56-2b98-4fb2-9bf2-5e6d8a104638。代码变更未提交。
- codestable：本记录说明诊断阶段现状；未改变原 OAuth 架构，尚未宣称连接修复。
