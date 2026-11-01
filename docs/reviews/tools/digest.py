#!/usr/bin/env python3
"""Turn a saved list_events result into a compact, chronological digest.

Usage:
  python3 digest.py <saved_result_file> [--out DIGEST_FILE] [--full]

The saved file is what the claude-code-remote list_events tool writes when its
result is too large ("Output has been saved to ..."). It holds one JSON object
{"ccr": {"data": [...], "has_more": ..., "first_id": ..., "last_id": ...}}
wrapped in an <other-session ...> block.

Prints (and appends to --out if given) one line per interesting event:
  T  tool call            name + compact input
  E  tool result error    first 600 chars
  A  assistant text       first 900 chars (or all with --full)
  U  human message        first 1500 chars
At the end prints: first_id, last_id, has_more, time range, counts.
"""
import json
import re
import sys
import collections

ERR_HINTS = re.compile(
    r'"error"|ToolError|ValidationError|not a parameter|refused|Traceback|'
    r'exceeds maximum|timed out|timeout|still_running|CancelledError|'
    r'Waiting for the person|Nobody allowed|no such file|not found|'
    r'cannot start|could not|failed|denied|拒绝|失败|超时|找不到',
    re.I,
)


def load(path):
    s = open(path, encoding="utf-8").read()
    i = s.find('{"ccr"')
    j = s.rfind("}")
    return json.loads(s[i : j + 1])["ccr"]


def compact(obj, n):
    t = json.dumps(obj, ensure_ascii=False)
    t = t.replace("\\n", " ").replace("\\r", "")
    return t if len(t) <= n else t[:n] + f"…(+{len(t) - n})"


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
                elif c.get("type") == "image":
                    out.append("[image]")
            else:
                out.append(str(c))
        return "\n".join(out)
    return str(content)


def main():
    args = sys.argv[1:]
    full = "--full" in args
    out = None
    if "--out" in args:
        out = args[args.index("--out") + 1]
    path = [a for a in args if not a.startswith("--") and a != out][0]
    ccr = load(path)
    data = ccr.get("data", [])
    lines = []
    counts = collections.Counter()
    errs = collections.Counter()
    pending = {}
    for ev in data:
        ts = ev.get("created_at", "")[:19]
        if "assistant" in ev:
            msg = ev["assistant"].get("internal_anthropic_catchall", {}).get("message", {})
            for c in msg.get("content", []) or []:
                if c.get("type") == "tool_use":
                    counts[c["name"]] += 1
                    pending[c.get("id")] = c["name"]
                    lines.append(f"{ts} T {c['name']} {compact(c.get('input', {}), 260)}")
                elif c.get("type") == "text" and c.get("text", "").strip():
                    t = c["text"].strip().replace("\n", " ⏎ ")
                    if not full and len(t) > 900:
                        t = t[:900] + f"…(+{len(t) - 900})"
                    lines.append(f"{ts} A {t}")
        elif "user" in ev:
            msg = ev["user"].get("internal_anthropic_catchall", {}).get("message", {})
            content = msg.get("content")
            if isinstance(content, str):
                lines.append(f"{ts} U {content[:1500]}")
                continue
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    name = pending.get(c.get("tool_use_id"), "?")
                    body = text_of(c.get("content"))
                    is_err = bool(c.get("is_error"))
                    if is_err or (len(body) < 4000 and ERR_HINTS.search(body[:1500])):
                        if is_err:
                            errs[name] += 1
                        flag = "E" if is_err else "e"
                        lines.append(f"{ts} {flag} [{name}] {body[:600].replace(chr(10), ' ')}")
                elif c.get("type") == "text":
                    t = c.get("text", "")
                    if t.strip() and not t.startswith("<system-reminder>"):
                        lines.append(f"{ts} U {t[:1500].replace(chr(10), ' ⏎ ')}")
    times = [ev.get("created_at", "") for ev in data if ev.get("created_at")]
    summary = (
        f"# page: events={len(data)} range={min(times) if times else '?'}..{max(times) if times else '?'} "
        f"has_more={ccr.get('has_more')} first_id={ccr.get('first_id')} last_id={ccr.get('last_id')}\n"
        f"# tool calls: {dict(counts.most_common())}\n# tool errors: {dict(errs)}"
    )
    text = "\n".join(lines)
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(summary + "\n" + text + "\n")
        print(summary)
        print(f"# appended {len(lines)} lines to {out}")
    else:
        print(summary)
        print(text)


if __name__ == "__main__":
    main()
