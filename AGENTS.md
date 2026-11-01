<!-- knowledge-sync:start -->
## CodeStable 与知识同步

- 入口是 `/cs`。小改走快改并留下 `ff`；纯格式、错别字、依赖版本号这类改动视为"不要痕迹"，不留 `ff`。
- 现状规格就是 `codestable/spec/`，不另建 ARCHITECTURE.md、GLOSSARY.md；统一语言写在 `codestable/spec/index.md` 和各子层的入口页。
- 决定（为什么这样选、否掉了什么）写 .agents/notes/，格式按 write-notes-like-deepseek，提交前跑 verify；Issue 的方案一节链接过去。`codestable/notes/` 只放坑点和操作经验。
- 结构路径在 `.arch-check.json`。改了这些目录，同一次提交必须改 `codestable/spec/` 或 .agents/notes/，或者在本次 `ff` / Issue 的「codestable」一栏写明影响；pre-commit 会检查，被拒绝时按提示补，不要绕过。
- `/cs` 的技能文件不在仓里（上游没有许可证）：会话开始时 `.githooks/ensure-cs.mjs` 按固定提交取到 .claude/skills/cs，云端自动装，本地缺了运行它加 --install。`.claude/skills/README.md` 让这个目录随仓存在，Claude Code 才会当场发现装进来的 cs，别删。输入框不认 /cs 时运行 /reload-skills；技能列表里还没有 cs 就直接读 .claude/skills/cs/SKILL.md。
- 看板不用手记：dev-board 每天采集时从这个仓的 `codestable/`（GitHub 上的仓读默认分支）生成看板卡片的「现状」「最近进展」「下一步」，取说明书第一段、关掉的票和没关的票，所以关票或落 `ff` 以后不跑 `board log`。票之外的事（发版、环境变化）要记一句，就运行 `board log "<一句话进展>" --force`；云端经 winhand 在 Windows 的检出里运行。
<!-- knowledge-sync:end -->
