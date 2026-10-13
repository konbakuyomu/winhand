"""desktop-redundancy: native UI Automation, OCR, waiting for the picture, and clear reasons for a
locked desktop or an administrator window. These run everywhere with the Windows parts simulated;
test_windows_scenarios.py drives the real thing."""

from __future__ import annotations

import time

import pytest
from fastmcp.client import Client
from PIL import Image, ImageDraw

from winhand import desktop, ocr, server, uia
from winhand import tools_desktop as td
from winhand.config import Config
from winhand.server import build_server

# ------------------------------------------------------------ native UI Automation

TYPES = {50000: "Button", 50011: "MenuItem", 50024: "TreeItem", 50033: "Pane", 50004: "Edit", 50020: "Text"}


def raw(**kw):
    base = {"type": 50000, "name": "", "automation_id": "", "class": "", "hwnd": 0, "enabled": True,
            "focused": False, "rect": (10.0, 20.0, 30.0, 40.0), "offscreen": False, "password": False,
            "runtime_id": (42, 7)}  # fmt: skip
    base.update(kw)
    return base


def test_native_elements_are_listed_like_the_powershell_script_did():
    item = uia.describe(raw(name="文件(F)", type=50011, ExpandCollapse=True, expanded=0), TYPES)
    assert item == {
        "type": "MenuItem",
        "name": "文件(F)",
        "automation_id": "",
        "class": "",
        "hwnd": 0,
        "enabled": True,
        "focused": False,
        "_runtime_id": [42, 7],
        "rect": [10, 20, 30, 40],
        "patterns": ["ExpandCollapse"],
        "expanded": "Collapsed",
    }
    # pattern state only where the pattern is there (UI Automation reports defaults otherwise)
    plain = uia.describe(raw(name="OK", toggle=2, expanded=3, selected=False, value="x"), TYPES)
    assert not {"toggle", "expanded", "selected", "value"} & plain.keys()
    # nameless containers are dropped, nameless actionable controls kept
    assert uia.describe(raw(type=50033), TYPES) is None
    assert uia.describe(raw(type=50024, SelectionItem=True, selected=True), TYPES)["selected"] is True
    # offscreen without a name: dropped; with a name: kept and marked
    assert uia.describe(raw(type=50024, offscreen=True), TYPES) is None
    assert uia.describe(raw(name="row", type=50024, offscreen=True), TYPES)["offscreen"] is True
    # passwords never show; empty rectangles are null; odd values do not break the listing
    secret = uia.describe(raw(type=50004, Value=True, value="hunter2", password=True), TYPES)
    assert secret["value"] == "(hidden)"
    odd = uia.describe(raw(name=object(), type="?", rect=(0, 0, 0, 0), runtime_id=None, hwnd=5), TYPES)
    assert odd["name"] == "" and odd["type"] == "" and odd["rect"] is None and odd["_runtime_id"] == []


