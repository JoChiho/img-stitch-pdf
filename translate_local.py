#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local EN→ZH image translation pipeline (OCR → MT → overlay).

v1 is intentionally offline-first:
  - OCR: EasyOCR (lazy import; first run downloads models)
  - MT: Argos Translate en→zh (lazy; installs language pack on first use)
  - Overlay: Pillow + system CJK font (never overwrites originals)

Heavy deps are optional at import time so unit tests for helpers can run
without torch / argos packages installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]  # (current, total, message)

# ---------------------------------------------------------------------------
# Naming / path helpers (no heavy deps)
# ---------------------------------------------------------------------------

TRANSLATED_SUFFIX = "_zh"
# Subfolder under the source image's directory (not siblings in the same folder).
TRANSLATED_SUBDIR = "translated_zh"


def translated_output_path(src: Path) -> Path:
    """Return path under a NEW ``translated_zh/`` subfolder (not a sibling).

    Examples::
        foo.jpg                  -> translated_zh/foo_zh.jpg
        a/b/c.JPEG               -> a/b/translated_zh/c_zh.jpeg
        a/translated_zh/x_zh.png -> a/translated_zh/x_zh.png  (idempotent)
    """
    src = Path(src)
    stem = src.stem
    suffix = src.suffix.lower() or src.suffix
    if stem.endswith(TRANSLATED_SUFFIX):
        out_name = f"{stem}{suffix}"
    else:
        out_name = f"{stem}{TRANSLATED_SUFFIX}{suffix}"

    parent = src.parent
    if parent.name == TRANSLATED_SUBDIR:
        return parent / out_name
    return parent / TRANSLATED_SUBDIR / out_name


def translated_sibling_path(src: Path) -> Path:
    """Deprecated alias for :func:`translated_output_path` (now uses a subfolder)."""
    return translated_output_path(src)


def is_already_translated_name(path: Path) -> bool:
    """True if path looks like a prior translation output (suffix or subfolder)."""
    path = Path(path)
    if path.stem.endswith(TRANSLATED_SUFFIX):
        return True
    if path.parent.name == TRANSLATED_SUBDIR:
        return True
    return False


def contains_cjk(text: str) -> bool:
    """Return True if ``text`` contains any CJK Unified Ideograph."""
    return any("一" <= ch <= "鿿" for ch in (text or ""))


# ---------------------------------------------------------------------------
# Font / text fitting helpers (Pillow only)
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)


def find_cjk_font(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate a system font that can render Chinese characters."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    env = os.environ.get("IMG_STITCH_CJK_FONT")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    for cand in _FONT_CANDIDATES:
        p = Path(cand)
        if p.is_file():
            return p
    return None


def _load_font(size: int, font_path: Optional[Path] = None) -> ImageFont.ImageFont:
    path = font_path or find_cjk_font()
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            logger.warning("Failed to load font %s; falling back to default", path)
    return ImageFont.load_default()


def wrap_text_to_width(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    draw: Optional[ImageDraw.ImageDraw] = None,
) -> List[str]:
    """Greedy wrap so each line fits ``max_width`` (CJK-friendly: char by char)."""
    text = (text or "").strip()
    if not text:
        return []
    if max_width <= 0:
        return [text]

    def _w(s: str) -> float:
        if draw is not None:
            bbox = draw.textbbox((0, 0), s, font=font)
            return float(bbox[2] - bbox[0])
        # Approximate without a Draw context
        try:
            bbox = font.getbbox(s)
            return float(bbox[2] - bbox[0])
        except Exception:
            return float(len(s) * max(getattr(font, "size", 12), 8))

    lines: List[str] = []
    # Prefer wrapping on spaces for Latin; otherwise character-wise for CJK.
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for para in paragraphs:
        if not para:
            lines.append("")
            continue
        current = ""
        for ch in para:
            trial = current + ch
            if current and _w(trial) > max_width:
                lines.append(current)
                current = ch
            else:
                current = trial
        if current:
            lines.append(current)
    return lines or [text]


def fit_font_size_for_box(
    text: str,
    box_w: int,
    box_h: int,
    *,
    font_path: Optional[Path] = None,
    max_size: int = 64,
    min_size: int = 8,
    padding: int = 2,
    line_spacing: float = 1.15,
) -> Tuple[ImageFont.ImageFont, List[str], int]:
    """Binary-search a font size so wrapped text fits inside the box.

    Returns ``(font, lines, chosen_size)``.
    """
    box_w = max(1, int(box_w) - 2 * padding)
    box_h = max(1, int(box_h) - 2 * padding)
    text = (text or "").strip() or " "
    # Dummy image for accurate measuring
    probe = Image.new("RGB", (max(box_w, 8), max(box_h, 8)))
    draw = ImageDraw.Draw(probe)

    lo, hi = min_size, max(min_size, max_size)
    best_font = _load_font(min_size, font_path)
    best_lines = wrap_text_to_width(text, best_font, box_w, draw)
    best_size = min_size

    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid, font_path)
        lines = wrap_text_to_width(text, font, box_w, draw)
        # Measure total height
        total_h = 0
        max_line_w = 0
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line or " ", font=font)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
            max_line_w = max(max_line_w, lw)
            total_h += lh
            if i:
                total_h += int(lh * (line_spacing - 1.0))
        fits = max_line_w <= box_w and total_h <= box_h
        if fits:
            best_font, best_lines, best_size = font, lines, mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best_font, best_lines, best_size


