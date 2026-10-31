"""0.10.0: the desktop (docs/reviews/2026-11-01-usage-review.md, round 4).

ui: filters with alternatives, refs, several steps in one call, the windows an action opens, an
inspect that survives a window changing under it; ocr finds labels with a middle dot; input
holds modifiers, triple-clicks, holds keys, tells the cursor position and takes window
coordinates; screenshots go to a file and minimized windows are shown; the owner can take over.
The Windows parts are simulated here; test_windows_scenarios.py drives the real thing.
"""

from __future__ import annotations

import pytest
from fastmcp.client import Client

from winhand import desktop, ocr, uia
from winhand import tools_desktop as td
from winhand.config import Config
from winhand.server import build_server


def _fake_window(monkeypatch, **extra):
    window = {"hwnd": 11, "title": "app", "process": "app.exe", "pid": 9, **extra}
    monkeypatch.setattr(td.desktop, "find_window", lambda _: window)
    monkeypatch.setattr(td.desktop, "native_controls", lambda hwnd: [])
    monkeypatch.setattr(td.desktop, "refuse_own", lambda hwnd: {})
    monkeypatch.setattr(td.desktop, "require_operable", lambda hwnd, what: None)
    monkeypatch.setattr(td.desktop, "pid_at", lambda x, y: window["pid"])
    monkeypatch.setattr(td.desktop, "windows", lambda: [window])
    td._refs.clear()
    return window


def _control(name, kind="Button", **extra):
    return {
        "type": kind,
        "name": name,
        "automation_id": extra.pop("automation_id", ""),
        "class": extra.pop("cls", ""),
        "hwnd": 0,
        "enabled": True,
        "rect": [1, 2, 3, 4],
        "patterns": extra.pop("patterns", ["Invoke"]),
        "_runtime_id": extra.pop("runtime_id", [len(name)]),
        "_element": extra.pop("element", object()),
        **extra,
    }


def _snapshot(controls):
    """A fresh reading each time, as uia.snapshot gives (merge_controls takes `index` out of it)."""
    return lambda hwnd, timeout=uia.TIMEOUT_S: {
        "window": "app",
        "count": len(controls),
        "elements": [{**c, "index": i} for i, c in enumerate(controls)],
    }


# ------------------------------------------------------------------ inspect


async def test_inspect_filters_take_alternatives_class_and_type(monkeypatch):
    _fake_window(monkeypatch)
    controls = [
        _control("打开(O)"),
        _control("取消"),
        _control("", "Edit", automation_id="1148", cls="ComboBoxEx32", patterns=["Value"]),
        _control("文件名:", "Text", patterns=[]),
    ]
    monkeypatch.setattr(uia, "snapshot", _snapshot(controls))
    async with Client(build_server(Config())) as client:

        async def inspect(**args):
            return (await client.call_tool("ui", {"window": "app", **args})).structured_content

        either = await inspect(filter="文件名|打开|1148")  # the filter that found nothing before
        buttons = await inspect(control_type="Button", filter="取消|打开")
        by_class = await inspect(filter="comboboxex32")
        nothing = await inspect(filter="nowhere")
    assert {e["name"] for e in either["elements"]} == {"打开(O)", "", "文件名:"}
    assert [e["name"] for e in buttons["elements"]] == ["打开(O)", "取消"]
    assert by_class["elements"][0]["automation_id"] == "1148"
    assert [e["ref"] for e in either["elements"]] == ["e1", "e3", "e4"]  # refs count every control
    assert nothing["shown"] == 0 and "without filter" in nothing["note"]


async def test_inspect_reads_again_when_the_window_changes_under_it(monkeypatch):
    _fake_window(monkeypatch)
    calls = []

    def changing(hwnd, timeout=uia.TIMEOUT_S):
        calls.append(hwnd)
        if len(calls) == 1:
            raise uia.Gone("UI Automation: the control is gone (the window changed); inspect it again")
        return {
            "window": "app",
            "count": 1,
            "elements": [{**_control("调试口 · 串口未打开", "Text"), "index": 0}],
        }

    monkeypatch.setattr(uia, "snapshot", changing)
    monkeypatch.setattr(td.time, "sleep", lambda s: None)
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("ui", {"window": "app", "filter": "调试口"})).structured_content
    assert len(calls) == 2 and out["shown"] == 1


# ------------------------------------------------------------------ refs and steps


