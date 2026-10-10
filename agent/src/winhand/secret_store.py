"""Let the person type a secret that is stored on this machine and never reaches the model.

The model needs a token to exist (a Cloudflare API token, a panel password) but should never
see it. secret_prompt opens a masked dialog; the answer goes straight into a file (whole file,
or one KEY=VALUE line of an .env file) readable only by this user, or into the Windows
Credential Manager. The value is also masked in every later tool result (redact.remember).
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys

from . import fs, proc, redact, winenv
from .secret_prompt import ask_secret

IS_WINDOWS = sys.platform == "win32"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class SecretError(RuntimeError):
    pass


def _restrict(path: str) -> None:
    """Only the current user may read the file."""
    full = winenv.long_path(path)
    if IS_WINDOWS:
        user = os.environ.get("USERNAME") or getpass.getuser()
        domain = os.environ.get("USERDOMAIN")
        who = f"{domain}\\{user}" if domain else user
        done = subprocess.run(
            ["icacls", winenv.display_path(full), "/inheritance:r", "/grant:r", f"{who}:F"],
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


def prompt_and_save(
    message: str,
    save_to: str | None = None,
    key: str | None = None,
    credential: str | None = None,
    validate: str | None = None,
    title: str = "winhand",
    attempts: int = 3,
) -> dict:
    if (save_to is None) == (credential is None):
        raise SecretError("give save_to (a file) or credential (a Credential Manager name)")
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
        if credential is not None:
            return save_to_credential_manager(credential, value, key)
        return save_to_file(save_to, value, key)
    return {"saved": False, "reason": f"no valid value after {attempts} tries; nothing saved"}
