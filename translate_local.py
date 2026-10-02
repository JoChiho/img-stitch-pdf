#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local EN→ZH image translation pipeline (OCR → MT → caption band).

Offline-first translation for screenshot / comic panels:
  - OCR: PaddleOCR EN by default (lazy import; first run downloads models),
    with EasyOCR fallback if Paddle fails. Select via ``ocr_engine`` /
    ``IMG_STITCH_OCR_ENGINE`` (``paddle`` | ``easyocr``).
    Optional OCR downscale (``ocr_downscale`` / max long-side) shrinks large
    images before Paddle/EasyOCR; box coords are scaled back to original size.
  - MT: Ollama HTTP API (preferred, e.g. qwen2.5) with Argos en→zh fallback
  - Caption (default): after OCR reading-order, join ALL English into ONE
    paragraph (spaces), translate once via Ollama/Argos, then append that
    single Chinese translation as a light band BELOW the original image.
    Nearby-box merge remains an optional fallback (``merge_nearby=True``).
    Does NOT paint over original text boxes (overlay helpers remain for tests
    only and are not used by the default pipeline).
  - OCR text export/import: UTF-8 .txt with machine-stable
    ``===PAGE NNN===`` keys for external translation workflows.

Heavy deps are optional at import time so unit tests for helpers can run
without paddle / torch / argos / a running Ollama server.
"""

from __future__ import annotations

import json
import re
import logging
import os
import subprocess
import sys
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]  # (current, total, message)

CancelCheck = Callable[[], bool]

class CancelledError(RuntimeError):
    """Raised when the user cancels OCR/translate/PDF work."""

    def __init__(self, message: str = "已取消") -> None:
        super().__init__(message)


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


def _font_getbbox(
    font: ImageFont.ImageFont, text: str
) -> Tuple[int, int, int, int]:
    """Ink bounding box via ``font.getbbox`` (left, top, right, bottom).

    Falls back to ``textbbox`` / crude metrics if getbbox is unavailable.
    """
    s = text if text else " "
    try:
        bbox = font.getbbox(s)
        if bbox is not None:
            return int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    except Exception:
        pass
    try:
        probe = Image.new("RGB", (8, 8))
        draw = ImageDraw.Draw(probe)
        bbox = draw.textbbox((0, 0), s, font=font)
        return int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    except Exception:
        size = int(getattr(font, "size", 12) or 12)
        return 0, 0, max(1, len(s) * size), size


def _line_ink_size(font: ImageFont.ImageFont, text: str) -> Tuple[int, int]:
    """Return ``(width, height)`` of glyph ink for one line."""
    left, top, right, bottom = _font_getbbox(font, text or " ")
    return max(1, right - left), max(1, bottom - top)


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
        # Prefer font.getbbox so metrics match draw_text_in_box fitting.
        try:
            left, _top, right, _bottom = _font_getbbox(font, s)
            return float(right - left)
        except Exception:
            pass
        if draw is not None:
            bbox = draw.textbbox((0, 0), s, font=font)
            return float(bbox[2] - bbox[0])
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


def measure_text_block(
    text: str,
    font: ImageFont.ImageFont,
    box_w: int,
    *,
    line_spacing: float = 1.15,
) -> Tuple[List[str], int, int]:
    """Wrap ``text`` and return ``(lines, block_width, block_height)`` using getbbox."""
    inner_w = max(1, int(box_w))
    lines = wrap_text_to_width(text, font, inner_w)
    if not lines:
        lines = [" "]
    max_line_w = 0
    total_h = 0
    line_heights: List[int] = []
    for line in lines:
        lw, lh = _line_ink_size(font, line or " ")
        max_line_w = max(max_line_w, lw)
        line_heights.append(lh)
    for i, lh in enumerate(line_heights):
        total_h += lh
        if i:
            total_h += int(lh * (line_spacing - 1.0))
    return lines, max_line_w, max(1, total_h)


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
) -> Tuple[ImageFont.ImageFont, List[str], int, int]:
    """Binary-search a font size so wrapped text ink fits inside the box.

    Uses ``font.getbbox`` for width/height so CJK ascent/descent is counted.
    Returns ``(font, lines, chosen_size, effective_padding)``.
    If even ``min_size`` overflows, ``effective_padding`` may be raised so the
    caller can grow the white rectangle instead of clipping glyphs.
    """
    pad = max(0, int(padding))
    inner_w = max(1, int(box_w) - 2 * pad)
    inner_h = max(1, int(box_h) - 2 * pad)
    text = (text or "").strip() or " "

    lo, hi = min_size, max(min_size, max_size)
    best_font = _load_font(min_size, font_path)
    best_lines, _bw, _bh = measure_text_block(
        text, best_font, inner_w, line_spacing=line_spacing
    )
    best_size = min_size

    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid, font_path)
        lines, max_line_w, total_h = measure_text_block(
            text, font, inner_w, line_spacing=line_spacing
        )
        fits = max_line_w <= inner_w and total_h <= inner_h
        if fits:
            best_font, best_lines, best_size = font, lines, mid
            lo = mid + 1
        else:
            hi = mid - 1

    # If min size still overflows, grow padding so caller expands the white rect.
    lines, max_line_w, total_h = measure_text_block(
        text, best_font, max(1, int(box_w) - 2 * pad), line_spacing=line_spacing
    )
    need_w = max_line_w + 2 * pad
    need_h = total_h + 2 * pad
    extra_w = max(0, need_w - int(box_w))
    extra_h = max(0, need_h - int(box_h))
    eff_pad = pad
    if extra_w or extra_h:
        # Grow pad so white rect covers full ink (+ original pad).
        eff_pad = pad + max((extra_w + 1) // 2, (extra_h + 1) // 2, 1)
        best_lines = lines

    return best_font, best_lines, best_size, eff_pad


def draw_text_in_box(
    img: Image.Image,
    box: Tuple[int, int, int, int],
    text: str,
    *,
    font_path: Optional[Path] = None,
    fill_rgba: Tuple[int, int, int, int] = (255, 255, 255, 230),
    text_fill: Tuple[int, int, int] = (0, 0, 0),
    padding: int = 2,
    line_spacing: float = 1.15,
) -> Image.Image:
    """Paint a semi-opaque rectangle over ``box`` and draw fitted Chinese text.

    ``box`` is ``(left, top, right, bottom)`` in image coordinates.
    Glyphs are measured with ``font.getbbox`` and drawn with ``anchor="lt"`` so
    the full ink sits inside the white rect. If the bar is shorter than the
    font, the rect is grown (pad) rather than clipping tops/bottoms.
    Returns a new RGB image (does not mutate the original if mode differs).
    """
    left, top, right, bottom = (int(v) for v in box)
    if right <= left or bottom <= top:
        return img.convert("RGB") if img.mode != "RGB" else img.copy()

    box_w, box_h = right - left, bottom - top
    font, lines, _size, eff_pad = fit_font_size_for_box(
        text,
        box_w,
        box_h,
        font_path=font_path,
        padding=padding,
        line_spacing=line_spacing,
    )

    # Re-measure at final font; expand white rect if ink needs more room.
    inner_w = max(1, box_w - 2 * eff_pad)
    lines, block_w, block_h = measure_text_block(
        text or " ", font, inner_w, line_spacing=line_spacing
    )
    need_w = block_w + 2 * eff_pad
    need_h = block_h + 2 * eff_pad
    expand_w = max(0, need_w - box_w)
    expand_h = max(0, need_h - box_h)
    if expand_w or expand_h:
        left -= expand_w // 2
        right += expand_w - expand_w // 2
        top -= expand_h // 2
        bottom += expand_h - expand_h // 2
        box_w, box_h = right - left, bottom - top

    # Clamp expanded rect to image bounds (shift inward if needed).
    img_w, img_h = img.size
    if right - left > img_w:
        left, right = 0, img_w
    else:
        if left < 0:
            right -= left
            left = 0
        if right > img_w:
            left -= right - img_w
            right = img_w
            left = max(0, left)
    if bottom - top > img_h:
        top, bottom = 0, img_h
    else:
        if top < 0:
            bottom -= top
            top = 0
        if bottom > img_h:
            top -= bottom - img_h
            bottom = img_h
            top = max(0, top)
    box_w, box_h = right - left, bottom - top

    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rectangle([left, top, right, bottom], fill=fill_rgba)

    composed = Image.alpha_composite(base, overlay).convert("RGB")
    draw = ImageDraw.Draw(composed)

    line_heights: List[int] = []
    line_widths: List[int] = []
    for line in lines:
        lw, lh = _line_ink_size(font, line or " ")
        line_widths.append(lw)
        line_heights.append(lh)
    spacing = max(
        0, int((line_heights[0] if line_heights else 12) * (line_spacing - 1.0))
    )
    total_h = sum(line_heights) + spacing * max(0, len(lines) - 1)
    y = top + max(eff_pad, (box_h - total_h) // 2)

    for line, lw, lh in zip(lines, line_widths, line_heights):
        x = left + max(eff_pad, (box_w - lw) // 2)
        # anchor="lt": (x, y) is the top-left of the ink bbox — no vertical clip.
        try:
            draw.text((x, y), line, font=font, fill=text_fill, anchor="lt")
        except TypeError:
            # Older Pillow without anchor: offset by getbbox top/left.
            bl, bt, _br, _bb = _font_getbbox(font, line or " ")
            draw.text((x - bl, y - bt), line, font=font, fill=text_fill)
        y += lh + spacing

    return composed



# ---------------------------------------------------------------------------
# Reading order + caption band (default render; does not paint over original)
# ---------------------------------------------------------------------------

def sort_ocr_boxes_reading_order(
    boxes: Sequence[OcrBox],
    *,
    row_tol: Optional[int] = None,
) -> List[OcrBox]:
    """Sort OCR boxes top-to-bottom, then left-to-right (reading order).

    Boxes whose tops fall within ``row_tol`` pixels are treated as one row
    (sorted left-to-right). If ``row_tol`` is omitted, half the median box
    height is used (minimum 8px).
    """
    items = list(boxes)
    if not items:
        return []
    heights = [max(1, b.box[3] - b.box[1]) for b in items]
    heights_sorted = sorted(heights)
    med_h = heights_sorted[len(heights_sorted) // 2]
    tol = int(row_tol) if row_tol is not None else max(8, med_h // 2)

    # Seed rows by top Y, then sort each row by left X.
    by_top = sorted(items, key=lambda b: (b.box[1], b.box[0]))
    rows: List[List[OcrBox]] = []
    for b in by_top:
        if not rows:
            rows.append([b])
            continue
        row_top = min(x.box[1] for x in rows[-1])
        if abs(b.box[1] - row_top) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    ordered: List[OcrBox] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b.box[0]))
    return ordered


def _ocr_box_height(box: Tuple[int, int, int, int]) -> int:
    return max(1, int(box[3]) - int(box[1]))


def _ocr_box_width(box: Tuple[int, int, int, int]) -> int:
    return max(1, int(box[2]) - int(box[0]))


def _combine_ocr_boxes(a: "OcrBox", b: "OcrBox") -> "OcrBox":
    """Union geometry + join English fragments for one MT call."""
    al, at, ar, ab = (int(v) for v in a.box)
    bl, bt, br, bb = (int(v) for v in b.box)
    ta = (a.text or "").strip()
    tb = (b.text or "").strip()
    if ta.endswith("-") and tb:
        # Hyphenated line-break: "some-" + "thing" -> "something"
        text = ta[:-1] + tb
    elif ta and tb:
        text = f"{ta} {tb}"
    else:
        text = ta or tb
    return OcrBox(
        text=text,
        box=(min(al, bl), min(at, bt), max(ar, br), max(ab, bb)),
        confidence=min(float(a.confidence), float(b.confidence)),
    )


def _ocr_boxes_nearby(
    a: "OcrBox",
    b: "OcrBox",
    *,
    same_line_y_ratio: float,
    max_h_gap_ratio: float,
    max_v_gap_ratio: float,
    min_x_overlap_ratio: float,
) -> bool:
    """True if ``a`` and ``b`` belong to the same sentence / speech bubble.

    ``a`` should already be earlier in reading order than ``b``.
    - Same line: tops close and horizontal gap small (or overlapping).
    - Vertical bubble: small gap below ``a``, with enough horizontal overlap
      (or tiny x-gap) so separate columns stay separate.
    """
    al, at, ar, ab = (int(v) for v in a.box)
    bl, bt, br, bb = (int(v) for v in b.box)
    ah = _ocr_box_height(a.box)
    bh = _ocr_box_height(b.box)
    aw = _ocr_box_width(a.box)
    bw = _ocr_box_width(b.box)
    med_h = max(1.0, (ah + bh) / 2.0)
    med_w = max(1.0, (aw + bw) / 2.0)

    y_tol = max(4.0, same_line_y_ratio * min(ah, bh))
    same_line = abs(at - bt) <= y_tol or abs((at + ab) / 2.0 - (bt + bb) / 2.0) <= y_tol

    # Horizontal gap: positive = separate, negative/zero = overlap
    h_gap = float(bl - ar)
    max_h_gap = max(6.0, max_h_gap_ratio * med_h)
    if same_line and h_gap <= max_h_gap:
        # Also allow mild reverse order / overlap on the same row
        if h_gap >= -0.5 * med_w:
            return True

    # Vertical continuation (wrapped line / bubble)
    v_gap = float(bt - ab)
    max_v_gap = max(6.0, max_v_gap_ratio * med_h)
    if v_gap < -0.35 * med_h:
        # Too much upward overlap / wrong order — not a clean stack
        return False
    if v_gap > max_v_gap:
        return False

    overlap_w = float(min(ar, br) - max(al, bl))
    min_overlap = min_x_overlap_ratio * min(aw, bw)
    x_gap = float(max(0, max(al, bl) - min(ar, br)))
    if overlap_w >= min_overlap:
        return True
    # Near-aligned columns with tiny x gap still count as one bubble
    if x_gap <= max(4.0, 0.25 * med_w) and abs(((al + ar) / 2.0) - ((bl + br) / 2.0)) <= 0.55 * med_w:
        return True
    return False


def merge_nearby_ocr_boxes(
    boxes: Sequence["OcrBox"],
    *,
    same_line_y_ratio: float = 0.55,
    max_h_gap_ratio: float = 1.25,
    max_v_gap_ratio: float = 0.85,
    min_x_overlap_ratio: float = 0.25,
    row_tol: Optional[int] = None,
) -> List["OcrBox"]:
    """Merge nearby EasyOCR boxes into full sentences / bubbles.

    1. Sort top-to-bottom, left-to-right (:func:`sort_ocr_boxes_reading_order`).
    2. Greedily merge consecutive boxes that share a line (small horizontal gap)
       or continue a bubble (small vertical gap + horizontal overlap).

    Returns a new list of :class:`OcrBox` with union boxes and joined English
    text (space-separated; hyphenated line-breaks glued). Empty input → [].
    """
    ordered = sort_ocr_boxes_reading_order(boxes, row_tol=row_tol)
    if not ordered:
        return []

    merged: List[OcrBox] = [ordered[0]]
    for b in ordered[1:]:
        prev = merged[-1]
        if _ocr_boxes_nearby(
            prev,
            b,
            same_line_y_ratio=same_line_y_ratio,
            max_h_gap_ratio=max_h_gap_ratio,
            max_v_gap_ratio=max_v_gap_ratio,
            min_x_overlap_ratio=min_x_overlap_ratio,
        ):
            merged[-1] = _combine_ocr_boxes(prev, b)
        else:
            merged.append(b)
    return merged


def collect_english_in_reading_order(
    boxes: Sequence[OcrBox],
    *,
    merge_nearby: bool = False,
    row_tol: Optional[int] = None,
) -> List[str]:
    """Return non-empty OCR English strings in reading order.

    Default is **whole-page** fragments (sort only; no nearby merge). Pass
    ``merge_nearby=True`` to run :func:`merge_nearby_ocr_boxes` first as an
    optional fallback before collecting strings.
    """
    ordered = (
        merge_nearby_ocr_boxes(boxes, row_tol=row_tol)
        if merge_nearby
        else sort_ocr_boxes_reading_order(boxes, row_tol=row_tol)
    )
    out: List[str] = []
    for ob in ordered:
        t = (ob.text or "").strip()
        if t:
            out.append(t)
    return out


def join_english_parts(parts: Sequence[str]) -> str:
    """Join English fragments into one paragraph (spaces; glue hyphen breaks)."""
    result = ""
    for raw in parts:
        t = (raw or "").strip()
        if not t:
            continue
        if not result:
            result = t
        elif result.endswith("-"):
            # Hyphenated line-break: "some-" + "thing" -> "something"
            result = result[:-1] + t
        else:
            result = f"{result} {t}"
    return result


def join_english_paragraph(
    boxes: Sequence[OcrBox],
    *,
    merge_nearby: bool = False,
    row_tol: Optional[int] = None,
) -> str:
    """Sort OCR boxes in reading order and join ALL English into one paragraph.

    Default (``merge_nearby=False``): whole-page — every non-empty box text is
    joined with spaces after reading-order sort. If ``merge_nearby=True``, first
    run :func:`merge_nearby_ocr_boxes` as an optional fallback, then join.
    """
    return join_english_parts(
        collect_english_in_reading_order(
            boxes, merge_nearby=merge_nearby, row_tol=row_tol
        )
    )


def join_chinese_caption_lines(zh_lines: Sequence[str]) -> str:
    """Join translated fragments into readable Chinese caption text.

    Prefer a single whole-page translation (one string). Multiple fragments are
    still joined with newlines for backward compatibility / merge fallback.
    """
    parts = [(s or "").strip() for s in zh_lines]
    parts = [p for p in parts if p]
    return "\n".join(parts)


def append_caption_band(
    img: Image.Image,
    text: str,
    *,
    font_path: Optional[Path] = None,
    bg_rgb: Tuple[int, int, int] = (250, 250, 250),
    text_fill: Tuple[int, int, int] = (20, 20, 20),
    margin: int = 24,
    line_spacing: float = 1.35,
    font_size: Optional[int] = None,
    max_band_height_ratio: float = 3.0,
) -> Image.Image:
    """Return a taller RGB image: original on top, caption band below.

    The original pixels are pasted unchanged; Chinese (or other) caption
    text is wrapped with a CJK font onto a light band under the image.
    If ``text`` is empty, returns an RGB copy of ``img`` (same size).
    """
    base = img.convert("RGB")
    w, h = base.size
    caption = (text or "").strip()
    if not caption:
        return base.copy()

    margin = max(8, int(margin))
    # Prefer a readable size scaled to image width; shrink if band grows too tall.
    if font_size is None:
        size = max(16, min(36, w // 28))
    else:
        size = max(10, int(font_size))

    max_band_h = max(h // 4, int(h * float(max_band_height_ratio)))
    chosen_font = _load_font(size, font_path)
    lines: List[str] = []
    line_heights: List[int] = []
    band_h = 0

    for attempt_size in range(size, 9, -1):
        chosen_font = _load_font(attempt_size, font_path)
        inner_w = max(1, w - 2 * margin)
        lines = wrap_text_to_width(caption, chosen_font, inner_w)
        if not lines:
            lines = [" "]
        line_heights = []
        for line in lines:
            _lw, lh = _line_ink_size(chosen_font, line or " ")
            line_heights.append(lh)
        spacing = max(
            0,
            int((line_heights[0] if line_heights else attempt_size) * (line_spacing - 1.0)),
        )
        total_text_h = sum(line_heights) + spacing * max(0, len(lines) - 1)
        band_h = margin + total_text_h + margin
        if band_h <= max_band_h or attempt_size <= 10:
            size = attempt_size
            break

    spacing = max(
        0,
        int((line_heights[0] if line_heights else size) * (line_spacing - 1.0)),
    )

    out = Image.new("RGB", (w, h + band_h), bg_rgb)
    out.paste(base, (0, 0))
    # Subtle separator line between image and caption
    draw = ImageDraw.Draw(out)
    sep = tuple(max(0, c - 18) for c in bg_rgb)
    draw.line([(0, h), (w - 1, h)], fill=sep, width=1)

    y = h + margin
    for line, lh in zip(lines, line_heights):
        x = margin
        try:
            draw.text((x, y), line, font=chosen_font, fill=text_fill, anchor="lt")
        except TypeError:
            bl, bt, _br, _bb = _font_getbbox(chosen_font, line or " ")
            draw.text((x - bl, y - bt), line, font=chosen_font, fill=text_fill)
        y += lh + spacing
    return out



# ---------------------------------------------------------------------------
# OCR / MT (lazy)
# ---------------------------------------------------------------------------


@dataclass
class OcrBox:
    """One detected text region."""

    text: str
    box: Tuple[int, int, int, int]  # left, top, right, bottom
    confidence: float = 1.0


# PaddleOCR (default) / EasyOCR backends live in ocr_backend.py
import ocr_backend as _ocr_backend

_ocr_backend.bind_ocr_box(OcrBox)

OCR_ENGINES = _ocr_backend.OCR_ENGINES
DEFAULT_OCR_ENGINE = _ocr_backend.DEFAULT_OCR_ENGINE
PADDLE_CPU_INDEX = _ocr_backend.PADDLE_CPU_INDEX
PADDLEPADDLE_SPEC = _ocr_backend.PADDLEPADDLE_SPEC
PADDLEOCR_SPEC = _ocr_backend.PADDLEOCR_SPEC


def normalize_ocr_engine(name: Optional[str]) -> str:
    return _ocr_backend.normalize_ocr_engine(name)


def get_ocr_engine() -> str:
    return _ocr_backend.get_ocr_engine()


def set_ocr_engine(engine: str) -> str:
    return _ocr_backend.set_ocr_engine(engine)


def get_ocr_downscale_enabled() -> bool:
    return _ocr_backend.get_ocr_downscale_enabled()


def get_ocr_downscale_max_long_side() -> int:
    return _ocr_backend.get_ocr_downscale_max_long_side()


def set_ocr_downscale_enabled(enabled: bool) -> bool:
    return _ocr_backend.set_ocr_downscale_enabled(enabled)


def set_ocr_downscale_max_long_side(value: int) -> int:
    return _ocr_backend.set_ocr_downscale_max_long_side(value)


def set_ocr_downscale(enabled: bool, max_long_side=None):
    return _ocr_backend.set_ocr_downscale(enabled, max_long_side)


_easyocr_reader = None  # legacy; cache lives in ocr_backend
_argos_translator = None  # Argos Translation object (debug / status only)
_translate_fn = None  # cached callable: str -> str
_mt_backend: Optional[str] = None  # "ollama" | "argos" | "deep_translator" | None
_mt_status_detail: str = ""  # human-readable note (fallback reason, model name, …)

# Ollama defaults (override with env / set_ollama_model). Prefer 14b when present.
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"
OLLAMA_HOST = os.environ.get("IMG_STITCH_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("IMG_STITCH_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
OLLAMA_TIMEOUT_SEC = float(os.environ.get("IMG_STITCH_OLLAMA_TIMEOUT", "120"))


def get_ollama_model() -> str:
    """Return the currently selected Ollama model name."""
    return OLLAMA_MODEL


def reset_mt_cache() -> None:
    """Clear cached MT callable so the next translate re-resolves the backend."""
    global _translate_fn, _mt_backend, _mt_status_detail, _argos_translator
    _translate_fn = None
    _mt_backend = None
    _mt_status_detail = ""
    _argos_translator = None


def set_ollama_model(model: str) -> str:
    """Set preferred Ollama model and clear MT cache so it takes effect next call."""
    global OLLAMA_MODEL
    name = (model or "").strip() or DEFAULT_OLLAMA_MODEL
    OLLAMA_MODEL = name
    reset_mt_cache()
    logger.info("Ollama model set to %s", OLLAMA_MODEL)
    return OLLAMA_MODEL


def pick_default_ollama_model(available: Optional[Sequence[str]] = None) -> str:
    """Prefer ``qwen2.5:14b`` when present in *available* (or live ``ollama list``).

    Does not change the current model unless a better default match is found and
    the current selection is empty. Returns the chosen name without writing config.
    """
    names = list(available) if available is not None else ollama_list_models()
    prefer = DEFAULT_OLLAMA_MODEL
    if not names:
        return prefer
    if prefer in names:
        return prefer
    # Accept close tags (e.g. qwen2.5:14b-instruct)
    for n in names:
        if n.startswith(prefer) or prefer in n:
            return n
    return names[0]


def check_deps() -> dict:
    """Return availability of optional heavy packages / Ollama (no downloads)."""
    info = {
        "paddlepaddle": False,
        "paddleocr": False,
        "easyocr": False,
        "argostranslate": False,
        "deep_translator": False,
        "ollama": False,
        "ollama_model": OLLAMA_MODEL,
        "ocr_engine": get_ocr_engine(),
        "cjk_font": find_cjk_font() is not None,
    }
    # Prefer metadata probes (avoid loading paddle/torch DLLs on Windows).
    # Importing paddle before torch breaks torch shm.dll; importing easyocr
    # pulls torch. Distribution metadata is enough for availability checks.
    try:
        info["paddlepaddle"] = _ocr_backend._distribution_installed("paddlepaddle")
        info["paddleocr"] = _ocr_backend._distribution_installed("paddleocr")
        info["easyocr"] = _ocr_backend._distribution_installed("easyocr")
    except Exception:
        pass
    try:
        import argostranslate  # noqa: F401

        info["argostranslate"] = True
    except Exception:
        pass
    try:
        import deep_translator  # noqa: F401

        info["deep_translator"] = True
    except Exception:
        pass
    info["ollama"] = ollama_is_available()
    return info



# Default translate export needs Paddle + Argos. EasyOCR is optional fallback
# (torch); do not require it when paddle works — on Windows, importing paddle
# before torch breaks torch DLLs, so probing easyocr via import is unsafe.
EASYOCR_PIP_SPEC = ("easyocr", "easyocr>=1.7.0")
REQUIRED_PIP_SPECS = (
    ("paddleocr", PADDLEOCR_SPEC),
    ("argostranslate", "argostranslate>=1.9.0"),
)


def _module_importable(name: str) -> bool:
    """True if ``import name`` succeeds.

    Avoid calling this for ``paddle`` before ``paddleocr``/``easyocr`` on
    Windows: loading paddle first commonly breaks torch ``shm.dll``.
    """
    if name in ("paddle", "paddleocr"):
        try:
            _ocr_backend.apply_paddle_windows_quirks()
        except Exception:
            pass
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _paddle_stack_ready() -> bool:
    """True when paddlepaddle + paddleocr distributions are installed."""
    return not _ocr_backend.paddle_stack_missing()


def missing_pip_packages(
    packages: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    """Return ``(import_name, pip_spec)`` pairs that still need installing.

    Paddle stack and EasyOCR are probed via pip *metadata* (not ``import``) so
    we never load paddle DLLs before torch / EasyOCR on Windows. When using the
    default list, missing ``paddle`` is reported ahead of ``paddleocr``.
    EasyOCR is only required when the selected OCR engine is ``easyocr``.
    """
    pkgs = list(packages) if packages is not None else list(REQUIRED_PIP_SPECS)
    if packages is None and get_ocr_engine() == "easyocr":
        if EASYOCR_PIP_SPEC not in pkgs:
            pkgs.append(EASYOCR_PIP_SPEC)
    missing: List[Tuple[str, str]] = []
    need_paddle = packages is None or any(
        n in ("paddle", "paddleocr") for n, _ in pkgs
    )
    if need_paddle:
        # Metadata only — never ``import paddle`` here (DLL clash with torch).
        missing.extend(_ocr_backend.paddle_stack_missing())
    for name, spec in pkgs:
        if name in ("paddle", "paddleocr"):
            continue
        if name == "easyocr":
            if not _ocr_backend._distribution_installed("easyocr"):
                missing.append((name, spec))
            continue
        if not _module_importable(name):
            missing.append((name, spec))
    return missing


def ensure_deps(
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    packages: Optional[Sequence[Tuple[str, str]]] = None,
) -> dict:
    """Ensure translation deps exist in *this* interpreter (``sys.executable``).

    Missing packages are installed automatically via::

        python -m pip install <spec>

    PaddlePaddle uses the official CPU index (important on Windows). Safe to
    call from a background thread. Returns the same shape as :func:`check_deps`.
    """

    def _msg(m: str) -> None:
        logger.info("%s", m)
        if progress_callback:
            progress_callback(m)

    if packages is None or any(n in ("paddle", "paddleocr") for n, _ in (packages or [])):
        try:
            _ocr_backend.ensure_paddle_stack(progress_callback=progress_callback)
        except Exception as e:
            _msg(f"Paddle install note: {e} (will fall back to EasyOCR if needed)")

    missing = [
        (n, s)
        for n, s in missing_pip_packages(packages)
        if n not in ("paddle", "paddleocr")
    ]
    # Default OCR is paddle: EasyOCR is optional. Skip soft-missing easyocr when
    # paddle stack is present so translate export does not need torch/easyocr.
    if _paddle_stack_ready() and get_ocr_engine() != "easyocr":
        missing = [(n, s) for n, s in missing if n != "easyocr"]
    if not missing:
        _msg("翻译依赖已就绪")
        ok_o, note_o = ensure_ollama(progress_callback=progress_callback)
        if not ok_o:
            _msg("提示: " + note_o + "（将回退 Argos）")
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
            f'  "{py}" -m pip install {" ".join(specs)}'
        ) from e

    tail = (proc.stdout or "")[-800:] + "\n" + (proc.stderr or "")[-800:]
    if proc.returncode != 0:
        raise RuntimeError(
            f"自动安装依赖失败（exit {proc.returncode}）。\n"
            f"解释器: {py}\n"
            f"命令: {' '.join(cmd)}\n"
            f"输出片段:\n{tail.strip()}"
        )

    still = [
        (n, s)
        for n, s in missing_pip_packages(packages)
        if n not in ("paddle", "paddleocr")
    ]
    if still:
        still_names = ", ".join(n for n, _ in still)
        # EasyOCR/torch import can fail after paddle DLLs are loaded (Windows).
        # When Paddle stack is installed, EasyOCR is optional — do not abort.
        soft = [n for n, _ in still if n == "easyocr"]
        hard = [n for n, _ in still if n != "easyocr"]
        if soft and _paddle_stack_ready() and not hard:
            _msg(
                f"EasyOCR import/probe failed but PaddleOCR is ready; "
                f"continuing with paddle (optional: {still_names})."
            )
        elif hard:
            raise RuntimeError(
                "pip satisfied but import still fails: "
                + ", ".join(hard)
                + "\ninterpreter: "
                + str(py)
                + "\nrestart the app and retry. pip output:\n"
                + tail.strip()
            )
        else:
            _msg(
                f"optional deps still unavailable after pip: {still_names}; continuing"
            )

    _msg(f"依赖安装完成: {names}")
    ok_o, note_o = ensure_ollama(progress_callback=progress_callback)
    if not ok_o:
        _msg("提示: " + note_o + "（将回退 Argos）")
    return check_deps()


def _get_easyocr_reader(languages: Optional[Sequence[str]] = None):
    """Compatibility wrapper around ocr_backend.get_easyocr_reader."""
    return _ocr_backend.get_easyocr_reader(languages)


def ocr_image(
    path: Path,
    *,
    min_confidence: float = 0.3,
    engine: Optional[str] = None,
) -> List[OcrBox]:
    """Run OCR (PaddleOCR EN default; EasyOCR fallback) on an image."""
    return _ocr_backend.ocr_image(
        path, min_confidence=min_confidence, engine=engine
    )


def ollama_is_available(timeout: float = 1.5) -> bool:
    """True if Ollama HTTP API responds at ``OLLAMA_HOST``."""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def ollama_list_models(timeout: float = 3.0) -> List[str]:
    """Return local Ollama model names (empty on failure)."""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        names = []
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model")
            if name:
                names.append(str(name))
        return names
    except Exception as e:
        logger.debug("ollama_list_models failed: %s", e)
        return []


def _ollama_model_ready(model: Optional[str] = None) -> bool:
    """True if ``model`` (or default) is present locally (tag or prefix match)."""
    want = (model or OLLAMA_MODEL).strip()
    if not want:
        return False
    names = ollama_list_models()
    if want in names:
        return True
    # Accept "qwen2.5:14b" matching "qwen2.5:14b" or bare "qwen2.5:14b-..." variants
    base = want.split(":")[0]
    for n in names:
        if n == want or n.startswith(want + "-") or n.startswith(want + ":"):
            return True
        if want in n:
            return True
        if n.split(":")[0] == base and ":" in want and want.split(":", 1)[1] in n:
            return True
    return False


def translate_via_ollama(
    text: str,
    *,
    model: Optional[str] = None,
    host: Optional[str] = None,
    timeout: Optional[float] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> str:
    """Translate English to Simplified Chinese via Ollama ``/api/generate``.

    Prompt asks for translation only (no explanation). Raises on HTTP / empty /
    timeout errors. ``cancel_check`` is polled while waiting so the UI can abort.
    """
    model = (model or OLLAMA_MODEL).strip()
    host = (host or OLLAMA_HOST).rstrip("/")
    timeout = float(timeout if timeout is not None else OLLAMA_TIMEOUT_SEC)
    if cancel_check and cancel_check():
        raise CancelledError("已取消 Ollama 翻译")
    prompt = (
        "Translate the following English text to Simplified Chinese. "
        "Output only the translation, with no explanation or quotes.\n\n"
        f"{text}"
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _timeout_error(exc: BaseException) -> RuntimeError:
        return RuntimeError(
            f"Ollama HTTP 超时（{timeout:.0f}s）：模型 {model} @ {host} 未在限时内返回。"
            f"较大模型（如 qwen2.5:32b）推理较慢时可设置环境变量 "
            f"IMG_STITCH_OLLAMA_TIMEOUT（秒）增大超时，或改用更小模型。"
            f" 原始错误: {exc}"
        )

    result: dict = {"raw": None, "err": None}

    def _do_request() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result["raw"] = resp.read().decode("utf-8", errors="replace")
        except BaseException as e:  # noqa: BLE001 — marshal to caller thread
            result["err"] = e

    t = threading.Thread(target=_do_request, name="ollama-http", daemon=True)
    t.start()
    while t.is_alive():
        if cancel_check and cancel_check():
            raise CancelledError("已取消 Ollama 翻译")
        t.join(0.25)
    err = result["err"]
    if err is not None:
        if isinstance(err, urllib.error.HTTPError):
            detail = ""
            try:
                detail = (
                    err.read().decode("utf-8", errors="replace")[:300]
                    if err.fp
                    else ""
                )
            except Exception:
                detail = ""
            raise RuntimeError(
                f"Ollama HTTP {err.code}: {detail or err.reason}"
            ) from err
        if isinstance(err, TimeoutError) or isinstance(err, socket.timeout):
            raise _timeout_error(err) from err
        if isinstance(err, urllib.error.URLError):
            reason = getattr(err, "reason", err)
            reason_s = str(reason).lower()
            if (
                isinstance(reason, (TimeoutError, socket.timeout))
                or "timed out" in reason_s
                or "timeout" in reason_s
            ):
                raise _timeout_error(err) from err
            raise RuntimeError(f"Ollama 无法连接 ({host}): {reason}") from err
        err_s = str(err).lower()
        if "timed out" in err_s or "timeout" in err_s:
            raise _timeout_error(err) from err
        raise RuntimeError(f"Ollama request failed: {err}") from err

    raw = result["raw"] or ""
    data = json.loads(raw)
    out = (data.get("response") or "").strip()
    if out.startswith("```") and out.endswith("```"):
        out = out.strip("`").strip()
    if (out.startswith('"') and out.endswith('"')) or (
        out.startswith("'") and out.endswith("'")
    ):
        out = out[1:-1].strip()
    if not out:
        raise RuntimeError("Ollama returned empty translation")
    return out



def ensure_ollama(
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Check Ollama serve + preferred model; return ``(ok, status_message)``.

    Does not auto-install the Ollama app (native installer). See README.
    """
    def _msg(m: str) -> None:
        logger.info("%s", m)
        if progress_callback:
            progress_callback(m)

    if not ollama_is_available():
        msg = (
            f"Ollama 不可用（未检测到 {OLLAMA_HOST}）。"
            "请安装并运行 `ollama serve`，或设置 IMG_STITCH_OLLAMA_HOST。"
        )
        _msg(msg)
        return False, msg
    if not _ollama_model_ready(OLLAMA_MODEL):
        msg = (
            f"Ollama 已运行，但未找到模型 {OLLAMA_MODEL}。"
            f"请执行: ollama pull {OLLAMA_MODEL}"
        )
        _msg(msg)
        return False, msg
    msg = f"Ollama 就绪（host={OLLAMA_HOST}, model={OLLAMA_MODEL}）"
    _msg(msg)
    return True, msg


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
    """Prefer Ollama (local LLM); fall back to Argos, then deep_translator.

    Always caches and returns a *callable* ``(text) -> str``.  Previously the
    Argos ``CachedTranslation`` object was stored in ``_argos_translator`` and
    returned on subsequent calls; that object is not callable, so every translate
    after the first (or after ``preload_models``) raised TypeError, was swallowed,
    and painted the original English back onto the image.
    """
    global _argos_translator, _translate_fn, _mt_backend, _mt_status_detail
    if _translate_fn is not None:
        return _translate_fn, _mt_backend

    # 1) Ollama HTTP API (best local MT when serve + model are ready)
    try:
        ok, note = ensure_ollama()
        if ok:
            # Smoke one call path without translating yet — model list already checked.
            _mt_backend = "ollama"
            _mt_status_detail = note

            def _ollama_fn(text: str) -> str:
                return translate_via_ollama(text)

            _translate_fn = _ollama_fn
            logger.info("MT backend ready: ollama %s @ %s", OLLAMA_MODEL, OLLAMA_HOST)
            return _translate_fn, _mt_backend
        _mt_status_detail = note
        logger.warning("Ollama unavailable (%s); falling back to Argos", note)
    except Exception as e:
        _mt_status_detail = f"Ollama error: {e}"
        logger.warning("Ollama failed (%s); falling back to Argos", e)

    # 2) Argos Translate (offline package)
    try:
        import argostranslate  # noqa: F401

        translator = _ensure_argos_en_zh()
        _argos_translator = translator
        _mt_backend = "argos"
        if _mt_status_detail:
            _mt_status_detail = (
                f"Using Argos fallback ({_mt_status_detail})"
            )
        else:
            _mt_status_detail = "Argos en→zh"

        def _argos_fn(text: str) -> str:
            return translator.translate(text)

        _translate_fn = _argos_fn
        logger.info("MT backend ready: argos en->zh (callable cached); %s", _mt_status_detail)
        return _translate_fn, _mt_backend
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Argos Translate failed (%s); trying deep_translator fallback", e)
        _mt_status_detail = f"Argos failed: {e}"

    # 3) Online last resort
    try:
        from deep_translator import GoogleTranslator

        gt = GoogleTranslator(source="en", target="zh-CN")
        _mt_backend = "deep_translator"
        _mt_status_detail = (
            (_mt_status_detail + "; ") if _mt_status_detail else ""
        ) + "deep_translator Google"

        def _google_fn(text: str) -> str:
            return gt.translate(text)

        _translate_fn = _google_fn
        logger.info("MT backend ready: deep_translator Google en->zh-CN")
        return _translate_fn, _mt_backend
    except ImportError as e:
        raise ImportError(
            "本地翻译需要 Ollama（推荐）或 argostranslate。\n"
            "安装 Ollama: https://ollama.com 并用 `ollama pull "
            f"{OLLAMA_MODEL}`；保持 `ollama serve` 运行。\n"
            "或安装 argostranslate（应用可自动 pip 安装）。\n"
            "若仍失败可额外安装 deep_translator 作为联网后备。"
        ) from e


