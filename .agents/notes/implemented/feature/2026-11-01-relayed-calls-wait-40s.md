# Agent Note: 经中转来的调用每次最多等 40 秒

Status: implemented

## Problem

0.8.0 让带进度令牌的客户端一次最多等 1800 秒：开始时立刻发一次进度，之后每 15 秒一次。依据是 Claude Code 的文档：远程 MCP 只限首字节 60 秒，之后只要每 5 分钟内有进度就一直等。开场说明也因此改成了 `job_status(wait_s=1800)`。

2026-11-01 装上 0.10.0 实测：

- 做法：在云端 Claude Code 会话里，经 claude.ai 连接器调 `job_status(wait_s=300)`，等一个要跑 150 秒的任务。
- 结果：客户端在 60 秒报 “tool job_status timed out after 60s”。
- 服务端确实走了长等待：不带进度令牌时 40 秒就会返回，这次没有。进度通知也照常发了，但没能让调用撑过 60 秒。

照 0.10.0 的开场说明去等，每次都会超时报错。

## Decision

- `waiting.limit`：只有同时满足两条，才给最多 1800 秒：
  - 请求带进度令牌；
  - 不是经中转来的。中转转发的每个请求都带 `x-winhand-user` 头，`waiting.relayed()` 用 FastMCP 的 `get_http_headers()` 看有没有这个头。
- 其余调用最多 40 秒。要的比这长时，回复里带 `wait_note`，说明原因（经中转，或不收进度），并提示按游标接着问。
- 开场说明、`help()`、工具说明、`run` 的 still_running 提示、README 都改回 `wait_s=40`。每次等不完就按游标再问。
- 心跳照旧：带进度令牌就发，对 40 秒内的等待没有坏处。

## Alternatives considered

- **按 `clientInfo` 名字区分客户端。** 最强的理由：能精确到哪个客户端能长等。否掉的原因：这次超时是哪一层切断的（云端会话自己的 MCP 客户端，还是 claude.ai 连接器那一层），从电脑这边看不出来；名字和版本也会变。按“是否经中转”划线，和这次的实测证据直接对得上。
- **全部退回 40 秒，连本机客户端也不例外。** 最强的理由：最简单，也不靠未经实测的假设。没这样做：本机 stdio 客户端没有中转和连接器这两层，这次的证据不涉及它们，长等待对它们仍可能有用。代价记在下面。
- **保留 1800 秒，只在超时以后让 AI 自己缩短。** 最强的理由：不用改代码。否掉的原因：每次先超时报错，AI 还会以为任务出了问题。复盘里正是这种“等不准”的情况让用户来回调轮询频率。

## Consequences

- **收益**：经连接器的会话不会再撞上 60 秒超时；开场说明给出的写法照做就能用。
- **代价与已知上限**：
  - 经中转的长任务还是要每 40 秒问一次，回合数省不下来；
  - 本机 stdio 客户端的长等待还没实测。要是那条路也被切断，就照同样的办法收紧。
  - 将来哪个经中转的客户端真能长等（比如连接器那一层放开了），要在 `waiting.relayed` 这里放开，并且先实测。

## Verification

- `agent/tests/test_review_round4.py::test_calls_through_the_relay_keep_the_40_s_wait`
- `agent/tests/test_review_round4.py::test_relayed_reads_the_header_the_relay_adds`
- 实测记录：`codestable/epics/001-x-使用复盘四轮强化/issues/002-x-第2轮长任务不用守着.md` 的「实测」一节。
