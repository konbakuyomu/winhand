"""0.9.0: files in and out, and servers (docs/reviews/2026-11-01-usage-review.md, round 3).

remote_run takes stored secrets and Python scripts get env and cwd too, remote_copy moves files
over scp, user:password in URLs is masked, run says so when it was used for ssh, a missed fs_edit
shows the closest text, several files read in one call, folders go in and out as one zip, and
context() can answer briefly.
"""

from __future__ import annotations

import pytest
from fastmcp.client import Client

from winhand import fs, remote
from winhand.config import Config
from winhand.redact import KNOWN_SECRETS, Redactor
from winhand.server import build_server

# ------------------------------------------------------------------ servers


def test_python_scripts_get_env_and_cwd_like_shell_scripts():
    _, body = remote.build("box", "print(1)", shell="python3", env={"TOKEN": "a'b"}, cwd="/srv/app")
    assert body.splitlines()[0] == "import os as _winhand_os"
    assert (
        '"TOKEN": "a\'b"' in body and '_winhand_os.chdir("/srv/app")' in body and body.endswith("print(1)\n")
    )
    _, shell_body = remote.build("box", "echo hi", env={"TOKEN": "x y"}, cwd="/srv")
    assert shell_body.startswith("export TOKEN='x y'\ncd -- /srv || exit 97\n")


async def test_remote_run_hands_stored_secrets_to_the_script(monkeypatch, winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text("CPAMP_ADMIN_KEY=s3cr3t-value-123\n", encoding="utf-8")
    seen = {}

    def fake(host, script, shell, args, env, cwd, timeout_s):
        seen.update(env=env)
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "host": host}

    monkeypatch.setattr(remote, "remote_run", fake)
    async with Client(build_server(Config())) as client:
        out = (
            await client.call_tool(
                "remote_run",
                {"host": "vps", "script": "echo $CPAMP_ADMIN_KEY", "secrets": ["CPAMP_ADMIN_KEY"]},
            )
        ).structured_content
    assert out["exit_code"] == 0 and seen["env"] == {"CPAMP_ADMIN_KEY": "s3cr3t-value-123"}


def test_remote_copy_checks_its_arguments(tmp_path):
    assert "not" in remote.remote_copy("bad host!", str(tmp_path), "/x")["error"]
    assert "direction" in remote.remote_copy("box", str(tmp_path), "/x", direction="sideways")["error"]
    assert "no such file" in remote.remote_copy("box", str(tmp_path / "gone"), "/x")["error"]


def test_passwords_in_urls_are_masked():
    redactor = Redactor({}, [], False, KNOWN_SECRETS)
    text, _ = redactor.text("proxy http://admin:hunter2pass@10.0.0.2:7890 and https://example.com/a:b@c")
    assert "hunter2pass" not in text and "admin" not in text and "@10.0.0.2:7890" in text
    assert "https://example.com/a:b@c" in text  # a path with : and @ is not a login


async def test_run_points_to_remote_run_when_it_starts_ssh():
    async with Client(build_server(Config())) as client:
        out = (
            await client.call_tool("run", {"command": "ssh", "args": ["-V"]}, raise_on_error=False)
        ).structured_content
    assert "remote_run" in out.get("hint_ssh", "")


# ------------------------------------------------------------------ editing