def translate_en_to_zh(
    text: str,
    *,
    cancel_check: Optional[CancelCheck] = None,
) -> str:
    """Translate a single English string to Chinese (Ollama / Argos / fallback)."""
    text = (text or "").strip()
    if not text:
        return text
    if cancel_check and cancel_check():
        raise CancelledError("已取消翻译")
    fn, backend = _get_translator()
    if not callable(fn):
        logger.error(
            "Translator is not callable (backend=%s, type=%s); returning original",
            backend,
            type(fn).__name__,
        )
        return text
    try:
        if backend == "ollama":
            out = translate_via_ollama(text, cancel_check=cancel_check)
        else:
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
    except CancelledError:
        raise
    except Exception as e:
        if backend == "ollama" and isinstance(e, RuntimeError):
            raise
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
    detail = f"; {_mt_status_detail}" if _mt_status_detail else ""
    model = f"; model={OLLAMA_MODEL}" if _mt_backend == "ollama" else ""
    return (
        f"MT backend={_mt_backend or 'none'}; callable_ready={ready}"
        f"{model}{detail}"
    )



# ---------------------------------------------------------------------------
# OCR text export / import (machine-stable ===PAGE NNN=== keys)
# ---------------------------------------------------------------------------

PAGE_MARKER_RE = re.compile(r"^===PAGE\s+(\d+)\s*===\s*$", re.MULTILINE)
_IMAGE_SUFFIXES_FOR_OCR = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
}


