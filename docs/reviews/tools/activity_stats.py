#!/usr/bin/env python3
"""Usage statistics from WinHand's own call log, for a periodic review.

Copy the PC's ~/.winhand/activity/*.jsonl into a folder (for example: zip them on the PC, hand the
zip over with fs_send (<= 20 MB; a cloud sandbox whose proxy only lets /hook/ through cannot use the
one-time links), unzip), then:

    python3 activity_stats.py <folder> [--since 2026-10-19] [--until 2026-11-01]

Each line of the log is one event: {"id", "t", "kind", "title", "status", "summary", "args",
"duration_ms"}. kind "tool" is a tool call (title = tool name, args truncated to ~500 chars),
"connection" a relay connect/disconnect, "transfer" a one-time-link upload or download.
Dates come from the file names (one file per local day).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import re
import statistics

GROUPS = {
    "run/job": ["run", "job_start", "job_status", "job_stop", "remote_run", "proc_list", "proc_kill"],
    "files": ["fs_read", "fs_edit", "fs_write", "fs_search", "fs_list", "fs_find", "fs_op", "fs_stat",
              "fs_apply_patch", "fs_write_bytes"],
    "transfer": ["fs_upload_url", "fs_download_url", "fs_send", "fs_pick"],
    "desktop": ["screenshot", "ui", "window", "input", "ocr", "clipboard"],
    "meta": ["context", "skill", "help", "history", "call", "sys_info", "secret_prompt"],
    "local mcp": ["mcp_call", "mcp_tools"],
}

# What the AI does inside run scripts: a high count can mean a missing tool.
RUN_PATTERNS = {
    "ssh/scp": r"(^|[\s;&|(\'\"])(ssh|scp|sftp)(\.exe)?[\s\'\"]",
    "Get-Content (read files)": r"\bGet-Content\b",
    "Select-String (search)": r"\bSelect-String\b",
    "Get-ChildItem (list)": r"\bGet-ChildItem\b",
    "Get-Date (clock)": r"\bGet-Date\b",
    "Start-Sleep (waiting)": r"\bStart-Sleep\b",
    "Get-Process/Stop-Process": r"\bGet-Process\b|\bStop-Process\b",
    "archives (zip/tar)": r"Compress-Archive|Expand-Archive|zipfile|\btar\b",
    "HTTP from the PC": r"Invoke-WebRequest|Invoke-RestMethod|\bcurl(\.exe)?\b",
    "Word COM / docx": r"Word\.Application|win32com|docx",
    "PDF libraries": r"\bfitz\b|pymupdf|pypdf|pdfplumber",
    "SetThreadExecutionState (keep awake)": r"SetThreadExecutionState",
    "Out-String -Width / Format-Table": r"Out-String\s+-Width|Format-Table",
}


def load(folder: str, since: str | None, until: str | None) -> list[dict]:
    records = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        day = os.path.basename(path)[:10]
        if (since and day < since) or (until and day > until):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    record["_day"] = day
                    records.append(record)
    records.sort(key=lambda r: r["t"])
    return records


def args_text(record: dict) -> str:
    a = record.get("args") or {}
    extra = a.get("args") or []
    return " ".join(str(a.get(k, "")) for k in ("command", "script")) + " " + " ".join(map(str, extra))


def bursts(calls: list[dict], gap_s: float) -> list[list[dict]]:
    out, current = [], []
    for record in calls:
        if current and record["t"] - current[-1]["t"] > gap_s:
            out.append(current)
            current = []
        current.append(record)
    if current:
        out.append(current)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--since")
    parser.add_argument("--until")
    opts = parser.parse_args()
    records = load(opts.folder, opts.since, opts.until)
    tools = [r for r in records if r["kind"] == "tool"]
    days = sorted({r["_day"] for r in tools})
    errors = [r for r in tools if r["status"] != "ok"]
    print(f"# {len(tools)} tool calls on {len(days)} days ({days[0] if days else '-'} .. {days[-1] if days else '-'}),"
          f" {len(errors)} errors ({100 * len(errors) / max(1, len(tools)):.1f}%)\n")

    calls = collections.Counter(r["title"] for r in tools)
    errs = collections.Counter(r["title"] for r in errors)
    print("## Calls and errors per tool")
    for name, n in calls.most_common():
        print(f"{name:28s} {n:6d} {100 * n / len(tools):5.1f}%  errors {errs[name]:4d}")
    print("\n## Groups")
    for group, names in GROUPS.items():
        n = sum(calls[x] for x in names)
        print(f"{group:10s} {n:6d} {100 * n / len(tools):5.1f}%")
    n = sum(v for k, v in calls.items() if k.startswith("session_"))
    print(f"{'sessions':10s} {n:6d} {100 * n / len(tools):5.1f}%")

    print("\n## Errors, grouped (paths and numbers blanked)")
    grouped: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in errors:
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"|,]*", "<PATH>", r["summary"])
        grouped[r["title"]][re.sub(r"\d+", "N", text)[:140].replace("\n", " ")] += 1
    for name, counter in sorted(grouped.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"- {name} ({sum(counter.values())})")
        for text, n in counter.most_common(6):
            print(f"    {n:3d}  {text}")

    print("\n## Parameters the AI tried that a tool does not have")
    for r in errors:
        if "not a parameter" in r["summary"] or "Unexpected keyword" in r["summary"] or "Missing required" in r["summary"]:
            print(f"  {r['_day']} {r['title']}: {r['summary'][:110].replace(chr(10), ' ')}")

    waits = [r for r in tools if r["title"] == "job_status"]
    if waits:
        durations = [r.get("duration_ms") or 0 for r in waits]
        full = sum(1 for d in durations if d >= 39_000)
        print(f"\n## Waiting: job_status {len(waits)} calls ({100 * len(waits) / len(tools):.1f}% of all),"
              f" {sum(durations) / 3.6e6:.1f} h inside, {full} ({100 * full / len(waits):.0f}%) waited the full 40 s")
        per_job: dict[str, list] = collections.defaultdict(list)
        for r in waits:
            key = (r.get("args") or {}).get("id")
            per_job[json.dumps(key, ensure_ascii=False)].append(r)
        for key, rs in sorted(per_job.items(), key=lambda kv: -len(kv[1]))[:8]:
            print(f"  {len(rs):4d} calls over {(rs[-1]['t'] - rs[0]['t']) / 60:7.1f} min  {key}")

    runs = [r for r in tools if r["title"] == "run"]
    if runs:
        still = sum(1 for r in runs if "still_running" in r["summary"])
        print(f"\n## run: {len(runs)} calls, {still} still running at the ~40 s answer")
        counter = collections.Counter()
        for r in runs:
            text = args_text(r)
            for label, pattern in RUN_PATTERNS.items():
                if re.search(pattern, text, re.I | re.M):
                    counter[label] += 1
        for label, n in counter.most_common():
            print(f"  {n:6d}  {label}")
        print(f"  remote_run calls: {calls['remote_run']}")

    uploads = [r for r in tools if r["title"] == "fs_upload_url"]
    if uploads:
        groups = [b for b in bursts(uploads, 120) if len(b) >= 4]
        print(f"\n## Transfers: {len(uploads)} upload links, {calls['fs_download_url']} download links,"
              f" {calls['fs_send']} fs_send; {len(groups)} bursts of >=4 uploads covering {sum(map(len, groups))}")
    reads = [r for r in tools if r["title"] == "fs_read"]
    if reads:
        multi = [b for b in bursts(reads, 8) if len({(x.get('args') or {}).get('path') for x in b}) >= 2]
        print(f"## fs_read: {len(reads)} calls; {len(multi)} bursts reading several files, covering {sum(map(len, multi))}")
        docs = [r for r in reads if re.search(r"\.(pdf|docx|docm|pptx|xlsx|xlsm)$",
                                              ((r.get("args") or {}).get("path") or "").lower())]
        print(f"## fs_read on documents (PDF/Office): {len(docs)}")

    drops = collections.Counter(r["_day"] for r in records if r["kind"] == "connection" and r["status"] == "offline")
    if drops:
        print("\n## Relay disconnects per day")
        print("  " + ", ".join(f"{d[5:]}:{n}" for d, n in sorted(drops.items())))
    offline_spans(records)
    job_follow_up(tools)
    context_by_folder(tools)
    desktop_by_day(tools)


def _utc(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m-%dT%H:%MZ")


def _pct(values: list, q: float):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


def offline_spans(records: list[dict]) -> None:
    """Offline -> next online, with no tool call in between: the PC really could not be reached."""
    spans, down = [], None
    for r in records:
        if r["kind"] != "connection":
            continue
        if r["status"] == "offline" and down is None:
            down = r
        elif r["status"] != "offline" and down is not None:
            spans.append((down["t"], r["t"]))
            down = None
    if not spans:
        return
    print(f"\n## Offline: {len(spans)} spans, {sum(b - a for a, b in spans) / 3600:.1f} h in total; spans of 20 min or more (UTC):")
    for a, b in spans:
        if b - a >= 1200:
            print(f"  {_utc(a)} -> {_utc(b)}  {(b - a) / 60:.0f} min")


def job_follow_up(tools: list[dict]) -> None:
    """How long polled jobs ran, how often they were asked about, and how late their end was seen."""
    starts = [r for r in tools if r["title"] == "job_start"]
    by_id: dict[str, list] = collections.defaultdict(list)
    for r in tools:
        job = (r.get("args") or {}).get("id") if r["title"] == "job_status" else None
        if isinstance(job, str):
            by_id[job].append(r)
    lengths, late, asks = [], [], []
    for job, rs in by_id.items():
        end = next((x for x in rs if "finished" in (x.get("summary") or "")), None)
        if not end:
            continue
        before = [x for x in rs if x["t"] < end["t"]]
        asks.append(len(before) + 1)
        if before and "hit=finished" not in (end.get("summary") or ""):
            last = before[-1]
            late.append(max(0.0, end["t"] - last["t"] - (last.get("duration_ms") or 0) / 1000))
        name = re.sub(r"-\d{10}$", "", job)
        started = [s["t"] for s in starts if (s.get("args") or {}).get("name") == name and s["t"] <= rs[0]["t"]]
        if started:
            lengths.append((end["t"] - started[-1]) / 60)
    if not asks:
        return
    longer = sum(1 for r in tools if r["title"] == "job_status" and ((r.get("args") or {}).get("wait_s") or 0) > 40)
    print(f"\n## Jobs followed to their end: {len(asks)}; asks per job p50={_pct(asks, .5)} p90={_pct(asks, .9)}")
    print(f"  length (start -> seen finished): p50={_pct(lengths, .5):.1f} min p90={_pct(lengths, .9):.1f} min")
    print(f"  seen late, upper bound (last 'running' answer -> the ask that saw it finished):"
          f" p50={_pct(late, .5) / 60:.1f} p75={_pct(late, .75) / 60:.1f} p90={_pct(late, .9) / 60:.1f} min,"
          f" sum {sum(late) / 3600:.1f} h")
    print(f"  job_status asks for more than 40 s: {longer}")
    clock = sum(1 for r in tools if r["title"] == "run" and re.search(r"\bGet-Date\b", args_text(r)) and len(args_text(r).strip()) < 80)
    print(f"  runs that only read the clock: {clock}")


def context_by_folder(tools: list[dict]) -> None:
    calls = [r for r in tools if r["title"] == "context"]
    if not calls:
        return
    folders = collections.Counter(
        re.split(r"[\\/]", ((r.get("args") or {}).get("path") or "(no path)").rstrip("\\/"))[-1] for r in calls)
    with_since = sum(1 for r in calls if (r.get("args") or {}).get("since"))
    print(f"\n## context(): {len(calls)} calls, {with_since} with since=; by folder:")
    for name, n in folders.most_common(8):
        print(f"  {n:5d}  {name}")


def desktop_by_day(tools: list[dict]) -> None:
    names = ("input", "screenshot", "ui", "ocr", "window")
    calls = [r for r in tools if r["title"] in names]
    if not calls:
        return
    windows = collections.Counter(
        str((r.get("args") or {}).get("window") or (r.get("args") or {}).get("title") or "")[:30] for r in calls)
    print("\n## Desktop by day and window")
    days = collections.Counter((r["_day"], r["title"]) for r in calls)
    for day in sorted({d for d, _ in days}):
        print(f"  {day[5:]}: " + ", ".join(f"{t} {days[(day, t)]}" for t in names if days[(day, t)]))
    print("  windows named:", ", ".join(f"{w} {n}" for w, n in windows.most_common(8) if w))


if __name__ == "__main__":
    main()
