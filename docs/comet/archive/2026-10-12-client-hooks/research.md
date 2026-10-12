# Hook facts for winhand: Codex and Claude Code PreToolUse

Research notes for the `client-hooks` change, written by a research subagent and copied here unchanged apart from these lines. They are the external facts the brief relies on.

Sources were fetched on 2026-10-11.

- **Codex:** `https://raw.githubusercontent.com/openai/codex/main/codex-rs/<path>`, from the `main` branch. I did not capture the commit SHA because the GitHub API is not enabled for openai/codex in this session. The local copies used during research were temporary and are not kept in the repository.
- **Claude Code:** the raw markdown of the docs, from `https://code.claude.com/docs/en/<page>.md` (`hooks`, `settings`, `settings-reference`, `permissions`, `managed-settings` and `tools-reference`).

Labels used below:
- **[V]** means I verified it directly in source or docs (file and line given).
- **[I]** means I inferred it.

---

## CODEX

### 1. Where hooks are configured, the schema, and how layers merge

**Two forms per config layer [V].**
- A `hooks.json` file in the layer's config folder.
- `[hooks]` tables inside that layer's `config.toml`.

Both are loaded. If both are non-empty, Codex only emits a warning: "loading hooks from both … prefer a single representation for this layer" (`hooks/src/engine/discovery.rs:146-164`).
- `load_hooks_json` joins `config_folder` with `"hooks.json"` (`discovery.rs:339-380`).
- TOML hooks come from `layer.config.get("hooks")` (`discovery.rs:382-400`).
- Each `hooks.json` folder is read only once per stack, through `visited_json_hook_folders`. So a user profile and the user config share `$CODEX_HOME/hooks.json`.

**The folder for each layer [V]** (`config/src/state.rs:219-243`, `ConfigLayerEntry::config_folder` and `hooks_config_folder`):
- **User:** the parent of `$CODEX_HOME/config.toml`, which is `~/.codex`. A selected profile `${CODEX_HOME}/<name>.config.toml` has the same folder.
- **Project:** `<dir>/.codex/` for every directory from the project root down to the cwd (`config/src/loader/mod.rs:1625-1724`).
  - The project root is the nearest ancestor that contains a root marker. The default marker is `[".git"]` (`config/src/project_root_markers.rs:5`).
  - The ordering is from the root (lowest precedence) to the cwd (highest).
  - A `.codex` folder equal to `CODEX_HOME` is skipped (`loader/mod.rs:1720-1724`).
  - For linked git worktrees, hooks come from the matching folder in the root checkout (`loader/mod.rs:1145-1150` and `1812-1836`).