@dataclass(frozen=True)
class OcrPageBlock:
    """One page block from an OCR / translation .txt export."""

    page: int  # 1-based page index
    path: str  # relative or basename (informational)
    text: str  # English (export) or Chinese (import)


def format_page_marker(page: int) -> str:
    """Return machine-stable page key, e.g. ``===PAGE 001===``."""
    n = int(page)
    if n < 1:
        raise ValueError(f"page must be >= 1, got {page!r}")
    width = 3 if n < 1000 else len(str(n))
    return f"===PAGE {n:0{width}d}==="


def page_path_label(path: Path, *, root: Optional[Path] = None) -> str:
    """Prefer POSIX path relative to ``root``; else basename."""
    path = Path(path)
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.name


def filter_paths_for_ocr(
    paths: Sequence[Path],
    *,
    skip_non_images: bool = True,
) -> Tuple[List[Path], List[Path], List[str]]:
    """Same image filter as translation: skip PDF / GIF / already-translated.

    Returns ``(image_paths, skipped_pdfs, warnings)``.
    """
    image_paths: List[Path] = []
    skipped_pdfs: List[Path] = []
    warnings: List[str] = []
    for p in paths:
        p = Path(p)
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
        if suf not in _IMAGE_SUFFIXES_FOR_OCR:
            if skip_non_images:
                warnings.append(f"已跳过非图片: {p.name}")
                continue
        image_paths.append(p)
    return image_paths, skipped_pdfs, warnings


