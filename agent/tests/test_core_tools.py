"""winhand 0.4 core: scripts and clean output for `run`, waiting on jobs, file tools, history."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from fastmcp.client import Client

from winhand import coalesce, config, fs, proc, winenv
from winhand.config import Config
from winhand.server import build_server

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shebang stand-ins")


def fake_program(directory: Path, name: str, body: str) -> Path:
    """An executable `name` on POSIX that runs `body` with this Python."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def path_with(directory: Path) -> dict[str, str]:
    return {"PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")}


# ------------------------------------------------------------------ run: scripts


def test_run_script_keeps_quotes_dollars_and_cjk():
    if sys.platform == "win32":
        script = '$x = "a \'b\' 中文"\nWrite-Output "[$x]"\nWrite-Output \'$literal "q"\''
    else:
        script = 'x="a \'b\' 中文"\necho "[$x]"\necho \'$literal "q"\''
    res = proc.run(script=script)
    assert res["exit_code"] == 0, res
    assert "[a 'b' 中文]" in res["stdout"] and '$literal "q"' in res["stdout"]
    assert not list((config.home() / "tmp").glob("run-*")), "the temporary script is removed"


def test_run_python_script_with_args():
    res = proc.run(script="import sys\nprint('参数', sys.argv[1:])", shell="python", args=["x y", "中"])
    assert res["exit_code"] == 0 and "参数 ['x y', '中']" in res["stdout"], res


def test_run_script_errors_are_explained():
    assert "not both" in proc.run("echo hi", script="echo hi")["error"]
    assert "shell must be" in proc.run(script="x", shell="fish")["error"]
    assert "give command" in proc.run()["error"]


def test_run_strips_ansi_unless_raw():
    code = "print('\\x1b[31mred\\x1b[0m plain')"
    assert proc.run(sys.executable, ["-c", code])["stdout"].strip() == "red plain"
    assert "\x1b[31m" in proc.run(sys.executable, ["-c", code], raw=True)["stdout"]


def test_run_children_get_quiet_defaults_and_english():
    code = "import os; print(os.environ.get('PYTHONUNBUFFERED'), os.environ.get('NO_COLOR'), os.environ.get('DOTNET_CLI_UI_LANGUAGE'))"
    assert proc.run(sys.executable, ["-c", code])["stdout"].split() == ["1", "1", "None"]
    assert proc.run(sys.executable, ["-c", code], lang="en")["stdout"].split()[-1] == "en"
    assert "lang supports" in proc.run(sys.executable, ["-c", code], lang="zh")["error"]