- **System:** the parent of the system `config.toml`. That is `/etc/codex/` on Unix and `%ProgramData%\OpenAI\Codex\` on Windows (`loader/mod.rs:123-134`).
- **MDM, cloud, session-flags and legacy-managed layers:** no folder, so `config.toml`-style hooks only. Paths are synthetic, for example `<session-flags>/config.toml` (`discovery.rs:402-423`).

**Managed requirement hooks [V].**
- `requirements.toml` has a top-level `[hooks]` table (`config/src/config_requirements.rs:1067`, `pub hooks: Option<ManagedHooksRequirementsToml>`).
- That struct is `{ managed_dir, windows_managed_dir, #[serde(flatten)] hooks: HookEventsToml }` (`config/src/hook_config.rs:219-252`).
- These hooks are loaded first, as "Required": a hook that fails to load stops the session (`discovery.rs:118-127` and `208-242`; `registry.rs:83-89`).
- `requirements.toml` lives at `/etc/codex/requirements.toml` on Unix and `%ProgramData%\OpenAI\Codex\requirements.toml` on Windows (`loader/mod.rs:113-114`).

**Order and merging [V].**
- Hooks are additive. Every enabled layer contributes handlers in order from low to high precedence (`discovery.rs:129`), followed by plugin hooks (`discovery.rs:190-198`). Nothing overrides anything else.
- Layer precedence (`config/src/config_layer_source.rs:33-51`): PackagedDefaults -10, Mdm 0, System 10, EnterpriseManaged 15, User 20 (21 with a profile), Project 25, SessionFlags 30, LegacyManagedFile 40, LegacyManagedMdm 50.
- Disabled layers contribute no hooks, because `layers_low_to_high()` filters `is_disabled()` (`state.rs:603-606`). An untrusted project is a disabled layer.

**Feature gate [V].** Feature `hooks` is `Stage::Stable` with `default_enabled: true` (`features/src/lib.rs:1333-1338`). The legacy alias is `codex_hooks` (`features/src/legacy.rs:49`). `[features] hooks = false` turns hooks off.

**JSON / TOML schema [V]** (`config/src/hook_config.rs`):
- **`HooksFile`**, the top level of `hooks.json`: `{ "description"?: string, "hooks": HookEventsToml }`. It is `#[serde(deny_unknown_fields)]` (lines 10-17).
- **`HookEventsToml`:** the keys are `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop` and `Interrupt`. Each maps to `Vec<MatcherGroup>` (lines 35-61).
- **`MatcherGroup`:** `{ matcher?: string, hooks: [HookHandlerConfig] }` (lines 153-159).
- **`HookHandlerConfig`** is `#[serde(tag="type")]` (lines 161-201):
  - `"command"`: `command` (required), `commandWindows` (alias `command_windows`), `timeout` (u64 seconds), `async` (bool, default false), `statusMessage`, `additionalContextLimit` (usize tokens, default 2500, 0 disables spilling).
  - `"mcp_tool"`: `server`, `tool`, `input` (a JSON object that must be representable as TOML), `timeout`, `statusMessage`.
  - `"prompt"` and `"agent"` are parsed, but discovery skips them with the warning "prompt/agent hooks are not supported yet" (`discovery.rs:637-656`).
  - There is **no** `"http"` type and **no** `args`, `if`, `shell` or `onFailure` [V: none of these fields or variants exist].
  - [I] An `http` handler fails serde parsing. For `hooks.json`, that discards the whole file with a "failed to parse hooks config" warning (`discovery.rs:359-368`).
  - [I] Unknown handler fields such as `args` are ignored silently, because the enum has no `deny_unknown_fields`. A Claude-style exec-form hook would therefore run `command` with no arguments.
- **`config.toml` form [V]** (`core/tests/suite/hooks.rs:600-668`; `config/src/hooks_tests.rs:193-205`):
  ```toml
  [[hooks.PreToolUse]]
  matcher = "Bash"
  [[hooks.PreToolUse.hooks]]
  type = "command"
  command = 'python3 /path/hook.py'
  timeout = 5
  statusMessage = "..."
  ```
  The same `[hooks]` table also holds `[hooks.state."<key>"]` (`HooksToml`, lines 19-25).
- **Event names [V]:** `hooks/src/lib.rs:23-36`. Matchers apply only to PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, SessionStart, SessionEnd, SubagentStart and SubagentStop. A matcher on UserPromptSubmit, Stop or Interrupt is ignored (`lib.rs:43-53`; `events/common.rs:112-128`).

**`commandWindows` [V].**
- During discovery, `let command = if cfg!(windows) { command_windows.unwrap_or(command) } else { command };` (`discovery.rs:515-519`; test at `discovery.rs:1632-1674`).
- An empty or whitespace-only command is skipped with a warning (`discovery.rs:520-526`).

**Defaults [V].**
- `timeout`: `timeout_sec.unwrap_or(600).max(1)`. SessionEnd and Interrupt default lower and are clamped (`discovery.rs:742-766`).
- `async`: forced to run synchronously on SessionEnd. Async hooks cannot apply control effects (`engine/mod.rs:144-159`).

### 2. Trust

**Every non-managed hook needs per-hook trust [V].** This applies to user hooks in `~/.codex` as well as project hooks.
- `hook_trust_status` (`discovery.rs:796-813`):
  - builtin → Trusted
  - `is_managed` → Managed
  - otherwise, `trusted_hash == current_hash` → Trusted
  - `Some(other)` → Modified
  - `None` → Untrusted
- A handler is admitted only if `enabled && (bypass_hook_trust || trust_status is Managed|Trusted)` (`discovery.rs:715-736`).
- Untrusted and Modified hooks still appear in `hooks/list` with their `current_hash`, but they never run.
- `is_managed` is true for System, Mdm, EnterpriseManaged and both LegacyManaged layers, and for requirements hooks. It is false for User, Project, SessionFlags, PackagedDefaults and plugins (`discovery.rs:825-841` and `:231`).
- The app-server test shows a user `config.toml` hook listed as `trust_status: Untrusted` (`app-server/tests/suite/v2/hooks_list.rs:215-240`).
- Core integration tests write `~/.codex/hooks.json` and must call `trust_discovered_hooks` before the hooks run (`core/tests/common/hooks.rs:9-69`).

**Project hooks are gated twice [V].**
1. The project layer itself must be trusted. Otherwise it is "loaded but disabled" with the reason "To load project-local config, hooks, and exec policies, add <key> as a trusted project in <user config.toml>" (`loader/mod.rs:1113-1131`). The project must be marked trusted under `[projects."<path>"]` (`trust_level` / `TrustLevel::Trusted`, `loader/mod.rs:1045-1063`).
2. Each hook must also have a matching `trusted_hash`.

**Where trust state is stored [V].**
- In `hooks.state` of the **user** `config.toml` (or session `-c` flags). Only User and SessionFlags layers are read, including disabled ones. Project and managed layers cannot write state (`hooks/src/config_rules.rs:8-66`).
- Fields are merged per key, with later layers winning field by field.
- Each state entry is `HookStateToml { enabled?: bool, trusted_hash?: string }` (`hook_config.rs:27-33`).

**Key layout [V].**
- `format!("{key_source}:{event_label}:{group_index}:{handler_index}")` (`hooks/src/lib.rs:113-123`).
- `event_label` is snake case: `pre_tool_use`, `permission_request`, `post_tool_use`, and so on (`lib.rs:95-110`).
- `key_source` is the absolute path, via `display()`, of the `hooks.json` or `config.toml` that declared the hook (`discovery.rs:174` and `667`). Plugin hooks use `plugin_hook_key_source(plugin_id, relative_path)` (`discovery.rs:279-282`; `declarations.rs`).
- Example: `[hooks.state."/home/u/.codex/hooks.json:pre_tool_use:0:0"]` with `trusted_hash = "sha256:…"` (`config/src/hooks_tests.rs:193-205`).
- On Windows the path contains backslashes [I].
- The TUI writes trust with `config/batchWrite` using `key_path: "hooks.state"`, value `{ "<key>": { "trusted_hash": "<current_hash>" } }` and `MergeStrategy::Upsert` (`tui/src/hooks_rpc.rs:58-92`). There is a startup review prompt with the choices Review / Trust all and continue / Continue without trusting (`tui/src/startup_hooks_review.rs:46-51`).

**What is hashed [V]** (`discovery.rs:768-794`, `config/src/fingerprint.rs:54-66`):

1. Codex builds `NormalizedHookIdentity { event_name: <snake label>, #[serde(flatten)] MatcherGroup { matcher: <effective matcher>, hooks: [<normalized handler>] } }`.
2. The normalized command handler is `{type:"command", command:<resolved command (commandWindows on Windows)>, timeout:<effective timeout, e.g. 600>, async:<bool>, statusMessage?, additionalContextLimit? (omitted if unset or == 2500)}`. `commandWindows` is set to None (`discovery.rs:561-568`). Plugin `${VAR}` substitution happens *after* this, so it is not hashed (`569-571`).
3. The identity is converted to a TOML value, then to `serde_json` with `sort_all_objects()`, serialized as compact JSON, and hashed with SHA-256. The result is formatted as `"sha256:<hex>"`.

Verification of the hash:
- I reproduced `version_for_toml` in Python as `json.dumps(v, sort_keys=True, separators=(",",":"), ensure_ascii=False)` followed by sha256. It matches the repository test vector `sha256:a5e4c8fb…3176` (`config/src/fingerprint_tests.rs`).
- Example identity (derived, not checked against a running Codex): `{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"python3 C:/hooks/guard.py","timeout":600,"type":"command"}],"matcher":"Bash"}` hashes to `sha256:5c6b5528…e720`.
- None fields such as `matcher` are omitted. [I/V] Serializing a matcher-less hook is `unreachable!`-guarded, so TOML must skip None rather than fail.

**Disable and bypass [V].**
- `enabled = false` in state disables a hook, except managed and builtin hooks (`discovery.rs:815-817`).
- `--dangerously-bypass-hook-trust` (`utils/cli/src/shared_options.rs:61-64`; it is a runtime override only, `core/src/config/mod.rs:2699` and `3396-3405`) runs enabled hooks without trust. It still respects `enabled = false` (`discovery.rs:1479-1542`).

**`allow_managed_hooks_only` [V].**
- It is read only from requirements (`config_requirements.rs:1057`; `discovery.rs:107-116`).
- When true, `policy.allows` rejects every non-managed source (`discovery.rs:88-92`, `143-145` and `461-463`). That covers User, Project, SessionFlags and plugin hooks.
- Docs: "Admins can set top-level `allow_managed_hooks_only = true` in `requirements.toml` … only supported in `requirements.toml`; putting it in `config.toml` does not enable managed-hooks-only mode" (`docs/config.md`).

### 3. PreToolUse

**stdin JSON [V].** `PreToolUseCommandInput` (`hooks/src/schema.rs:276-296`; `events/pre_tool_use.rs:175-191`):
```json
{"session_id":"…","turn_id":"…","agent_id":"…?","agent_type":"…?",
 "transcript_path":"…|null","cwd":"<abs>","hook_event_name":"PreToolUse",
 "model":"<slug>","permission_mode":"default|bypassPermissions",
 "tool_name":"…","tool_input":{…},"tool_use_id":"<call id>"}
```
- `agent_id` and `agent_type` are present only in thread-spawned subagents.
- `permission_mode` is `bypassPermissions` when the approval policy is Never, and `default` otherwise (`core/src/hook_runtime.rs:1032-1040`). The schema also allows acceptEdits, plan and dontAsk (`schema.rs:843-851`).

**Tool names and `tool_input` shapes [V].**
- **Shell (`exec_command` / unified exec):** `tool_name: "Bash"`, `tool_input: {"command": "<cmd string>"}`, with no description or timeout (`core/src/tools/handlers/unified_exec/exec_command.rs:506-516`; `tools/hook_names.rs:53-56`). `write_stdin` emits no PreToolUse (`unified_exec/write_stdin.rs:144-149`).
- **`apply_patch`:** `tool_name: "apply_patch"`, `tool_input: {"command": "<full patch text>"}` (`handlers/apply_patch.rs:395-400`). For **matching only**, the aliases `Write` and `Edit` also select handlers. stdin still says `apply_patch` (`hook_names.rs:28-39`; `events/common.rs:137-146`). Each handler runs once even if several aliases match (`engine/dispatcher.rs:41-75`).
- **`spawn_agent`:** alias `Agent` (`hook_names.rs:41-51`).
- **MCP tools:** `mcp__<server>__<tool>` with the tool arguments object (`handlers/mcp.rs:109-123` and `449-458`).
- **Other function tools:** the flattened tool name, with the parsed JSON arguments, or `{}` when empty (`tools/registry.rs:150-159` and `942-960`).

**Matcher semantics [V]** (`engine/matcher.rs:11-40`):

| Matcher | How it matches |
| :- | :- |
| `""`, `"*"` or omitted | Matches everything |
| Only `[A-Za-z0-9_\|]` | Exact match on any `\|`-separated alternative |
| Anything else | Rust `regex` crate, **unanchored** `is_match`, so `^Bash` matches `BashOutput` |

- An invalid regex skips that group with a warning (`discovery.rs:488-503`).
- Tests are in `events/common.rs:157-313`. For example, `mcp__memory` does NOT match `mcp__memory__create_entities`, while `mcp__memory__.*` does.
- Differences from Claude Code: no `-`, space or `,` in exact matching, and the regex dialect is Rust rather than JavaScript.

**Execution [V].**
- All matched synchronous handlers run concurrently using `FuturesUnordered`.
- Async handlers are fire-and-forget and cannot block (`dispatcher.rs:115-170`).
- The outcome blocks if any handler blocked. `block_reason` is the first one in configured order (`pre_tool_use.rs:115-118`).
- `updatedInput` is taken from the hook that finished last (`pre_tool_use.rs:149-167`).

**Output parsing for synchronous handlers [V]** (`pre_tool_use.rs:193-310`; `engine/output_parser.rs:121-182` and `369-507`):
- **Exit 0, empty or plain-text stdout:** no-op.
- **Exit 0, JSON object:** parsed with `deny_unknown_fields` (`schema.rs:88-99`, `127-140` and `242-266`). Any unknown field, a wrong type, or a non-object makes the output invalid. If the stdout starts with `{` or `[`, the run is Failed with "hook returned invalid pre-tool-use JSON output". This fails open.
- **Universal fields:** `continue` (default true), `stopReason`, `suppressOutput`, `systemMessage`. `continue:false`, any `stopReason` and `suppressOutput:true` are all *unsupported* for PreToolUse → Failed, fail-open (`output_parser.rs:369-379`). `systemMessage` becomes a warning entry.
- **`hookSpecificOutput`:** `{hookEventName (required), permissionDecision: allow|deny|ask, permissionDecisionReason, updatedInput, additionalContext}`.
  - `deny` with a non-empty reason → **block**, using the trimmed `permissionDecisionReason`.
  - `deny` without a reason → Failed, fail-open.
  - `allow` **requires** `updatedInput`, and the input is rewritten. `allow` alone gives "unsupported permissionDecision:allow" → Failed, fail-open.
  - `updatedInput` without `allow` → Failed.
  - `ask` gives "unsupported permissionDecision:ask" → Failed, fail-open.
  - `permissionDecisionReason` without a decision → Failed.
  - `additionalContext` is added to the model context unless the output was invalid.
- **Legacy top-level form** (used only when `hookSpecificOutput` carries no decision, reason or `updatedInput`): `{"decision":"block","reason":"…"}` → block, and a reason is required. `"approve"` gives "unsupported decision:approve" → fail-open. A `reason` without a `decision` → Failed.
- **Exit 2:** blocks with the trimmed **stderr** as the reason. Stdout is ignored. Exit 2 with empty stderr → Failed, fail-open.
- **Any other exit code, a signal, a spawn error or a timeout:** Failed, fail-open. The timeout error text is "hook timed out after {n}s" (`command_runner.rs:276-321`).
- **Effect of a block:** the model gets `"Command blocked by PreToolUse hook: {reason}. Command: {command}"` for Bash and apply_patch, and `"Tool call blocked by PreToolUse hook: {reason}. Tool: {name}"` for other tools (`core/src/hook_runtime.rs:226-245`).
- **Allow does not bypass approvals [I].** A non-blocking result just continues into the normal approval and sandbox path, with the rewritten input if any (`tools/registry.rs:700-740`). PreToolUse has no "allow → skip approval" path. Approval decisions are a separate `PermissionRequest` event, whose `hookSpecificOutput.decision.behavior` is `allow|deny` (`output_parser.rs:184-205` and `416-429`).

**Process environment [V]** (`engine/command_runner.rs`):
- **cwd:** the turn's local-environment cwd, falling back to the session cwd. It is not the project root (`command_runner.rs:226`; `hook_runtime.rs:248-253`).
- **stdin:** the JSON followed by EOF. Stdout and stderr are captured, and the stdin write counts toward the timeout (`:259-279`).
- **Environment:** a snapshot of the Codex process environment taken at session start, plus handler env. Handler env is only `PLUGIN_ROOT`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_DATA` and `CLAUDE_PLUGIN_DATA` for plugin hooks (`discovery.rs:262-270`).
- Both launchers clear the environment and then replay that snapshot, minus `NON_INHERITABLE_ENV_VARS` (`command_runner.rs:416-435`; `protocol/src/shell_environment.rs:15-28`).
- **No `CLAUDE_PROJECT_DIR`** is set (a GitHub code search for it in openai/codex returned 0 hits).

**Shell used [V].**
- `HooksConfig.shell_program/shell_args` come from the session's shell via `shell.derive_exec_args("", use_login_shell=false)`, with the program and the trailing empty argument stripped (`core/src/session/mod.rs:5289-5321`).
- `derive_exec_args` (`core/src/shell.rs:24-51`):
  - bash/zsh/sh → `<path> -c <cmd>`
  - PowerShell → `<path> -NoProfile -Command <cmd>`
  - cmd → `<path> /c <cmd>`
- The Windows default user shell is PowerShell (pwsh preferred, then powershell.exe), with `cmd.exe` as the fallback (`shell-command/src/shell_detect.rs:280-332` and `363-366`).
- **So on Windows, a hook normally runs as `pwsh.exe -NoProfile -Command <commandWindows or command>` [V + I].**
- If no shell is known, the command uses `%COMSPEC%` (falling back to `cmd.exe`) with `/C "<cmd>"`. For the COMSPEC default, or any shell whose args contain `/c`, the command line is passed as `raw_arg(format!(r#""{command_line}""#))`: it is wrapped in double quotes and not re-escaped. For PowerShell it is a normal `.arg(command_line)` (`command_runner.rs:384-410` and `439-462`).
- On Windows the process gets `CREATE_NO_WINDOW` and runs in a Job object. The process tree is killed on timeout (`command_runner.rs:228-357`).
- On Unix, with no shell known, the command runs as `$SHELL -lc` (falling back to `/bin/sh`), in a new session.

---

## CLAUDE CODE

### 4. Settings locations, merge order and schema

**Locations [V]** (hooks.md "Hook locations", lines 251-262):

| Location | Scope |
| :- | :- |
| `~/.claude/settings.json` | All your projects |
| `.claude/settings.json` | Single project |
| `.claude/settings.local.json` | Single project, gitignored |
| Managed policy settings | Organization-wide |
| Plugin `hooks/hooks.json` | When the plugin is enabled |
| Skill frontmatter | The rest of the session, once the skill is invoked |
| Subagent frontmatter | While that subagent runs |

- On Windows, `~/.claude` is `%USERPROFILE%\.claude`. `CLAUDE_CONFIG_DIR` relocates it (settings.md:440).
- Managed files are `managed-settings.json` plus `managed-settings.d/*.json` in:
  - `/Library/Application Support/ClaudeCode/` on macOS
  - `/etc/claude-code/` on Linux and WSL
  - `C:\Program Files\ClaudeCode\` on Windows, but not the legacy `C:\ProgramData\ClaudeCode` (managed-settings.md:40-42, 111-118)
- Policy can also come from MDM or server-managed settings.
- In a git repository, `settings.local.json` is read and written at the repository root, with exceptions such as Windows (settings.md:475-484). The shared `.claude/settings.json` is read from the primary working directory (settings.md:492).

**Merge [V].**
- "Hook entries merge across settings levels rather than replacing each other" (hooks.md:280). `settings-reference.md:4114` says the same.
- Scalar precedence, highest first: Managed > `--settings` CLI > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json` (settings.md:624-640).
- "All matching hooks run in parallel. If you define the same handler in more than one settings file, it runs once" (hooks.md:412).

**Schema [V]** (hooks.md:239-245, 402-470, 506-600; settings-reference.md:4110-4116):
```json
{"hooks": {"PreToolUse": [ {"matcher": "Bash", "hooks": [ {"type": "command", "command": "...", ...} ]} ]}}
```
- Handler `type` is one of `"command"`, `"http"`, `"mcp_tool"`, `"prompt"` or `"agent"`.
- **Common fields:**
  - `if`: a single permission rule, such as `"Bash(git *)"` or `"Edit(*.ts)"`. Evaluated only on tool events.
  - `timeout`: seconds.
  - `statusMessage`
  - `once`: honored only in skill frontmatter.
- **Command fields:**
  - `command`
  - `args`: exec form, no shell.
  - `async`
  - `asyncRewake`
  - `shell`: `"bash"` or `"powershell"`.
  - `onFailure`: `"continue"` (default) or `"block"`, from v2.1.295.
- **HTTP fields:** `url`, `headers`, `allowedEnvVars`, `onFailure`.
- **`mcp_tool` fields:** `server`, `tool`, `input` with `${path}` templating.
- **`prompt` and `agent` fields:** `prompt`, `model`.
- Plugin `hooks.json` may also carry a top-level `"description"`.

**Matcher syntax [V]** (hooks.md:287-299, 355-366):

| Matcher | How it matches |
| :- | :- |
| `"*"`, `""` or omitted | Match all |
| Only letters, digits, `_`, `-`, spaces, `,` and `\|` | Exact string, or a list split on `\|` or `,` (optional surrounding whitespace) |
| Anything else | JavaScript `RegExp.prototype.test`, **unanchored** |

- `FileChanged` and `StopFailure` use the narrower set of letters, digits, `_` and `|`.
- MCP tool names are `mcp__<server>__<tool>`. `mcp__memory` alone matches nothing; use `mcp__memory__.*`. Plugin MCP tools are `mcp__plugin_<plugin>_<server>__<tool>`.
- On Windows, match `Bash|PowerShell`. Without Git Bash, the Bash tool is not registered at all (hooks.md:1771-1775).

**Environment [V].**
- Handlers "run in the current directory with Claude Code's environment" (hooks.md:414). The process inherits the parent environment minus `OTEL_*` exporter variables, and minus scrubbed variables when `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` (hooks.md:754).
- The placeholders and exported variables `CLAUDE_PROJECT_DIR` (the project root where the session started), `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` are set in both exec and shell form (hooks.md:496, 603-607).
- Also set: `CLAUDE_CODE_REMOTE` (`"true"` in web sessions), `CLAUDE_EFFORT`, and `CLAUDE_ENV_FILE` (SessionStart only) (hooks.md:416, 1292).
- In PowerShell shell form, `${CLAUDE_PROJECT_DIR}` is rewritten to `${env:CLAUDE_PROJECT_DIR}` (hooks.md:4130).

**Default timeout [V].** 600 s for command, http and mcp_tool; 30 s for prompt; 60 s for agent. Command, http and mcp_tool drop to 30 s on UserPromptSubmit and the model-switch events, and to 10 s on MessageDisplay (hooks.md:426).

**Shell [V].**
- Shell form uses "`sh -c` on macOS and Linux, Git Bash on Windows, or PowerShell when Git Bash isn't installed".
- `"shell":"powershell"` forces PowerShell. Claude Code auto-detects `pwsh.exe`, falling back to `powershell.exe`.
- Exec form (`args` present) spawns `command` directly. On Windows it must be a real executable; `.cmd` and `.bat` shims don't work (hooks.md:460, 471-474, 4107-4110).

### 5. PreToolUse input/output and trust

**Input [V]** (hooks.md:728-778, 1695-1712):
- `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `permission_mode`, `effort{level}`, `hook_event_name: "PreToolUse"`, `tool_name`, `tool_input`, `tool_use_id`, plus `agent_id` and `agent_type` inside subagents.
- `permission_mode` is one of `default`, `plan`, `acceptEdits`, `auto`, `dontAsk` or `bypassPermissions`.
- MCP tools also get `mcp_server{name, source}`, from v2.1.274.
- For Write, Edit and Read, `tool_input.file_path` is always absolute, with `~` and relative paths expanded. It uses backslashes on Windows.

**Exit codes [V]** (hooks.md:780-871):
- **0:** success. JSON is applied if stdout is exactly one JSON object.
- **2:** blocking error, with stderr as the reason. Any JSON that passes validation is still read but can't override the block, so even `allow` doesn't let the call through. A JSON deny reason replaces stderr.
- **Other:** non-blocking error ("`<hook> hook error`" notice), and the action proceeds. But if stdout is valid JSON, it counts as success and the JSON decides, even on exit 1.
- **Invalid JSON with exit 0:** non-blocking error.
- **Timeout:** a timed-out PreToolUse command, http or mcp_tool hook does **not** block unless `onFailure: "block"` (hooks.md:872-880, 881-929).

**JSON [V]** (hooks.md:1008-1015, 1903-1925):
- **Universal fields:**
  - `continue` (false stops Claude entirely; takes precedence)
  - `stopReason`
  - `suppressOutput` (no effect)
  - `systemMessage` (shown to the user)
  - `terminalSequence`
- **`hookSpecificOutput`:** `{hookEventName:"PreToolUse", permissionDecision, permissionDecisionReason, updatedInput, additionalContext}`.
  - `permissionDecision`:
    - `"allow"` skips the permission prompt, except for some exceptions.
    - `"deny"` prevents the call.
    - `"ask"` prompts the user.
    - `"defer"` is honored only in `-p` mode; interactive sessions ignore it with a warning.
    - Deny and ask permission rules are still evaluated regardless.
  - `permissionDecisionReason`:
    - For ask, it is shown to the user.
    - For deny, it is shown to Claude.
    - For allow and defer, it goes to the debug log only.
  - `updatedInput` replaces the **entire** input. It works with allow (auto-approve) or ask. It is ignored for defer.
  - `additionalContext` is added next to the tool result.
- With multiple hooks, precedence is `deny > defer > ask > allow`.
- Exit 2 routes like deny.
- **Legacy:** "PreToolUse previously used top-level `decision` and `reason` fields, but these are deprecated for this event … The deprecated values `"approve"` and `"block"` map to `"allow"` and `"deny"` respectively" (hooks.md:1937). That is still accepted per the docs.

**Trust [V]** (hooks.md:4088-4095; permissions.md:708-750):
- **Interactive sessions:** "Claude Code holds back hooks from every settings file, including your own `~/.claude/settings.json`, until you accept the workspace trust dialog for the folder, or for a parent directory whose trust extends to it."
- **`-p` and SDK sessions:** these never show the dialog and treat the folder as trusted.
- Trust is keyed on the git repository root (the main checkout for worktrees), or on the start directory outside a repository. It covers subfolders except nested repositories.
- Trust is stored as `projects["<path>"].hasTrustDialogAccepted = true` in `~/.claude.json` (permissions.md:750).
- In the "trusted only a parent folder" situation, hooks in settings files are "Used" (permissions.md:743).
- Project-subagent frontmatter hooks need trust in the exact folder (permissions.md:745).
- There is no per-hook hash trust.
- **`disableAllHooks`** (settings-reference.md:4033-4058):
  - It is a scalar following precedence, so a project `false` overrides a user `true`.
  - Outside managed settings it cannot disable managed hooks, managed force-enabled plugin hooks, or SDK hooks.
  - In managed settings it disables all hooks.
- **`allowManagedHooksOnly`** (managed scope only; settings-reference.md:4004-4031): only managed hooks, SDK hooks and hooks from plugins force-enabled in managed `enabledPlugins` run. User, project, local, other-plugin and agent-frontmatter hooks are blocked.

### 6. Tool names and `tool_input` shapes [V] (hooks.md:1714-1801)

| Tool | `tool_input` |
| :- | :- |
| `Bash` | `{command, description?, timeout? (ms), run_in_background?}` |
| `PowerShell` | Same as Bash: `{command, description?, timeout?, run_in_background?}` |
| `Write` | `{file_path (absolute), content}` |
| `Edit` | `{file_path, old_string, new_string, replace_all}` |
| `Read` | `{file_path, offset?, limit?}` |
| `Glob` | `{pattern, path?}` |
| `Grep` | `{pattern, path?, glob?, output_mode?, -i?, multiline?}` |

- **MultiEdit [V absent / I old shape]:** it appears nowhere in the current docs. It is not in the tools table (tools-reference.md:20-67) or in the PreToolUse tool list. The current list includes `NotebookEdit`, which uses `Edit(...)` permission rules. From memory, which is unverified, the old shape was `{file_path, edits:[{old_string,new_string,replace_all?}]}`.
- There is **no delete or move tool** in either product. Deletes and moves are shell commands (Bash or PowerShell in Claude Code; `Bash` in Codex) [I, based on the tool tables].
- [I] Mapping suggestion for winhand:
  - command → `Bash`, or `PowerShell` on Windows. The docs say to match `Bash|PowerShell`.
  - write → `Write`.
  - edit → `Edit`.
  - read → `Read`.
  - delete and move → `Bash` or `PowerShell` with the equivalent command string.
  - For Codex hooks, write and edit could be presented as `apply_patch` with `{command: <patch>}`. Codex matchers `Write` and `Edit` select apply_patch hooks only through Codex's internal alias, so a Codex hook with matcher `Write` expects stdin `tool_name: "apply_patch"`.