def test_a_missed_edit_shows_the_closest_text(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("def a():\n    return 1\n\ndef b():\n    return 'two'\n", encoding="utf-8")
    with pytest.raises(fs.FsError) as missed:
        fs.fs_edit(str(path), old="def b():\n    return 'too'", new="def b():\n    return 2")
    message = str(missed.value)
    assert "Closest text: lines 4-5" in message and "+    return 'two'" in message


def test_doubly_escaped_quotes_are_pointed_out(tmp_path):
    path = tmp_path / "note.md"
    path.write_text('He said "yes".\n', encoding="utf-8")
    with pytest.raises(fs.FsError, match="escaped twice"):
        fs.fs_edit(str(path), old='He said \\"yes\\".', new="x")
    out = fs.fs_edit(str(path), old='"yes"', new='\\"no\\"')
    assert "escaped twice" in out["warning"]


# ------------------------------------------------------------------ several files at once


async def test_several_files_are_read_in_one_call(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("beta\n" * 10, encoding="utf-8")
    (tmp_path / "c.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    from test_media import _docx

    _docx(tmp_path / "d.docx")
    names = [str(tmp_path / n) for n in ("a.txt", "b.md", "c.png", "gone.txt", "d.docx")]
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("fs_read", {"paths": names})).structured_content
        none = (await client.call_tool("fs_read", {}, raise_on_error=False)).structured_content
    files = {f["path"]: f for f in out["files"]}
    assert "alpha" in files[names[0]]["content"] and "beta" in files[names[1]]["content"]
    assert "picture" in files[names[2]]["note"] and "error" in files[names[3]]
    assert files[names[4]]["type"] == "docx" and files[names[4]]["text"].strip()
    assert out["each_up_to"] >= 2000 and "paths" in none["error"]


def test_reading_several_files_is_judged_for_each():
    from winhand import actions

    (act,) = actions.describe("fs_read", {"path": "/a/x", "paths": ["/b/y", "/c/z"]})
    assert act.kind == "read" and len(act.paths) == 3
    (dl,) = actions.describe("fs_download_url", {"paths": ["/d/one", "/d/two"]})
    assert dl.kind == "read" and len(dl.paths) == 2


# ------------------------------------------------------------------ folders through one link

import hashlib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import tarfile  # noqa: E402
import textwrap  # noqa: E402
import zipfile  # noqa: E402

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402

from winhand import bundle, transfer  # noqa: E402


@pytest.fixture
def relay():
    transfer.set_relay("wss://relay.example/agent")
    app = Starlette(routes=[Route("/transfer/{token}", transfer.endpoint, methods=["GET", "PUT", "POST"])])
    yield httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")
    transfer._tickets.clear()


def _local(url: str) -> str:
    return "/transfer/" + url.rsplit("/", 1)[1]


def _tree(root):
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (root / "src" / "deep" / "日志.log").write_text("中文日志\n", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (root / "photo.png").write_bytes(b"\x89PNG" + bytes(200))
    return root


async def test_a_folder_downloads_as_one_zip(relay, tmp_path):
    root = _tree(tmp_path / "out")
    ticket = transfer.issue_download(str(root), exclude=["node_modules"])
    assert ticket["zip"] == "out.zip" and ticket["files"] == 3 and "sha256" not in ticket
    got = await relay.get(_local(ticket["url"]))
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["out/photo.png", "out/src/app.py", "out/src/deep/日志.log"]
        assert zf.read("out/src/deep/日志.log").decode() == "中文日志\n"
        assert zf.getinfo("out/photo.png").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("out/src/app.py").compress_type == zipfile.ZIP_DEFLATED
    assert (await relay.get(_local(ticket["url"]))).status_code == 404  # single use
    logs = transfer.issue_download(str(root), include=["*.log"])
    assert logs["files"] == 1
    both = transfer.issue_download(None, paths=[str(root / "src"), str(root / "photo.png")])
    assert both["zip"] == "files.zip" and both["files"] == 3
    with pytest.raises(transfer.TransferError, match="no files"):
        transfer.issue_download(str(root), include=["*.nothing"])


async def test_a_folder_download_leaves_out_what_the_read_rules_protect(relay, tmp_path):
    root = _tree(tmp_path / "proj")
    (root / ".env").write_text("TOKEN=1", encoding="utf-8")
    (root / ".winhand").mkdir()
    (root / ".winhand" / "guards.toml").write_text(
        textwrap.dedent(
            """\
            [[guard]]
            on = "read"
            path = ["**/.env"]
            decision = "deny"
            """
        ),
        encoding="utf-8",
    )
    ticket = transfer.issue_download(str(root), exclude=["node_modules"])
    assert "proj/.env" in ticket["left_out"]
    got = await relay.get(_local(ticket["url"]))
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        assert "proj/.env" not in zf.namelist() and "proj/src/app.py" in zf.namelist()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_in_a_folder_are_not_followed(tmp_path):
    root = _tree(tmp_path / "out")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("s", encoding="utf-8")
    os.symlink(tmp_path / "outside", root / "door")
    plan = bundle.plan_download([str(root)])
    assert not any("secret" in name for _, name, _ in plan.files)
    assert any("door" in s and "link" in s for s in plan.skipped)


def test_a_file_gone_while_zipping_is_listed_inside_the_zip(tmp_path):
    root = _tree(tmp_path / "out")
    plan = bundle.plan_download([str(root)], exclude=["node_modules"])
    os.remove(root / "src" / "app.py")
    data = b"".join(bundle.stream_zip(plan))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "app.py" in zf.read(bundle.ERRORS_NAME).decode()
        assert "out/src/app.py" not in zf.namelist() and "out/photo.png" in zf.namelist()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def test_an_uploaded_zip_is_unpacked_into_the_folder(relay, tmp_path):
    data = _zip(
        {"site/index.html": b"<h1>hi</h1>", "site/css/a.css": b"body{}", "readme.txt": "说明".encode()}
    )
    target = tmp_path / "dest"
    ticket = transfer.issue_upload(str(target), sha256=hashlib.sha256(data).hexdigest(), extract=True)
    res = await relay.put(_local(ticket["url"]), content=data)
    body = res.json()
    assert res.status_code == 200 and body["files"] == 3, body
    assert (target / "site" / "css" / "a.css").read_bytes() == b"body{}"
    assert (target / "readme.txt").read_text(encoding="utf-8") == "说明"
    listed = {f["path"]: f["sha256"] for f in body["listing"]}
    assert listed["site/index.html"] == hashlib.sha256(b"<h1>hi</h1>").hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest"]  # no temporary files left
    again = transfer.issue_upload(str(target), extract=True)
    clash = await relay.put(_local(again["url"]), content=data)
    assert clash.status_code == 422 and "already exist" in clash.json()["error"]
    redo = transfer.issue_upload(str(target), extract=True, overwrite=True)
    assert (await relay.put(_local(redo["url"]), content=data)).status_code == 200


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/etc/passwd", "C:/Windows/x.dll", "CON.txt", "a:b.txt", "dir./x", "a\\..\\..\\x"],
)
def test_unsafe_names_refuse_the_whole_archive(tmp_path, name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_zip({"fine.txt": b"ok", name: b"bad"}))
    with pytest.raises(bundle.BundleError, match="nothing was unpacked"):
        bundle.unpack(str(archive), str(tmp_path / "dest"))
    assert not (tmp_path / "dest").exists()


def test_a_tar_unpacks_but_links_in_it_refuse_it(tmp_path):
    good = tmp_path / "good.tar.gz"
    with tarfile.open(good, "w:gz") as tf:
        info = tarfile.TarInfo("pkg/run.sh")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"echo\n"))
    out = bundle.unpack(str(good), str(tmp_path / "a"))
    assert out["files"] == 1 and (tmp_path / "a" / "pkg" / "run.sh").read_bytes() == b"echo\n"
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tf:
        link = tarfile.TarInfo("pkg/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc"
        tf.addfile(link)
    with pytest.raises(bundle.BundleError, match="a link"):
        bundle.unpack(str(bad), str(tmp_path / "b"))
    clash = tmp_path / "clash.zip"
    clash.write_bytes(_zip({"Read.me": b"1", "read.ME": b"2"}))
    with pytest.raises(bundle.BundleError, match="clash"):
        bundle.unpack(str(clash), str(tmp_path / "c"))


def test_unpacking_keeps_to_the_write_rules(tmp_path):
    dest = tmp_path / "proj"
    (dest / ".winhand").mkdir(parents=True)
    (dest / ".winhand" / "guards.toml").write_text(
        '[[guard]]\non = "write"\npath = ["build/**"]\ndecision = "deny"\n', encoding="utf-8"
    )
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip({"src/a.py": b"1", "build/out.bin": b"2"}))
    with pytest.raises(bundle.BundleError, match="rules protect.*build/out.bin"):
        bundle.unpack(str(archive), str(dest))
    assert not (dest / "src").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_unpacking_never_writes_through_a_link_in_the_folder(tmp_path):
    dest, elsewhere = tmp_path / "dest", tmp_path / "elsewhere"
    dest.mkdir()
    elsewhere.mkdir()
    os.symlink(elsewhere, dest / "door")
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip({"door/planted.txt": b"x"}))
    with pytest.raises(bundle.BundleError, match="outside"):
        bundle.unpack(str(archive), str(dest))
    assert not (elsewhere / "planted.txt").exists()
    archive.write_bytes(_zip({"door/empty/": b""}))  # a folder alone
    with pytest.raises(bundle.BundleError, match="outside"):
        bundle.unpack(str(archive), str(dest))
    assert not (elsewhere / "empty").exists()


