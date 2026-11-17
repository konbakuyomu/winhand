from __future__ import annotations

import pytest

from winhand import fs


@pytest.mark.parametrize(
    ("initial", "content", "expected", "newline"),
    [
        (b"a\r\nb\r\n", "x\ny\n", b"a\r\nb\r\nx\r\ny\r\n", "crlf"),
        (b"a\nb\n", "x\r\ny\r\n", b"a\nb\nx\ny\n", "lf"),
    ],
)
def test_append_follows_file_newlines(tmp_path, initial, content, expected, newline):
    path = tmp_path / "append.txt"
    path.write_bytes(initial)

    out = fs.fs_write(str(path), content, append=True)

    assert path.read_bytes() == expected
    assert out["newline"] == newline


def test_edit_mixed_newlines_keeps_untouched_bytes(tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"first\r\nsecond\r\nthird\nfourth\n")

    out = fs.fs_edit(str(path), old="third\nfourth\n", new="THIRD\r\nFOURTH\r\n")

    assert out["replacements"] == 1 and out["line"] == 3
    assert path.read_bytes() == b"first\r\nsecond\r\nTHIRD\nFOURTH\n"

    out = fs.fs_edit(str(path), old="first\r\nsecond\n", new="FIRST\nSECOND\n")

    assert out["replacements"] == 1 and out["line"] == 1
    assert path.read_bytes() == b"FIRST\r\nSECOND\r\nTHIRD\nFOURTH\n"


def test_replace_all_uses_each_match_newlines(tmp_path):
    path = tmp_path / "replace-all.txt"
    original = b"head\r\nsame\r\nnext\r\nuntouched\r\nsame\nnext\ntail\n"
    path.write_bytes(original)
    old = "same\r\nnext\n"
    new = "changed\nnext\r\nextra\n"

    with pytest.raises(fs.FsError, match="old text occurs 2 times"):
        fs.fs_edit(str(path), old=old, new=new)
    assert path.read_bytes() == original

    out = fs.fs_edit(str(path), old=old, new=new, replace_all=True)

    assert out["replacements"] == 2 and out["line"] == 2
    assert path.read_bytes() == (
        b"head\r\nchanged\r\nnext\r\nextra\r\nuntouched\r\nchanged\nnext\nextra\ntail\n"
    )


@pytest.mark.parametrize(
    ("file_ending", "old_ending", "file_style", "old_style"),
    [("\n", "\r\n", "LF", "CRLF"), ("\r\n", "\n", "CRLF", "LF")],
)
def test_closest_reports_line_endings(file_ending, old_ending, file_style, old_style):
    text = file_ending.join(("one", "two", "three", "four", "same", ""))

    assert fs._closest(text, "same" + old_ending) == (
        ". Closest text: lines 5-5 match except for line endings "
        f"(the file has {file_style} there, your old text has {old_style})"
    )