def build_ocr_export_text(
    pages: Sequence[Tuple[int, str, str]],
) -> str:
    """Build UTF-8 OCR export text from ``(page, path_label, english)`` tuples.

    Format per page::

        ===PAGE 001===
        path: relative/or/basename.jpg

        english text...

    Pages are separated by a blank line. Text uses ``\\n`` newlines.
    """
    chunks: List[str] = []
    for page, path_label, text in pages:
        marker = format_page_marker(page)
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        path_line = f"path: {(path_label or '').strip()}"
        block = f"{marker}\n{path_line}\n\n"
        if body:
            block += body + "\n"
        chunks.append(block)
    if not chunks:
        return ""
    return "\n".join(chunks)


def parse_ocr_export_text(content: str) -> List[OcrPageBlock]:
    """Parse ``===PAGE NNN===`` blocks; mapping key is the page index.

    Accepts optional ``path:`` line immediately after the marker.
    Body is everything after the path line (or after marker if no path line)
    until the next marker. Blank lines around body are trimmed.
    """
    raw = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return []
    matches = list(PAGE_MARKER_RE.finditer(raw))
    if not matches:
        raise ValueError(
            "未找到 ===PAGE NNN=== 标记。请使用导出的 OCR 文本格式（保留页标记）。"
        )
    blocks: List[OcrPageBlock] = []
    seen: set = set()
    for i, m in enumerate(matches):
        page = int(m.group(1))
        if page in seen:
            raise ValueError(f"重复的页标记：===PAGE {page:03d}===")
        seen.add(page)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        section = raw[start:end]
        if section.startswith("\n"):
            section = section[1:]
        path_label = ""
        lines = section.split("\n")
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx < len(lines) and lines[idx].lower().startswith("path:"):
            path_label = lines[idx].split(":", 1)[1].strip()
            body = "\n".join(lines[idx + 1 :])
        else:
            body = section
        text = body.strip("\n")
        if text.startswith("\n"):
            text = text[1:]
        text = text.strip("\n")
        blocks.append(OcrPageBlock(page=page, path=path_label, text=text))
    blocks.sort(key=lambda b: b.page)
    return blocks


