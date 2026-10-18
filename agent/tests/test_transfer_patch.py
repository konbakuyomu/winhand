"""winhand 0.4.1: one-time file links and applying unified diffs."""

from __future__ import annotations

import base64
import difflib
import hashlib
import time
from datetime import datetime

import httpx
import pytest
from fastmcp.client import Client
from starlette.applications import Starlette
from starlette.routing import Route

from winhand import patching, transfer
from winhand.config import Config
from winhand.server import build_server


def udiff(old: str, new: str, name: str, a: str = "a/", b: str = "b/") -> str:
    return "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"{a}{name}", f"{b}{name}")
    )


# ------------------------------------------------------------------ patches


def test_patch_keeps_gbk_crlf_and_finds_moved_hunks(tmp_path):
    original = "".join(f"line {i}\n" for i in range(1, 21)) + "// 中文注释\n"
    changed = original.replace("line 10\n", "line ten\n").replace("// 中文注释", "// 改过的注释")
    patch = udiff(original, changed, "src/main.c")
    target = tmp_path / "src" / "main.c"
    target.parent.mkdir()
    # the copy here has two more lines at the top and CRLF line endings, in GBK
    target.write_bytes(("extra 1\nextra 2\n" + original).replace("\n", "\r\n").encode("gbk"))
    out = patching.apply(patch, str(tmp_path))
    assert out["ok"] and out["written"] and out["strip"] == 1, out
    hunks = out["files"][0]["hunks"]
    assert all(h["offset"] == 2 for h in hunks)
    expected = ("extra 1\nextra 2\n" + changed).replace("\n", "\r\n").encode("gbk")
    assert target.read_bytes() == expected
    assert out["files"][0]["encoding"] == "gb18030" and out["files"][0]["newline"] == "crlf"


def test_patch_creates_deletes_and_renames(tmp_path):
    (tmp_path / "old.txt").write_text("bye\n")
    (tmp_path / "keep.txt").write_text("same\n")
    patch = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
        "diff --git a/old.txt b/old.txt\n"
        "deleted file mode 100644\n"
        "--- a/old.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-bye\n"
        "diff --git a/keep.txt b/moved/keep.txt\n"
        "similarity index 100%\n"
        "rename from keep.txt\n"
        "rename to moved/keep.txt\n"
    )
    out = patching.apply(patch, str(tmp_path))
    assert out["ok"], out
    assert [f["action"] for f in out["files"]] == ["create", "delete", "rename"]
    assert (tmp_path / "new.txt").read_text() == "hello\nworld\n"
    assert not (tmp_path / "old.txt").exists() and not (tmp_path / "keep.txt").exists()
    assert (tmp_path / "moved" / "keep.txt").read_text() == "same\n"