# ------------------------------------------------------------------ big files in parts


async def test_a_big_file_goes_up_in_parts(relay, tmp_path):
    data = os.urandom(9 * 2**20 + 123)
    target = tmp_path / "video.mp4"
    ticket = transfer.issue_upload(
        str(target), sha256=hashlib.sha256(data).hexdigest(), size=len(data), part_mb=4
    )
    assert len(ticket["parts"]) == 3 and ticket["part_bytes"] == 4 * 2**20 and "split -b" in ticket["curl"]
    step = ticket["part_bytes"]
    pieces = [data[i : i + step] for i in range(0, len(data), step)]
    short = await relay.put(_local(ticket["parts"][2]), content=pieces[2][:-1])
    assert short.status_code == 400 and "send it again" in short.json()["error"]
    first = await relay.put(_local(ticket["parts"][2]), content=pieces[2])  # resent, out of order
    assert first.json() == {"ok": True, "part": 2, "received": 1, "parts": 3}
    await relay.put(_local(ticket["parts"][0]), content=pieces[0])
    last = (await relay.put(_local(ticket["parts"][1]), content=pieces[1])).json()
    assert last["done"] and last["sha256"] == hashlib.sha256(data).hexdigest()
    assert target.read_bytes() == data
    assert sorted(p.name for p in tmp_path.iterdir()) == ["video.mp4"]  # the staging file is gone
    assert (await relay.put(_local(ticket["parts"][0]), content=pieces[0])).status_code == 404