@POSIX
def test_nested_powershell_command_runs_without_outer_shell(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_program(bin_dir, "pwsh", "import sys, json; print(json.dumps(sys.argv[1:]))")
    command = 'pwsh -NoProfile -Command "$p = \'D:\\x\'; Get-Item \\"a b\\" | % { $_.Name }"'
    res = proc.run(command, env=path_with(bin_dir))
    argv = json.loads(res["stdout"])
    assert argv[:2] == ["-NoProfile", "-Command"]
    assert argv[2].endswith("; $p = 'D:\\x'; Get-Item \"a b\" | % { $_.Name }")
    assert "OutputEncoding" in argv[2] and "nested PowerShell" in res["notes"][0]


def test_nested_powershell_detection_leaves_pipelines_alone(monkeypatch):
    monkeypatch.setattr(proc.winenv, "resolve_executable", lambda name, env=None: "/x/" + name)
    assert proc.nested_powershell('powershell -Command "a" | Select-String x') is None
    assert proc.nested_powershell('Write-Output 1; powershell -Command "$x"') is None
    assert proc.nested_powershell("powershell -File x.ps1") is None
    argv = proc.nested_powershell('powershell -ExecutionPolicy Bypass -c "echo ""hi"" $env:X"')
    assert argv[1:4] == ["-ExecutionPolicy", "Bypass", "-Command"] and argv[4].endswith('echo "hi" $env:X')


def test_failure_hint_for_expanded_nested_powershell():
    hint = proc._failure_hint('powershell -Command "$x" | sort', False, "ParserError: Unexpected token")
    assert hint and "script" in hint
    assert proc._failure_hint("git status", False, "ParserError") is None
    assert "stdin" in proc._failure_hint(None, False, "mise ERROR batch file arguments are invalid")


def test_adjust_argv_fixes_python_isolation_and_powershell_encoding():
    argv, notes = proc.adjust_argv(["/usr/bin/python3", "-I", "tool.py", "-I"])
    assert argv == ["/usr/bin/python3", "-X", "utf8", "-I", "tool.py", "-I"] and notes
    assert proc.adjust_argv(["python.exe", "-X", "utf8", "-I", "t.py"])[0][1:3] == ["-X", "utf8"]
    assert proc.adjust_argv(["python", "-Ic", "print(1)"])[0][1:3] == ["-X", "utf8"]
    assert proc.adjust_argv(["python", "t.py", "-I"])[0] == ["python", "t.py", "-I"]
    argv, _ = proc.adjust_argv([r"C:\x\powershell.exe", "-NoProfile", "-Command", "Get-Date"])
    assert argv[3].startswith("[Console]::OutputEncoding") and argv[3].endswith("; Get-Date")
    with pytest.raises(ValueError, match="line breaks"):
        proc.adjust_argv([r"C:\mise\shims\comet.cmd", "next", "line one\nline two"])


def test_decode_mixes_utf8_and_legacy_lines():
    """One line in the machine's legacy code page (cp936 on Chinese Windows, cp1252 on an English
    one) does not garble the UTF-8 lines around it, nor does half a character at the end."""
    legacy = proc._legacy_encodings()[0]
    text = next(t for t in ("第二行 旧代码页", "Ünïcödé façade", "plain") if _encodable(t, legacy))
    data = "第一行 utf-8\n".encode() + f"{text}\n".encode(legacy) + "完成".encode()[:-1]
    assert proc._decode(data) == f"第一行 utf-8\n{text}\n完"


def _encodable(text: str, enc: str) -> bool:
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def test_exit_codes_are_signed_with_crash_notes(monkeypatch):
    monkeypatch.setattr(proc.winenv, "IS_WINDOWS", True)
    assert proc.exit_info(4294967295)[0] == -1
    code, note = proc.exit_info(0xC0000005)
    assert code == -1073741819 and "ACCESS_VIOLATION" in note
    assert proc.exit_info(3) == (3, None)
    if sys.platform != "win32":  # Windows has no SIGKILL
        monkeypatch.setattr(proc.winenv, "IS_WINDOWS", False)
        assert proc.exit_info(-9) == (-9, "killed by signal SIGKILL")


# ------------------------------------------------------------------ waiting


async def test_job_status_waits_for_a_pattern_then_for_the_end():
    code = (
        "import time\n"
        "print('starting', flush=True)\n"
        "time.sleep(1.0)\n"
        "print('ready', flush=True)\n"
        "time.sleep(1.0)\n"
        "raise SystemExit(5)\n"
    )
    res = proc.run(sys.executable, ["-c", code], timeout_s=60, reply_within_s=0.3)
    assert res["still_running"]
    async with Client(build_server(Config())) as client:
        began = time.monotonic()
        hit = (
            await client.call_tool(
                "job_status", {"id": res["id"], "since": res["cursor"], "wait_s": 10, "patterns": ["^ready"]}
            )
        ).structured_content
        assert hit["state"] == "running" and hit["matched"] == "^ready" and "ready" in hit["output"]
        done = (
            await client.call_tool("job_status", {"id": res["id"], "since": hit["cursor"], "wait_s": 10})
        ).structured_content
        assert done["state"] == "finished" and done["exit_code"] == 5
        assert time.monotonic() - began < 8


async def test_job_status_wait_runs_out_and_says_so():
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(30)"], timeout_s=60, reply_within_s=0.3)
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("job_status", {"id": res["id"], "wait_s": 1})).structured_content
        assert out["state"] == "running" and "call job_status again" in out["note"]
        bad = await client.call_tool("job_status", {"id": res["id"], "wait_s": 1, "patterns": ["("]})
        assert "invalid regex" in bad.structured_content["error"]
        await client.call_tool("job_stop", {"id": res["id"]})


