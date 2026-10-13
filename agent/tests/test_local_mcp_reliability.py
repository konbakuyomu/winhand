"""local-mcp-reliability: every tool keeps a line of description in a session's start, and a `uv run`
server whose project environment points to a removed Python is rebuilt before it starts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import textwrap

import pytest
from fastmcp import Client
from mcp.types import TextContent

from winhand import config, local_mcp, mcp_bridge, session_bridge, venv_repair
from winhand.config import Config, ServerEntry
from winhand.mcp_bridge import Services
from winhand.server import build_server

from .test_mcp_bridge import fake_entry

UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv is needed")


@pytest.fixture(autouse=True)
def _no_uv_settings_of_this_shell(monkeypatch):
    """The developer's own UV_* settings must not change which environment a test's server uses."""
    for name in ("UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR", "UV_NO_PROJECT", "UV_ISOLATED"):
        monkeypatch.delenv(name, raising=False)


# name and description lengths of the tools on the machine this was reported on (2026-10-13)
MSI = {
    "eshop-mcp": (
        0,
        [
            (13, 61),
            (19, 81),
            (16, 49),
            (13, 84),
            (18, 237),
            (8, 68),
            (9, 122),
            (14, 119),
            (15, 117),
            (10, 110),
            (29, 45),
            (24, 62),
            (22, 77),
            (24, 29),
            (24, 56),
            (27, 52),
            (28, 53),
            (28, 59),
            (22, 49),
            (31, 104),
            (30, 64),
            (30, 93),
            (29, 35),
            (33, 70),
            (35, 93),
            (23, 54),
            (25, 55),
            (22, 68),
            (32, 50),
            (32, 62),
            (30, 60),
            (32, 65),
        ],
    ),  # noqa: E501
    "fast-context": (0, [(19, 208), (20, 185)]),
    "freecad-mcp": (
        54,
        [
            (15, 33),
            (13, 31),
            (11, 26),
            (13, 28),
            (18, 62),
            (12, 41),
            (8, 36),
            (24, 43),
            (11, 30),
            (10, 30),
            (14, 49),
            (15, 62),
            (14, 42),
            (14, 40),
            (16, 93),
        ],
    ),  # noqa: E501
    "pyocd-debug": (
        0,
        [
            (16, 88),
            (16, 54),
            (21, 153),
            (24, 54),
            (20, 59),
            (19, 313),
            (17, 119),
            (18, 300),
            (17, 60),
            (17, 56),
            (19, 60),
            (18, 69),
            (19, 70),
            (19, 65),
            (20, 37),
            (23, 71),
            (17, 50),
            (18, 43),
            (24, 33),
            (17, 98),
            (20, 276),
            (22, 44),
            (26, 30),
            (21, 28),
            (16, 195),
            (17, 62),
            (16, 35),
            (14, 74),
            (27, 198),
            (16, 132),
            (26, 54),
            (24, 44),
            (14, 148),
            (15, 47),
            (21, 94),
            (19, 214),
            (23, 247),
            (18, 214),
            (20, 255),
            (22, 41),
            (26, 30),
            (21, 28),
            (22, 488),
            (25, 304),
            (32, 234),
            (27, 270),
            (27, 169),
            (17, 193),
            (21, 225),
            (21, 396),
            (15, 195),
            (14, 31),
            (14, 115),
            (15, 100),
            (16, 63),
            (18, 247),
            (18, 275),
            (24, 414),
        ],
    ),  # noqa: E501
    "usb-camera-mcp": (0, [(13, 91), (19, 95), (14, 84), (13, 85), (14, 85)]),
}


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


def servers_like(shape: dict, monkeypatch) -> dict[str, list[tuple[str, str]]]:
    """Register servers with tools of the given name and description lengths; their (name, description)."""
    entries = [fake_entry(name) for name in shape]
    monkeypatch.setattr(local_mcp, "active", Services(entries))
    made = {}
    for entry, (instructions, tools) in zip(entries, shape.values(), strict=True):
        listed = []
        for i, (name_len, desc_len) in enumerate(tools):
            name = f"{entry.name[:3]}{i:02}".ljust(name_len, "x")
            description = (f"tool {i} does " + "something-useful-" * 40)[
                :desc_len
            ]  # no space to strip at the end
            listed.append((name, description))
        local_mcp.remember(
            entry,
            [{"name": n, "description": d} for n, d in listed],
            "i" * instructions if instructions else None,
        )
        made[entry.name] = listed
    return made


def tool_lines(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"^- (\S+?): (.*)$", text, re.M)}


# ------------------------------------------------------------------ A1, A2 the tools part


def test_every_tool_keeps_a_line_of_description(winhand_home, monkeypatch):
    made = servers_like(MSI, monkeypatch)
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert session_bridge._size(text) <= session_bridge.BUDGET_CHARS
    lines = tool_lines(text)
    every = [t for tools in made.values() for t in tools]
    assert len(every) == 112 and set(lines) == {name for name, _ in every}  # pyocd-debug's 55 included
    cut = {len(line) for name, desc in every if (line := lines[name]) != desc}
    assert len(cut) == 1 and cut.pop() >= local_mcp.MIN_TOOL_CHARS  # one common length
    for name, desc in every:  # a short description stays whole, a long one ends in "…"
        assert lines[name] == desc or (lines[name].endswith("…") and desc.startswith(lines[name][:-1]))


def test_when_everything_fits_nothing_is_cut(winhand_home, monkeypatch):
    made = servers_like({"a": (0, [(10, 140)] * 5), "b": (40, [(12, 90)] * 5)}, monkeypatch)
    lines = tool_lines(local_mcp.session_text(session_bridge.BUDGET_CHARS))
    assert all(lines[name] == desc for tools in made.values() for name, desc in tools)


def test_the_last_servers_are_named_only_then_left_out(winhand_home, monkeypatch):
    made = servers_like({f"s{i}": (100, [(30, 200)] * 60) for i in range(3)}, monkeypatch)
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert session_bridge._size(text) <= session_bridge.BUDGET_CHARS
    lines = tool_lines(text)
    assert all(len(lines[name]) == local_mcp.MIN_TOOL_CHARS for name, _ in made["s0"])  # described, shortest
    for later in ("s1", "s2"):  # by tool names only
        assert f"## {later}\n" + ", ".join(name for name, _ in made[later]) in text

    servers_like({f"many-{i}": (100, [(30, 200)] * 60) for i in range(10)}, monkeypatch)
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert session_bridge._size(text) <= session_bridge.BUDGET_CHARS
    shown = sum(f"## many-{i}\n" in text for i in range(10))
    assert 0 < shown < 10 and f"{10 - shown} more server(s) left out for lack of room" in text


# ------------------------------------------------------------------ R2 which servers are checked


def entry(*args, command="uv", cwd=None, env=None, **kw) -> ServerEntry:
    return ServerEntry(name="s", command=command, args=list(args), cwd=cwd, env=env or {}, **kw)


def project_at(folder) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text("[project]\nname = 'p'\nversion = '0'\n", encoding="utf-8")
    return str(folder)


def test_the_project_of_a_uv_run_server(tmp_path):
    d = project_at(tmp_path)
    project_at(tmp_path / "sub")
    found = venv_repair.uv_project(entry("--directory", d, "run", "--locked", "usb-camera-mcp"))
    assert (found.project, found.venv, found.lock_args) == (d, os.path.join(d, ".venv"), ["--locked"])
    found = venv_repair.uv_project(
        entry(
            f"--directory={d}",
            "run",
            "--with",
            "rich",
            "--python",
            "3.12",
            "--managed-python",
            "--frozen",
            "x",
        )
    )
    assert found.python_args == ["--python", "3.12"] and found.lock_args == ["--frozen"]
    assert found.global_args == ["--directory", d, "--managed-python"]
    carried = venv_repair.uv_project(
        entry("--offline", "--config-file", "uv.toml", "--directory", d, "run", "-p3.11", "--locked", "x")
    )
    assert carried.global_args == ["--offline", "--config-file", "uv.toml", "--directory", d]
    assert carried.python_args == ["--python", "3.11"]
    stepped = venv_repair.uv_project(entry("run", "--refresh-package", "p", "--locked", "x", cwd=d))
    assert stepped.lock_args == ["--locked"]  # the value of --refresh-package is not the command
    upper = venv_repair.uv_project(
        entry("run", "x", command=r"C:\Users\me\AppData\Local\mise\shims\UV.EXE", cwd=d)
    )
    assert upper.project == d
    named = venv_repair.uv_project(entry("run", "x", cwd=d, env={"UV_PROJECT_ENVIRONMENT": "envs/main"}))
    assert named.venv == os.path.join(d, "envs", "main")
    nested = venv_repair.uv_project(entry("--project", "sub", "run", "x", cwd=d))
    assert nested.project == os.path.join(d, "sub")


@pytest.mark.parametrize(
    "make",
    [
        lambda d: entry("run", "--no-project", "x", cwd=d),
        lambda d: entry("run", "--isolated", "x", cwd=d),
        lambda d: entry("run", "--script", "x.py", cwd=d),
        lambda d: entry("run", "-s", "x.py", cwd=d),
        lambda d: entry("run", "--active", "x", cwd=d),
        lambda d: entry("run", "--refresh-package", "p", "--no-project", "x", cwd=d),
        lambda d: entry("run", "x", cwd=d, env={"UV_NO_PROJECT": "1"}),
        lambda d: entry("run", "x"),  # neither a project directory nor a cwd
        lambda d: entry("tool", "run", "x", cwd=d),
        lambda d: entry("x", command="uvx", cwd=d),
        lambda d: entry("-y", "eshop-mcp", command="npx", cwd=d),
        lambda d: ServerEntry(name="h", url="http://127.0.0.1:1/mcp"),
    ],
)
def test_servers_that_are_not_checked(make, tmp_path):
    assert venv_repair.uv_project(make(project_at(tmp_path / "p"))) is None


def test_a_folder_without_a_project_is_not_checked(tmp_path):
    (tmp_path / "loose" / ".venv").mkdir(parents=True)  # a .venv but no pyproject.toml up to the root
    assert venv_repair.uv_project(entry("--directory", str(tmp_path / "loose"), "run", "x")) is None


def test_a_subfolder_and_a_workspace_member_use_the_environment_uv_uses(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'ws'\nversion = '0'\n"
        "[tool.uv.workspace]\nmembers = ['packages/*']\nexclude = ['packages/own']\n",
        encoding="utf-8",
    )
    member = project_at(root / "packages" / "server")
    (root / "packages" / "server" / "src").mkdir()
    own = project_at(root / "packages" / "own")
    inside = venv_repair.uv_project(
        entry("--directory", str(root / "packages" / "server" / "src"), "run", "x")
    )
    assert inside.project == member and inside.venv == str(root / ".venv")
    alone = venv_repair.uv_project(entry("run", "x", cwd=own))
    assert alone.project == own and alone.venv == os.path.join(own, ".venv")


@pytest.mark.skipif(os.name == "nt", reason="POSIX environments link to their base Python")
def test_a_base_with_only_a_versioned_python_is_sound(tmp_path):
    base = tmp_path / "onlyver" / "bin"
    base.mkdir(parents=True)
    os.symlink(os.path.realpath(sys.executable), base / "python3.11")
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(base / "python3.11", venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    assert venv_repair.missing_python(str(venv)) is None
    (base / "python3.11").unlink()  # now the link leads nowhere and the base has no Python
    assert venv_repair.missing_python(str(venv)) is not None


def test_a_broken_environment_is_told_from_a_sound_one(tmp_path):
    venv = tmp_path / ".venv"
    venv.mkdir()
    assert venv_repair.missing_python(str(venv)) is None  # no pyvenv.cfg: nothing to say
    (venv / "pyvenv.cfg").write_text("home = " + os.path.dirname(sys.executable) + "\n", encoding="utf-8")
    real = os.path.dirname(os.path.realpath(sys.executable))
    (venv / "pyvenv.cfg").write_text(f"home = {real}\nversion_info = 3\n", encoding="utf-8")
    assert venv_repair.missing_python(str(venv)) is None
    gone = tmp_path / "gone" / "3.12.14"
    (venv / "pyvenv.cfg").write_text(f"home = {gone}\nimplementation = CPython\n", encoding="utf-8")
    assert venv_repair.missing_python(str(venv)).startswith(str(gone))


# ------------------------------------------------------------------ A3, A4 the repair, with uv itself

TINY_SERVER = textwrap.dedent(
    """\
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "tiny", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "hello", "description": "Say hello.", "inputSchema": {"type": "object"}}]}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """
)


@pytest.fixture
def broken_project(tmp_path):
    """A uv project with a stdlib-only MCP server, locked, its .venv pointing to a removed Python."""
    import subprocess

    project = tmp_path / "tiny project"  # a space, as in D:\\Dev\\20_个人项目
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'tiny'\nversion = '0.1.0'\nrequires-python = '>=3.10'\ndependencies = []\n"
        "[tool.uv]\npackage = false\n",
        encoding="utf-8",
    )
    (project / "server.py").write_text(TINY_SERVER, encoding="utf-8")
    env = {**os.environ, "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    for command in (["venv", "--python", sys.executable], ["lock"]):
        subprocess.run([UV, *command], cwd=project, env=env, check=True, capture_output=True)
    cfg = project / ".venv" / "pyvenv.cfg"
    gone = tmp_path / "mise" / "installs" / "python" / "3.12.14"
    text = re.sub(
        r"(?m)^home = .*$", lambda m: f"home = {gone}", cfg.read_text(encoding="utf-8")
    )  # a Windows path
    cfg.write_text(text, encoding="utf-8")
    if os.name != "nt":  # there the environment's pythons are links to the base one: make them lead nowhere
        for link in (project / ".venv" / "bin").glob("python*"):
            if link.is_symlink():
                link.unlink()
                link.symlink_to(gone / "bin" / "python3")
    assert venv_repair.missing_python(str(project / ".venv"))
    return project


def tiny_entry(project, **kw) -> ServerEntry:
    return ServerEntry(
        name="tiny",
        command=UV,
        args=[
            "--directory",
            str(project),
            "run",
            "--locked",
            "--python",
            sys.executable,
            "python",
            "server.py",
        ],
        env={"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"},
        startup_timeout_s=60,
        **kw,
    )


@needs_uv
async def test_a_removed_python_is_rebuilt_before_the_server_starts(
    broken_project, winhand_home, monkeypatch
):
    services = Services([tiny_entry(broken_project)])
    monkeypatch.setattr(local_mcp, "active", services)
    try:
        listed = await local_mcp.listing("tiny", 60)
        assert [t["name"] for t in listed["tools"]] == ["hello"]
        assert venv_repair.missing_python(str(broken_project / ".venv")) is None
        status = services.services["tiny"].status()
        assert status["state"] == "running" and "repaired its Python environment" in status["note"]
        log = (winhand_home / "logs" / "mcp" / "tiny.log").read_text(encoding="utf-8")
        assert "3.12.14" in log and "rebuilding it" in log and "sync --python" in log and "--locked" in log
    finally:
        await services.close()


@needs_uv
async def test_with_repair_off_the_cause_and_the_fix_are_named(broken_project, winhand_home, monkeypatch):
    services = Services([tiny_entry(broken_project, repair_env=False)])
    monkeypatch.setattr(local_mcp, "active", services)
    before = (broken_project / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    async with Client(build_server(Config())) as c:
        refused = await c.call_tool("mcp_tools", {"server": "tiny"}, raise_on_error=False)
    text = text_of(refused)
    assert (
        refused.is_error and "3.12.14" in text and "no longer exists" in text and "repair_env = false" in text
    )
    assert "venv --clear" in text and " sync " in text
    assert (broken_project / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8") == before
    assert services.services["tiny"].process is None
    await services.close()


@needs_uv
async def test_the_test_button_repairs_too(broken_project, winhand_home):
    result = await mcp_bridge.probe(tiny_entry(broken_project), timeout=60)
    assert result["ok"] and "repaired its Python environment" in result["repaired"]
    assert local_mcp.cached(tiny_entry(broken_project))["tools"][0]["name"] == "hello"


@needs_uv
def test_sys_info_names_a_broken_environment(broken_project, winhand_home, monkeypatch):
    found = venv_repair.checks([tiny_entry(broken_project), fake_entry()])
    (item,) = found
    assert item["check"] == "mcp_env:tiny" and not item["ok"] and "3.12.14" in item["detail"]
    assert "rebuilds it the next time" in item["fix"]


# ------------------------------------------------------------------ A6 a failed start says why


async def test_no_python_at_is_explained(winhand_home, monkeypatch):
    say = "import sys; sys.stderr.write('No Python at \\'\"C:\\\\gone\\\\python.exe\\'\\n'); sys.exit(103)"
    dying = ServerEntry(name="dying", command=sys.executable, args=["-c", say], startup_timeout_s=10)
    services = Services([dying])
    monkeypatch.setattr(local_mcp, "active", services)
    async with Client(build_server(Config())) as c:
        failed = await c.call_tool("mcp_tools", {"server": "dying"}, raise_on_error=False)
    assert (
        failed.is_error and r"C:\gone\python.exe" in text_of(failed) and "no longer exists" in text_of(failed)
    )
    probed = await mcp_bridge.probe(dying, timeout=10)
    assert not probed["ok"] and "no longer exists" in probed["error"]
    await services.close()


async def test_an_old_line_in_the_test_log_is_not_the_cause(winhand_home):
    log = winhand_home / "logs" / "mcp" / "plain.test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("No Python at '\"C:\\old\\python.exe'\n", encoding="utf-8")
    plain = ServerEntry(name="plain", command=sys.executable, args=["-c", "import sys; sys.exit(1)"])
    probed = await mcp_bridge.probe(plain, timeout=10)
    assert not probed["ok"] and "no longer exists" not in probed["error"]


def test_with_repair_on_and_a_sound_environment_the_other_one_is_named(tmp_path):
    d = project_at(tmp_path / "p")
    why = venv_repair.explain(entry("--directory", d, "run", "x"), ["No Python at '\"C:\\gone\\python.exe'"])
    assert "automatic repair is on" in why and "another one" in why and "uv venv --clear" in why
    off = venv_repair.explain(
        entry("--directory", d, "run", "x", repair_env=False), ["No Python at '\"C:\\gone\\python.exe'"]
    )
    assert "sound or absent" in off and "repair_env = false" in off and f'{d}{os.sep}.venv"' not in off


# ------------------------------------------------------------------ round 2 of the verification


def test_more_uv_spellings(tmp_path):
    d = project_at(tmp_path / "p")
    assert venv_repair.uv_project(entry("run", "-p=3.13", "x", cwd=d)).python_args == ["--python", "3.13"]
    assert venv_repair.uv_project(entry("--isolated", "run", "x", cwd=d)) is None
    assert (
        venv_repair.uv_project(entry("run", "--with-requirements", "r.txt", "x", cwd=d)) is not None
    )  # uses .venv
    hidden = venv_repair.uv_project(
        entry("run", "--python-preference", "only-managed", "--locked", "x", cwd=d)
    )
    assert hidden.lock_args == ["--locked"] and hidden.global_args == ["--python-preference", "only-managed"]


@pytest.mark.parametrize(
    "text", ["[tool]\nuv = 1\n", "tool = 'x'\n", "[tool.uv]\nworkspace = 3\n", "not toml ["]
)
def test_an_odd_pyproject_does_not_break_anything(tmp_path, text):
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    server = entry("run", "x", cwd=str(tmp_path))
    assert venv_repair.prepare(server) is None
    assert venv_repair.checks([server]) == []


def test_a_folder_that_cannot_be_read_is_not_taken_for_gone(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'share'}\n", encoding="utf-8")
    monkeypatch.setattr(venv_repair.winenv, "IS_WINDOWS", True)

    def refuse(path):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(venv_repair.os, "listdir", refuse)
    assert venv_repair.missing_python(str(venv)) is None


def test_workspace_members_match_case_as_uv_does(tmp_path, monkeypatch):
    root = tmp_path / "WS"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['packages/*']\n", encoding="utf-8")
    other_case = project_at(root / "Packages" / "Srv")
    same_case = project_at(root / "packages" / "srv")
    monkeypatch.setattr(venv_repair.winenv, "IS_WINDOWS", True)  # uv's glob ignores no case there either
    assert venv_repair._workspace_root(other_case) == other_case
    assert venv_repair._workspace_root(same_case) == str(root)


def test_an_enclosing_project_that_is_no_workspace_ends_the_search(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['mid/*']\n", encoding="utf-8")
    project_at(outer / "mid")  # a project of its own, not a workspace
    inner = project_at(outer / "mid" / "inner")
    assert venv_repair._workspace_root(inner) == inner


@pytest.mark.skipif(os.name == "nt", reason="POSIX links")
def test_a_linked_project_is_taken_as_written_like_uv_does(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['m']\n", encoding="utf-8")
    project_at(ws / "m")
    (tmp_path / "links").mkdir()
    os.symlink(ws / "m", tmp_path / "links" / "m")
    by_project = venv_repair.uv_project(entry("--project", str(tmp_path / "links" / "m"), "run", "x"))
    assert by_project.venv == str(tmp_path / "links" / "m" / ".venv")  # uv does not resolve --project
    by_directory = venv_repair.uv_project(entry("--directory", str(tmp_path / "links" / "m"), "run", "x"))
    assert by_directory.venv == str(ws / ".venv")  # but its current directory is the real one


def test_odd_home_values_are_not_taken_for_gone(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = C:\\gone\x00\n", encoding="utf-8")
    assert venv_repair.missing_python(str(venv)) is None  # a NUL in the path
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'share'}\n", encoding="utf-8")
    monkeypatch.setattr(venv_repair.winenv, "IS_WINDOWS", True)

    def offline(path):
        error = FileNotFoundError(2, "The network path was not found", str(path))
        error.winerror = 53
        raise error

    monkeypatch.setattr(venv_repair.os, "listdir", offline)
    assert venv_repair.missing_python(str(venv)) is None


@pytest.mark.skipif(os.name == "nt", reason="the doubled output only happened on POSIX")
def test_a_timed_out_command_keeps_its_output_once():
    import time

    code, output = venv_repair._run(
        [sys.executable, "-c", "import time; print('first-line', flush=True); time.sleep(30)"],
        os.getcwd(),
        dict(os.environ),
        time.monotonic() + 1.5,
    )
    assert code is None and output.count("first-line") == 1


def test_a_server_with_no_tools_is_not_left_out_early(winhand_home, monkeypatch):
    servers_like({"a": (0, []), "b": (0, [(1, 400)])}, monkeypatch)
    text = local_mcp.session_text(260)
    assert "## a\n(no tools)" in text and "## b" in text and "left out" not in text


def test_many_servers_are_quick(winhand_home, monkeypatch):
    import time

    servers_like({f"m{i:03}": (50, [(30, 120)] * 40) for i in range(100)}, monkeypatch)
    started = time.monotonic()
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert time.monotonic() - started < 5 and session_bridge._size(text) <= session_bridge.BUDGET_CHARS


# ------------------------------------------------------------------ A8 configuration


def test_repair_env_survives_a_save(winhand_home):
    cfg = Config(
        servers=[ServerEntry(name="a", command="uv", repair_env=False), ServerEntry(name="b", command="uv")]
    )
    config.save(cfg)
    text = (winhand_home / "config.toml").read_text(encoding="utf-8")
    assert text.count("repair_env") == 1  # written only when it is off
    loaded = {s.name: s.repair_env for s in config.load().servers}
    assert loaded == {"a": False, "b": True}
    assert json.dumps(loaded)  # plain booleans