async def test_parts_can_carry_an_archive_to_unpack(relay, tmp_path):
    data = _zip({f"f{i}.bin": os.urandom(300_000) for i in range(8)})
    ticket = transfer.issue_upload(str(tmp_path / "many"), extract=True, size=len(data), part_mb=1)
    step = ticket["part_bytes"]
    for i, url in enumerate(ticket["parts"]):
        res = await relay.put(_local(url), content=data[i * step : (i + 1) * step])
    assert res.json()["files"] == 8 and len(list((tmp_path / "many").iterdir())) == 8


def test_parts_that_never_finish_leave_no_staging_file(tmp_path, monkeypatch):
    ticket = transfer.issue_upload(str(tmp_path / "x.bin"), size=3 * 2**20, part_mb=1)
    (key, _), *_ = [(k, t) for k, t in transfer._tickets.items() if t.index == 0]
    staging = transfer._tickets[key].parted.staging
    with open(staging, "wb") as fh:
        fh.write(b"partial")
    real_time = transfer.time.time
    monkeypatch.setattr(transfer.time, "time", lambda: real_time() + 3600)
    assert transfer.take(ticket["parts"][0].rsplit("/", 1)[1], "upload") is None
    assert not os.path.exists(staging)


# ------------------------------------------------------------------ context(), shorter


def _skills(folder, count):
    for i in range(count):
        here = folder / f"skill-{i:02d}"
        here.mkdir(parents=True)
        (here / "SKILL.md").write_text(
            f"---\nname: skill-{i:02d}\ndescription: {'does a long thing ' * 20}\n---\nbody\n",
            encoding="utf-8",
        )


