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
    # not WinHand's own window, and allowed (approvals are tested in test_approvals.py)
    monkeypatch.setattr(td.desktop, "refuse_own", lambda hwnd: {})
    monkeypatch.setattr(td.desktop, "require_operable", lambda hwnd, what: None)
    monkeypatch.setattr(td.desktop, "pid_at", lambda x, y: window["pid"])  # nothing covers it
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

    def refresh(e):
        assert e is element  # the very element that was operated, not a look-alike by runtime id
        return {**snapshot(11)["elements"][0], "_runtime_id": [9, 9]}

    monkeypatch.setattr(uia, "snapshot", snapshot)
    monkeypatch.setattr(uia, "refresh", refresh)
    monkeypatch.setattr(
        uia, "act", lambda e, verb, value=None, timeout=0: acted.append((e, verb)) or "expanded"
    )
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


async def test_classic_buttons_get_a_posted_click_that_a_modal_dialog_cannot_hold_up(monkeypatch):
    _fake_window(monkeypatch)
    button = {"index": 0, "type": "Pane", "name": "Browse...", "automation_id": "", "hwnd": 77, "enabled": True,
              "rect": [1, 2, 3, 4], "patterns": ["Invoke"], "_runtime_id": [7], "_element": object()}  # fmt: skip
    monkeypatch.setattr(
        uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 1, "elements": [button]}
    )
    monkeypatch.setattr(td.desktop, "native_controls", lambda hwnd: [
        {"hwnd": 77, "type": "Button", "name": "Browse...", "class": "Button", "enabled": True, "rect": [1, 2, 3, 4]}
    ])  # fmt: skip
    monkeypatch.setattr(uia, "act", lambda *a, **k: pytest.fail("UI Automation Invoke can block"))
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    posted = []
    monkeypatch.setattr(
        td.desktop, "native_act", lambda control, verb, value: posted.append(verb) or "invoked"
    )
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool(
                "ui", {"window": "app", "action": "invoke", "name": "Browse...", "verify_wait_s": 0}
            )
        ).structured_content
    assert posted == ["invoke"] and done["done"] == "invoked"


async def test_com_errors_become_plain_errors(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(uia, "_enter_apartment", lambda: None)
    monkeypatch.setattr(uia, "_worker", None)

    class COMError(Exception):
        pass

    def gone():
        raise COMError(-2147220991, "The element is not available", None)

    with pytest.raises(desktop.DesktopError, match="the control is gone"):
        uia.call(gone)

    def timed_out():
        raise COMError(-2146233083, None, None)  # UIA_E_TIMEOUT

    with pytest.raises(uia.NoAnswer):
        uia.call(timed_out)

    def odd():
        raise COMError(-2147467259, "Unspecified error", None)

    with pytest.raises(desktop.DesktopError, match="UI Automation failed: COMError"):
        uia.call(odd)


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


async def test_a_dialog_opened_by_invoke_does_not_hold_up_the_call(monkeypatch):
    _fake_window(monkeypatch)
    element = object()
    item = {"index": 0, "type": "MenuItem", "name": "Open...", "automation_id": "", "hwnd": 0, "enabled": True,
            "rect": [1, 2, 3, 4], "patterns": ["Invoke"], "_runtime_id": [5], "_element": element}  # fmt: skip
    monkeypatch.setattr(
        uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 1, "elements": [item]}
    )
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    timeouts = []

    def act(e, verb, value=None, timeout=uia.TIMEOUT_S):
        timeouts.append(timeout)
        raise uia.NoAnswer(uia.NO_ANSWER)

    monkeypatch.setattr(uia, "act", act)
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool(
                "ui", {"window": "app", "action": "invoke", "name": "Open...", "verify_wait_s": 0}
            )
        ).structured_content
        failed = (
            await client.call_tool(
                "ui", {"window": "app", "action": "set_value", "name": "Open...", "value": "x"}
            )
        ).structured_content
    assert timeouts[0] <= 5 and "dialog it opened" in done["done"] and done["backend"] == "native"
    assert "error" in failed  # set_value has no pattern here: nothing to send


