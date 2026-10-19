"""Let the person type a secret that is stored on this machine and never reaches the model.

The model needs a token to exist (a Cloudflare API token, a panel password) but should never
see it. secret_prompt opens a masked dialog; the answer goes straight into a file (whole file,
or one KEY=VALUE line of an .env file) readable only by this user, into the Windows Credential
Manager, or into a Harmonia environment, which syncs it to the owner's other machines
(harmonia.py). The value is also masked in every later tool result (redact.remember).

With only a key it goes into this machine's own store, ~/.winhand/secrets.env: run and job_start
hand the keys a program needs to it as environment variables (`secrets=[...]`), so a token is
stored once and used from then on without anyone pasting it. The store is masked in every result,
also after a restart, and the AI's tools never read or change it (guard.py). The person manages
it in the app's 设置 → 密钥 (put, remove, reveal; desktop_backend.py).
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import fs, harmonia, proc, redact, winenv
from .secret_prompt import ask_secret

IS_WINDOWS = sys.platform == "win32"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
log = logging.getLogger("winhand.secrets")


STORE_NAME = "secrets.env"


class SecretError(RuntimeError):
    pass


def store_path() -> Path:
    from .config import home

    return home() / STORE_NAME


def _store_items() -> dict[str, str]:
    path = store_path()
    if not path.is_file():
        return {}
    text, _ = fs.read_text(str(path))
    items: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and _KEY.match(key.strip()):
            items[key.strip()] = value
    return items


def stored_names() -> list[str]:
    """The names in the store (values never leave this module except into a program's env)."""
    try:
        return sorted(_store_items())
    except (OSError, ValueError, fs.FsError):
        return []


def load(names: list[str]) -> dict[str, str]:
    """The stored values of these names, for a program's environment."""
    if not names:
        return {}
    try:
        items = _store_items()
    except (OSError, ValueError, fs.FsError) as exc:
        raise SecretError(f"cannot read the secret store {store_path()}: {exc}") from exc
    missing = [n for n in names if n not in items]
    if missing:
        have = ", ".join(sorted(items)) or "nothing yet"
        raise SecretError(
            f"not in this machine's secret store: {', '.join(missing)} (it holds: {have}). "
            f"secret_prompt(message, key='{missing[0]}') asks the person to type it in"
        )
    return {n: items[n] for n in names}


# ------------------------------------------------------------------ the app's 密钥 section

listeners: list[Callable[[], None]] = []  # told whenever the store changes (the app shows it)
SHOWN_FOR_S = 35.0  # the app hides a shown value after 30 s; screenshots wait at most this long
_shown_until = 0.0


def _changed() -> None:
    for listener in list(listeners):
        try:
            listener()
        except Exception:  # noqa: BLE001 - a listener's failure is not the store's
            log.exception("secret store listener failed")


def summary() -> dict:
    """What the app lists: the file, and each name with its length. Never a value."""
    out: dict = {"file": str(store_path()), "items": []}
    try:
        out["items"] = [{"name": k, "chars": len(v.strip())} for k, v in sorted(_store_items().items())]
    except (OSError, ValueError, fs.FsError) as exc:
        out["error"] = str(exc)
    return out


def _rewrite(old: str | None, key: str, value: str | None) -> None:
    """Put `key=value` where `old` or `key` was (value None: remove `key`); other lines stay."""
    full = winenv.long_path(str(store_path()))
    text = fs.read_text(full)[0] if os.path.isfile(full) else ""
    lines, placed = [], False
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if "=" in line and name in (old, key):
            if value is not None and not placed:
                lines.append(f"{key}={value}")
                placed = True
            continue
        lines.append(line)
    if value is not None and not placed:
        lines.append(f"{key}={value}")
    fs.write_bytes_atomic(full, ("\n".join(lines) + "\n").encode("utf-8") if lines else b"")
    _restrict(full)


def put(name: str, value: str = "", original: str | None = None) -> str:
    """Add a key, change its value or rename it (`original`: its current name). An empty value
    keeps the current one. The person does this in the app; its messages are for them."""
    name, value, original = (name or "").strip(), (value or "").strip(), (original or "").strip() or None
    if not _KEY.match(name):
        raise SecretError(f"名字只能用英文字母、数字和 _ . -，不能以数字开头：{name!r}")
    if "\n" in value or "\r" in value:
        raise SecretError("值只能有一行")
    items = _store_items()
    if original is not None and original not in items:
        raise SecretError(f"没有叫 {original} 的密钥（可能刚被删除）")
    if name != original and name in items:
        raise SecretError(f"已经有叫 {name} 的密钥：要改它的值，请点它那一行的“修改”")
    if not value:
        if original is None:
            raise SecretError("请填写值")
        value = items[original].strip()
    redact.remember(value)
    _rewrite(original, name, value)
    _changed()
    return name


def remove(name: str) -> None:
    if name not in _store_items():
        raise SecretError(f"没有叫 {name} 的密钥（可能已经删除）")
    _rewrite(None, name, None)
    _changed()


def reveal(name: str, show: bool = True) -> str:
    """A stored value, for the person at the machine (the app's 显示 and 复制). While one is shown,
    winhand takes no screenshots (desktop.capture): a picture would carry it past the masking."""
    global _shown_until
    items = _store_items()
    if name not in items:
        raise SecretError(f"没有叫 {name} 的密钥（可能已经删除）")
    if show:
        _shown_until = time.monotonic() + SHOWN_FOR_S
    return items[name].strip()


def hide() -> None:
    global _shown_until
    _shown_until = 0.0


def shown() -> bool:
    """Whether the app is showing a stored value right now."""
    return time.monotonic() < _shown_until


def _is_store(path: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.expanduser(os.path.expandvars(p))))

    return norm(path) == norm(str(store_path()))