async def test_context_brief_lists_skills_by_name_and_says_when_nothing_changed(tmp_path):
    folder = tmp_path / "skills"
    _skills(folder, 12)
    async with Client(build_server(Config(context={"skills": [str(folder)]}))) as client:
        full = (await client.call_tool("context", {})).structured_content
        brief = (await client.call_tool("context", {"brief": True})).structured_content
        same = (await client.call_tool("context", {"since": full["digest"]})).structured_content
        (folder / "skill-00" / "SKILL.md").write_text(
            "---\nname: skill-00\ndescription: changed\n---\n", encoding="utf-8"
        )
        changed = (
            await client.call_tool("context", {"since": full["digest"], "brief": True})
        ).structured_content
    assert isinstance(full["skills"][0], dict) and len(full["digest"]) == 16
    assert brief["digest"] == full["digest"] and "skill-07" in brief["skills"]
    assert all(isinstance(s, str) for s in brief["skills"])
    assert len(str(brief)) < len(str(full)) / 2
    assert same == {"unchanged": True, "digest": full["digest"], "note": same["note"]}
    assert "without since" in same["note"]
    assert "unchanged" not in changed and changed["digest"] != full["digest"]


async def test_the_scratch_folder_is_named_and_handed_to_programs(winhand_home):
    async with Client(build_server(Config())) as client:
        ctx = (await client.call_tool("context", {"brief": True})).structured_content
    folder = ctx["scratch"]["folder"]
    assert folder == str(winhand_home / "scratch") and os.path.isdir(folder)
    assert os.environ["WINHAND_SCRATCH"] == folder and "deleted automatically" in ctx["scratch"]["note"]


# ------------------------------------------------------------------ editing several files


async def test_edits_in_several_files_are_all_or_nothing(tmp_path):
    cards = [tmp_path / f"card{i}.md" for i in range(3)]
    for i, card in enumerate(cards):
        card.write_text(f"status: open\nowner: a{i}\n", encoding="utf-8")
    files = [{"path": str(c), "old": "status: open", "new": "status: done"} for c in cards]
    async with Client(build_server(Config())) as client:
        preview = (await client.call_tool("fs_edit", {"files": files, "dry_run": True})).structured_content
        assert all("-status: open" in f["diff"] and "+status: done" in f["diff"] for f in preview["files"])
        assert all("open" in c.read_text(encoding="utf-8") for c in cards)  # a dry run writes nothing
        bad = [*files[:2], {"path": str(cards[2]), "old": "status: missing", "new": "x"}]
        failed = (await client.call_tool("fs_edit", {"files": bad}, raise_on_error=False)).structured_content
        assert "nothing was written" in failed["error"] and "card2.md" in failed["error"]
        assert all("open" in c.read_text(encoding="utf-8") for c in cards)
        both = [*files, {"path": str(cards[0]), "old": "owner: a0", "new": "owner: b0"}]  # one file twice
        done = (await client.call_tool("fs_edit", {"files": both})).structured_content
    assert done["replacements"] == 4 and len(done["files"]) == 3
    assert cards[0].read_text(encoding="utf-8") == "status: done\nowner: b0\n"


def test_a_failed_write_puts_back_the_files_already_written(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("one\r\n", encoding="utf-8")
    b.write_text("two\n", encoding="utf-8")
    real = fs.write_text_like

    def flaky(path, text, encoding):
        if path.endswith("b.txt"):
            raise PermissionError("locked by another program")
        return real(path, text, encoding)

    monkeypatch.setattr(fs, "write_text_like", flaky)
    with pytest.raises(fs.FsError, match="put back"):
        fs.fs_edit_files(
            [{"path": str(a), "old": "one", "new": "1"}, {"path": str(b), "old": "two", "new": "2"}]
        )
    assert a.read_bytes() == b"one\r\n"


def test_owner_hooks_see_each_files_own_edits(tmp_path):
    from winhand import actions, hooks

    one, two = str(tmp_path / "one.py"), str(tmp_path / "two.py")
    args = {
        "files": [{"path": one, "old": "a", "new": "b"}, {"path": two, "edits": [{"old": "c", "new": "d"}]}]
    }
    edit, read = actions.describe("fs_edit", args)
    assert edit.kind == "edit" and len(edit.paths) == 2 and read.kind == "read"
    seen = {p: hooks._edits(args, p) for p in edit.paths}
    assert seen == {one: [("a", "b")], two: [("c", "d")]}
    (dry,) = actions.describe("fs_edit", {**args, "dry_run": True})
    assert dry.kind == "read"