def test_patch_is_all_or_nothing_unless_partial(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    (tmp_path / "b.txt").write_text("three\nfour\n")
    patch = udiff("one\ntwo\n", "one\n2\n", "a.txt") + udiff("THREE\nfour\n", "THREE\n4\n", "b.txt")
    out = patching.apply(patch, str(tmp_path))
    assert not out["ok"] and not out["written"] and "nothing written" in out["summary"]
    failed = out["files"][1]["hunks"][0]
    assert failed["error"] == "context not found" and failed["file_has"][0] == "three"
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\n"
    dry = patching.apply(udiff("one\ntwo\n", "one\n2\n", "a.txt"), str(tmp_path), dry_run=True)
    assert dry["ok"] and not dry["written"] and (tmp_path / "a.txt").read_text() == "one\ntwo\n"
    part = patching.apply(patch, str(tmp_path), partial=True)
    assert part["written"] and (tmp_path / "a.txt").read_text() == "one\n2\n"
    assert (tmp_path / "b.txt").read_text() == "three\nfour\n"


def test_patch_handles_missing_final_newline_and_trailing_spaces(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("a\nb  \nc")  # no newline at the end, trailing spaces on b
    patch = (
        "--- x.txt\n+++ x.txt\n@@ -1,3 +1,3 @@\n a\n b\n-c\n\\ No newline at end of file\n+C\n"
        "\\ No newline at end of file\n"
    )
    out = patching.apply(patch, str(tmp_path))
    assert out["ok"] and out["strip"] == 0, out
    assert p.read_text() == "a\nb  \nC"
    assert out["files"][0]["hunks"][0]["matched"] == "ignoring trailing whitespace"


def test_patch_strip_and_paths_are_checked(tmp_path):
    (tmp_path / "f.txt").write_text("x\n")
    with pytest.raises(patching.PatchError, match="none of -p0"):
        patching.apply(udiff("x\n", "y\n", "nope/zzz.txt", "", ""), str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    out = patching.apply(udiff("x\n", "y\n", "../f.txt", "", ""), str(sub), strip=0)
    assert not out["ok"] and "leaves the root" in out["files"][0]["error"]
    with pytest.raises(patching.PatchError, match="no file changes"):
        patching.apply("just some text\n", str(tmp_path))
    with pytest.raises(patching.PatchError, match="cut short"):
        patching.apply("--- f.txt\n+++ f.txt\n@@ -1,2 +1,2 @@\n-x\n", str(tmp_path))
    with pytest.raises(patching.PatchError, match="give patch"):
        patching.apply(None, str(tmp_path))


def test_patch_from_a_file_and_quoted_git_paths(tmp_path):
    (tmp_path / "文档.txt").write_text("旧\n", encoding="utf-8")
    patch = (
        'diff --git "a/\\346\\226\\207\\346\\241\\243.txt" "b/\\346\\226\\207\\346\\241\\243.txt"\n'
        '--- "a/\\346\\226\\207\\346\\241\\243.txt"\n'
        '+++ "b/\\346\\226\\207\\346\\241\\243.txt"\n'
        "@@ -1 +1 @@\n-旧\n+新\n"
    )
    pf = tmp_path / "change.patch"
    pf.write_text(patch, encoding="utf-8")
    out = patching.apply(patch_file=str(pf), root=str(tmp_path))
    assert out["ok"], out
    assert (tmp_path / "文档.txt").read_text(encoding="utf-8") == "新\n"


async def test_fs_apply_patch_tool(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    async with Client(build_server(Config())) as client:
        res = await client.call_tool(
            "fs_apply_patch", {"root": str(tmp_path), "patch": udiff("x = 1\n", "x = 2\n", "a.py")}
        )
        assert res.structured_content["ok"] and (tmp_path / "a.py").read_text() == "x = 2\n"
        bad = await client.call_tool("fs_apply_patch", {"root": str(tmp_path), "patch": "nothing"})
        assert "no file changes" in bad.structured_content["error"]


# ------------------------------------------------------------------ one-time links


@pytest.fixture
def relay():
    transfer.set_relay("wss://relay.example/agent")
    app = Starlette(routes=[Route("/transfer/{token}", transfer.endpoint, methods=["GET", "PUT", "POST"])])
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")
    yield http
    transfer._tickets.clear()


def _local(url: str) -> str:
    assert url.startswith("https://relay.example/t/")
    return "/transfer/" + url.rsplit("/", 1)[1]


async def test_upload_link_writes_once_and_checks_sha(relay, tmp_path):
    data = "补丁内容\n".encode() * 1000
    target = tmp_path / "in" / "change.patch"
    ticket = transfer.issue_upload(str(target), sha256=hashlib.sha256(data).hexdigest())
    assert "curl -fsS -T" in ticket["curl"]
    res = await relay.put(_local(ticket["url"]), content=data)
    assert res.status_code == 200 and res.json()["bytes"] == len(data), res.text
    assert target.read_bytes() == data
    again = await relay.put(_local(ticket["url"]), content=b"x")
    assert again.status_code == 404
    assert [e["kind"] for e in transfer.recent][-1] == "upload"
    assert sorted(p.name for p in target.parent.iterdir()) == ["change.patch"]


async def test_upload_link_refuses_bad_content(relay, tmp_path):
    t1 = transfer.issue_upload(str(tmp_path / "a.bin"), sha256="0" * 64)
    assert (await relay.put(_local(t1["url"]), content=b"abc")).status_code == 422
    t2 = transfer.issue_upload(str(tmp_path / "b.bin"), max_mb=0.001)
    assert (await relay.put(_local(t2["url"]), content=b"x" * 5000)).status_code == 413
    assert not list(tmp_path.iterdir())
    (tmp_path / "c.bin").write_bytes(b"old")
    with pytest.raises(transfer.TransferError, match="already exists"):
        transfer.issue_upload(str(tmp_path / "c.bin"))
    with pytest.raises(transfer.TransferError, match="absolute path"):
        transfer.issue_upload("relative/name.txt")
    t3 = transfer.issue_upload(str(tmp_path / "c.bin"), overwrite=True)
    assert (await relay.put(_local(t3["url"]), content=b"new")).status_code == 200
    assert (tmp_path / "c.bin").read_bytes() == b"new"


async def test_links_expire_and_downloads_work_once(relay, tmp_path, monkeypatch):
    source = tmp_path / "build.zip"
    source.write_bytes(b"PK" + bytes(range(256)))
    ticket = transfer.issue_download(str(source))
    assert ticket["size"] == 258 and ticket["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    got = await relay.get(_local(ticket["url"]))
    assert got.status_code == 200 and got.content == source.read_bytes()
    assert (await relay.get(_local(ticket["url"]))).status_code == 404
    late = transfer.issue_upload(str(tmp_path / "late.txt"), ttl_s=60)
    # an absolute time a sandbox in another time zone reads right, and the plain countdown
    when = datetime.fromisoformat(late["expires_at"])
    assert when.tzinfo is not None and abs(when.timestamp() - time.time() - 60) < 5
    assert 55 <= late["expires_in_s"] <= 60
    real_time = time.time
    monkeypatch.setattr(transfer.time, "time", lambda: real_time() + 120)
    assert (await relay.put(_local(late["url"]), content=b"x")).status_code == 404
    assert (await relay.get("/transfer/short")).status_code == 404


def test_links_need_a_relay(monkeypatch):
    monkeypatch.setattr(transfer, "_public_base", None)
    with pytest.raises(transfer.TransferError, match="not connected through a relay"):
        transfer.public_base()


async def test_fs_write_bytes_resumes_and_verifies(tmp_path):
    path = tmp_path / "blob.bin"
    data = bytes(range(200)) * 10
    async with Client(build_server(Config())) as client:

        async def put(chunk: bytes, **kw):
            args = {"path": str(path), "data_base64": base64.b64encode(chunk).decode(), **kw}
            return (await client.call_tool("fs_write_bytes", args)).structured_content

        await put(data[:1000])
        await put(b"garbage", append=True)
        out = await put(data[1000:], offset=1000, sha256=hashlib.sha256(data).hexdigest())
        assert out["size"] == len(data) and "error" not in out and path.read_bytes() == data
        bad = await put(b"x", offset=1, sha256="0" * 64)
        assert "sha256 mismatch" in bad["error"]
        past = await put(b"x", offset=10_000)
        assert "past the end" in past["error"]