def test_a_program_that_does_not_answer_costs_a_thread_not_winhand(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(uia, "_enter_apartment", lambda: None)
    monkeypatch.setattr(uia, "_worker", None)
    began = time.monotonic()
    with pytest.raises(uia.NoAnswer, match="did not answer"):
        uia.call(time.sleep, 2, timeout=0.2)
    assert time.monotonic() - began < 1.5
    assert uia.call(lambda: 42) == 42  # a fresh thread takes the next call


def test_com_that_cannot_start_falls_back(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)

    def broken():
        raise OSError("no COM here")

    monkeypatch.setattr(uia, "_enter_apartment", broken)
    monkeypatch.setattr(uia, "_worker", None)
    with pytest.raises(uia.Unavailable, match="no COM here"):
        uia.call(lambda: 1)
    assert uia.status().startswith("powershell: ")
    monkeypatch.setattr(uia, "_worker", None)


def _fake_window(monkeypatch, **extra):
    window = {"hwnd": 11, "title": "app", "process": "app.exe", "pid": 9, **extra}
    monkeypatch.setattr(td.desktop, "find_window", lambda _: window)
    monkeypatch.setattr(td.desktop, "native_controls", lambda hwnd: [])
    return window


async def test_ui_acts_on_the_element_it_just_read(monkeypatch):
    _fake_window(monkeypatch)
    element = object()
    acted = []
    reads = []

    def snapshot(hwnd, timeout=uia.TIMEOUT_S):
        reads.append(hwnd)
        expanded = "Expanded" if acted else "Collapsed"
        return {"window": "app", "count": 1, "elements": [
            {"index": 0, "type": "MenuItem", "name": "文件(F)", "automation_id": "", "hwnd": 0, "enabled": True,
             "rect": [1, 2, 3, 4], "patterns": ["ExpandCollapse"], "expanded": expanded,
             "_runtime_id": [1, 2], "_element": element},
        ]}  # fmt: skip

    monkeypatch.setattr(uia, "snapshot", snapshot)
    monkeypatch.setattr(uia, "act", lambda e, verb, value=None: acted.append((e, verb)) or "expanded")
    monkeypatch.setattr(td, "_run_uia", lambda *a, **k: pytest.fail("the PowerShell script must not run"))
    async with Client(build_server(Config())) as client:
        listed = (await client.call_tool("ui", {"window": "app"})).structured_content
        done = (
            await client.call_tool("ui", {"window": "app", "action": "expand", "name": "文件(F)"})
        ).structured_content
    assert listed["backend"] == "native" and listed["elements"][0]["name"] == "文件(F)"
    assert "_element" not in listed["elements"][0]
    assert acted == [(element, "expand")]
    assert done["verified"] is True and done["element"]["expanded"] == "Expanded"


async def test_ui_falls_back_to_powershell(monkeypatch):
    _fake_window(monkeypatch)

    def unavailable(hwnd, timeout=uia.TIMEOUT_S):
        raise uia.Unavailable("ModuleNotFoundError: No module named 'comtypes'")

    calls = []

    def script(hwnd, **params):
        calls.append(params)
        if params["Mode"] == "inspect":
            return {"window": "app", "count": 1, "elements": [
                {"index": 0, "type": "Button", "name": "OK", "automation_id": "", "hwnd": 0, "enabled": True,
                 "rect": [1, 2, 3, 4], "patterns": ["Invoke"], "_runtime_id": [3]},
            ]}  # fmt: skip
        return {"done": "invoked"}

    monkeypatch.setattr(uia, "snapshot", unavailable)
    monkeypatch.setattr(td, "_run_uia", script)
    async with Client(build_server(Config())) as client:
        listed = (await client.call_tool("ui", {"window": "app"})).structured_content
        done = (
            await client.call_tool(
                "ui", {"window": "app", "action": "invoke", "name": "OK", "verify_wait_s": 0}
            )
        ).structured_content
    assert listed["backend"] == "powershell" and "comtypes" in listed["backend_note"]
    assert done["done"] == "invoked"
    assert [c["Mode"] for c in calls] == ["inspect", "inspect", "act", "inspect"]


async def test_ui_reports_a_hung_program(monkeypatch):
    _fake_window(monkeypatch)

    def hung(hwnd, timeout=uia.TIMEOUT_S):
        raise uia.NoAnswer("the program did not answer UI Automation within 20 s")

    monkeypatch.setattr(uia, "snapshot", hung)
    async with Client(build_server(Config())) as client:
        result = (await client.call_tool("ui", {"window": "app"})).structured_content
    assert "did not answer" in result["error"]


async def test_scrolls_a_row_into_view_before_clicking_it(monkeypatch):
    _fake_window(monkeypatch)
    element = object()
    clicks = []
    monkeypatch.setattr(uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 1, "elements": [
        {"index": 0, "type": "ListItem", "name": "row 90", "automation_id": "", "hwnd": 0, "enabled": True,
         "rect": None, "offscreen": True, "patterns": ["ScrollItem"], "_runtime_id": [5], "_element": element},
    ]})  # fmt: skip
    monkeypatch.setattr(uia, "scroll_into_view", lambda e: [100, 200, 50, 20] if e is element else None)
    monkeypatch.setattr(td.desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(td.desktop, "input_action", lambda *a, **k: clicks.append(a) or {})
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool(
                "ui", {"window": "app", "action": "click", "name": "row 90", "verify_wait_s": 0}
            )
        ).structured_content
    assert clicks == [("click", 125, 210)] and done["done"] == "clicked at (125, 210)"