async def test_an_action_by_ref_uses_the_control_that_inspect_listed(monkeypatch):
    _fake_window(monkeypatch)
    first, second = object(), object()
    controls = [_control("OK", element=first), _control("OK", element=second, runtime_id=[99])]
    monkeypatch.setattr(uia, "snapshot", _snapshot(controls))
    monkeypatch.setattr(uia, "refresh", lambda e: {**controls[1], "_element": e} if e is second else None)
    acted = []
    monkeypatch.setattr(uia, "act", lambda e, verb, value=None, timeout=0: acted.append(e) or "invoked")
    async with Client(build_server(Config())) as client:
        call = client.call_tool
        listed = (await call("ui", {"window": "app"})).structured_content
        ambiguous = (await call("ui", {"window": "app", "action": "invoke", "name": "OK"})).structured_content
        done = (await call("ui", {"window": "app", "action": "invoke", "ref": "e2"})).structured_content
        gone = (await call("ui", {"window": "app", "action": "invoke", "ref": "e1"})).structured_content
        unknown = (await call("ui", {"window": "app", "action": "invoke", "ref": "e9"})).structured_content
    assert [e["ref"] for e in listed["elements"]] == ["e1", "e2"]
    assert "2 controls match" in ambiguous["error"]
    assert acted == [second] and done["ref"] == "e2"
    assert "inspect it again" in gone["error"] and "e1 to e2" in unknown["error"]


async def test_an_action_reports_the_dialog_it_opened(monkeypatch):
    window = _fake_window(monkeypatch)
    dialog = {"hwnd": 22, "title": "确认", "process": "app.exe", "pid": 9, "class": "#32770"}
    opened = []
    monkeypatch.setattr(td.desktop, "windows", lambda: [window, dialog] if opened else [window])
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("删除")]))
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    monkeypatch.setattr(uia, "act", lambda e, verb, value=None, timeout=0: opened.append(1) or "invoked")
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool("ui", {"window": "app", "action": "invoke", "name": "删除"})
        ).structured_content
    assert done["new_windows"] == [{"hwnd": 22, "title": "确认", "process": "app.exe", "dialog": True}]
    assert "before going on" in done["new_windows_note"]


async def test_steps_run_in_order_stop_at_a_failure_and_end_with_a_picture(monkeypatch):
    _fake_window(monkeypatch)
    user = _control("用户名", "Edit", patterns=["Value"], value="")
    login = _control("登录")
    controls = [user, login]
    monkeypatch.setattr(uia, "snapshot", _snapshot(controls))
    monkeypatch.setattr(
        uia, "refresh", lambda e: {**user, "value": "alice"} if e is user["_element"] else None
    )
    acted = []
    monkeypatch.setattr(
        uia, "act", lambda e, verb, value=None, timeout=0: acted.append((verb, value)) or f"{verb} done"
    )
    keys = []
    monkeypatch.setattr(td.desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(
        td.desktop, "input_action", lambda action, **k: keys.append(k) or {"foreground": "app"}
    )
    monkeypatch.setattr(
        td.desktop, "screenshot", lambda target=None, *a, **k: (b"\x89PNG", "png", {"width": 1})
    )
    steps = [
        {"action": "set_value", "name": "用户名", "value": "alice"},
        {"action": "keys", "value": "tab"},
        {"action": "invoke", "name": "注册"},  # not there: stops here
        {"action": "invoke", "name": "登录"},
    ]
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("ui", {"window": "app", "steps": steps})
    out = result.structured_content
    assert [s["step"] for s in out["steps"]] == [1, 2, 3] and out["done"] == 2
    assert out["steps"][0]["verified"] is True and "step 3 failed" in out["stopped"]
    assert acted == [("set_value", "alice")] and keys == [{"keys": "tab"}]
    assert any(block.type == "image" for block in result.content)


# ------------------------------------------------------------------ ocr


def _words(*texts):
    left = 0
    out = []
    for text in texts:
        out.append(ocr.Word(text, [left, 0, 10 * len(text), 12]))
        left += 10 * len(text) + 5
    return out


def test_ocr_finds_a_label_whatever_became_of_its_middle_dot():
    for read_as in (["COM17", "•", "115200"], ["COM17", "115200"], ["COM17-115200"]):
        lines = [_words("串口未打开"), _words(*read_as)]
        (hit,) = ocr.find_in(lines, "COM17 · 115200")
        assert hit["loose"] is True and hit["rect"][1] == 0
    exact = ocr.find_in([_words("COM17", "·", "115200")], "COM17 · 115200")
    assert exact and "loose" not in exact[0]
    assert ocr.find_in([_words("C", "D")], "C++") == []  # punctuation alone is not the text


# ------------------------------------------------------------------ input and screenshots


def test_modifiers_and_the_new_input_actions_are_checked_before_anything_is_sent():
    assert desktop.modifier_codes("ctrl+shift") == [0x11, 0x10]
    assert desktop.modifier_codes(["Ctrl", "alt", "control"]) == [0x11, 0x12]
    with pytest.raises(desktop.DesktopError, match="modifiers are"):
        desktop.modifier_codes(["hyper"])
    desktop._check_arguments("triple_click", (1, 1), None, "left", None, None)
    desktop._check_arguments("hold_key", None, None, "left", None, "shift")
    with pytest.raises(desktop.DesktopError, match="one key"):
        desktop._check_arguments("hold_key", None, None, "left", None, "shift tab")
    with pytest.raises(desktop.DesktopError, match="action is one of"):
        desktop._check_arguments("quadruple_click", (1, 1), None, "left", None, None)


def test_window_pixels_use_the_scale_of_that_windows_latest_screenshot(monkeypatch):
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 5, "left": 100, "top": 50})
    monkeypatch.setattr(desktop, "_window_scale", {5: 0.5})
    assert desktop.in_window(5, 40, 20) == (180, 90)
    monkeypatch.setattr(desktop, "_window_scale", {})
    assert desktop.in_window(5, 40, 20) == (140, 70)  # no picture of it yet: 1:1


