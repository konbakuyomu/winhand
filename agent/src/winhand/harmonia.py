"""Keys and tokens that Harmonia (https://github.com/harmonia-vault/harmonia) syncs to this machine.

The owner keeps API keys in the Harmonia app on their phone; Harmonia syncs them, end-to-end
encrypted, to the machines they approve, and `harmonia exec -- <program>` hands them to one
program. winhand only drives its command line, and adds what an AI on the other side of the
relay needs:

- every value Harmonia gives this machine is masked in results, labelled with the variable's
  name («redacted:harmonia:CLOUDFLARE_API_TOKEN»), values added on the phone later included;
- its data folder (the device keys that decrypt everything, local values) is off limits to the
  AI, and the commands that print values or change the setup ask the person first (guard.py);
- secret_prompt can store a new key straight into one of its environments (secret_store.py).

winhand reads the values with `harmonia export`, in this process: they never leave it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import winenv

IS_WINDOWS = sys.platform == "win32"
EXPORT_TIMEOUT_S = 15.0  # one environment: harmonia syncs first (3 s at most), then decrypts
REFRESH_BUDGET_S = 30.0  # all of them; past that the values read so far are used
SET_TIMEOUT_S = 60.0
# the files whose change means the values may have changed: the synced cache and the local values
_WATCHED = ("cache.json", "overrides.json")


class HarmoniaError(RuntimeError):
    pass


def data_dir() -> Path:
    return Path(os.environ.get("HARMONIA_HOME") or Path.home() / ".harmonia")


def executable() -> str | None:
    """The harmonia command line, on PATH or where its installer puts it."""
    found = shutil.which("harmonia")
    if found:
        return found
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        candidate = Path(local) / "Programs" / "Harmonia" / "harmonia.exe" if local else None
    else:
        candidate = Path.home() / ".local" / "bin" / "harmonia"
    return str(candidate) if candidate is not None and candidate.is_file() else None


def _read_json(name: str) -> dict:
    try:
        data = json.loads((data_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def paired() -> bool:
    """Whether this machine is connected to a Harmonia account (the person approved it)."""
    return bool(_read_json("config.json").get("deviceId"))


def present() -> bool:
    return executable() is not None or data_dir().is_dir()


def environments() -> list[dict]:
    """The environments this machine may use, from Harmonia's cache: names and ids are not secret
    (its server sees them too); the values in the cache are encrypted."""
    out = []
    for env in _read_json("cache.json").get("environments") or []:
        if isinstance(env, dict) and env.get("id"):
            out.append(
                {"id": str(env["id"]), "name": str(env.get("name") or ""), "active": bool(env.get("active"))}
            )
    return out


def _cli(
    args: list[str], stdin: str | None = None, timeout: float = EXPORT_TIMEOUT_S
) -> tuple[int, str, str]:
    """Run the harmonia command line; (exit code, stdout, stderr). Its output is UTF-8."""
    exe = executable()
    if exe is None:
        raise HarmoniaError("harmonia is not installed on this machine")
    kwargs: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        done = subprocess.run(
            [exe, *args],
            input=(stdin or "").encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=winenv.build_env(),
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise HarmoniaError(f"harmonia {args[0]} did not finish within {timeout:.0f} s") from exc
    except OSError as exc:
        raise HarmoniaError(f"cannot run harmonia: {exc}") from exc

    def text(raw: bytes | None) -> str:
        return (raw or b"").decode("utf-8", errors="replace").strip()

    return done.returncode, text(done.stdout), text(done.stderr)


# ------------------------------------------------------------------ the values, for masking


class _Known:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stamp: tuple | None = None  # (mtime, size) of the watched files when last read
        self.digest: str | None = None  # their content then
        self.values: dict[str, set[str]] = {}  # variable name -> its values in every environment
        self.version = 0  # goes up when the values change
        self.error: str | None = None


_known = _Known()


def _stamp() -> tuple:
    out = []
    for name in _WATCHED:
        try:
            st = (data_dir() / name).stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _digest() -> str:
    h = hashlib.sha256()
    for name in _WATCHED:
        try:
            h.update((data_dir() / name).read_bytes())
        except OSError:
            h.update(b"-")
        h.update(b"\0")
    return h.hexdigest()


def changed() -> bool:
    """Whether Harmonia's files changed since the values were read (two stat calls)."""
    return _stamp() != _known.stamp