# --------------------------------------------------------------------- OCR


def words(*items):
    return [ocr.Word(text, rect) for text, rect in items]


CHINESE = words(
    ("状", (0, 0, 20, 20)), ("态", (22, 0, 20, 20)), ("：", (44, 0, 20, 20)), ("已", (66, 0, 20, 20)),
    ("完", (88, 0, 20, 20)), ("成", (110, 0, 20, 20)), ("Done", (138, 0, 40, 20)),
)  # fmt: skip


def test_lines_read_as_a_person_reads_them():
    assert ocr.line_text(CHINESE) == "状态：已完成 Done"
    tight = words(("成", (110, 0, 20, 20)), ("Done", (131, 0, 40, 20)))
    assert ocr.line_text(tight) == "成Done"
    assert ocr.line_text(words(("Press", (0, 0, 40, 20)), ("me", (41, 0, 20, 20)))) == "Press me"
    assert ocr.normalize("Ｐｒｅｓｓ  ME（F）") == "pressme(f)"


def test_text_is_found_regardless_of_case_width_and_spaces():
    lines = [
        words(("Press", (10, 100, 40, 20)), ("me", (55, 100, 20, 20)), ("later", (80, 100, 40, 20))),
        CHINESE,
        words(("press", (300, 2, 40, 20)), ("ME", (345, 2, 20, 20))),
    ]
    found = ocr.find_in(lines, "press  ME")
    # reading order: the top row first, left to right
    assert [m["rect"] for m in found] == [[300, 2, 65, 20], [10, 100, 65, 20]]
    assert found[1]["text"] == "Press me later" and found[1]["match"] == "Press me"
    assert found[1]["center"] == [42, 110]
    assert ocr.find_in([CHINESE], "已完成")[0]["rect"] == [66, 0, 64, 20]
    assert ocr.find_in([CHINESE], "ｄｏｎｅ")[0]["match"] == "Done"
    with pytest.raises(ocr.OcrError):
        ocr.find_in(lines, "  ")


def test_closest_lines_show_how_ocr_misread_a_text():
    misread = [words(("状", (0, 0, 20, 20)), ("态", (22, 0, 20, 20)), ("已", (44, 0, 20, 20)),
                     ("宛", (66, 0, 20, 20)), ("成", (88, 0, 20, 20)))]  # fmt: skip
    assert ocr.find_in(misread, "已完成") == []
    near = ocr.similar(misread, "已完成")
    assert near and near[0]["text"] == "状态已宛成" and near[0]["score"] >= 0.6


def test_script_answers_become_screen_coordinates():
    data = {
        "available": ["en-US", "zh-Hans-CN"],
        "user": "zh-Hans-CN",
        "passes": [
            {"image": 0, "language": "zh-Hans-CN", "lines": [[{"t": "保存", "r": [20, 40, 60, 20]}]]},
            # PowerShell 5.1 turns a one-item array into the item itself
            {"image": 1, "language": "en-US", "lines": {"t": "Save", "r": [10, 20, 30, 10]}},
        ],
    }
    passes, languages = ocr.parse(data, [2.0, 1.0], 100, 200)
    assert languages == {"available": ["en-US", "zh-Hans-CN"], "user": "zh-Hans-CN"}
    assert passes[0].lines[0][0].rect == (110.0, 220.0, 30.0, 10.0)
    assert passes[1].lines[0][0].text == "Save" and passes[1].lines[0][0].rect == (110.0, 220.0, 30.0, 10.0)
    with pytest.raises(ocr.OcrError, match="Add-WindowsCapability"):
        ocr.parse({"error": "no_language", "available": []}, [1.0], 0, 0)
    with pytest.raises(ocr.OcrError, match="available: en-US, ja"):
        ocr.parse({"error": "language", "language": "fr-FR", "available": ["en-US", "ja"]}, [1.0], 0, 0)


