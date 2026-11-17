"""Scratch cleanup uses temporary folders, fake dialog answers and a fake Recycle Bin."""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import Mock

import pytest
from conftest import on_windows
from fastmcp import Client

from winhand import actions, config, fs, scratch_cleanup, secret_prompt
from winhand.config import Config
from winhand.server import build_server

DAY = 86400
NOW = 2_000_000_000.0


@pytest.fixture(autouse=True)
def no_real_cleanup(monkeypatch):
    monkeypatch.setattr(secret_prompt, "ask_confirm", Mock(side_effect=AssertionError("real dialog")))
    monkeypatch.setattr(secret_prompt, "_screen_state", lambda: "ok")
    monkeypatch.setattr(fs, "_recycle", Mock(side_effect=AssertionError("real Recycle Bin")))


@pytest.fixture
def scratch(tmp_path, winhand_home, monkeypatch):
    folder = tmp_path / "scratch"
    folder.mkdir()
    winhand_home.mkdir()
    config.config_path().write_text(f'[scratch]\nfolder = "{folder.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setattr(scratch_cleanup.time, "time", lambda: NOW)
    return folder


def old_folder(scratch, name, when=NOW - 15 * DAY):
    folder = scratch / name
    folder.mkdir()
    os.utime(folder, (when, when))
    return folder


def test_candidates_keep_boundary_first_level_activity_and_sorting(scratch):
    old_folder(scratch, "older", NOW - 30 * DAY)
    old_folder(scratch, "old", NOW - 15 * DAY)
    old_folder(scratch, "boundary", NOW - 14 * DAY)
    old_folder(scratch, "recent", NOW - 13 * DAY)
    (scratch / "just-a-file").write_text("not a folder", encoding="utf-8")

    kept = old_folder(scratch, "holdout-sealed")
    (kept / ".keep").touch()
    os.utime(kept, (NOW - 30 * DAY, NOW - 30 * DAY))
    os.utime(kept / ".keep", (NOW - 30 * DAY, NOW - 30 * DAY))

    touched = old_folder(scratch, "touched-child")
    child = touched / "file.txt"
    child.write_bytes(b"recent")
    os.utime(child, (NOW - DAY, NOW - DAY))
    os.utime(touched, (NOW - 30 * DAY, NOW - 30 * DAY))

    nested = old_folder(scratch, "deep-change", NOW - 20 * DAY)
    sub = nested / "sub"
    sub.mkdir()
    (sub / "file.txt").write_bytes(b"deep")
    os.utime(sub / "file.txt", (NOW - DAY, NOW - DAY))
    os.utime(sub, (NOW - 20 * DAY, NOW - 20 * DAY))
    os.utime(nested, (NOW - 30 * DAY, NOW - 30 * DAY))

    found = scratch_cleanup.candidates(NOW)
    assert [item["name"] for item in found] == ["older", "deep-change", "old"]
    assert found[1] == {
        "name": "deep-change",
        "path": str(nested),
        "last_modified": NOW - 20 * DAY,
        "size_mb": 4 / (1024 * 1024),
    }


@pytest.mark.skipif(not on_windows(), reason="Windows junctions")
def test_candidates_skip_junctions_and_never_count_their_targets(scratch, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * (1024 * 1024))
    _winapi.CreateJunction(str(outside), str(scratch / "linked-folder"))
    owned = old_folder(scratch, "owned")
    (owned / "small.bin").write_bytes(b"own")
    _winapi.CreateJunction(str(outside), str(owned / "linked-child"))
    os.utime(owned, (NOW - 15 * DAY, NOW - 15 * DAY))
    found = scratch_cleanup.candidates(NOW)
    assert [item["name"] for item in found] == ["owned"]
    assert found[0]["size_mb"] == 3 / (1024 * 1024)
    assert (outside / "large.bin").stat().st_size == 1024 * 1024


def test_size_count_stops_after_three_seconds_and_returns_the_partial_sum(scratch, monkeypatch):
    folder = old_folder(scratch, "large")
    (folder / "a").write_bytes(b"123456")
    (folder / "b").write_bytes(b"123456")
    os.utime(folder, (NOW - 15 * DAY, NOW - 15 * DAY))
    later = old_folder(scratch, "later", NOW - 16 * DAY)
    (later / "c").write_bytes(b"123456")
    os.utime(later, (NOW - 16 * DAY, NOW - 16 * DAY))
    # the deadline, then "large": its first file counted, its second at 3 s; "later" gets no time left
    monkeypatch.setattr(scratch_cleanup.time, "monotonic", Mock(side_effect=[0.0, 0.0, 3.0, 3.0]))
    items = {item["name"]: item for item in scratch_cleanup.candidates(NOW)}
    assert items["large"]["size_capped"] is True and items["large"]["size_mb"] == 6 / (1024 * 1024)
    assert items["later"]["size_capped"] is True and items["later"]["size_mb"] == 0


@pytest.mark.parametrize(
    "age,force,skipped", [(7 * DAY - 1, False, True), (7 * DAY, False, False), (0, True, False)]
)
def test_weekly_interval_and_force(scratch, monkeypatch, age, force, skipped):
    folder = old_folder(scratch, "old")
    state = config.home() / "scratch-cleanup.json"
    original = json.dumps({"last_asked": NOW - age})
    state.write_text(original, encoding="utf-8")
    ask = Mock(return_value=False)
    monkeypatch.setattr(secret_prompt, "ask_confirm", ask)
    result = scratch_cleanup.ask_and_clean(force=force)
    assert result == ({"skipped": "asked recently"} if skipped else {"approved": False})
    assert ask.call_count == (0 if skipped else 1)
    assert folder.is_dir()
    assert json.loads(state.read_text(encoding="utf-8"))["last_asked"] == (NOW - age if skipped else NOW)
    fs._recycle.assert_not_called()


def test_no_candidates_does_not_ask_or_record_an_asking_time(scratch):
    assert scratch_cleanup.ask_and_clean() == {"candidates": 0}
    assert not (config.home() / "scratch-cleanup.json").exists()
    secret_prompt.ask_confirm.assert_not_called()
    fs._recycle.assert_not_called()


@pytest.mark.parametrize("timeout", [False, True])
def test_decline_or_timeout_keeps_files_and_records_last_asked(scratch, monkeypatch, timeout):
    folder = old_folder(scratch, "old")
    (folder / "file.txt").write_bytes(b"keep me")
    os.utime(folder, (NOW - 15 * DAY, NOW - 15 * DAY))
    ask = Mock(
        side_effect=secret_prompt.PromptUnavailable("nobody answered the dialog within 120s")
        if timeout
        else None,
        return_value=False,
    )
    monkeypatch.setattr(secret_prompt, "ask_confirm", ask)
    assert scratch_cleanup.ask_and_clean() == {"approved": False}
    assert (folder / "file.txt").read_bytes() == b"keep me"
    assert json.loads((config.home() / "scratch-cleanup.json").read_text(encoding="utf-8")) == {
        "last_asked": NOW
    }
    fs._recycle.assert_not_called()


def test_dialog_errors_are_exposed_without_recycling(scratch, monkeypatch):
    old_folder(scratch, "old")
    monkeypatch.setattr(
        secret_prompt,
        "ask_confirm",
        Mock(side_effect=secret_prompt.PromptUnavailable("cannot open a dialog")),
    )
    with pytest.raises(secret_prompt.PromptUnavailable, match="^cannot open a dialog$"):
        scratch_cleanup.ask_and_clean()
    fs._recycle.assert_not_called()


def test_dialog_lists_twenty_names_dates_sizes_and_the_total(scratch, monkeypatch):
    for i in range(21):
        old_folder(scratch, f"stale-{i:02d}", NOW - (40 - i) * DAY)
    ask = Mock(return_value=False)
    monkeypatch.setattr(secret_prompt, "ask_confirm", ask)
    assert scratch_cleanup.ask_and_clean() == {"approved": False}
    title, message = ask.call_args.args
    assert title == "清理 WinHand 暂存目录"
    assert ask.call_args.kwargs == {"timeout_s": 120}
    date = datetime.fromtimestamp(NOW - 40 * DAY).strftime("%Y-%m-%d")
    assert f"stale-00 | {date} | 0.0 MB" in message
    assert "stale-19" in message and "stale-20" not in message
    assert "合计 21 个文件夹，0.0 MB" in message
    assert "会移到回收站，可以还原；带 .keep 的和联接点不动。" in message


def test_approval_recycles_each_candidate(scratch, monkeypatch):
    older = old_folder(scratch, "older", NOW - 30 * DAY)
    old = old_folder(scratch, "old", NOW - 15 * DAY)
    ask = Mock(return_value=True)
    monkeypatch.setattr(secret_prompt, "ask_confirm", ask)
    recycle = Mock()
    monkeypatch.setattr(fs, "_recycle", recycle)
    assert scratch_cleanup.ask_and_clean() == {
        "approved": True,
        "moved": [str(older), str(old)],
        "failed": [],
    }
    assert [call.args for call in recycle.call_args_list] == [(str(older),), (str(old),)]
    assert older.is_dir() and old.is_dir()  # the Recycle Bin call was replaced
    assert json.loads((config.home() / "scratch-cleanup.json").read_text(encoding="utf-8")) == {
        "last_asked": NOW
    }


def test_recycle_failure_is_returned_with_its_original_error(scratch, monkeypatch):
    folder = old_folder(scratch, "old")
    monkeypatch.setattr(secret_prompt, "ask_confirm", Mock(return_value=True))
    monkeypatch.setattr(fs, "_recycle", Mock(side_effect=fs.FsError("Recycle Bin error 0x71")))
    assert scratch_cleanup.ask_and_clean() == {
        "approved": True,
        "moved": [],
        "failed": [{"path": str(folder), "error": "Recycle Bin error 0x71"}],
    }
    assert folder.is_dir()


def test_keep_added_while_dialog_is_open_is_not_recycled(scratch, monkeypatch):
    folder = old_folder(scratch, "old")

    def approve(*args, **kwargs):
        (folder / ".keep").touch()
        return True

    monkeypatch.setattr(secret_prompt, "ask_confirm", approve)
    result = scratch_cleanup.ask_and_clean()
    assert result["moved"] == []
    assert result["failed"] == [{"path": str(folder), "error": f"folder has .keep: {folder}"}]
    fs._recycle.assert_not_called()


async def test_fs_op_clean_scratch_calls_forced_cleanup_and_bypasses_fs_op(scratch, monkeypatch):
    expected = {"approved": True, "moved": [str(scratch / "old")], "failed": []}
    clean = Mock(return_value=expected)
    ordinary_op = Mock(side_effect=AssertionError("ordinary fs_op was called"))
    monkeypatch.setattr(scratch_cleanup, "ask_and_clean", clean)
    monkeypatch.setattr(fs, "fs_op", ordinary_op)
    args = {"action": "clean_scratch", "path": "ignored"}
    assert actions.describe("fs_op", args) == []
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("fs_op", args)
    assert result.structured_content == expected
    clean.assert_called_once_with(force=True)
    ordinary_op.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_a_locked_screen_skips_without_scanning_or_counting_as_asked(scratch, monkeypatch, force):
    old_folder(scratch, "old")
    monkeypatch.setattr(secret_prompt, "_screen_state", lambda: "locked")
    monkeypatch.setattr(scratch_cleanup, "candidates", Mock(side_effect=AssertionError("scanned")))
    result = scratch_cleanup.ask_and_clean(force=force)
    assert result == {"skipped": "nobody can see a dialog now: the screen at the machine is locked"}
    assert not (config.home() / "scratch-cleanup.json").exists()