async def test_waiting_for_a_control_tells_a_hung_program_from_a_gone_one(monkeypatch):
    window = {"hwnd": 11, "title": "Setup", "process": "setup.exe", "pid": 9}
    monkeypatch.setattr(td.desktop, "find_window", lambda _: window)

    def hung(hwnd):
        raise uia.NoAnswer(uia.NO_ANSWER)

    with pytest.raises(uia.NoAnswer):
        td.wait_control("Setup", "Installing...", None, None, "gone", 1, inspect=hung)

    def changing(hwnd):
        raise td.desktop.DesktopError(
            "UI Automation: the control is gone (the window changed); inspect it again"
        )

    monkeypatch.setattr(td, "WAIT_POLL_S", 0.01)
    waited = td.wait_control("Setup", "Installing...", None, None, "gone", 0.1, inspect=changing)
    assert waited["matched"] is False and "the control is gone" in waited["last_error"]  # not "gone"
    monkeypatch.setattr(td.desktop, "find_window", lambda _: {**window, "admin": True})
    with pytest.raises(td.desktop.DesktopError, match="administrator"):
        td.wait_control("Setup", "Installing...", None, None, "gone", 1, inspect=changing)


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


async def test_a_mouse_click_on_a_control_needs_the_control_on_screen(monkeypatch):
    _fake_window(monkeypatch)
    monkeypatch.setattr(td.desktop, "pid_at", lambda x, y: 4321)  # another program's window is there
    monkeypatch.setattr(uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 1, "elements": [
        {"index": 0, "type": "ListItem", "name": "row 90", "automation_id": "", "hwnd": 0, "enabled": True,
         "rect": [100, 2000, 50, 20], "offscreen": True, "patterns": [], "_runtime_id": [5], "_element": object()},
    ]})  # fmt: skip
    monkeypatch.setattr(td.desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(td.desktop, "input_action", lambda *a, **k: pytest.fail("clicked another window"))
    async with Client(build_server(Config())) as client:
        refused = (
            await client.call_tool("ui", {"window": "app", "action": "click", "name": "row 90"})
        ).structured_content
    assert "nothing was clicked" in refused["error"] and "out of sight" in refused["error"]


# --------------------------------------------------------------------- OCR


def words(*items):
    return [ocr.Word(text, rect) for text, rect in items]


CHINESE = words(
    ("状", (0, 0, 20, 20)), ("态", (22, 0, 20, 20)), ("：", (44, 0, 20, 20)), ("已", (66, 0, 20, 20)),
    ("完", (88, 0, 20, 20)), ("成", (110, 0, 20, 20)), ("Done", (138, 0, 40, 20)),
)  # fmt: skip


def test_lines_read_as_a_person_reads_them():
    assert ocr.line_text(CHINESE) == "状态：已完成 Done"
    spaced_colon = words(("状", (0, 0, 20, 20)), ("：", (30, 0, 6, 20)), ("已", (50, 0, 20, 20)))
    assert ocr.line_text(spaced_colon) == "状：已"  # full-width punctuation has narrow ink
    page = words(("Page", (0, 0, 40, 20)), ("１２", (50, 0, 20, 20)))
    assert ocr.line_text(page) == "Page １２"  # full-width digits are not punctuation
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
    # one line of two words with the outer array unwrapped too
    collapsed = {"passes": [{"image": 0, "language": "en-US", "lines": [{"t": "Press", "r": [0, 0, 40, 10]},
                                                                      {"t": "me", "r": [45, 0, 20, 10]}]}]}  # fmt: skip
    passes, _ = ocr.parse(collapsed, [1.0], 0, 0)
    assert len(passes[0].lines) == 1 and ocr.find_in(passes[0].lines, "press me")
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
    monkeypatch.setattr(desktop, "require_operable", lambda hwnd, what: None)
    monkeypatch.setattr(desktop, "root_window_at", lambda x, y: 11)
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: clicks.append(a) or {"foreground": "app"})
    done = ocr.act("click", "保存", window="app")
    assert clicks == [("click", 31, 20)] and done["done"] == "click at (31, 20)"
    missed = ocr.act("click", "已完成X", window="app")
    assert "nothing was clicked" in missed["error"] and missed["similar"][0]["score"] >= 0.5
    assert len(clicks) == 1