def _restrict(path: str) -> None:
    """Only the current user may read the file."""
    full = winenv.long_path(path)
    if IS_WINDOWS:
        user = os.environ.get("USERNAME") or getpass.getuser()
        domain = os.environ.get("USERDOMAIN")
        who = f"{domain}\\{user}" if domain else user
        done = subprocess.run(
            ["icacls", winenv.display_path(full), "/inheritance:r", "/grant:r", f"{who}:F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30,
        )
        if done.returncode != 0:
            # icacls prints in the console code page (cp936 on Chinese Windows)
            message = proc._decode(done.stdout + done.stderr)
            raise SecretError(f"saved, but could not restrict who may read it: {message.strip()[-300:]}")
    else:
        os.chmod(full, 0o600)


def save_to_file(path: str, value: str, key: str | None = None) -> dict:
    full = winenv.long_path(os.path.expanduser(os.path.expandvars(path)))
    if key is not None:
        if not _KEY.match(key):
            raise SecretError(f"key must look like an environment variable name, not {key!r}")
        lines = []
        if os.path.isfile(full):
            text, _ = fs.read_text(full)
            lines = text.splitlines()
        line = f"{key}={value}"
        for i, existing in enumerate(lines):
            if existing.split("=", 1)[0].strip().removeprefix("export ").strip() == key:
                lines[i] = line
                break
        else:
            lines.append(line)
        data = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        data = (value + "\n").encode("utf-8")
    fs.write_bytes_atomic(full, data)
    _restrict(full)
    return {"saved": True, "path": winenv.display_path(full), "key": key, "chars": len(value)}


def save_to_credential_manager(target: str, value: str, user: str | None = None) -> dict:
    if not IS_WINDOWS:
        raise SecretError("the Credential Manager is a Windows feature; use save_to on this system")
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob = value.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.Comment = "saved by winhand secret_prompt"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user or getpass.getuser()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise SecretError(f"CredWriteW failed (error {ctypes.get_last_error()})")
    return {"saved": True, "credential": target, "chars": len(value)}


def _harmonia_target(spec: str) -> tuple[str, str]:
    env, sep, name = spec.rpartition("/")
    if not sep or not env.strip() or not _KEY.match(name):
        raise SecretError(
            f"harmonia is <environment>/<VARIABLE_NAME>, e.g. Cloudflare/CLOUDFLARE_API_TOKEN, not {spec!r}"
        )
    return env.strip(), name


def prompt_and_save(
    message: str,
    save_to: str | None = None,
    key: str | None = None,
    credential: str | None = None,
    validate: str | None = None,
    title: str = "winhand",
    attempts: int = 3,
    harmonia_target: str | None = None,
) -> dict:
    if save_to is None and credential is None and harmonia_target is None and key is not None:
        save_to = str(store_path())  # the machine's own store, for run(secrets=[key])
    if sum(x is not None for x in (save_to, credential, harmonia_target)) != 1:
        raise SecretError(
            "give key (a name: stored on this machine for run(secrets=[...])), save_to (a file), "
            "credential (a Credential Manager name) or harmonia (<environment>/<NAME> in Harmonia)"
        )
    target = _harmonia_target(harmonia_target) if harmonia_target is not None else None
    pattern = None
    if validate:
        try:
            pattern = re.compile(validate)
        except re.error as exc:
            raise SecretError(f"invalid validate regex: {exc}") from exc
    question = message
    for _ in range(max(1, attempts)):
        value = ask_secret(title, question)
        if value is None:
            return {"saved": False, "reason": "the person cancelled"}
        value = value.strip()
        if not value:
            question = "没有输入内容，请重新输入。\n\n" + message
            continue
        if pattern is not None and not pattern.fullmatch(value):
            question = "格式不对，请检查是否复制完整后重新输入。\n\n" + message
            continue
        redact.remember(value)
        if target is not None:
            try:
                return harmonia.store(*target, value)
            except harmonia.HarmoniaError as exc:
                raise SecretError(str(exc)) from exc
        if credential is not None:
            return save_to_credential_manager(credential, value, key)
        saved = save_to_file(save_to, value, key)
        if _is_store(save_to):
            _changed()  # the app's list shows the new name at once
        return saved
    return {"saved": False, "reason": f"no valid value after {attempts} tries; nothing saved"}