def draw_text_in_box(
    img: Image.Image,
    box: Tuple[int, int, int, int],
    text: str,
    *,
    font_path: Optional[Path] = None,
    fill_rgba: Tuple[int, int, int, int] = (255, 255, 255, 230),
    text_fill: Tuple[int, int, int] = (0, 0, 0),
    padding: int = 2,
) -> Image.Image:
    """Paint a semi-opaque rectangle over ``box`` and draw fitted Chinese text.

    ``box`` is ``(left, top, right, bottom)`` in image coordinates.
    Returns a new RGB image (does not mutate the original if mode differs).
    """
    left, top, right, bottom = (int(v) for v in box)
    if right <= left or bottom <= top:
        return img.convert("RGB") if img.mode != "RGB" else img.copy()

    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rectangle([left, top, right, bottom], fill=fill_rgba)

    box_w, box_h = right - left, bottom - top
    font, lines, _size = fit_font_size_for_box(
        text, box_w, box_h, font_path=font_path, padding=padding
    )

    # Composite fill first, then draw text on the result
    composed = Image.alpha_composite(base, overlay).convert("RGB")
    draw = ImageDraw.Draw(composed)

    # Vertical centering of the block
    line_heights: List[int] = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line or " ", font=font)
        line_heights.append(bbox[3] - bbox[1])
    spacing = max(0, int((line_heights[0] if line_heights else 12) * 0.15))
    total_h = sum(line_heights) + spacing * max(0, len(lines) - 1)
    y = top + max(padding, (box_h - total_h) // 2)

    for line, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line or " ", font=font)
        lw = bbox[2] - bbox[0]
        x = left + max(padding, (box_w - lw) // 2)
        draw.text((x, y), line, font=font, fill=text_fill)
        y += lh + spacing

    return composed


# ---------------------------------------------------------------------------
# OCR / MT (lazy)
# ---------------------------------------------------------------------------

@dataclass
class OcrBox:
    """One detected text region."""

    text: str
    box: Tuple[int, int, int, int]  # left, top, right, bottom
    confidence: float = 1.0


_easyocr_reader = None
_argos_translator = None  # Argos Translation object (debug / status only)
_translate_fn = None  # cached callable: str -> str
_mt_backend: Optional[str] = None  # "argos" | "deep_translator" | None


def check_deps() -> dict:
    """Return availability of optional heavy packages (no downloads)."""
    info = {
        "easyocr": False,
        "argostranslate": False,
        "deep_translator": False,
        "cjk_font": find_cjk_font() is not None,
    }
    try:
        import easyocr  # noqa: F401

        info["easyocr"] = True
    except ImportError:
        pass
    try:
        import argostranslate  # noqa: F401

        info["argostranslate"] = True
    except ImportError:
        pass
    try:
        import deep_translator  # noqa: F401

        info["deep_translator"] = True
    except ImportError:
        pass
    return info



REQUIRED_PIP_SPECS = (
    ("easyocr", "easyocr>=1.7.0"),
    ("argostranslate", "argostranslate>=1.9.0"),
)


def _module_importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def missing_pip_packages(
    packages: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    """Return ``(import_name, pip_spec)`` pairs that fail to import."""
    pkgs = list(packages) if packages is not None else list(REQUIRED_PIP_SPECS)
    return [(name, spec) for name, spec in pkgs if not _module_importable(name)]


def ensure_deps(
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    packages: Optional[Sequence[Tuple[str, str]]] = None,
) -> dict:
    """Ensure translation deps exist in *this* interpreter (``sys.executable``).

    Missing packages are installed automatically via::

        python -m pip install <spec>

    No manual pip required. Safe to call from a background thread.
    Returns the same shape as :func:`check_deps` after install attempts.
    """
    def _msg(m: str) -> None:
        logger.info("%s", m)
        if progress_callback:
            progress_callback(m)

    missing = missing_pip_packages(packages)
    if not missing:
        _msg("翻译依赖已就绪。")
        return check_deps()

    specs = [spec for _name, spec in missing]
    names = ", ".join(name for name, _spec in missing)
    py = sys.executable
    _msg(f"正在自动安装缺失依赖（{names}）到:\n{py}")

    cmd = [
        py,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *specs,
    ]
    _msg("执行: " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as e:
        raise RuntimeError(
            f"无法启动 pip（解释器: {py}）：{e}\n"
            f"请确认该 Python 可运行，或手动执行:\n"
            f"  \"{py}\" -m pip install {' '.join(specs)}"
        ) from e

    tail = (proc.stdout or "")[-800:] + "\n" + (proc.stderr or "")[-800:]
    if proc.returncode != 0:
        raise RuntimeError(
            f"自动安装依赖失败（exit {proc.returncode}）。\n"
            f"解释器: {py}\n"
            f"命令: {' '.join(cmd)}\n"
            f"输出片段:\n{tail.strip()}"
        )

    still = missing_pip_packages(packages)
    if still:
        still_names = ", ".join(n for n, _ in still)
        raise RuntimeError(
            f"pip 已运行但仍无法导入: {still_names}\n"
            f"解释器: {py}\n"
            f"请重启应用后再试。输出片段:\n{tail.strip()}"
        )

    _msg(f"依赖安装完成: {names}")
    return check_deps()



def _get_easyocr_reader(languages: Optional[Sequence[str]] = None):
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    try:
        import easyocr
    except ImportError as e:
        raise ImportError(
            "本地 OCR 需要 easyocr（应用应已自动安装）。若仍失败，请重启应用。\n"
            "首次运行会下载检测/识别模型（可能较大），请保持网络畅通。"
        ) from e
    langs = list(languages) if languages else ["en"]
    # gpu=False for broader Windows CPU compatibility
    _easyocr_reader = easyocr.Reader(langs, gpu=False, verbose=False)
    return _easyocr_reader


def ocr_image(path: Path, *, min_confidence: float = 0.3) -> List[OcrBox]:
    """Run EasyOCR on an image; return axis-aligned boxes with English text."""
    reader = _get_easyocr_reader(["en"])
    path = Path(path)
    # detail=1 → (bbox, text, conf); paragraph=False keeps per-line boxes
    raw = reader.readtext(str(path), detail=1, paragraph=False)
    results: List[OcrBox] = []
    for item in raw:
        if len(item) < 3:
            continue
        bbox, text, conf = item[0], item[1], float(item[2])
        text = (text or "").strip()
        if not text or conf < min_confidence:
            continue
        xs = [float(p[0]) for p in bbox]
        ys = [float(p[1]) for p in bbox]
        left, right = int(min(xs)), int(max(xs))
        top, bottom = int(min(ys)), int(max(ys))
        if right - left < 2 or bottom - top < 2:
            continue
        results.append(OcrBox(text=text, box=(left, top, right, bottom), confidence=conf))
    return results


def _ensure_argos_en_zh():
    """Install Argos en→zh package if missing (network on first run)."""
    from argostranslate import package, translate

    installed = translate.get_installed_languages()
    from_lang = next((l for l in installed if l.code == "en"), None)
    to_lang = next((l for l in installed if l.code == "zh"), None)
    if from_lang is not None and to_lang is not None:
        t = from_lang.get_translation(to_lang)
        if t is not None:
            return t

    package.update_package_index()
    available = package.get_available_packages()
    pkg = next(
        (p for p in available if p.from_code == "en" and p.to_code == "zh"),
        None,
    )
    if pkg is None:
        raise RuntimeError("Argos Translate 未找到 en→zh 语言包。")
    package.install_from_path(pkg.download())
    installed = translate.get_installed_languages()
    from_lang = next(l for l in installed if l.code == "en")
    to_lang = next(l for l in installed if l.code == "zh")
    t = from_lang.get_translation(to_lang)
    if t is None:
        raise RuntimeError("Argos Translate en→zh 安装后仍无法创建翻译器。")
    return t


def _get_translator():
    """Prefer Argos (local); fall back to deep_translator Google only if Argos unavailable.

    Always caches and returns a *callable* ``(text) -> str``.  Previously the
    Argos ``CachedTranslation`` object was stored in ``_argos_translator`` and
    returned on subsequent calls; that object is not callable, so every translate
    after the first (or after ``preload_models``) raised TypeError, was swallowed,
    and painted the original English back onto the image.
    """
    global _argos_translator, _translate_fn, _mt_backend
    if _translate_fn is not None:
        return _translate_fn, _mt_backend

    try:
        import argostranslate  # noqa: F401

        translator = _ensure_argos_en_zh()
        _argos_translator = translator
        _mt_backend = "argos"

        def _argos_fn(text: str) -> str:
            return translator.translate(text)

        _translate_fn = _argos_fn
        logger.info("MT backend ready: argos en->zh (callable cached)")
        return _translate_fn, _mt_backend
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Argos Translate failed (%s); trying deep_translator fallback", e)

    try:
        from deep_translator import GoogleTranslator

        gt = GoogleTranslator(source="en", target="zh-CN")
        _mt_backend = "deep_translator"

        def _google_fn(text: str) -> str:
            return gt.translate(text)

        _translate_fn = _google_fn
        logger.info("MT backend ready: deep_translator Google en->zh-CN")
        return _translate_fn, _mt_backend
    except ImportError as e:
        raise ImportError(
            "本地翻译需要 argostranslate（应用应已自动安装）。\n"
            "若仍失败请重启应用。首次运行会下载 en→zh 语言模型。\n"
            "若本地安装失败，可额外安装 deep_translator 作为联网后备。"
        ) from e


def translate_en_to_zh(text: str) -> str:
    """Translate a single English string to Chinese (Argos / fallback)."""
    text = (text or "").strip()
    if not text:
        return text
    fn, backend = _get_translator()
    if not callable(fn):
        logger.error(
            "Translator is not callable (backend=%s, type=%s); returning original",
            backend,
            type(fn).__name__,
        )
        return text
    try:
        out = fn(text)
        out = (out or "").strip() or text
        if out == text:
            logger.warning(
                "Translate returned unchanged text %r (backend=%s)",
                text[:60],
                backend,
            )
        else:
            logger.info(
                "Translated (%s): %r -> %r",
                backend,
                text[:60],
                out[:60],
            )
        return out
    except Exception as e:
        logger.exception(
            "Translate failed for %r (backend=%s): %s", text[:60], backend, e
        )
        return text


def translate_text(text: str) -> str:
    """Public alias: translate English text to Chinese."""
    return translate_en_to_zh(text)


def mt_status() -> str:
    """Short status string for logging / UI (backend + whether callable is ready)."""
    ready = _translate_fn is not None and callable(_translate_fn)
    return f"MT backend={_mt_backend or 'none'}; callable_ready={ready}"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def translate_image_file(
    src: Path,
    *,
    dst: Optional[Path] = None,
    font_path: Optional[Path] = None,
    min_confidence: float = 0.3,
) -> Path:
    """OCR -> translate -> overlay -> save under ``translated_zh/``. Never overwrites ``src``."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    out = Path(dst) if dst is not None else translated_output_path(src)
    if out.resolve() == src.resolve():
        # Safety: if somehow same path, force a distinct name under subfolder
        out = src.parent / TRANSLATED_SUBDIR / f"{src.stem}_zh_out{src.suffix.lower()}"

    boxes = ocr_image(src, min_confidence=min_confidence)
    # Ensure translator is ready before the loop so status/logging is accurate
    _get_translator()
    logger.info(
        "Translating %s: %d OCR box(es); %s; out=%s",
        src.name,
        len(boxes),
        mt_status(),
        out,
    )
    translated_boxes = 0
    with Image.open(src) as im:
        im.load()
        canvas = im.convert("RGB")
        for ob in boxes:
            zh = translate_en_to_zh(ob.text)
            if not zh:
                continue
            if contains_cjk(zh) or zh != ob.text:
                translated_boxes += 1
            canvas = draw_text_in_box(canvas, ob.box, zh, font_path=font_path)

        out.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Saved %s (%d/%d boxes changed from source text); %s",
            out,
            translated_boxes,
            len(boxes),
            mt_status(),
        )
        suf = out.suffix.lower()
        save_kw = {}
        fmt = None
        if suf in (".jpg", ".jpeg"):
            fmt = "JPEG"
            save_kw["quality"] = 95
            if canvas.mode != "RGB":
                canvas = canvas.convert("RGB")
        elif suf == ".png":
            fmt = "PNG"
        elif suf == ".webp":
            fmt = "WEBP"
            save_kw["quality"] = 95
        elif suf in (".tif", ".tiff"):
            fmt = "TIFF"
        elif suf == ".bmp":
            fmt = "BMP"
        canvas.save(out, format=fmt, **save_kw)

    return out


def translate_image_paths(
    paths: Sequence[Path],
    *,
    progress_callback: Optional[ProgressCallback] = None,
    skip_non_images: bool = True,
) -> Tuple[List[Path], List[Path], List[str]]:
    """Translate each image into ``<parent>/translated_zh/*_zh.*``.

    Returns ``(translated_paths, skipped_pdfs, warnings)``.
    GIFs, non-images, and already-translated outputs are skipped; PDFs go in
    ``skipped_pdfs``.
    """
    paths = [Path(p) for p in paths]
    translated: List[Path] = []
    skipped_pdfs: List[Path] = []
    warnings: List[str] = []

    image_paths: List[Path] = []
    for p in paths:
        suf = p.suffix.lower()
        if suf == ".pdf":
            skipped_pdfs.append(p)
            continue
        if suf == ".gif":
            warnings.append(f"已跳过 GIF: {p.name}")
            continue
        if is_already_translated_name(p):
            warnings.append(f"已跳过译图/已有后缀: {p.name}")
            continue
        if suf not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
            if skip_non_images:
                warnings.append(f"已跳过非图片: {p.name}")
                continue
        image_paths.append(p)

    if skipped_pdfs:
        warnings.append(
            f"翻译模式跳过 {len(skipped_pdfs)} 个 PDF（仅处理图片）。"
        )

    n = len(image_paths)
    for i, src in enumerate(image_paths, start=1):
        if progress_callback is not None:
            progress_callback(i, n, f"正在翻译 {src.name}")
        try:
            out = translate_image_file(src)
            translated.append(out)
        except Exception as e:
            warnings.append(f"{src.name}: {e}")
            logger.exception("Failed translating %s", src)

    return translated, skipped_pdfs, warnings


def preload_models(progress_callback: Optional[Callable[[str], None]] = None) -> str:
    """Eagerly load OCR + MT so the first real image is faster.

    Safe to call on a background thread. Returns a short status string.
    Auto-installs missing pip packages into ``sys.executable`` first.
    """
    def _msg(m: str) -> None:
        if progress_callback:
            progress_callback(m)

    ensure_deps(progress_callback=progress_callback)
    _msg("正在加载 EasyOCR 模型（首次会下载）…")
    _get_easyocr_reader(["en"])
    _msg("正在准备 Argos Translate en→zh（首次会下载语言包）…")
    _get_translator()
    backend = _mt_backend or "unknown"
    if _translate_fn is None or not callable(_translate_fn):
        raise RuntimeError(
            f"翻译器未就绪（backend={backend}, fn={type(_translate_fn).__name__}）。"
            "请检查 Argos en→zh 语言包是否已安装。"
        )
    # Smoke-check: one word must become Chinese, proving the callable path works
    sample = translate_text("Hello")
    if not contains_cjk(sample):
        raise RuntimeError(
            f"翻译冒烟失败：translate_text('Hello') -> {sample!r}（期望含中文）。"
            f"{mt_status()}"
        )
    _msg(f"翻译冒烟通过：Hello → {sample}（{mt_status()}）")
    font = find_cjk_font()
    font_note = str(font) if font else "未找到 CJK 字体（中文可能显示为方框）"
    return f"就绪（OCR=easyocr, MT={backend}, font={font_note}）"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python translate_local.py <image> [more images…]")
        print("Deps:", check_deps())
        raise SystemExit(2)
    for arg in sys.argv[1:]:
        out = translate_image_file(Path(arg))
        print(out)