def test_ocr_click_does_not_guess_between_places_showing_the_same_text(monkeypatch):
    ok_left, ok_right = words(("OK", (10, 100, 20, 20))), words(("OK", (300, 100, 20, 20)))
    sentence = words(("Press", (10, 10, 50, 20)), ("OK", (64, 10, 20, 20)), ("to", (88, 10, 20, 20)))
    screens = [[sentence, ok_left], [ok_left, ok_right], [ok_left, ok_right]]
    _fake_ocr(monkeypatch, screens)
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 11, "title": "app", "process": "a.exe"})
    monkeypatch.setattr(desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(desktop, "require_operable", lambda hwnd, what: None)
    monkeypatch.setattr(desktop, "root_window_at", lambda x, y: 11)
    clicks = []
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: clicks.append(a[1:3]) or {})
    # the button (a line of its own) rather than the sentence that mentions it
    done = ocr.act("click", "OK", window="app")
    assert clicks == [(20, 110)] and done["index"] == 1
    # two buttons: which one is the caller's to say; nothing is clicked meanwhile
    ambiguous = ocr.act("click", "OK", window="app")
    assert "2 times; nothing was clicked" in ambiguous["error"] and len(clicks) == 1
    assert [(m["index"], m["center"]) for m in ambiguous["matches"]] == [(0, [20, 110]), (1, [310, 110])]
    ocr.act("click", "OK", window="app", index=1)
    assert clicks[-1] == (310, 110)


async def test_ui_does_not_guess_between_controls_of_the_same_name(monkeypatch):
    _fake_window(monkeypatch)
    buttons = [
        {"index": i, "type": "Button", "name": "删除", "automation_id": aid, "hwnd": 0, "enabled": True,
         "rect": [10 + 100 * i, 2, 30, 20], "patterns": ["Invoke"], "_runtime_id": [i], "_element": object()}
        for i, aid in enumerate(("delRow", "delAll"))
    ]  # fmt: skip
    monkeypatch.setattr(
        uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 2, "elements": buttons}
    )
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    acted = []
    monkeypatch.setattr(uia, "act", lambda e, verb, value=None, timeout=0: acted.append(e) or "invoked")
    async with Client(build_server(Config())) as client:
        ambiguous = (
            await client.call_tool("ui", {"window": "app", "action": "invoke", "name": "删除"})
        ).structured_content
        chosen = (
            await client.call_tool(
                "ui", {"window": "app", "action": "invoke", "name": "删除", "index": 1, "verify_wait_s": 0}
            )
        ).structured_content
    assert "2 controls match; nothing was done" in ambiguous["error"]
    assert [(m["index"], m["automation_id"]) for m in ambiguous["matches"]] == [(0, "delRow"), (1, "delAll")]
    assert acted == [buttons[1]["_element"]] and "error" not in chosen


def test_ocr_click_refuses_a_covered_spot_and_admin_windows(monkeypatch):
    _fake_ocr(monkeypatch, [[words(("OK", (10, 10, 20, 20)))]])
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 11, "title": "app", "process": "a.exe"})
    monkeypatch.setattr(desktop, "window_action", lambda *a, **k: {})
    monkeypatch.setattr(desktop, "require_operable", lambda hwnd, what: None)
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


def test_changes_are_outlined_and_small_ones_recognized():
    assert desktop.changed_box(picture("42%"), picture("42%")) is None
    caret = desktop.changed_box(picture("42%"), picture("42%", caret=True))
    assert caret and desktop.small_change(caret)
    digit = desktop.changed_box(picture("42%"), picture("43%"))
    assert digit and desktop.small_change(digit)  # small, but it lasts: see the waits below
    assert desktop.changed_box(picture("42%"), picture("42%", size=(401, 200))) == (0, 0, 401, 200)
    big = desktop.changed_box(picture("Status: waiting"), picture("Status: done, all files copied"))
    assert big and not desktop.small_change(big)


def bar(width: int, block: bool = False, shade: int = 255) -> Image.Image:
    image = Image.new("L", (400, 200), shade)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 100, 20 + width, 115), fill=0)  # a 16 px high progress bar
    if block:
        draw.rectangle((300, 40, 308, 57), fill=0)  # a console's 9 x 18 block cursor
    return image


