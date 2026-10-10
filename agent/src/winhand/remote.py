"""Run a script on another machine over SSH, from this one.

The AI's own sandbox often cannot reach the user's servers; this machine can (keys, ssh config,
jump hosts are all here). Every call so far was hand-built: ssh options, `bash -s`, the script
through stdin, IP masking with sed. `remote_run` does that once, properly, and says whether a
failure was SSH (connection, host key, key refused) or the script itself.
"""

from __future__ import annotations

import re
import shlex

from . import proc

SSH_OPTIONS = [
    "-o", "BatchMode=yes",  # never hang on a password or host-key question
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "RequestTTY=no",
    "-T",
]  # fmt: skip

_HOST = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:%\[\]-]*$")
_SHELL = re.compile(r"^[A-Za-z0-9_./-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SSH_ERRORS = [
    (
        "host_key",
        r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|No \S+ host key is known",
        "the server's host key is unknown or changed: check the fingerprint with the person, then record "
        "it (a first interactive login with the ssh profile, or ssh-keyscan)",
    ),
    (
        "auth",
        r"Permission denied \(|Too many authentication failures|Load key .*: (?:invalid|bad)",
        "the key was refused: check User and IdentityFile for this host in ~/.ssh/config and the "
        "server's authorized_keys",
    ),
    (
        "dns",
        r"Could not resolve hostname|Name or service not known|No such host is known",
        "the name does not resolve: is it a Host alias in ~/.ssh/config?",
    ),
    ("refused", r"Connection refused", "nothing listens there: wrong Port in ~/.ssh/config, or sshd is down"),
    (
        "timeout",
        r"Connection timed out|Operation timed out|timed out",
        "no answer: the host is down, the port is filtered, or it needs a ProxyJump",
    ),
    (
        "closed",
        r"Connection closed by|Connection reset by|closed by remote host|Broken pipe|kex_exchange_identification",
        "the connection dropped (server rebooting, fail2ban, or the network); try again",
    ),
    (
        "remote_command",
        r"Cannot execute command-line and remote command",
        "this Host has RemoteCommand in ~/.ssh/config; use an alias without it",
    ),
]


def classify(stderr: str) -> dict | None:
    for kind, pattern, hint in _SSH_ERRORS:
        if re.search(pattern, stderr):
            return {"kind": kind, "hint": hint}
    return None


def build(
    host: str,
    script: str,
    shell: str = "bash",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[list[str], str]:
    """(ssh arguments, stdin) for running `script` with `shell` on `host`."""
    if not _HOST.match(host or ""):
        raise ValueError(f"host must be an ssh alias or user@host, not {host!r}")
    if not _SHELL.match(shell or ""):
        raise ValueError(f"shell must be a program name such as bash, sh or python3, not {shell!r}")
    quoted = " ".join(shlex.quote(a) for a in args or [])
    if shell.rsplit("/", 1)[-1].startswith("python"):
        remote = f"{shell} - {quoted}".strip()
    else:
        remote = f"{shell} -s -- {quoted}".strip()
    head = []
    for key, value in (env or {}).items():
        if not _ENV_NAME.match(key):
            raise ValueError(f"not a valid environment variable name: {key!r}")
        head.append(f"export {key}={shlex.quote(str(value))}")
    if cwd:
        head.append(f"cd -- {shlex.quote(cwd)} || exit 97")
    if head and shell.rsplit("/", 1)[-1].startswith("python"):
        raise ValueError("env and cwd are for shell scripts; set them inside the Python script")
    body = "\n".join([*head, script.replace("\r\n", "\n")])
    if not body.endswith("\n"):
        body += "\n"
    return [*SSH_OPTIONS, host, remote], body


def remote_run(
    host: str,
    script: str,
    shell: str = "bash",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 120,
) -> dict:
    try:
        ssh_args, body = build(host, script, shell, args, env, cwd)
    except ValueError as exc:
        return {"error": str(exc)}
    result = proc.run("ssh", ssh_args, timeout_s=timeout_s, stdin=body)
    result["host"] = host
    if result.get("exit_code") == 255 or (
        result.get("exit_code") == -1 and not result.get("timed_out") and not result.get("stopped")
    ):
        problem = classify(result.get("stderr", ""))
        result["ssh_error"] = problem or {
            "kind": "ssh",
            "hint": "exit 255 comes from ssh itself (connection or authentication) unless your script "
            "returned 255; the stderr above says which",
        }
    elif result.get("exit_code") == 97 and cwd:
        result["hint"] = f"the remote folder {cwd!r} does not exist or is not accessible"
    return result
