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
    Image.new("RGB", (100, 40), (200, 200, 200)).save(src, format="JPEG")

    boxes = [tl.OcrBox(text="Hello", box=(5, 5, 90, 30), confidence=0.9)]

    with patch.object(tl, "ocr_image", return_value=boxes), patch.object(
        tl, "translate_en_to_zh", return_value="你好"
    ), patch.object(tl, "_get_translator", return_value=(lambda t: "你好", "mock")):
        out = tl.translate_image_file(src)

    expected = tmp_path / "translated_zh" / "hello_zh.jpg"
    assert out == expected
    assert out.is_file()
    assert out.parent.name == tl.TRANSLATED_SUBDIR
    assert src.is_file()  # original preserved
    assert src.read_bytes() != out.read_bytes()
    # Must NOT create sibling in the same folder
    assert not (tmp_path / "hello_zh.jpg").exists()


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


