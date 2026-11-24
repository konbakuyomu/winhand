#!/usr/bin/env python3
"""How the AI learnt that a background job had ended: woken, by waiting inside a turn, or by asking.

Two steps. On the PC, pack WinHand's call log and, for each job, only its name and start and finish
times (no commands, no output):

    python wake_stats.py pack week.zip --since 2026-11-02

hand the zip over with fs_send, unzip it into an empty folder, then:

    python3 wake_stats.py <folder> --period 2026-11-02:2026-11-16 --period 2026-11-18:2026-11-24

For each job that ended in a period (its end is the time exit.json was written; stopped and declined
jobs are left out), by how the AI first learnt of the end:
  looked  nothing asked about it before the end; the first call naming the job came after it (woken
          by `wake`, by an alarm, or the session came back by itself). latency = that call - the end
  caught  a job_status wait was open when it ended: seen at once, but by waiting inside a turn
  polled  job_status answered "running" before the end, and a later call saw it
Days are the PC's local days.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import zipfile
from pathlib import Path


def pack(out: str, since: str | None) -> None:
    home = Path(os.environ.get("USERPROFILE") or Path.home()) / ".winhand"
    rows = []
    for d in (home / "jobs").iterdir():
        meta = _read(d / "job.json") if d.is_dir() else None
        if meta is None:
            continue
        row = {"id": d.name, "started": meta.get("started"), "stopped": bool(meta.get("stopped"))}
        done = _read(d / "exit.json")
        if done is not None:
            row.update(exit_code=done.get("exit_code"), declined=bool(done.get("declined")),
                       stopped=row["stopped"] or bool(done.get("stopped")), end=(d / "exit.json").stat().st_mtime)
        rows.append(row)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((home / "activity").glob("*.jsonl")):
            if not since or f.name[:10] >= since:
                z.write(f, "activity/" + f.name)
        z.writestr("jobs.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        offset = datetime.datetime.now().astimezone().utcoffset()
        z.writestr("pc.json", json.dumps({"utc_offset_s": int(offset.total_seconds()) if offset else 0}))
    print(f"{len(rows)} jobs -> {out} ({os.path.getsize(out)} bytes)")


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _pct(values: list, q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else float("nan")


def analyse(folder: str, periods: list[str]) -> None:
    root = Path(folder)
    tz = datetime.timezone(datetime.timedelta(seconds=json.loads((root / "pc.json").read_text())["utc_offset_s"]))
    tools = []
    for f in sorted((root / "activity").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line) if line.strip() else {}
            if r.get("kind") == "tool":
                r["_args"] = json.dumps(r.get("args") or {}, ensure_ascii=False)
                tools.append(r)
    tools.sort(key=lambda r: r["t"])
    jobs = [json.loads(x) for x in (root / "jobs.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    named = collections.defaultdict(list)  # job id -> calls whose arguments name it
    for r in tools:
        for jid in set(re.findall(r"[A-Za-z0-9._-]+-\d{10}", r["_args"])):
            named[jid].append(r)

    for period in periods:
        first, last = period.split(":")
        lo = datetime.datetime.fromisoformat(first).replace(tzinfo=tz).timestamp()
        hi = (datetime.datetime.fromisoformat(last) + datetime.timedelta(days=1)).replace(tzinfo=tz).timestamp()
        cats, looked, polls, unasked = collections.Counter(), [], [], 0
        for job in jobs:
            end = job.get("end")
            if not end or not lo <= end < hi or job["stopped"] or job.get("declined"):
                continue
            calls = named.get(job["id"], [])
            status = [c for c in calls if c["title"] == "job_status"]
            open_at_end = [c for c in status if c["t"] <= end <= c["t"] + (c.get("duration_ms") or 0) / 1000 + 1]
            before = [c for c in status if c["t"] + (c.get("duration_ms") or 0) / 1000 < end - 1]
            after = next((c for c in calls if end < c["t"] < end + 86400), None)
            if open_at_end:
                cats["caught"] += 1
            elif before:
                cats["polled"] += 1
            elif after is not None:
                cats["looked"] += 1
                looked.append(after["t"] - end)
            else:
                unasked += 1
                continue
            polls.append(len(before) + len(open_at_end))
        total = sum(cats.values())
        in_turn = cats["caught"] + cats["polled"]
        print(f"## {first} .. {last}: {total} jobs ended and were looked at ({unasked} more nobody asked about)")
        for cat in ("looked", "caught", "polled"):
            print(f"  {cat:7s} {cats[cat]:5d}  {100 * cats[cat] / max(1, total):3.0f}%")
        print(f"  waited inside a turn (caught + polled): {100 * in_turn / max(1, total):.0f}%;"
              f" job_status asks before the end per job: mean {sum(polls) / max(1, len(polls)):.2f}, p90 {_pct(polls, .9)}")
        if looked:
            soon = sum(1 for x in looked if x <= 60)
            print(f"  looked: end -> first call naming the job p50 {_pct(looked, .5) / 60:.1f}"
                  f" p90 {_pct(looked, .9) / 60:.1f} min; within a minute {100 * soon / len(looked):.0f}%")
        days = collections.defaultdict(collections.Counter)
        for r in tools:
            if lo <= r["t"] < hi:
                day = datetime.datetime.fromtimestamp(r["t"], tz).strftime("%m-%d")
                a = r.get("args") or {}
                if r["title"] == "job_start" or (r["title"] == "run" and "still_running" in (r.get("summary") or "")):
                    days[day]["started"] += 1
                elif r["title"] == "job_status":
                    days[day]["asks"] += 1
                    days[day]["wake"] += bool(a.get("wake"))
                    days[day]["full"] += (r.get("duration_ms") or 0) >= 39_000
        print("  per day, job_status asks per job started in the background (waited the full 40 s, wake=true):")
        print("   " + ", ".join(f"{d} {c['asks'] / max(1, c['started']):.1f} ({c['full']}, {c['wake']})"
                                for d, c in sorted(days.items())) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", help="'pack' on the PC, else the unzipped folder")
    parser.add_argument("out", nargs="?", help="with pack: the zip to write")
    parser.add_argument("--period", action="append", default=[], help="first:last local day, e.g. 2026-11-18:2026-11-24")
    parser.add_argument("--since", help="with pack: only the call log from this local day on")
    opts = parser.parse_args()
    if opts.folder == "pack":
        pack(opts.out or "winhand-week.zip", opts.since)
    else:
        analyse(opts.folder, opts.period)


if __name__ == "__main__":
    main()