def test_slow_progress_and_fades_are_not_stillness(monkeypatch):
    # one pixel more per capture: each step alone is tiny, but they add up against the reference
    _frames(monkeypatch, [bar(10 + i) for i in range(200)])
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 0.6, 400)
    assert facts["matched"] is False and "kept changing" in facts["note"]
    _frames(monkeypatch, [bar(10, shade=255 - 2 * i) for i in range(100)])  # a slow fade
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 0.6, 400)
    assert facts["matched"] is False


def test_tall_cursors_slow_blinks_and_faint_noise(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(desktop.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(desktop.time, "sleep", lambda s: clock.update(now=clock["now"] + 0.28))

    def frame(on: bool, noise: int = 0, height: int = 18) -> Image.Image:
        image = Image.new("L", (400, 200), 200 + noise)
        if on:
            ImageDraw.Draw(image).rectangle((300, 40, 308, 40 + height), fill=0)
        return image

    # faint flicker elsewhere (under 16 grey levels) does not hide a cursor blinking back
    _frames(monkeypatch, [frame(i % 2 == 0, noise=i % 3) for i in range(400)])
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 6, 1000)
    assert facts["matched"] is True
    # a 70 px cursor on a 300 % screen is still a cursor
    tall = [frame(i % 2 == 0, height=70) for i in range(400)]
    _frames(monkeypatch, tall)
    monkeypatch.setattr(desktop, "_zoom", lambda: 3.0)
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 6, 1000)
    assert facts["matched"] is True
    _frames(monkeypatch, tall)
    monkeypatch.setattr(desktop, "_zoom", lambda: 3.0)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 3, 1000)
    assert facts["matched"] is False
    # the slowest blink setting (1.2 s per phase) is not a change either
    slow = [frame((i // 5) % 2 == 0) for i in range(400)]
    _frames(monkeypatch, slow)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 8, 1000)
    assert facts["matched"] is False


def _clock(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(desktop.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(desktop.time, "sleep", lambda s: clock.update(now=clock["now"] + 0.3))


def _form(cursor: bool = False, glitch: bool = False, spinner: int | None = None) -> Image.Image:
    image = Image.new("L", (400, 200), 220)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 380, 60), fill=255)  # a list view
    if cursor:
        draw.rectangle((300, 120, 301, 140), fill=0)
    if glitch:  # one half-painted picture of the list view
        draw.rectangle((20, 40, 300, 60), fill=0)
    if spinner is not None:  # a large loading animation
        draw.pieslice((150, 80, 214, 144), spinner * 90, spinner * 90 + 90, fill=0)
    return image


def test_one_half_painted_picture_is_not_a_change(monkeypatch):
    _clock(monkeypatch)
    # the cursor blinks, and once the list view comes out half painted
    frames = [_form(cursor=i % 4 < 2, glitch=i == 7) for i in range(60)]
    _frames(monkeypatch, frames)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 6, 1000)
    assert facts["matched"] is False
    _frames(monkeypatch, [_form(glitch=i == 3) for i in range(60)])
    _, _, facts = desktop.wait_picture(1, None, None, "change", 6, 1000)
    assert facts["matched"] is False
    # a large change that stays is found on the next capture
    _frames(monkeypatch, [_form()] * 3 + [_form(spinner=0)] * 30)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 6, 1000)
    assert facts["matched"] is True and facts["waited_s"] < 1.5


def test_the_tall_cursor_of_a_large_font_is_a_cursor(monkeypatch):
    _clock(monkeypatch)
    assert desktop.small_change((300, 40, 303, 100)) and not desktop.small_change((300, 40, 340, 100))
    tall = []
    for i in range(60):  # a 3 x 60 pixel cursor: three captures on, three off
        frame = _form()
        if (i // 3) % 2 == 0:
            ImageDraw.Draw(frame).rectangle((300, 80, 302, 140), fill=0)
        tall.append(frame)
    _frames(monkeypatch, tall)
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 6, 1000)
    assert facts["matched"] is True
    _frames(monkeypatch, tall)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 4, 1000)
    assert facts["matched"] is False