def ocr_image_to_english(
    path: Path,
    *,
    min_confidence: float = 0.3,
    merge_nearby: bool = False,
    engine: Optional[str] = None,
) -> str:
    """OCR one image and join all English into one reading-order paragraph."""
    boxes = ocr_image(path, min_confidence=min_confidence, engine=engine)
    return join_english_paragraph(boxes, merge_nearby=merge_nearby)


def export_ocr_text_for_paths(
    paths: Sequence[Path],
    *,
    root: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
    min_confidence: float = 0.3,
    merge_nearby: bool = False,
) -> Tuple[str, List[Path], List[str]]:
    """OCR listed images in order → one UTF-8 export string.

    Returns ``(text, image_paths, warnings)``. Page indices are 1-based in
    image-list order (PDF/GIF skipped). ``cancel_check`` returning True aborts
    with :class:`CancelledError`.
    """
    image_paths, skipped_pdfs, warnings = filter_paths_for_ocr(paths)
    if skipped_pdfs:
        warnings.append(
            f"OCR 导出跳过 {len(skipped_pdfs)} 个 PDF（仅处理图片）。"
        )
    # Create PaddleOCR/EasyOCR once for this batch/process; reuse for every image.
    if image_paths:
        if progress_callback is not None:
            progress_callback(0, len(image_paths), "OCR model warm-up")
        try:
            _ocr_backend.preload_ocr(get_ocr_engine())
        except Exception as warm_err:
            warnings.append(f"OCR preload note: {warm_err}")
            logger.warning("OCR preload failed before export batch: %s", warm_err)

    pages: List[Tuple[int, str, str]] = []
    n = len(image_paths)
    for i, src in enumerate(image_paths, start=1):
        if cancel_check is not None and cancel_check():
            raise CancelledError("已取消 OCR 导出")
        if progress_callback is not None:
            progress_callback(i, n, f"OCR {src.name}")
        label = page_path_label(src, root=root)
        try:
            english = ocr_image_to_english(
                src, min_confidence=min_confidence, merge_nearby=merge_nearby
            )
        except CancelledError:
            raise
        except Exception as e:
            warnings.append(f"{src.name}: {e}")
            logger.exception("OCR failed for %s", src)
            english = ""
        pages.append((i, label, english))
    return build_ocr_export_text(pages), image_paths, warnings