def _export_all() -> tuple[dict[str, set[str]], str | None]:
    """Every environment's values, one `harmonia export --env <id>` each: an export of all of
    them at once keeps only one value per name, and a name shared by a test and a production
    environment would leave the other's value unmasked."""
    values: dict[str, set[str]] = {}
    problems: list[str] = []
    deadline = time.monotonic() + REFRESH_BUDGET_S
    for env in environments():
        left = deadline - time.monotonic()
        if left <= 1:
            problems.append("ran out of time; the other environments' values are not masked yet")
            break
        try:
            code, out, err = _cli(
                ["export", "--env", env["id"], "--format", "json"], timeout=min(EXPORT_TIMEOUT_S, left)
            )
        except HarmoniaError as exc:
            problems.append(f"{env['name'] or env['id']}: {exc}")
            continue
        if code != 0:
            problems.append(f"{env['name'] or env['id']}: {(err or out)[-300:]}")
            continue
        try:
            data = json.loads(out or "{}")
        except ValueError:
            problems.append(f"{env['name'] or env['id']}: harmonia export did not answer JSON")
            continue
        for name, value in data.items() if isinstance(data, dict) else []:
            if isinstance(value, str) and value:
                values.setdefault(str(name), set()).add(value)
    return values, "; ".join(problems) or None


def refresh(force: bool = False) -> bool:
    """Read the values again when Harmonia's cache or local values changed; whether they changed.
    Nothing to do (two stat calls) when its files did not change."""
    if not force and not changed():
        return False
    with _known.lock:
        stamp = _stamp()
        if not force and stamp == _known.stamp:
            return False  # another call read them meanwhile
        digest = _digest()
        if not force and digest == _known.digest:
            _known.stamp = stamp  # rewritten by a sync, same content
            return False
        if executable() is None or not paired():
            values, error = {}, None
        else:
            values, error = _export_all()
        # harmonia synced while exporting and may have rewritten its cache: what was read is current
        _known.stamp, _known.digest, _known.error = _stamp(), _digest(), error
        if values != _known.values:
            _known.values = values
            _known.version += 1
            return True
        return False


def version() -> int:
    return _known.version


def last_error() -> str | None:
    return _known.error


def masks(min_length: int = 6) -> dict[str, list[str]]:
    """Values to mask, by label: {"harmonia:CLOUDFLARE_API_TOKEN": [...]} (no Harmonia: empty)."""
    known = _known.values  # replaced whole, never changed in place: safe to read while it is renewed
    return {
        f"harmonia:{name}": sorted(v for v in values if len(v) >= min_length)
        for name, values in known.items()
        if any(len(v) >= min_length for v in values)
    }


def remember(name: str, value: str) -> None:
    """A value just stored through secret_prompt: masked before the next refresh reads it back."""
    with _known.lock:
        values = {k: set(v) for k, v in _known.values.items()}
        values.setdefault(name, set()).add(value)
        _known.values = values
        _known.version += 1


# ------------------------------------------------------------------ storing a new key


def store(env: str, name: str, value: str) -> dict:
    """Write a variable into one of the machine's Harmonia environments (it then syncs to every
    machine allowed to use that environment). Needs read-write access to it."""
    if executable() is None:
        raise HarmoniaError(
            "Harmonia is not installed on this machine: ask the person to install it "
            "(https://github.com/harmonia-vault/harmonia) or store the key with save_to / credential"
        )
    if not paired():
        raise HarmoniaError(
            "this machine is not connected to a Harmonia account yet: the person connects it with "
            "`harmonia login <server>` and approves it on their phone"
        )
    if "\n" in value or "\r" in value:
        raise HarmoniaError("Harmonia stores one line per variable; this value has several")
    remember(name, value)
    code, out, err = _cli(["var", "set", env, name], stdin=value + "\n", timeout=SET_TIMEOUT_S)
    if code != 0:
        raise HarmoniaError(f"harmonia var set failed: {(err or out)[-400:]}")
    result: dict = {"saved": True, "harmonia": f"{env}/{name}", "chars": len(value)}
    notes = [line.strip() for line in (out + "\n" + err).splitlines() if line.strip().startswith("注意")]
    if notes:  # not enabled here, shadowed by an environment in front of it, a local override
        result["notes"] = notes
    refresh(force=True)
    return result


def summary() -> dict | None:
    """For context(): what an AI needs to use the keys here, or None without Harmonia."""
    if not present():
        return None
    out: dict = {
        "installed": executable() is not None,
        "connected": paired(),
        "environments": [e["name"] or e["id"] for e in environments()],
        "use": "run(command='harmonia', args=['exec', '--env', '<environment>', '--', <program and its "
        "arguments>]) gives that program the keys; you never see them (they are masked in results). "
        "A new key: secret_prompt(message, harmonia='<environment>/<NAME>'). `harmonia skill` has "
        "Harmonia's own guide.",
    }
    if _known.error:
        out["masking_problem"] = _known.error
    return out
