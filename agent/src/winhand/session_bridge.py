"""What a cloud Claude Code session gets when it starts (and after /clear, a resume or a compaction):
this machine's rules and skill names, as the SessionStart hook's additionalContext.

The repository's .claude/settings.json runs a command hook (see `winhand bridge init`) that POSTs the
hook input to the relay's /hook/session-start with a hook token; the relay forwards it here as the
`hook` identity. That identity reaches only this endpoint, which only reads files: it runs no tool
and no command but `git ls-files`. Rules the cloud checkout already has (files git tracks) are left
out; Claude Code reads those there itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess

from . import config, redact, workspace

EVENTS = ("session-start",)
BUDGET_CHARS = 9_000  # Claude Code keeps 10,000 characters of additionalContext
LEFT_OUT_CHARS = 600  # room kept for naming the rules that do not fit
SKILLS_CHARS = 2_500  # room first kept for skill names; more when the rules leave some
HOOK_USER = "hook"


def _tracked(root: str, paths: list[str]) -> set[str]:
    """The paths among `paths` (inside `root`) that git tracks there; none when git cannot say."""
    inside = {os.path.relpath(p, root): p for p in paths}
    if not inside:
        return set()
    try:
        done = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--", *inside],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    if done.returncode != 0:
        return set()
    listed = {
        os.path.normcase(os.path.normpath(p)) for p in done.stdout.decode("utf-8", "replace").split("\0") if p
    }
    return {full for rel, full in inside.items() if os.path.normcase(os.path.normpath(rel)) in listed}


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))]
        ) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def _size(text: str) -> int:
    """Length as Claude Code counts it (JavaScript strings: UTF-16 code units)."""
    return len(text) + sum(1 for c in text if ord(c) > 0xFFFF)


def _skills_line(names: list[str], room: int) -> str:
    """'Skills on the PC: a, b, ...' in at most `room` characters, saying how many were left out."""
    if not names:
        return ""
    head = "Skills on the PC: "
    full = head + ", ".join(names)
    if _size(full) <= room:
        return full
    kept: list[str] = []
    used = _size(head) + 30  # " (and N more)"
    for name in names:
        if used + _size(name) + 2 > room:
            break
        kept.append(name)
        used += _size(name) + 2
    return head + ", ".join(kept) + f" (and {len(names) - len(kept)} more)" if kept or room >= 40 else ""


def session_start(repo: str | None, cfg: config.Config | None = None, with_skill_names: bool = True) -> str:
    """The additionalContext for a session of `repo` (a remote URL or owner/name; may be empty): the
    rules, and the skills' names unless the skills come in a part of their own."""
    cfg = cfg or config.load()
    redactor = redact.current()

    def mask(text: str) -> str:
        text = workspace._sendable(text)
        return text if redactor.empty else redactor.data(text)[0]

    machine = socket.gethostname() or "this PC"
    # owner/name, or the address with anything up to an @ (a credential) cut off; kept short
    named = (workspace.repo_key(repo) or repo.rsplit("@", 1)[-1])[:100] if repo else None
    checkout, missing = None, False
    if repo:
        try:
            checkout = workspace.find_repo(cfg, repo)["checkout"]
        except ValueError:
            missing = True
    root = workspace.project_root(checkout) if checkout else None
    rules = workspace._collect(cfg, root, checkout, checkout)
    ordered = rules.ordered()  # imported files too
    project_files = [
        i["path"] for i in ordered if i.get("scope") == "project" and root and _inside(i["path"], root)
    ]
    in_cloud = _tracked(root, project_files) if root else set()

    lines = [
        f"Rules and skills of the PC {machine!r}, sent by winhand at session start"
        + (f" (its checkout of {named}: {checkout})." if checkout else "."),
    ]
    if missing:
        lines.append(f"This PC has no checkout of {named}; only its own rules and skills follow.")
    elif not repo:
        lines.append("The repository was not named; only the PC's own rules and skills follow.")
    lines.append(
        "Skills and their scripts are on that PC: read them and run them with the winhand tools. "
        + (
            f'For everything (and the rules of other folders) call context(repo="{named}").'
            if named
            else "For everything, call context()."
        )
    )
    intro = mask("\n".join(lines))

    sections: list[tuple[str, str | None, str]] = []  # (path, text or None, why it has no text)
    for item in ordered:
        if item.get("scope") == "project" and item["path"] in in_cloud:
            continue  # the cloud checkout has it
        text = rules.texts.get(id(item))
        why = str(item.get("skipped") or item.get("error") or "")
        sections.append((mask(item["path"]), None if text is None else mask(text), mask(why)))

    names = (
        [
            mask(n)
            for n in sorted({s["name"] for s in workspace.skills(cfg, root, rules.refused)}, key=str.lower)
        ]
        if with_skill_names
        else []
    )

    budget = BUDGET_CHARS - _size(intro) - 2
    parts = [intro]
    skills_room = min(_size(_skills_line(names, 10**9)), SKILLS_CHARS)  # held back for now
    budget -= skills_room + 2 if names else 0
    left_out_room = min(LEFT_OUT_CHARS, max(budget, 0))  # kept for naming what does not fit
    budget -= left_out_room

    left_out: list[str] = []
    for path, text, why in sections:
        head = f"## {path}"
        if text is None:
            entry = f"{head}\n({why if why.startswith('not read') else 'not read: ' + why})" if why else head
        else:
            entry = f"{head}\n{text}"
        if _size(entry) + 2 <= budget:
            parts.append(entry)
            budget -= _size(entry) + 2
            continue
        note = "\n[cut here: read the rest with context() or fs_read]"
        room = budget - _size(head) - _size(note) - 3
        if text is not None and room >= 400 and not left_out:
            cut = text[:room]
            while _size(cut) > room:  # characters past the BMP count twice: trim half the excess
                cut = cut[: -max(1, (_size(cut) - room + 1) // 2)]
            parts.append(f"{head}\n{cut}{note}")
            budget -= _size(head) + _size(cut) + _size(note) + 3
            continue
        left_out.append(path)
    if left_out:
        tail = "Not included for lack of room (read them with context() or fs_read): "
        listed: list[str] = []
        for path in left_out:
            if _size(tail) + sum(_size(p) + 2 for p in listed) + _size(path) + 30 > left_out_room:
                break
            listed.append(path)
        more = f" and {len(left_out) - len(listed)} more" if len(listed) < len(left_out) else ""
        parts.append(tail + "; ".join(listed) + more)
        budget += left_out_room - _size(parts[-1]) - 2
    else:
        budget += left_out_room
    if names:
        parts.append(_skills_line(names, skills_room + max(budget, 0)))  # whatever room is left
    text = "\n\n".join(p for p in parts if p)
    while _size(text) > BUDGET_CHARS:  # never past the budget, whatever the arithmetic above
        text = text[: -max(1, _size(text) - BUDGET_CHARS)]
    return text


SKILL_DESC_CHARS = workspace.SKILL_DESC_LIMIT  # as Claude Code cuts description + when_to_use
MIN_DESC_CHARS = 80


def _checkout(repo: str | None, cfg: config.Config) -> tuple[str | None, str | None]:
    """(project root of the local checkout of `repo`, owner/name to show) — (None, …) when none."""
    named = (workspace.repo_key(repo) or repo.rsplit("@", 1)[-1])[:100] if repo else None
    if not repo:
        return None, named
    try:
        return workspace.project_root(workspace.find_repo(cfg, repo)["checkout"]), named
    except ValueError:
        return None, named


def skills_text(repo: str | None, cfg: config.Config | None = None, budget: int = BUDGET_CHARS) -> str:
    """The skills part of a session's start: each skill's name and description, as Claude Code lists
    them; descriptions shortened evenly when they do not all fit."""
    cfg = cfg or config.load()
    redactor = redact.current()

    def mask(text: str) -> str:
        text = workspace._sendable(text)
        return text if redactor.empty else redactor.data(text)[0]

    root, _ = _checkout(repo, cfg)
    rules = workspace._collect(cfg, root, root, root)  # for the guard's read rules
    found = workspace.skills(cfg, root, rules.refused, desc_chars=SKILL_DESC_CHARS)
    listed = [(mask(s["name"]), mask(s["description"])) for s in found if s.get("model_invocable", True)]
    on_request = [mask(s["name"]) for s in found if not s.get("model_invocable", True)]
    machine = socket.gethostname() or "this PC"
    head = mask(
        f"Skills on the PC {machine!r}. They and their scripts are on that PC: when one fits the task, call "
        "skill(name) for its SKILL.md and files, follow it, and run its scripts there with run."
    )
    if not found:
        return head + "\n(no skills found)"
    tail = f"Used only when asked for by name: {', '.join(on_request)}" if on_request else ""
    room = budget - _size(head) - (_size(tail) + 2 if tail else 0) - 2

    def render(cap: int, described: int) -> list[str]:
        out = []
        for i, (name, desc) in enumerate(listed):
            if i < described and desc:
                short = desc if len(desc) <= cap else desc[: cap - 1] + "…"
                out.append(f"- {name}: {short}")
            else:
                out.append(f"- {name}")
        return out

    def size(lines: list[str]) -> int:
        return sum(_size(x) + 1 for x in lines)

    lines = render(SKILL_DESC_CHARS, len(listed))
    if size(lines) > room:
        low, high = MIN_DESC_CHARS, SKILL_DESC_CHARS  # the longest cap that fits
        while low < high:
            mid = (low + high + 1) // 2
            if size(render(mid, len(listed))) <= room:
                low = mid
            else:
                high = mid - 1
        lines = render(low, len(listed))
        described = len(listed)
        while size(lines) > room and described > 0:  # still too much: the last ones by name only
            described -= 1
            lines = render(MIN_DESC_CHARS, described)
    text = "\n".join([head, *lines] + ([tail] if tail else []))
    while _size(text) > budget:
        text = text[: -max(1, (_size(text) - budget + 1) // 2)]
    return text


PARTS = ("rules", "skills", "tools")


async def endpoint(request):
    """Starlette handler for POST /hook/{event}, for the relay's `hook` identity only."""
    from starlette.responses import JSONResponse, PlainTextResponse

    if request.headers.get("x-winhand-user") != HOOK_USER:
        return PlainTextResponse("forbidden", status_code=403)
    event = request.path_params.get("event")
    if event not in EVENTS:
        return PlainTextResponse("not found", status_code=404)
    raw = await request.body()
    try:
        hook_input = json.loads(raw or b"{}")
    except ValueError:
        hook_input = {}
    repo = (request.headers.get("x-winhand-repo") or "").strip()[:500] or None
    del hook_input  # startup, resume, clear and compact all get the same rules
    part = (request.headers.get("x-winhand-part") or "").strip().lower()
    if part and part not in PARTS:
        return PlainTextResponse("unknown part", status_code=404)
    if part == "skills":
        text = await asyncio.to_thread(skills_text, repo)
    elif part == "tools":
        from . import local_mcp

        text = await asyncio.to_thread(local_mcp.session_text, BUDGET_CHARS)
    else:  # "rules"; no part at all is a hook from 0.4.7, which also wants the skills' names
        text = await asyncio.to_thread(session_start, repo, None, not part)
    return JSONResponse({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


class HookIdentityOnly:
    """ASGI middleware: the relay's `hook` identity reaches /hook/... and nothing else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            users = {
                v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"x-winhand-user"
            }
            if HOOK_USER in users and not scope.get("path", "").startswith("/hook/"):
                from starlette.responses import PlainTextResponse

                await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ------------------------------------------------------------------ winhand bridge init / token

HOOK_MARK = "/hook/session-start"
MATCHER = "startup|resume|clear|compact"


def relay_origin(url: str) -> str:
    """https://<host> of the relay, from its agent URL (wss://<host>/agent) or any URL on it."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.strip())
    if parts.scheme not in ("wss", "https", "ws", "http") or not parts.hostname:
        raise ValueError(f"not a relay URL: {url!r}")
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    return f"{scheme}://{parts.netloc}"


def hook_command(origin: str, part: str | None = None) -> str:
    """A SessionStart command for a repository's .claude/settings.json (POSIX sh): in a cloud
    session only, POST the hook input to the relay with the repository's remote (credentials cut
    off) and which part of the start it asks for, and print the answer only when it is a 2xx;
    quiet and exit 0 whatever happens. The token comes from a Network secret (added by the egress
    proxy) or WINHAND_HOOK_TOKEN."""
    return (
        '[ "$CLAUDE_CODE_REMOTE" = true ] || exit 0; '
        'repo=$(git -C "${CLAUDE_PROJECT_DIR:-.}" remote get-url origin 2>/dev/null); repo=${repo##*@}; '
        "out=$(mktemp) || exit 0; "
        "code=$(curl -sS --max-time 25 -o \"$out\" -w '%{http_code}' -X POST "
        "-H 'content-type: application/json' "
        '-H "X-Winhand-Repo: $repo" '
        + (f"-H 'X-Winhand-Part: {part}' " if part else "")
        + '${WINHAND_HOOK_TOKEN:+-H "X-Winhand-Hook-Token: $WINHAND_HOOK_TOKEN"} '
        f"--data-binary @- '{origin}{HOOK_MARK}' 2>/dev/null); "
        'case "$code" in 2??) cat "$out";; esac; rm -f "$out"; exit 0'
    )


def hook_group(origin: str) -> dict:
    """Three hooks, run side by side, each with its own 10,000-character room: rules, skills, tools."""
    return {
        "matcher": MATCHER,
        "hooks": [
            {"type": "command", "timeout": 30, "command": hook_command(origin, part)} for part in PARTS
        ],
    }


def merge_settings(path: str, origin: str) -> dict:
    """Put the SessionStart hook into a .claude/settings.json, keeping everything else and replacing
    an earlier copy of it. Raises ValueError (and writes nothing) when the file is not a JSON object."""
    data: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            try:
                data = json.load(fh)
            except ValueError as exc:
                raise ValueError(f"{path} is not valid JSON ({exc}); fix it first") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f'"hooks" in {path} is not an object')
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ValueError(f'"hooks.SessionStart" in {path} is not a list')
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            ours = [
                h for h in group["hooks"] if isinstance(h, dict) and HOOK_MARK in str(h.get("command", ""))
            ]
            if ours:
                rest = [h for h in group["hooks"] if h not in ours]
                if not rest:
                    continue
                group = {**group, "hooks": rest}
        kept.append(group)
    kept.append(hook_group(origin))
    hooks["SessionStart"] = kept
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return data


def setup_text(origin: str) -> str:
    """What the person sets up in the cloud environment, printed by `winhand bridge init`."""
    host = origin.split("://", 1)[1]
    return f"""云端环境里还要做一次设置（会话标题栏的云环境菜单 → Edit）：

1. 推荐：Network secrets（Pro/Max 才有）里加一条
   - 网站：{host}
   - 类型：自定义请求头，名称 X-Winhand-Hook-Token，前缀留空
   - 值：hook 令牌（winhand bridge token 生成，与中转的 HOOK_TOKEN 相同）
   令牌由代理在请求离开容器后加上，不进容器。
2. 没有 Network secrets 时：
   - Network access 的 Allowed domains 加上 {host}；
   - Environment variables 里设 WINHAND_HOOK_TOKEN=<hook 令牌>（使用这个环境的人都能看到）。
两种方式只用一种。之后新开的会话，开场就会带上这台电脑的规矩和技能名单。"""


def new_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def token_text(token: str) -> str:
    return f"""hook 令牌（只显示这一次，电脑上不保存）：

{token}

1. 在中转目录执行 npx wrangler secret put HOOK_TOKEN，粘贴上面的值，再 npx wrangler deploy；
2. 在云端环境里设同一个值（winhand bridge init 会说明在哪里设）。
换令牌时两处都要换。"""