def test_a_screenshot_can_be_saved_to_a_file(tmp_path):
    from PIL import Image

    picture = Image.new("RGB", (40, 30), "white")
    png = desktop._save_picture(picture, str(tmp_path / "shots" / "a.png"))
    jpg = desktop._save_picture(picture, str(tmp_path / "a.jpg"))
    assert Image.open(png).format == "PNG" and Image.open(jpg).format == "JPEG"
    with pytest.raises(desktop.DesktopError, match="absolute"):
        desktop._save_picture(picture, "relative.png")


async def test_input_and_screenshot_pass_the_new_arguments_on(monkeypatch):
    seen = {}
    monkeypatch.setattr(td.desktop, "input_action", lambda *a: seen.update(input=a) or {"action": a[0]})
    monkeypatch.setattr(
        td.desktop, "screenshot", lambda *a: seen.update(shot=a) or (b"\x89PNG", "png", {"width": 1})
    )
    async with Client(build_server(Config())) as client:
        await client.call_tool(
            "input", {"action": "click", "x": 3, "y": 4, "modifiers": ["ctrl"], "window": "app", "repeat": 2}
        )
        await client.call_tool("screenshot", {"window": "app", "path": "/tmp/x.png"})
    assert seen["input"][10:] == (["ctrl"], 1.0, 2, "app")
    assert seen["shot"][0] == "app" and seen["shot"][-1] == "/tmp/x.png"


def test_saving_a_screenshot_is_judged_as_a_write():
    import os

    from winhand import actions

    (act,) = actions.describe("screenshot", {"window": "app", "path": "/data/shot.png"})
    assert act.kind == "write" and act.paths == (os.path.abspath("/data/shot.png"),)  # D:\data\... on Windows
    assert actions.describe("screenshot", {"window": "app"}) == []


# ------------------------------------------------------------------ the owner comes first

import types  # noqa: E402

from winhand import presence  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    """A fake clock for presence: GetTickCount, monotonic and sleep move together."""
    now = [1000.0]
    fake = types.SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(presence, "time", fake)
    monkeypatch.setattr(presence, "_tick", lambda: int(now[0] * 1000) & 0xFFFFFFFF)
    monkeypatch.setattr(presence, "_ours", None)
    monkeypatch.setattr(presence, "_dialogs", [])
    monkeypatch.setattr(presence, "_stopped", None)
    return now


def _owner_used_it(monkeypatch, clock, seconds_ago):
    tick = int((clock[0] - seconds_ago) * 1000)
    monkeypatch.setattr(presence, "_last_input_tick", lambda: tick)


def test_only_the_persons_own_input_counts(monkeypatch, clock):
    _owner_used_it(monkeypatch, clock, 1.0)
    assert presence.owner_input_ago() == pytest.approx(1.0)
    _owner_used_it(monkeypatch, clock, 30.0)
    assert presence.owner_input_ago() is None  # long enough ago
    presence.note_sent()
    _owner_used_it(monkeypatch, clock, 0.0)  # the input just seen is what winhand sent
    assert presence.owner_input_ago() is None
    clock[0] += 3
    with presence.dialog():  # the person clicks in winhand's approval dialog
        clock[0] += 2
        _owner_used_it(monkeypatch, clock, 0.5)
    assert presence.owner_input_ago() is None


def test_an_action_waits_while_the_person_uses_the_machine(monkeypatch, clock):
    _owner_used_it(monkeypatch, clock, 1.0)
    said = presence.before("input click")
    assert said["waited_for_owner_s"] == pytest.approx(presence.IDLE_S - 1.0, abs=0.3)
    busy_tick = [0]

    def always_busy():
        busy_tick[0] = int(clock[0] * 1000)  # input all the time
        return busy_tick[0]

    monkeypatch.setattr(presence, "_last_input_tick", always_busy)
    with pytest.raises(desktop.OwnerBusy, match="using the mouse or keyboard"):
        presence.before("input click")


