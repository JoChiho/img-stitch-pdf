# -*- coding: utf-8 -*-
"""Unit tests for local EN→ZH helpers (no OCR/MT models required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw, ImageFont

import translate_local as tl


def test_translated_sibling_path_basic():
    assert tl.translated_sibling_path(Path("a/orig.jpg")) == Path("a/orig_zh.jpg")
    assert tl.translated_sibling_path(Path("shot.PNG")) == Path("shot_zh.png")
    assert tl.translated_sibling_path(Path("/tmp/x.jpeg")) == Path("/tmp/x_zh.jpeg")


def test_translated_sibling_path_idempotent_and_webp():
    p = Path("folder/name_zh.png")
    assert tl.translated_sibling_path(p) == p
    assert tl.translated_sibling_path(Path("a.webp")).name == "a_zh.webp"


def test_is_already_translated_name():
    assert tl.is_already_translated_name(Path("x_zh.jpg"))
    assert not tl.is_already_translated_name(Path("x.jpg"))


def test_wrap_text_to_width_splits_long_cjk():
    font = ImageFont.load_default()
    img = Image.new("RGB", (200, 50))
    draw = ImageDraw.Draw(img)
    lines = tl.wrap_text_to_width("这是一段比较长的中文测试文本内容", font, max_width=40, draw=draw)
    assert len(lines) >= 2
    assert "".join(lines) == "这是一段比较长的中文测试文本内容"


def test_fit_font_size_for_box_returns_usable_font():
    font, lines, size = tl.fit_font_size_for_box(
        "你好世界", 80, 40, max_size=48, min_size=8
    )
    assert size >= 8
    assert lines
    assert font is not None


def test_draw_text_in_box_paints_without_mutating_size():
    img = Image.new("RGB", (120, 80), (10, 20, 30))
    out = tl.draw_text_in_box(img, (10, 10, 100, 50), "测试")
    assert out.size == img.size
    assert out.mode == "RGB"
    # Original top-left pixel should be unchanged (outside box)
    assert img.getpixel((0, 0)) == (10, 20, 30)
    # Box area should have been whitened / changed
    assert out.getpixel((20, 20)) != (10, 20, 30)


def test_translate_image_paths_skips_pdf_and_gif(tmp_path: Path):
    img = tmp_path / "a.png"
    Image.new("RGB", (20, 20), (255, 255, 255)).save(img)
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    gif = tmp_path / "c.gif"
    gif.write_bytes(b"GIF89a")

    fake_out = tmp_path / "a_zh.png"

    def fake_translate(src, **_kw):
        Image.new("RGB", (20, 20), (0, 0, 0)).save(fake_out)
        return fake_out

    with patch.object(tl, "translate_image_file", side_effect=fake_translate):
        translated, skipped_pdfs, warnings = tl.translate_image_paths(
            [img, pdf, gif]
        )

    assert translated == [fake_out]
    assert skipped_pdfs == [pdf]
    assert any("PDF" in w for w in warnings)
    assert any("GIF" in w for w in warnings)


def test_translate_image_file_uses_sibling_and_mocks(tmp_path: Path):
    src = tmp_path / "hello.jpg"
    Image.new("RGB", (100, 40), (200, 200, 200)).save(src, format="JPEG")

    boxes = [tl.OcrBox(text="Hello", box=(5, 5, 90, 30), confidence=0.9)]

    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", return_value="你好"
    ):
        out = tl.translate_image_file(src)

    assert out == tmp_path / "hello_zh.jpg"
    assert out.is_file()
    assert src.is_file()  # original preserved
    assert src.read_bytes() != out.read_bytes()


def test_check_deps_returns_keys():
    info = tl.check_deps()
    assert "easyocr" in info
    assert "argostranslate" in info
    assert "cjk_font" in info


@pytest.mark.skipif(
    not tl.check_deps().get("easyocr") or not tl.check_deps().get("argostranslate"),
    reason="OCR/MT models not installed (optional smoke)",
)
def test_smoke_translate_real_if_models_present(tmp_path: Path):
    src = tmp_path / "smoke.png"
    img = Image.new("RGB", (200, 60), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((10, 20), "Hello", fill=(0, 0, 0))
    img.save(src)
    out = tl.translate_image_file(src)
    assert out.is_file()
    assert out != src
