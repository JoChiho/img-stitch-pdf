# -*- coding: utf-8 -*-
"""Unit tests for local EN→ZH helpers (no OCR/MT models required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw, ImageFont

import translate_local as tl


def test_translated_output_path_uses_subdir():
    assert tl.translated_output_path(Path("a/orig.jpg")) == Path(
        "a/translated_zh/orig_zh.jpg"
    )
    assert tl.translated_output_path(Path("shot.PNG")) == Path(
        "translated_zh/shot_zh.png"
    )
    assert tl.translated_output_path(Path("/tmp/x.jpeg")) == Path(
        "/tmp/translated_zh/x_zh.jpeg"
    )
    assert tl.TRANSLATED_SUBDIR == "translated_zh"


def test_translated_sibling_path_alias_uses_subdir():
    # Deprecated alias must still point into the subfolder (not same-folder sibling).
    assert tl.translated_sibling_path(Path("a/orig.jpg")) == Path(
        "a/translated_zh/orig_zh.jpg"
    )


def test_translated_output_path_idempotent_inside_subdir():
    p = Path("folder/translated_zh/name_zh.png")
    assert tl.translated_output_path(p) == p
    assert tl.translated_output_path(Path("a.webp")).name == "a_zh.webp"
    assert tl.translated_output_path(Path("a.webp")).parent.name == "translated_zh"


def test_is_already_translated_name():
    assert tl.is_already_translated_name(Path("x_zh.jpg"))
    assert tl.is_already_translated_name(Path("folder/translated_zh/x.png"))
    assert not tl.is_already_translated_name(Path("x.jpg"))
    assert not tl.is_already_translated_name(Path("folder/x.png"))


def test_contains_cjk():
    assert tl.contains_cjk("你好")
    assert tl.contains_cjk("Hello 世界")
    assert not tl.contains_cjk("Hello")
    assert not tl.contains_cjk("")


def test_wrap_text_to_width_splits_long_cjk():
    font = ImageFont.load_default()
    img = Image.new("RGB", (200, 50))
    draw = ImageDraw.Draw(img)
    lines = tl.wrap_text_to_width("这是一段比较长的中文测试文本内容", font, max_width=40, draw=draw)
    assert len(lines) >= 2
    assert "".join(lines) == "这是一段比较长的中文测试文本内容"


def test_fit_font_size_for_box_returns_usable_font():
    font, lines, size, pad = tl.fit_font_size_for_box(
        "测试中文", 80, 40, max_size=48, min_size=8
    )
    assert size >= 8
    assert lines
    assert font is not None
    assert pad >= 0


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

    fake_out = tmp_path / "translated_zh" / "a_zh.png"
    fake_out.parent.mkdir(parents=True, exist_ok=True)

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


def test_translate_image_paths_skips_already_translated(tmp_path: Path):
    img = tmp_path / "a.png"
    Image.new("RGB", (20, 20), (255, 255, 255)).save(img)
    already = tmp_path / "translated_zh" / "a_zh.png"
    already.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), (0, 0, 0)).save(already)
    sibling_zh = tmp_path / "old_zh.png"
    Image.new("RGB", (20, 20), (1, 1, 1)).save(sibling_zh)

    calls = []

    def fake_translate(src, **_kw):
        calls.append(src)
        out = tl.translated_output_path(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20), (0, 0, 0)).save(out)
        return out

    with patch.object(tl, "translate_image_file", side_effect=fake_translate):
        translated, _skipped, warnings = tl.translate_image_paths(
            [img, already, sibling_zh]
        )

    assert calls == [img]
    assert translated == [tl.translated_output_path(img)]
    assert any("译图" in w or "后缀" in w for w in warnings)


def test_translate_image_file_writes_under_subdir(tmp_path: Path):
    src = tmp_path / "hello.jpg"
    # Distinct top-left color so we can prove original pixels stay intact
    img = Image.new("RGB", (100, 40), (200, 200, 200))
    img.putpixel((0, 0), (11, 22, 33))
    img.save(src, format="JPEG")

    boxes = [tl.OcrBox(text="Hello", box=(5, 5, 90, 30), confidence=0.9)]
    zh = "\u4f60\u597d"  # nihao

    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", return_value=zh
    ), patch.object(tl, "_get_translator", return_value=(lambda t: zh, "mock")):
        out = tl.translate_image_file(src)

    expected = tmp_path / "translated_zh" / "hello_zh.jpg"
    assert out == expected
    assert out.is_file()
    assert out.parent.name == tl.TRANSLATED_SUBDIR
    assert src.is_file()  # original preserved
    assert src.read_bytes() != out.read_bytes()
    # Must NOT create sibling in the same folder
    assert not (tmp_path / "hello_zh.jpg").exists()

    # Caption band: taller than source; top region keeps original content
    with Image.open(src) as src_im, Image.open(out) as out_im:
        src_im = src_im.convert("RGB")
        out_im = out_im.convert("RGB")
        assert out_im.size[0] == 100
        assert out_im.size[1] > src_im.size[1]
        # Original image region pasted at (0,0) — sample a few pixels vs source
        for xy in [(1, 1), (50, 20), (90, 35)]:
            a = src_im.getpixel(xy)
            b = out_im.getpixel(xy)
            assert all(abs(a[i] - b[i]) <= 8 for i in range(3)), (xy, a, b)
        # Band below original should be light (caption background)
        band_y = src_im.size[1] + 5
        br, bg, bb = out_im.getpixel((50, band_y))
        assert br > 200 and bg > 200 and bb > 200


def test_translate_text_mock_returns_chinese():
    """Unit test: mock translate_text('Hello') returns Chinese chars."""
    with patch.object(tl, "_get_translator", return_value=(lambda t: "你好", "mock")):
        # Clear any prior cache so _get_translator mock is used via translate_en_to_zh
        # translate_en_to_zh calls _get_translator each time when we also clear _translate_fn
        tl._translate_fn = None
        out = tl.translate_text("Hello")
    assert tl.contains_cjk(out)
    assert out == "你好"


def test_get_translator_caches_callable_not_translation_object():
    """Regression: must not return CachedTranslation on 2nd call (not callable)."""
    fake_translation = MagicMock(name="CachedTranslation")
    fake_translation.translate.side_effect = lambda s: f"ZH:{s}"

    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None
    tl._mt_status_detail = ""

    with patch.object(tl, "ensure_ollama", return_value=(False, "ollama down")), patch.object(
        tl, "_ensure_argos_en_zh", return_value=fake_translation
    ), patch.dict("sys.modules", {"argostranslate": MagicMock()}):
        import sys

        sys.modules["argostranslate"] = MagicMock()
        fn1, backend1 = tl._get_translator()
        fn2, backend2 = tl._get_translator()

    assert backend1 == "argos" and backend2 == "argos"
    assert callable(fn1) and callable(fn2)
    assert fn1 is fn2
    assert fn1("Hello") == "ZH:Hello"
    assert fn2("World") == "ZH:World"
    assert tl._translate_fn is fn1
    assert callable(tl._translate_fn)



def test_translate_en_to_zh_multiple_calls_with_mock():
    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None

    calls = []

    def fake_fn(text: str) -> str:
        calls.append(text)
        return f"译:{text}"

    with patch.object(tl, "_get_translator", return_value=(fake_fn, "mock")):
        a = tl.translate_en_to_zh("Hello")
        b = tl.translate_en_to_zh("World")
    assert a == "译:Hello"
    assert b == "译:World"
    assert calls == ["Hello", "World"]
    assert tl.contains_cjk(a) and tl.contains_cjk(b)


def test_check_deps_returns_keys():
    info = tl.check_deps()
    assert "easyocr" in info
    assert "argostranslate" in info
    assert "ollama" in info
    assert "ollama_model" in info
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
    # Reset translator cache so real path is exercised after other tests
    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None
    out = tl.translate_image_file(src)
    assert out.is_file()
    assert out != src
    assert out.parent.name == tl.TRANSLATED_SUBDIR
    # Real MT: Hello should become Chinese
    sample = tl.translate_text("Hello")
    assert tl.contains_cjk(sample)


@pytest.mark.skipif(
    not tl.check_deps().get("argostranslate"),
    reason="argos not installed",
)
def test_real_translate_text_hello_returns_cjk():
    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None
    out = tl.translate_text("Hello")
    assert tl.contains_cjk(out), f"expected Chinese, got {out!r}"
    # Second call must still work (callable cache regression)
    out2 = tl.translate_text("World")
    assert tl.contains_cjk(out2), f"expected Chinese on 2nd call, got {out2!r}"
    status = tl.mt_status()
    assert status.startswith("MT backend=ollama") or status.startswith("MT backend=argos"), status
    assert "callable_ready=True" in tl.mt_status()


def test_missing_pip_packages_reports_when_unavailable():
    from unittest.mock import patch

    with patch.object(tl, "_module_importable", return_value=False):
        missing = tl.missing_pip_packages()
    assert ("easyocr", "easyocr>=1.7.0") in missing
    assert ("argostranslate", "argostranslate>=1.9.0") in missing


def test_ensure_deps_skips_pip_when_present():
    from unittest.mock import patch

    calls = []
    with patch.object(tl, "missing_pip_packages", return_value=[]), patch(
        "subprocess.run"
    ) as run:
        info = tl.ensure_deps(progress_callback=calls.append)
    run.assert_not_called()
    assert "easyocr" in info
    assert calls


def test_ensure_deps_runs_pip_for_missing():
    from unittest.mock import MagicMock, patch

    msgs = []
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Successfully installed easyocr"
    fake.stderr = ""
    seq = iter(
        [
            [("easyocr", "easyocr>=1.7.0")],
            [],
        ]
    )
    with patch.object(
        tl, "missing_pip_packages", side_effect=lambda packages=None: next(seq)
    ), patch("subprocess.run", return_value=fake) as run, patch.object(
        tl,
        "check_deps",
        return_value={"easyocr": True, "argostranslate": True},
    ):
        info = tl.ensure_deps(progress_callback=msgs.append)
    run.assert_called_once()
    args = run.call_args[0][0]
    assert "-m" in args and "pip" in args and "install" in args
    assert "easyocr>=1.7.0" in args
    assert info["easyocr"] is True


def test_draw_text_in_box_glyphs_fit_inside_white_rect():
    """CJK ink must stay inside the (possibly grown) white bar — no vertical clip."""
    font_path = tl.find_cjk_font()
    # Gray background; white fill; red text for easy scanning
    img = Image.new("RGB", (240, 120), (80, 80, 80))
    # Deliberately short bar (height 16) that used to clip taller CJK glyphs
    box = (20, 50, 220, 66)
    out = tl.draw_text_in_box(
        img,
        box,
        "中文测试字形高度",
        font_path=font_path,
        padding=2,
        fill_rgba=(255, 255, 255, 255),
        text_fill=(220, 0, 0),
    )
    # Collect white and red pixels
    white_pixels = []
    red_pixels = []
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b = out.getpixel((x, y))
            if r > 240 and g > 240 and b > 240:
                white_pixels.append((x, y))
            elif r > 150 and g < 80 and b < 80:
                red_pixels.append((x, y))
    assert white_pixels, "expected a white overlay rect"
    assert red_pixels, "expected red text pixels"
    wx = [p[0] for p in white_pixels]
    wy = [p[1] for p in white_pixels]
    left, right = min(wx), max(wx)
    top, bottom = min(wy), max(wy)
    # Every red (text) pixel must lie inside the white rect bbox
    for x, y in red_pixels:
        assert left <= x <= right and top <= y <= bottom, (
            f"glyph pixel ({x},{y}) outside white rect "
            f"[{left},{top},{right},{bottom}]"
        )


def test_fit_font_size_getbbox_respects_short_box():
    """When the bar is short, font shrinks (or pad grows) so ink height fits."""
    font_path = tl.find_cjk_font()
    # Very short box height
    font, lines, size, pad = tl.fit_font_size_for_box(
        "高度测试",
        box_w=100,
        box_h=14,
        font_path=font_path,
        max_size=48,
        min_size=8,
        padding=1,
    )
    assert size >= 8
    _lines, block_w, block_h = tl.measure_text_block("高度测试", font, max(1, 100 - 2 * pad))
    # Either ink fits in box_h with pad, or pad grew enough that need_h is covered by growth
    need_h = block_h + 2 * pad
    assert need_h <= 14 or pad > 1, (
        f"expected shrink or grow-pad; size={size} pad={pad} block_h={block_h} need_h={need_h}"
    )


def test_translate_via_ollama_mock_http():
    """Optional: mock urllib so Ollama path returns Chinese without a server."""
    payload = {"response": "你好"}
    fake_resp = MagicMock()
    fake_resp.read.return_value = __import__("json").dumps(payload).encode("utf-8")
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.status = 200

    with patch("urllib.request.urlopen", return_value=fake_resp):
        out = tl.translate_via_ollama("Hello")
    assert out == "你好"
    assert tl.contains_cjk(out)


def test_get_translator_prefers_ollama_when_available():
    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None
    tl._mt_status_detail = ""

    with patch.object(
        tl, "ensure_ollama", return_value=(True, "Ollama ready")
    ), patch.object(tl, "translate_via_ollama", side_effect=lambda s: f"译:{s}"):
        fn, backend = tl._get_translator()
        assert backend == "ollama"
        assert callable(fn)
        assert fn("Hi") == "译:Hi"
        assert tl._mt_backend == "ollama"


def test_get_translator_falls_back_to_argos_with_status():
    tl._translate_fn = None
    tl._argos_translator = None
    tl._mt_backend = None
    tl._mt_status_detail = ""

    fake_translation = MagicMock()
    fake_translation.translate.side_effect = lambda s: f"阿:{s}"

    with patch.object(
        tl, "ensure_ollama", return_value=(False, "Ollama 不可用")
    ), patch.object(tl, "_ensure_argos_en_zh", return_value=fake_translation), patch.dict(
        "sys.modules", {"argostranslate": MagicMock()}
    ):
        import sys

        sys.modules["argostranslate"] = MagicMock()
        fn, backend = tl._get_translator()

    assert backend == "argos"
    assert "Argos" in tl._mt_status_detail or "Ollama" in tl._mt_status_detail
    assert fn("Hello") == "阿:Hello"
    status = tl.mt_status()
    assert "argos" in status


def test_sort_ocr_boxes_reading_order_rows():
    boxes = [
        tl.OcrBox("B", (80, 10, 120, 30), 0.9),
        tl.OcrBox("A", (10, 12, 50, 28), 0.9),
        tl.OcrBox("C", (10, 50, 90, 70), 0.9),
    ]
    ordered = tl.sort_ocr_boxes_reading_order(boxes, row_tol=10)
    assert [b.text for b in ordered] == ["A", "B", "C"]


def test_collect_english_and_join_caption():
    # Default: reading order only (no merge) — far boxes stay separate fragments
    boxes = [
        tl.OcrBox("world", (10, 120, 80, 140), 0.9),
        tl.OcrBox("Hello", (10, 5, 80, 25), 0.9),
    ]
    assert tl.collect_english_in_reading_order(boxes) == ["Hello", "world"]
    assert tl.join_english_paragraph(boxes) == "Hello world"
    assert tl.join_chinese_caption_lines(["你好", "世界"]) == "你好\n世界"
    assert tl.join_chinese_caption_lines(["整页一句"]) == "整页一句"


def test_join_english_parts_hyphen_and_spaces():
    assert tl.join_english_parts(["Hello", "world"]) == "Hello world"
    assert tl.join_english_parts(["some-", "thing", "else"]) == "something else"
    assert tl.join_english_parts(["", "  ", "Only"]) == "Only"


def test_join_english_paragraph_optional_merge_fallback():
    boxes = [
        tl.OcrBox("world", (70, 10, 130, 30), 0.8),
        tl.OcrBox("Hello", (10, 12, 60, 28), 0.9),
        tl.OcrBox("Below", (10, 120, 80, 140), 0.9),
    ]
    # Default whole-page: all fragments joined regardless of distance
    assert tl.join_english_paragraph(boxes) == "Hello world Below"
    # Optional merge still available; then join remaining
    assert tl.collect_english_in_reading_order(boxes, merge_nearby=True) == [
        "Hello world",
        "Below",
    ]
    assert tl.join_english_paragraph(boxes, merge_nearby=True) == "Hello world Below"


def test_merge_nearby_ocr_boxes_same_line():
    boxes = [
        tl.OcrBox("world", (70, 10, 130, 30), 0.8),
        tl.OcrBox("Hello", (10, 12, 60, 28), 0.9),
    ]
    merged = tl.merge_nearby_ocr_boxes(boxes)
    assert len(merged) == 1
    assert merged[0].text == "Hello world"
    assert merged[0].box == (10, 10, 130, 30)
    assert merged[0].confidence == 0.8


def test_merge_nearby_ocr_boxes_vertical_bubble():
    boxes = [
        tl.OcrBox("line two", (12, 36, 100, 52), 0.85),
        tl.OcrBox("line one", (10, 10, 98, 28), 0.9),
    ]
    merged = tl.merge_nearby_ocr_boxes(boxes)
    assert len(merged) == 1
    assert merged[0].text == "line one line two"
    left, top, right, bottom = merged[0].box
    assert left == 10 and top == 10 and right == 100 and bottom == 52


def test_merge_nearby_ocr_boxes_keeps_distant_separate():
    boxes = [
        tl.OcrBox("Left", (10, 10, 50, 28), 0.9),
        tl.OcrBox("Right", (200, 12, 260, 30), 0.9),  # large h gap, same line
        tl.OcrBox("Below", (10, 120, 80, 140), 0.9),  # large v gap
    ]
    merged = tl.merge_nearby_ocr_boxes(boxes)
    assert [b.text for b in merged] == ["Left", "Right", "Below"]


def test_merge_nearby_ocr_boxes_hyphen_linebreak():
    boxes = [
        tl.OcrBox("some-", (10, 10, 60, 26), 0.9),
        tl.OcrBox("thing", (12, 30, 70, 46), 0.9),
    ]
    merged = tl.merge_nearby_ocr_boxes(boxes)
    assert len(merged) == 1
    assert merged[0].text == "something"


def test_merge_nearby_ocr_boxes_empty_and_reading_order():
    assert tl.merge_nearby_ocr_boxes([]) == []
    boxes = [
        tl.OcrBox("C", (10, 80, 40, 96), 0.9),
        tl.OcrBox("B", (55, 12, 90, 28), 0.9),
        tl.OcrBox("A", (10, 10, 45, 26), 0.9),
    ]
    # A+B same line close; C far below
    merged = tl.merge_nearby_ocr_boxes(boxes, row_tol=10)
    assert [b.text for b in merged] == ["A B", "C"]


def test_append_caption_band_taller_and_preserves_top():
    img = Image.new("RGB", (160, 60), (40, 50, 60))
    img.putpixel((2, 2), (1, 2, 3))
    out = tl.append_caption_band(img, "\u4e00\u884c\u4e2d\u6587\u6807\u6ce8" * 3)
    assert out.size[0] == 160
    assert out.size[1] > 60
    assert out.getpixel((2, 2)) == (1, 2, 3)
    # Original bottom row unchanged at y=59
    assert out.getpixel((80, 59)) == (40, 50, 60)
    # Caption band is light
    r, g, b = out.getpixel((80, 70))
    assert r > 200 and g > 200 and b > 200


def test_append_caption_band_empty_returns_same_size():
    img = Image.new("RGB", (50, 30), (9, 9, 9))
    out = tl.append_caption_band(img, "   ")
    assert out.size == img.size
    assert out.getpixel((0, 0)) == (9, 9, 9)


def test_translate_image_file_default_is_caption_not_overlay(tmp_path: Path):
    src = tmp_path / "panel.png"
    Image.new("RGB", (120, 50), (90, 90, 90)).save(src)
    # Distant boxes still joined into ONE paragraph → ONE MT call (whole-page)
    boxes = [
        tl.OcrBox("One", (5, 5, 50, 20), 0.95),
        tl.OcrBox("Two", (5, 80, 50, 95), 0.95),  # far below — merge would keep separate
    ]
    calls = []

    def fake_tr(t):
        calls.append(t)
        return {"One Two": "一二", "One": "一", "Two": "二"}.get(t, t)

    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", side_effect=fake_tr
    ), patch.object(tl, "_get_translator", return_value=(fake_tr, "mock")), patch.object(
        tl, "draw_text_in_box", side_effect=AssertionError("overlay must not run by default")
    ):
        out = tl.translate_image_file(src)

    assert calls == ["One Two"]
    with Image.open(out) as im:
        assert im.size[1] > 50
        # Top-left of original region still gray (not whitened by overlay)
        assert im.getpixel((2, 2)) == (90, 90, 90)


def test_translate_image_file_merge_nearby_optional(tmp_path: Path):
    src = tmp_path / "panel2.png"
    Image.new("RGB", (120, 50), (90, 90, 90)).save(src)
    boxes = [
        tl.OcrBox("One", (5, 5, 50, 20), 0.95),
        tl.OcrBox("Two", (5, 25, 50, 40), 0.95),
    ]
    calls = []

    def fake_tr(t):
        calls.append(t)
        return "合并译文"

    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", side_effect=fake_tr
    ), patch.object(tl, "_get_translator", return_value=(fake_tr, "mock")):
        tl.translate_image_file(src, merge_nearby=True)

    assert calls == ["One Two"]


def test_set_ollama_model_updates_and_resets_cache():
    prev = tl.get_ollama_model()
    tl._translate_fn = lambda t: t  # type: ignore
    tl._mt_backend = "ollama"
    try:
        assert tl.set_ollama_model("qwen2.5:7b") == "qwen2.5:7b"
        assert tl.get_ollama_model() == "qwen2.5:7b"
        assert tl._translate_fn is None
        assert tl._mt_backend is None
    finally:
        tl.set_ollama_model(prev)


def test_pick_default_ollama_model_prefers_14b():
    assert tl.pick_default_ollama_model(["llama3:8b", "qwen2.5:14b"]) == "qwen2.5:14b"
    assert tl.pick_default_ollama_model(["qwen2.5:7b", "llama3:8b"]) == "qwen2.5:7b"
    assert tl.pick_default_ollama_model([]) == "qwen2.5:14b"



def test_translate_image_file_overlay_mode_still_available(tmp_path: Path):
    src = tmp_path / "old.jpg"
    Image.new("RGB", (100, 40), (10, 20, 30)).save(src, format="JPEG")
    boxes = [tl.OcrBox("Hi", (5, 5, 90, 30), 0.9)]
    zh = "\u4f60\u597d"
    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", return_value=zh
    ), patch.object(tl, "_get_translator", return_value=(lambda t: zh, "mock")):
        out = tl.translate_image_file(src, render_mode="overlay")
    with Image.open(out) as im:
        # Overlay keeps same dimensions
        assert im.size == (100, 40)