def test_yielding_can_be_turned_off(monkeypatch, clock, winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text("[desktop]\nyield_to_owner = false\n", encoding="utf-8")
    _owner_used_it(monkeypatch, clock, 0.5)
    assert presence.before("input click") == {}


def test_the_stop_keys_refuse_desktop_actions_for_a_while(monkeypatch, clock):
    monkeypatch.setattr(presence, "_last_input_tick", lambda: None)
    presence.stop_pressed()
    with pytest.raises(desktop.OwnerStopped, match="Ctrl\\+Alt\\+Shift\\+Backspace"):
        presence.before("ui invoke")
    clock[0] += presence.STOP_S + 1
    assert presence.before("ui invoke") == {}
    presence.stop_pressed()
    presence.resume()
    assert presence.before("ui invoke") == {}


async def test_steps_stop_when_the_person_presses_the_stop_keys(monkeypatch, clock):
    monkeypatch.setattr(presence, "_last_input_tick", lambda: None)
    _fake_window(monkeypatch)
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("下一步"), _control("完成")]))
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    acted = []

    def act(e, verb, value=None, timeout=0):
        acted.append(verb)
        presence.stop_pressed()  # the person hits the keys during the first step
        return "invoked"

    monkeypatch.setattr(uia, "act", act)
    monkeypatch.setattr(td.desktop, "screenshot", lambda *a, **k: (b"\x89PNG", "png", {"width": 1}))
    steps = [{"action": "invoke", "name": "下一步"}, {"action": "invoke", "name": "完成"}]
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("ui", {"window": "app", "steps": steps})).structured_content
    assert acted == ["invoke"] and "pressed Ctrl+Alt+Shift+Backspace" in out["steps"][1]["error"]
    assert "step 2 failed" in out["stopped"]


# ------------------------------------------------------------------ a command held up by a prompt

from winhand import jobwatch  # noqa: E402


@pytest.fixture
def windows_now(monkeypatch):
    """jobwatch on a simulated Windows desktop: the windows listed are what windows_now holds."""
    listed: list[dict] = [{"hwnd": 1, "title": "editor", "process": "code.exe", "pid": 10}]
    flashed = []
    monkeypatch.setattr(jobwatch, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(desktop, "top_level_handles", lambda: {w["hwnd"] for w in listed})
    monkeypatch.setattr(desktop, "windows", lambda: list(listed))
    monkeypatch.setattr(desktop, "flash", flashed.append)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(jobwatch, "_baselines", {})
    monkeypatch.setattr(jobwatch, "_flashed", {})
    return listed, flashed


def test_a_signing_prompt_that_appears_while_a_command_runs_is_named_and_flashed(windows_now):
    listed, flashed = windows_now
    jobwatch.remember_windows("run-7")
    listed += [
        {"hwnd": 2, "title": "1Password", "process": "1Password.exe", "pid": 50},
        {"hwnd": 3, "title": "Untitled - Notepad", "process": "notepad.exe", "pid": 51},  # not a prompt
        {"hwnd": 4, "title": "Confirm", "process": "git.exe", "pid": 60, "class": "#32770"},  # its own
    ]
    found = jobwatch.prompts_since("run-7", {60})
    assert [p["hwnd"] for p in found] == [2] and flashed == [2]
    jobwatch.prompts_since("run-7", {60})
    assert flashed == [2]  # flashed once
    assert jobwatch.prompts_since("run-never-started") == []


async def test_a_long_wait_ends_when_a_prompt_or_a_secure_screen_appears(monkeypatch):
    monkeypatch.setattr(jobwatch, "CHECK_EVERY_S", 0.01)
    monkeypatch.setattr(td, "JOB_POLL_S", 0.01)
    looks = []

    def watch():
        looks.append(1)
        return (
            {}
            if len(looks) == 1
            else {"prompts": [{"hwnd": 9, "title": "Windows 安全中心", "process": "CredentialUIBroker.exe"}]}
        )

    out = await td.wait_job(lambda: {"state": "running"}, 30, [], watch=watch)
    assert out["matched"] == "prompt" and "sign-in" in out["note"]
    looks.clear()

    def secure():
        looks.append(1)
        return {} if len(looks) == 1 else {"secure_screen": True}

    out = await td.wait_job(lambda: {"state": "running"}, 30, [], watch=secure)
    assert out["matched"] == "secure_screen" and "UAC" in out["note"]