async def test_identical_waits_share_one_task():
    started = 0

    async def slow():
        nonlocal started
        started += 1
        await asyncio.sleep(0.3)
        return {"value": 1}

    results = await asyncio.gather(*(coalesce.shared(("k", 1), slow) for _ in range(6)))
    assert started == 1 and all(r == {"value": 1} for r in results)
    results[0]["value"] = 2
    assert results[1]["value"] == 1, "each caller gets its own copy"
    assert coalesce.inflight() == 0


# ------------------------------------------------------------------ history


async def test_history_shows_what_ran():
    async with Client(build_server(Config())) as client:
        await client.call_tool("run", {"command": sys.executable, "args": ["-c", "raise SystemExit(7)"]})
        await client.call_tool("fs_stat", {"path": "."})
        calls = (await client.call_tool("history", {"limit": 5})).structured_content["calls"]
        assert [c["tool"] for c in calls[:2]] == ["fs_stat", "run"]
        assert calls[1]["exit_code"] == 7 and calls[1]["status"] == "ok"
        only_run = (await client.call_tool("history", {"tool": "run"})).structured_content["calls"]
        assert len(only_run) == 1


# ------------------------------------------------------------------ files: read/write/edit


def test_fs_read_tail_budget_and_hash(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text("\n".join(f"line {i:04d}" for i in range(1, 501)), encoding="utf-8")
    tail = fs.fs_read(str(p), tail=3)
    assert tail["content"].splitlines()[-1].endswith("line 0500") and len(tail["content"].splitlines()) == 3
    capped = fs.fs_read(str(p), max_chars=1000)
    shown = capped["content"].splitlines()
    assert len(capped["content"]) <= 1000 and f"offset={len(shown) + 1}" in capped["more"]
    assert "max_chars" in capped["more"] and len(capped["sha256"]) == 64


def test_fs_write_is_atomic_appends_and_detects_changes(tmp_path):
    p = tmp_path / "notes" / "a.md"
    first = fs.fs_write(str(p), "one\n")
    assert first["sha256"] == fs.sha256_of(str(p))
    with pytest.raises(fs.FsError, match="must not|changed since"):
        fs.fs_write(str(p), "two\n", expected_sha256="")
    p.write_text("someone else\n")
    with pytest.raises(fs.FsError, match="changed since you read it"):
        fs.fs_write(str(p), "two\n", expected_sha256=first["sha256"])
    current = fs.fs_read(str(p))["sha256"]
    fs.fs_write(str(p), "two\n", expected_sha256=current)
    appended = fs.fs_write(str(p), "three\n", append=True)
    assert p.read_text() == "two\nthree\n" and appended["size"] == 10
    assert sorted(x.name for x in p.parent.iterdir()) == ["a.md"], "no temporary files left"


def test_fs_edit_applies_several_edits_or_none(tmp_path):
    p = tmp_path / "cfg.ini"
    p.write_bytes("a=1\r\nb=2\r\n# 中文\r\n".encode("gbk"))
    before = p.read_bytes()
    with pytest.raises(fs.FsError, match="edit 2: old text not found"):
        fs.fs_edit(str(p), edits=[{"old": "a=1", "new": "a=10"}, {"old": "zzz", "new": "q"}])
    assert p.read_bytes() == before
    res = fs.fs_edit(str(p), edits=[{"old": "a=1", "new": "a=10"}, {"old": "b=2\n", "new": "b=20\n"}])
    assert res["replacements"] == 2 and [e["line"] for e in res["edits"]] == [1, 2]
    assert p.read_bytes() == "a=10\r\nb=20\r\n# 中文\r\n".encode("gbk")
    with pytest.raises(fs.FsError, match="changed since"):
        fs.fs_edit(str(p), "a=10", "a=11", expected_sha256="0" * 64)
    with pytest.raises(fs.FsError, match="not both"):
        fs.fs_edit(str(p), "a", "b", edits=[{"old": "a", "new": "b"}])


async def test_fs_edit_tool_takes_edit_objects(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("alpha beta\n")
    async with Client(build_server(Config())) as client:
        res = await client.call_tool(
            "fs_edit",
            {"path": str(p), "edits": [{"old": "alpha", "new": "A"}, {"old": "beta", "new": "B"}]},
        )
        assert res.structured_content["replacements"] == 2 and p.read_text() == "A B\n"


# ------------------------------------------------------------------ files: list/find/stat


def test_fs_list_sorts_and_filters(tmp_path):
    for name, size, age in (("old.log", 10, 7200), ("big.bin", 500, 3600), ("new.txt", 1, 0)):
        f = tmp_path / name
        f.write_bytes(b"x" * size)
        os.utime(f, (time.time() - age, time.time() - age))
    (tmp_path / "sub").mkdir()
    by_time = [e["path"] for e in fs.fs_list(str(tmp_path), sort="mtime", kind="file")["entries"]]
    assert by_time == ["new.txt", "big.bin", "old.log"]
    by_size = [e["path"] for e in fs.fs_list(str(tmp_path), sort="size", kind="file")["entries"]]
    assert by_size[0] == "big.bin"
    recent = [e["path"] for e in fs.fs_list(str(tmp_path), newer_than="90m", kind="file")["entries"]]
    assert recent == ["big.bin", "new.txt"]
    assert [e["path"] for e in fs.fs_list(str(tmp_path), kind="dir")["entries"]] == ["sub/"]
    with pytest.raises(fs.FsError, match="newer_than"):
        fs.fs_list(str(tmp_path), newer_than="yesterday-ish")


def test_fs_find_by_glob_or_text(tmp_path):
    (tmp_path / "a" / "frontier-chain-skeleton").mkdir(parents=True)
    (tmp_path / "a" / "node_modules" / "skip-me.py").parent.mkdir()
    (tmp_path / "a" / "node_modules" / "skip-me.py").write_text("")
    (tmp_path / "a" / "Report-1.XLSX").write_text("")
    (tmp_path / "b.py").write_text("")
    found = fs.fs_find("chain-skel", str(tmp_path))
    assert [m["path"] for m in found["matches"]] == [os.path.join("a", "frontier-chain-skeleton") + "/"]
    assert [m["path"] for m in fs.fs_find("report-*.xlsx", str(tmp_path))["matches"]] == [
        os.path.join("a", "Report-1.XLSX")
    ]
    assert [m["path"] for m in fs.fs_find("*.py", str(tmp_path))["matches"]] == ["b.py"]
    assert fs.fs_find("*.py", str(tmp_path), max_depth=1)["count"] == 1
    assert fs.fs_find("a", str(tmp_path), kind="dir", max_depth=1)["count"] == 1
    assert "timed_out" in fs.fs_find("x", str(tmp_path), budget_s=-1)


def test_fs_stat_hash(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    out = fs.fs_stat(str(p), hash="sha256")
    assert out["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert fs.fs_stat(str(p), hash="md5")["md5"] == "900150983cd24fb0d6963f7d28e17f72"


# ------------------------------------------------------------------ files: search


def test_fs_search_context_and_files_only(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("one\ntwo needle\nthree\n")
    (tmp_path / "b.txt").write_text("needle\n")
    for use_rg in (True, False):
        if not use_rg:
            monkeypatch.setattr(fs.shutil, "which", lambda _name: None)
        ctx = fs.fs_search("two needle", str(tmp_path), context=1)
        assert ctx["matches"] == ["a.txt-1-one", "a.txt:2:two needle", "a.txt-3-three"], ctx
        files = fs.fs_search("needle", str(tmp_path), files_only=True)
        assert sorted(files["matches"]) == ["a.txt", "b.txt"], files


def test_fs_search_reads_gbk_matches(tmp_path):
    (tmp_path / "gbk.c").write_bytes("// 初始化串口 uart_init\n".encode("gbk"))
    out = fs.fs_search("uart_init", str(tmp_path))
    assert out["matches"] == ["gbk.c:1:// 初始化串口 uart_init"], out


@POSIX
def test_fs_search_retries_with_pcre2_and_keeps_partial_results(tmp_path, monkeypatch):
    rg = fake_program(
        tmp_path / "bin",
        "rg",
        "import sys, time\n"
        "if '--pcre2' in sys.argv:\n"
        "    print(sys.argv[-1] + '/x.txt:1:found'); raise SystemExit(0)\n"
        "if 'slow' in sys.argv:\n"
        "    print(sys.argv[-1] + '/y.txt:1:early', flush=True); time.sleep(30)\n"
        "sys.stderr.write('regex parse error: look-around, including look-ahead and look-behind, is not supported')\n"
        "raise SystemExit(2)\n",
    )
    monkeypatch.setattr(fs.shutil, "which", lambda _name: str(rg))
    out = fs.fs_search("a(?!b)", str(tmp_path))
    assert out["engine"] == "ripgrep (pcre2)" and out["matches"] == ["x.txt:1:found"]
    began = time.monotonic()
    slow = fs.fs_search("slow", str(tmp_path), budget_s=1)
    assert slow["matches"] == ["y.txt:1:early"] and "timed_out" in slow and time.monotonic() - began < 10


# ------------------------------------------------------------------ files: managing


def test_fs_op_copy_move_delete_mkdir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("A")
    assert fs.fs_op("mkdir", str(tmp_path / "dst" / "deep"))["created"]
    fs.fs_op("copy", str(src / "a.txt"), str(tmp_path / "dst"))
    assert (tmp_path / "dst" / "a.txt").read_text() == "A"
    with pytest.raises(fs.FsError, match="already exists"):
        fs.fs_op("copy", str(src / "a.txt"), str(tmp_path / "dst"))
    fs.fs_op("copy", str(src), str(tmp_path / "copy"))
    assert (tmp_path / "copy" / "a.txt").exists()
    moved = fs.fs_op("move", str(tmp_path / "copy"), str(tmp_path / "moved"))
    assert (
        moved["type"] == "dir"
        and (tmp_path / "moved" / "a.txt").exists()
        and not (tmp_path / "copy").exists()
    )
    with pytest.raises(fs.FsError, match="not empty"):
        fs.fs_op("delete", str(tmp_path / "moved"), permanent=True)
    assert not fs.fs_op("delete", str(tmp_path / "moved"), recursive=True, permanent=True)["recycled"]
    assert not (tmp_path / "moved").exists()
    with pytest.raises(fs.FsError, match="drive root or system folder"):
        fs.fs_op("delete", str(Path.home()), recursive=True, permanent=True)
    with pytest.raises(fs.FsError, match="action is"):
        fs.fs_op("chmod", str(src))


def test_script_shell_default_matches_the_platform(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    expected = ("pwsh", "powershell") if sys.platform == "win32" else ("bash",)
    assert proc.default_script_shell() in expected
    assert "PYTHONUNBUFFERED" in winenv.build_env() and "NO_COLOR" not in winenv.build_env(terminal=True)


@POSIX
def test_script_python_skips_the_store_placeholder(tmp_path):
    stub = fake_program(tmp_path / "WindowsApps", "python", "raise SystemExit(9009)")
    real = fake_program(tmp_path / "real", "python", "print('real')")
    path = os.pathsep.join([str(stub.parent), str(real.parent)])
    assert proc._python(path) == str(real)


@POSIX
def test_atomic_write_keeps_mode_and_symlinks(tmp_path):
    script = tmp_path / "tool.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    fs.fs_write(str(script), "echo new\n")
    assert script.stat().st_mode & 0o777 == 0o755
    link = tmp_path / "link.sh"
    link.symlink_to(script)
    fs.fs_edit(str(link), "new", "newer")
    assert link.is_symlink() and script.read_text() == "echo newer\n"