def render_image_with_caption(
    src: Path,
    caption: str,
    *,
    dst: Optional[Path] = None,
    font_path: Optional[Path] = None,
) -> Path:
    """Append Chinese (or other) caption band below ``src`` → translated_zh/."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    out = Path(dst) if dst is not None else translated_output_path(src)
    if out.resolve() == src.resolve():
        out = (
            src.parent
            / TRANSLATED_SUBDIR
            / f"{src.stem}_zh_out{src.suffix.lower()}"
        )
    with Image.open(src) as im:
        im.load()
        original = im.convert("RGB")
        canvas = append_caption_band(
            original, caption or "", font_path=font_path
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        suf = out.suffix.lower()
        save_kw: dict = {}
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


def apply_translated_captions(
    paths: Sequence[Path],
    blocks: Sequence[OcrPageBlock],
    *,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
    font_path: Optional[Path] = None,
) -> Tuple[List[Path], List[str]]:
    """Map ``===PAGE N===`` by page index onto filtered image list; write captions.

    Page ``N`` (1-based) maps to the N-th OCR-eligible image in ``paths`` order.
    Returns ``(output_paths, warnings)``.
    """
    image_paths, skipped_pdfs, warnings = filter_paths_for_ocr(paths)
    if skipped_pdfs:
        warnings.append(
            f"导入译文跳过 {len(skipped_pdfs)} 个 PDF（仅处理图片）。"
        )
    by_page = {b.page: b for b in blocks}
    if not by_page:
        warnings.append("译文文件没有有效页块。")
        return [], warnings
    max_page = max(by_page)
    if max_page > len(image_paths):
        warnings.append(
            f"译文最多到 PAGE {max_page:03d}，但列表仅有 {len(image_paths)} 张可处理图片；"
            "超出部分将忽略。"
        )
    missing = [p for p in range(1, len(image_paths) + 1) if p not in by_page]
    if missing:
        preview = ", ".join(f"{n:03d}" for n in missing[:8])
        more = f" 等{len(missing)}页" if len(missing) > 8 else ""
        warnings.append(f"缺少页标记：{preview}{more}（对应图片将跳过）。")

    outputs: List[Path] = []
    n = len(image_paths)
    for i, src in enumerate(image_paths, start=1):
        if cancel_check is not None and cancel_check():
            raise CancelledError("已取消导入译文")
        block = by_page.get(i)
        if block is None:
            continue
        caption = (block.text or "").strip()
        if progress_callback is not None:
            progress_callback(i, n, f"写译文条 {src.name}")
        try:
            out = render_image_with_caption(
                src, caption, font_path=font_path
            )
            outputs.append(out)
        except CancelledError:
            raise
        except Exception as e:
            warnings.append(f"{src.name}: {e}")
            logger.exception("Caption render failed for %s", src)
    return outputs, warnings



# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def translate_image_file(
    src: Path,
    *,
    dst: Optional[Path] = None,
    font_path: Optional[Path] = None,
    min_confidence: float = 0.3,
    render_mode: str = "caption",
    merge_nearby: bool = False,
    cancel_check: Optional[CancelCheck] = None,
) -> Path:
    """OCR → whole-page English paragraph → one MT call → caption band.

    Default ``render_mode="caption"`` keeps the original image intact and
    appends a single Chinese translation band below it (all OCR English joined
    in reading order, then translated once). Pass ``merge_nearby=True`` to
    optionally merge nearby boxes before joining (fallback only).
    ``render_mode="overlay"`` is kept for backward compatibility / debugging.

    Never overwrites ``src``.
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    out = Path(dst) if dst is not None else translated_output_path(src)
    if out.resolve() == src.resolve():
        # Safety: if somehow same path, force a distinct name under subfolder
        out = src.parent / TRANSLATED_SUBDIR / f"{src.stem}_zh_out{src.suffix.lower()}"

    mode = (render_mode or "caption").strip().lower()
    if mode not in ("caption", "overlay"):
        mode = "caption"

    if cancel_check and cancel_check():
        raise CancelledError("已取消 OCR")
    boxes = ocr_image(src, min_confidence=min_confidence)
    # Ensure translator is ready before the loop so status/logging is accurate
    _get_translator()
    logger.info(
        "Translating %s: %d OCR box(es); mode=%s; %s; out=%s",
        src.name,
        len(boxes),
        mode,
        mt_status(),
        out,
    )

    with Image.open(src) as im:
        im.load()
        original = im.convert("RGB")

        if mode == "overlay":
            # Legacy path (disabled as default): paint Chinese over each box.
            canvas = original
            translated_boxes = 0
            for ob in boxes:
                if cancel_check and cancel_check():
                    raise CancelledError("已取消翻译")
                zh = translate_en_to_zh(ob.text, cancel_check=cancel_check)
                if not zh:
                    continue
                if contains_cjk(zh) or zh != ob.text:
                    translated_boxes += 1
                canvas = draw_text_in_box(canvas, ob.box, zh, font_path=font_path)
            logger.info(
                "Overlay mode: %d/%d boxes changed; %s",
                translated_boxes,
                len(boxes),
                mt_status(),
            )
        else:
            # Default: reading-order → one English paragraph → one MT call → caption.
            english = join_english_paragraph(boxes, merge_nearby=merge_nearby)
            caption = ""
            if english:
                if cancel_check and cancel_check():
                    raise CancelledError("已取消翻译")
                caption = translate_en_to_zh(
                    english, cancel_check=cancel_check
                )
            canvas = append_caption_band(
                original, caption, font_path=font_path
            )
            logger.info(
                "Caption mode: %d raw OCR → whole-page paragraph (%d chars EN) "
                "→ 1 MT call (merge_nearby=%s); out size %sx%s (src %sx%s); %s",
                len(boxes),
                len(english),
                merge_nearby,
                canvas.size[0],
                canvas.size[1],
                original.size[0],
                original.size[1],
                mt_status(),
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        suf = out.suffix.lower()
        save_kw: dict = {}
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
    cancel_check: Optional[CancelCheck] = None,
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

    # Create PaddleOCR/EasyOCR once for this batch/process; reuse for every image.
    if image_paths:
        if progress_callback is not None:
            progress_callback(0, len(image_paths), "OCR model warm-up")
        try:
            _ocr_backend.preload_ocr(get_ocr_engine())
        except Exception as warm_err:
            warnings.append(f"OCR preload note: {warm_err}")
            logger.warning("OCR preload failed before translate batch: %s", warm_err)

    n = len(image_paths)
    for i, src in enumerate(image_paths, start=1):
        if cancel_check and cancel_check():
            raise CancelledError(
                f"已取消翻译（完成 {len(translated)}/{n}）"
            )
        if progress_callback is not None:
            progress_callback(i, n, f"正在翻译 {src.name}")
        try:
            out = translate_image_file(src, cancel_check=cancel_check)
            translated.append(out)
        except CancelledError:
            raise
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
    ocr_note = _ocr_backend.preload_ocr(
        get_ocr_engine(), progress_callback=progress_callback
    )
    _msg(f"正在准备本地 MT（优先 Ollama {OLLAMA_MODEL}，否则 Argos）…")
    _get_translator()
    backend = _mt_backend or "unknown"
    if _translate_fn is None or not callable(_translate_fn):
        raise RuntimeError(
            f"翻译器未就绪（backend={backend}, fn={type(_translate_fn).__name__}）。"
            "请检查 Ollama 是否运行（ollama serve）及模型是否已 pull，或 Argos en→zh 语言包是否已安装。"
        )
    sample = translate_text("Hello")
    if not contains_cjk(sample):
        raise RuntimeError(
            f"翻译冒烟失败：translate_text('Hello') -> {sample!r}（期望含中文）。"
            f"{mt_status()}"
        )
    _msg(f"翻译冒烟通过：Hello → {sample}（{mt_status()}）")
    font = find_cjk_font()
    font_note = str(font) if font else "未找到 CJK 字体（中文可能显示为方框）"
    return f"就绪（OCR={ocr_note}, MT={backend}, font={font_note}）"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python translate_local.py <image> [more images…]")
        print("Deps:", check_deps())
        raise SystemExit(2)
    for arg in sys.argv[1:]:
        out = translate_image_file(Path(arg))
        print(out)