def test_english_queries_try_the_english_reading_first():
    passes = [ocr.Pass("zh-Hans-CN", 2.0, []), ocr.Pass("en-US", 2.0, []), ocr.Pass("zh-Hans-CN", 1.0, []),
              ocr.Pass("en-US", 1.0, [])]  # fmt: skip
    order = ocr._search_order(passes, "Press me")
    assert [(p.language, p.scale) for p in order][:2] == [("en-US", 2.0), ("en-US", 1.0)]
    assert ocr._search_order(passes, "保存")[0].language == "zh-Hans-CN"
    assert ocr._preferred(passes) is passes[0]


def _fake_ocr(monkeypatch, readings):
    """recognize() returns the next of `readings` (lists of lines) as one zh pass."""
    seen = iter(readings)
    monkeypatch.setattr(
        desktop,
        "capture",
        lambda *a, **k: (Image.new("RGB", (400, 300)), {"left": 0, "top": 0, "target": {}}),
    )
    monkeypatch.setattr(
        ocr,
        "recognize",
        lambda image, left, top, language=None: ([ocr.Pass("zh-Hans-CN", 2.0, next(seen))], {}),
    )


def test_ocr_click_moves_the_mouse_only_on_an_exact_match(monkeypatch):
    _fake_ocr(monkeypatch, [[words(("保", (10, 10, 20, 20)), ("存", (32, 10, 20, 20)))], [CHINESE]])
    window = {"hwnd": 11, "title": "app", "process": "app.exe", "pid": 9}
    clicks = []
    monkeypatch.setattr(desktop, "find_window", lambda _: window)
    monkeypatch.setattr(desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(desktop, "root_window_at", lambda x, y: 11)
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: clicks.append(a) or {"foreground": "app"})
    done = ocr.act("click", "保存", window="app")
    assert clicks == [("click", 31, 20)] and done["done"] == "click at (31, 20)"
    with pytest.raises(ocr.OcrError, match="closest"):
        ocr.act("click", "已宛成", window="app")
    assert len(clicks) == 1


def test_ocr_click_refuses_a_covered_spot_and_admin_windows(monkeypatch):
    _fake_ocr(monkeypatch, [[words(("OK", (10, 10, 20, 20)))]])
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 11, "title": "app", "process": "a.exe"})
    monkeypatch.setattr(desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(desktop, "root_window_at", lambda x, y: 99)
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: pytest.fail("must not click"))
    with pytest.raises(ocr.OcrError, match="covers that spot"):
        ocr.act("click", "OK", window="app")
    monkeypatch.setattr(
        desktop,
        "find_window",
        lambda _: {"hwnd": 11, "title": "Setup", "process": "setup.exe", "admin": True},
    )
    with pytest.raises(desktop.DesktopError, match="setup.exe, which runs as administrator"):
        ocr.act("click", "OK", window="app")


def test_ocr_waits_for_text_to_appear_and_to_go(monkeypatch):
    nothing: list = []
    _fake_ocr(monkeypatch, [nothing, nothing, [CHINESE], [CHINESE], nothing] + [[CHINESE]] * 50)
    monkeypatch.setattr(ocr, "WAIT_POLL_S", 0.01)
    came = ocr.wait("Done", timeout_s=5)
    assert came["matched"] is True and came["match"]["match"] == "Done"
    gone = ocr.wait("Done", state="gone", timeout_s=5)
    assert gone["matched"] is True
    late = ocr.wait("Done", state="gone", timeout_s=0.05)
    assert late["matched"] is False and "call again" in late["note"]


# ------------------------------------------------------ waiting for the picture


def picture(text="", caret=False, size=(400, 200)):
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), text, fill=0)
    if caret:
        draw.rectangle((200, 50, 201, 70), fill=0)
    return image


def test_a_blinking_cursor_is_not_a_change_but_a_small_text_is():
    assert not desktop.pictures_differ(picture("42%"), picture("42%", caret=True))
    assert desktop.pictures_differ(picture("42%"), picture("43%"))
    assert desktop.pictures_differ(picture("42%"), picture("42%", size=(401, 200)))