def test_a_large_two_picture_animation_is_not_still(monkeypatch):
    _clock(monkeypatch)
    # alternating on every capture, every second one, and out of step with the captures (0.5 s phases
    # sampled every 0.3 s): each picture is often caught once, like a one-off half-painted picture
    for name, phase in (("every capture", lambda i: i % 2), ("every second", lambda i: (i // 2) % 2),
                        ("out of step", lambda i: int(i * 0.3 / 0.5) % 2)):  # fmt: skip
        _frames(monkeypatch, [_form(spinner=phase(i)) for i in range(80)])
        _, _, facts = desktop.wait_picture(1, None, None, "stable", 8, 1000)
        assert facts["matched"] is False, name


def test_large_animations_are_not_still_but_a_one_off_picture_is_overlooked(monkeypatch):
    _clock(monkeypatch)
    _frames(monkeypatch, [_form(glitch=i == 2) for i in range(60)])
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 6, 1000)
    assert facts["matched"] is True and facts["waited_s"] <= 1.3  # not pushed back by the glitch
    for states in (2, 4):  # a loading animation is still going
        _frames(monkeypatch, [_form(spinner=i % states) for i in range(60)])
        _, _, facts = desktop.wait_picture(1, None, None, "stable", 6, 1000)
        assert facts["matched"] is False, states


def test_an_abandoned_worker_thread_ends_even_with_cancelled_calls_queued(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(uia, "_enter_apartment", lambda: None)
    monkeypatch.setattr(uia, "_worker", None)
    stuck = uia._Worker()
    monkeypatch.setattr(uia, "_worker", stuck)
    first = stuck.submit(time.sleep, 0.3)
    queued = stuck.submit(lambda: 1)
    queued.cancel()
    stuck.abandoned = True
    first.result(timeout=2)
    stuck.thread.join(timeout=2)
    assert not stuck.thread.is_alive() and queued.cancelled()


def test_an_abandoned_worker_thread_ends(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(uia, "_enter_apartment", lambda: None)
    monkeypatch.setattr(uia, "_worker", None)
    import threading

    before = threading.active_count()
    with pytest.raises(uia.NoAnswer):
        uia.call(time.sleep, 0.3, timeout=0.05)
    time.sleep(0.6)
    assert uia.call(lambda: 7) == 7
    time.sleep(0.1)
    assert threading.active_count() <= before + 1  # the given-up thread finished and left


def test_a_blinking_cursor_neither_changes_nor_unsettles_the_picture(monkeypatch):
    blinking = [bar(50, block=i % 2 == 0) for i in range(300)]
    _frames(monkeypatch, blinking)
    _, _, facts = desktop.wait_picture(1, None, None, "stable", 5, 300)
    assert facts["matched"] is True and facts["waited_s"] < 2
    _frames(monkeypatch, blinking)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 0.3, 1000)
    assert facts["matched"] is False
    # a small change that stays is a change
    _frames(monkeypatch, [bar(50)] + [bar(56)] * 50)
    _, _, facts = desktop.wait_picture(1, None, None, "change", 5, 1000)
    assert facts["matched"] is True


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
    monkeypatch.setattr(desktop, "_zoom", lambda: 1.0)  # sizes as on a 100 % screen, whatever this one is


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
    whole = Image.new("RGB", (600, 400), "white")  # drawn at full size already (GDI scaling)
    assert desktop._fit_drawing(whole, 1.5) is whole


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
    monkeypatch.setattr(desktop, "check_window_at", lambda x, y, what="": checked.append(("at", x, y)))
    monkeypatch.setattr(desktop, "check_foreground", lambda what="": checked.append("foreground"))
    desktop._check_target("click", (5, 6), None)
    desktop._check_target("drag", (5, 6), (7, 8))
    desktop._check_target("keys", None, None)
    desktop._check_target("keys", (3, 4), None)  # keys go to the foreground window, not to x, y
    desktop._check_target("type", (1, 2), None)
    desktop._check_target("move", (1, 2), None)  # moving the pointer is not blocked by Windows
    assert checked == [("at", 5, 6), ("at", 5, 6), ("at", 7, 8), "foreground", "foreground", ("at", 1, 2)]
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
