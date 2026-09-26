"""Files the model can look at or receive: images, documents, hand-over and binary writes."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile

import openpyxl
import pytest
from fastmcp import Client
from mcp.types import EmbeddedResource, ImageContent, TextContent
from PIL import Image

from winhand.config import Config
from winhand.server import build_server


@pytest.fixture
async def client():
    async with Client(build_server(Config())) as c:
        yield c


def texts(result):
    return [block.text for block in result.content if isinstance(block, TextContent)]


def images(result):
    return [block for block in result.content if isinstance(block, ImageContent)]


async def test_images_are_returned_as_pictures_and_scaled(client, tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 2000), (30, 120, 200)).save(path)
    result = await client.call_tool("fs_read", {"path": str(path)})
    facts = json.loads(texts(result)[0])
    assert facts["original_width"] == 4000 and facts["width"] == 1568 and facts["height"] == 784
    (picture,) = images(result)
    assert (
        picture.mime_type in ("image/png", "image/jpeg")
        and len(base64.b64decode(picture.data)) == facts["bytes"]
    )


def _docx(path):
    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>项目计划</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>第一段</w:t></w:r><w:r><w:tab/><w:t>tabbed</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


def _pptx(path):
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        for n, words in ((1, "封面标题"), (2, "第二页要点"), (10, "last slide")):
            z.writestr(
                f"ppt/slides/slide{n}.xml",
                f"<p:sld {ns} xmlns:p='x'><a:p><a:r><a:t>{words}</a:t></a:r></a:p></p:sld>",
            )


def _pdf_with_text(path):
    content = b"BT /F1 24 Tf 72 700 Td (Hello winhand PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>stream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


async def test_office_documents_become_text(client, tmp_path):
    _docx(tmp_path / "plan.docx")
    text = "\n".join(texts(await client.call_tool("fs_read", {"path": str(tmp_path / "plan.docx")})))
    assert "# 项目计划" in text and "第一段\ttabbed" in text and "| A1 | B1 |" in text

    _pptx(tmp_path / "deck.pptx")
    text = "\n".join(texts(await client.call_tool("fs_read", {"path": str(tmp_path / "deck.pptx")})))
    assert (
        text.index("封面标题") < text.index("第二页要点") < text.index("last slide")
    )  # slide 10 after slide 2

    book = openpyxl.Workbook()
    book.active.title = "预算"
    book.active.append(["项目", "金额"])
    book.active.append(["服务器", 1200])
    book.save(tmp_path / "budget.xlsx")
    text = "\n".join(texts(await client.call_tool("fs_read", {"path": str(tmp_path / "budget.xlsx")})))
    assert "sheet: 预算" in text and "服务器\t1200" in text


async def test_pdf_text_and_scanned_pages(client, tmp_path):
    _pdf_with_text(tmp_path / "text.pdf")
    result = await client.call_tool("fs_read", {"path": str(tmp_path / "text.pdf")})
    assert "Hello winhand PDF" in "\n".join(texts(result)) and not images(result)

    scan = tmp_path / "scan.pdf"
    Image.new("RGB", (800, 1100), "white").save(scan)  # an image-only page, like a scanner makes
    result = await client.call_tool("fs_read", {"path": str(scan)})
    facts = json.loads(texts(result)[0])
    assert facts["pages_without_text"] == [1] and len(images(result)) == 1


async def test_files_can_be_handed_over_and_written_as_bytes(client, tmp_path):
    payload = bytes(range(256)) * 10
    source = tmp_path / "blob.bin"
    source.write_bytes(payload)
    result = await client.call_tool("fs_send", {"path": str(source)})
    facts = json.loads(texts(result)[0])
    resource = next(b for b in result.content if isinstance(b, EmbeddedResource)).resource
    assert (
        base64.b64decode(resource.blob) == payload and facts["sha256"] == hashlib.sha256(payload).hexdigest()
    )

    target = tmp_path / "out" / "copy.bin"
    half = len(payload) // 2
    for part, append in ((payload[:half], False), (payload[half:], True)):
        written = await client.call_tool(
            "fs_write_bytes",
            {"path": str(target), "data_base64": base64.b64encode(part).decode(), "append": append},
        )
    assert target.read_bytes() == payload and written.structured_content["sha256"] == facts["sha256"]


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows message")
async def test_desktop_tools_explain_they_need_windows(client):
    result = await client.call_tool("screenshot", {}, raise_on_error=False)
    assert "only available on Windows" in "\n".join(texts(result))


async def test_image_results_carry_structured_content(client, tmp_path):
    # A client that cached an older fs_read definition (with an output schema) rejects results
    # without structured content, so pictures must bring their facts in both forms.
    path = tmp_path / "small.png"
    Image.new("RGB", (40, 30), "red").save(path)
    result = await client.call_tool("fs_read", {"path": str(path)})
    assert result.structured_content["width"] == 40 and images(result)


def test_busy_images_stay_within_the_size_budget():
    import random

    from winhand import media

    rng = random.Random(1)
    noisy = Image.frombytes("RGB", (3000, 2000), bytes(rng.getrandbits(8) for _ in range(3000 * 2000 * 3)))
    data, fmt, facts = media.encode_image(noisy)
    assert len(data) <= media.IMAGE_BUDGET and facts["bytes"] == len(data) and fmt == "jpeg"
    # coordinates stay right when the budget forced an extra scale-down
    assert facts["scale"] == round(facts["width"] / 3000, 4)
    flat = Image.new("RGB", (1200, 800), "white")
    assert media.encode_image(flat)[1] == "png"  # screen-like content keeps sharp PNG


async def test_zoom_into_part_of_an_image(client, tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (3000, 2000), "blue").save(path)
    result = await client.call_tool("fs_read", {"path": str(path), "region": [100, 200, 800, 600]})
    facts = result.structured_content
    assert (facts["width"], facts["height"]) == (800, 600) and facts["region"] == [100, 200, 800, 600]
    bad = await client.call_tool(
        "fs_read", {"path": str(path), "region": [5000, 0, 10, 10]}, raise_on_error=False
    )
    assert "outside" in json.dumps(bad.structured_content)


async def test_call_reaches_every_tool_and_help_lists_them(client, tmp_path):
    path = tmp_path / "dot.png"
    Image.new("RGB", (8, 8), "green").save(path)
    via_call = await client.call_tool("call", {"tool": "fs_read", "arguments": {"path": str(path)}})
    assert images(via_call) and via_call.structured_content["width"] == 8
    unknown = await client.call_tool("call", {"tool": "nope"}, raise_on_error=False)
    assert "screenshot" in unknown.structured_content["tools"]
    guide = (await client.call_tool("help", {})).data
    assert "screenshot" in guide and "call(tool, arguments)" in guide