def _frames(monkeypatch, frames):
    sequence = iter(frames)
    last = {}

    def capture(window=None, monitor=None, region=None):
        last["image"] = next(sequence, last.get("image"))
        return last["image"].convert("RGB"), {
            "left": 0,
            "top": 0,
            "target": {"window": 1},
            "key": ("window", 1),
        }

    monkeypatch.setattr(desktop, "capture", capture)
    monkeypatch.setattr(desktop, "WAIT_PICTURE_S", 0.01)
    monkeypatch.setattr(desktop, "_seen", {})


def test_waiting_for_a_change_and_for_stillness(monkeypatch):
    still, moved = picture("Status: waiting"), picture("Status: done")
    _frames(monkeypatch, [still, still, still, moved])
    image, place, facts = desktop.wait_picture(1, None, None, "change", 5, 1000)
    assert facts["matched"] is True and image.convert("L").tobytes() == moved.tobytes()
    # a change since the previous screenshot of the same window counts at once
    _frames(monkeypatch, [moved])
    desktop._remember(("window", 1), still)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 5, 1000)
    assert facts["matched"] is True and facts["waited_s"] == 0
    # stable: changes first, then holds still with a blinking cursor
    _frames(monkeypatch, [still, moved, still, moved] + [picture("x", caret=i % 2 == 0) for i in range(500)])
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 10, 200)
    assert facts["matched"] is True and facts["waited_s"] < 5
    # nothing changes: the wait ends with the current picture
    _frames(monkeypatch, [still])
    _, _, facts = desktop.wait_picture(1, None, None, "change", 0.05, 1000)
    assert facts["matched"] is False and "did not change" in facts["note"]
    with pytest.raises(desktop.DesktopError):
        desktop.wait_picture(1, None, None, "soon", 1, 1000)


def test_dpi_unaware_drawings_are_enlarged_to_the_window():
    drawing = Image.new("RGB", (600, 400), "black")
    drawing.paste(Image.new("RGB", (400, 267), "white"), (0, 0))  # drawn at 2/3 size (150 % scaling)
    fitted = desktop._fit_drawing(drawing, 1.5)
    assert fitted.size == (600, 400)
    assert fitted.getpixel((590, 390)) == (255, 255, 255)  # no black band left
    assert desktop._fit_drawing(drawing, 1.0) is drawing


# ------------------------------------------- locked desktop, administrator windows


@pytest.mark.parametrize(
    "session,desk,state",
    [
        ((4, 1), "Default", "disconnected"),
        ((0, 0), "", "locked"),
        ((0, 1), "", "secure"),
        ((0, 1), "Winlogon", "secure"),
        ((0, 1), "Default", "ok"),
        ((None, None), None, "ok"),  # unknown: never block
    ],
)
def test_desktop_state(monkeypatch, session, desk, state):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "_session", lambda: session)
    monkeypatch.setattr(desktop, "_input_desktop", lambda: desk)
    assert desktop.desktop_state() == state


@pytest.mark.parametrize("state", ["locked", "secure", "disconnected"])
def test_nothing_is_sent_while_the_desktop_is_unavailable(monkeypatch, state):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "desktop_state", lambda: state)
    monkeypatch.setattr(desktop, "_send", lambda *events: pytest.fail("input was sent"))
    with pytest.raises(desktop.DesktopUnavailable) as raised:
        desktop.input_action("keys", keys="ctrl+s")
    assert raised.value.state == state and "keep working" in str(raised.value)
    with pytest.raises(desktop.DesktopUnavailable):
        desktop.capture()  # the whole desktop would only be a black picture
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 5, "title": "x", "process": "x"})
    with pytest.raises(desktop.DesktopUnavailable):
        desktop.window_action("x", "focus")


