"""A PreToolUse hook for the tests: reads the call from stdin, answers as `mode` says, and notes
every call in the file named by WINHAND_HOOK_MARK."""

import json
import os
import subprocess
import sys
import time

mode = sys.argv[1]
call = json.load(sys.stdin)
mark = os.environ.get("WINHAND_HOOK_MARK")
if mark:
    with open(mark, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"mode": mode, **call}) + "\n")
text = json.dumps(call["tool_input"])


def answer(**specific):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **specific}}))


if mode == "deny-src":
    if "src/" in text.replace("\\\\", "/"):
        print("no writes in src/ in this phase", file=sys.stderr)
        sys.exit(2)
elif mode == "deny-rm":
    if "rm -rf" in text:
        print("no rm -rf", file=sys.stderr)
        sys.exit(2)
elif mode == "by-command":
    if "DENY" in text:
        answer(permissionDecision="deny", permissionDecisionReason="the hook says no")
    elif "ASK" in text:
        answer(permissionDecision="ask", permissionDecisionReason="check this one")
    elif "NOTE" in text:
        answer(additionalContext="remember the house style")
    elif "REWRITE" in text:
        answer(permissionDecision="allow", updatedInput={"command": "echo safer"})
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    if mark:
        with open(mark + ".pid", "w") as fh:
            fh.write(str(child.pid))
    time.sleep(120)
elif mode == "exit1":
    sys.exit(1)
elif mode == "badjson":
    print("{not json")