def test_input_to_an_administrator_window_is_refused(monkeypatch):
    checked = []
    monkeypatch.setattr(desktop, "check_window_at", lambda x, y: checked.append(("at", x, y)))
    monkeypatch.setattr(desktop, "check_foreground", lambda: checked.append("foreground"))
    desktop._check_target("click", (5, 6), None)
    desktop._check_target("drag", (5, 6), (7, 8))
    desktop._check_target("keys", None, None)
    desktop._check_target("type", (1, 2), None)
    desktop._check_target("move", (1, 2), None)  # moving the pointer is not blocked by Windows
    assert checked == [("at", 5, 6), ("at", 5, 6), ("at", 7, 8), "foreground", ("at", 1, 2)]
    error = desktop.admin_error({"title": "PixPin", "process": "PixPin.exe", "pid": 3})
    assert "PixPin.exe, which runs as administrator" in str(error) and "screenshot and ocr" in str(error)


def test_only_a_higher_integrity_level_counts_as_administrator(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    levels = {0: 0x2000, 1: 0x3000, 2: 0x2000, 3: None}
    monkeypatch.setattr(desktop, "integrity", lambda pid: levels[pid])
    assert [desktop.is_elevated(p) for p in (1, 2, 3)] == [True, False, False]
    levels[0] = 0x3000  # winhand itself runs as administrator
    assert desktop.is_elevated(1) is False


async def test_window_list_names_the_desktop_state_and_wait_still_works(monkeypatch):
    monkeypatch.setattr(td.desktop, "monitors", lambda: [])
    monkeypatch.setattr(td.desktop, "windows", lambda: [{"hwnd": 1, "title": "Setup", "admin": True}])
    monkeypatch.setattr(td.desktop, "desktop_state", lambda: "locked")
    monkeypatch.setattr(td.desktop, "find_window", lambda _: {"hwnd": 1, "title": "Setup"})
    async with Client(build_server(Config())) as client:
        listed = (await client.call_tool("window", {})).structured_content
        waited = (
            await client.call_tool("window", {"action": "wait", "target": "Setup", "timeout_s": 0})
        ).structured_content
    assert listed["desktop"] == "locked" and listed["windows"][0]["admin"] is True
    assert waited["matched"] is True


async def test_ui_refuses_an_administrator_window(monkeypatch):
    _fake_window(monkeypatch, title="Setup", process="setup.exe", admin=True)
    monkeypatch.setattr(uia, "snapshot", lambda *a, **k: pytest.fail("must not read it"))
    async with Client(build_server(Config())) as client:
        result = (await client.call_tool("ui", {"window": "Setup"})).structured_content
    assert "setup.exe, which runs as administrator" in result["error"]


async def test_sys_info_lists_what_blocks_the_desktop(monkeypatch):
    extra = [{"check": "desktop", "ok": False, "detail": "locked", "fix": desktop.DESKTOP_MESSAGES["locked"]}]
    monkeypatch.setattr(desktop, "checks", lambda: extra)
    async with Client(build_server(Config())) as client:
        info = (await client.call_tool("sys_info", {"checks": True})).structured_content
    assert extra[0] in info["checks"]


def test_checks_name_admin_windows(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(desktop, "integrity", lambda pid: 0x2000)
    monkeypatch.setattr(
        desktop,
        "windows",
        lambda: [{"title": "PixPin", "process": "PixPin.exe", "admin": True}, {"title": "x"}],
    )
    found = desktop.checks()
    assert [c["check"] for c in found] == ["admin_windows"] and "PixPin.exe" in found[0]["detail"]


# ------------------------------------------------------------------- guide


def test_the_guide_names_the_ways_and_stays_short():
    text = server.instructions(Config())
    assert len(text) <= 4096
    assert "ocr(action='click'" in text and "screenshot(wait=" in text
    assert "ocr(action='wait'" in server.GUIDE_FULL and "admin=true" in server.GUIDE_FULL


@pytest.mark.skipif(desktop.IS_WINDOWS, reason="checks the non-Windows message")
async def test_ocr_explains_it_needs_windows():
    async with Client(build_server(Config())) as client:
        result = (await client.call_tool("ocr", {})).structured_content
    assert "only available on Windows" in result["error"]
