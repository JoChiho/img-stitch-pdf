# -*- coding: utf-8 -*-
"""OCR backends: PaddleOCR (default) with EasyOCR fallback.

On Windows, never ``import paddle`` before ``paddleocr``/``torch`` in the
same process: paddle DLLs commonly break torch ``shm.dll`` (EasyOCR).
Availability probes for the Paddle stack must use pip metadata, not import.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Set by bind_ocr_box() from translate_local.OcrBox
OcrBox = None

_easyocr_reader = None
_paddleocr_reader = None
_last_ocr_engine_used: Optional[str] = None

OCR_ENGINES = ("paddle", "easyocr")
DEFAULT_OCR_ENGINE = "paddle"
_ocr_env = (os.environ.get("IMG_STITCH_OCR_ENGINE") or DEFAULT_OCR_ENGINE).strip().lower()
OCR_ENGINE = _ocr_env if _ocr_env in OCR_ENGINES else DEFAULT_OCR_ENGINE

PADDLE_CPU_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cpu/"
PADDLEPADDLE_SPEC = "paddlepaddle"
PADDLEOCR_SPEC = "paddleocr>=2.7.0"


def bind_ocr_box(cls) -> None:
    global OcrBox
    OcrBox = cls


def normalize_ocr_engine(name: Optional[str]) -> str:
    raw = (name or "").strip().lower()
    if raw in OCR_ENGINES:
        return raw
    return DEFAULT_OCR_ENGINE


def get_ocr_engine() -> str:
    return OCR_ENGINE


def set_ocr_engine(engine: str) -> str:
    global OCR_ENGINE, _last_ocr_engine_used
    OCR_ENGINE = normalize_ocr_engine(engine)
    _last_ocr_engine_used = None
    logger.info("OCR engine set to %s", OCR_ENGINE)
    return OCR_ENGINE


def get_last_ocr_engine_used() -> Optional[str]:
    return _last_ocr_engine_used


def apply_paddle_windows_quirks() -> None:
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_onednn", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def _distribution_installed(dist_name: str) -> bool:
    """True if a pip distribution is installed (no import / no DLL load)."""
    try:
        import importlib.metadata as md

        md.version(dist_name)
        return True
    except Exception:
        return False


def _module_importable(name: str) -> bool:
    """Try a real import. Prefer ``_distribution_installed`` / ``paddle_stack_missing``
    for Paddle availability on Windows (importing paddle poisons torch/EasyOCR).
    """
    if name in ("paddle", "paddleocr"):
        apply_paddle_windows_quirks()
    try:
        __import__(name)
        return True
    except Exception:
        return False


def paddle_stack_missing() -> List[Tuple[str, str]]:
    """Report missing paddle packages via metadata (safe on Windows)."""
    missing: List[Tuple[str, str]] = []
    if not _distribution_installed("paddlepaddle"):
        missing.append(("paddle", PADDLEPADDLE_SPEC))
    if not _distribution_installed("paddleocr"):
        missing.append(("paddleocr", PADDLEOCR_SPEC))
    return missing


def _run_pip_install(
    specs: Sequence[str],
    *,
    extra_args: Optional[Sequence[str]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> None:
    def _msg(m: str) -> None:
        logger.info("%s", m)
        if progress_callback:
            progress_callback(m)

    py = sys.executable
    cmd = [
        py,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *list(extra_args or ()),
        *specs,
    ]
    _msg("pip: " + " ".join(cmd))
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
        raise RuntimeError(f"Cannot start pip ({py}): {e}") from e
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-800:] + "\n" + (proc.stderr or "")[-800:]
        raise RuntimeError(
            f"pip install failed (exit {proc.returncode}):\n{tail.strip()}"
        )


def ensure_paddle_stack(
    progress_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Install paddlepaddle (CPU index) + paddleocr if missing."""

    def _msg(m: str) -> None:
        logger.info("%s", m)
        if progress_callback:
            progress_callback(m)

    if not _distribution_installed("paddlepaddle"):
        _msg(f"Installing paddlepaddle (CPU) via {PADDLE_CPU_INDEX}")
        _run_pip_install(
            [PADDLEPADDLE_SPEC],
            extra_args=["-i", PADDLE_CPU_INDEX],
            progress_callback=progress_callback,
        )
        if not _distribution_installed("paddlepaddle"):
            raise RuntimeError(
                "paddlepaddle installed but not visible to pip metadata. Try:\n"
                f'  "{sys.executable}" -m pip install {PADDLEPADDLE_SPEC} '
                f"-i {PADDLE_CPU_INDEX}"
            )
        _msg("paddlepaddle ready")

    if not _distribution_installed("paddleocr"):
        _msg("Installing paddleocr...")
        _run_pip_install([PADDLEOCR_SPEC], progress_callback=progress_callback)
        if not _distribution_installed("paddleocr"):
            raise RuntimeError(
                "paddleocr installed but not visible to pip metadata; restart the app."
            )
        _msg("paddleocr ready")


def get_paddleocr_reader(languages: Optional[Sequence[str]] = None):
    global _paddleocr_reader
    if _paddleocr_reader is not None:
        return _paddleocr_reader
    apply_paddle_windows_quirks()
    try:
        from paddleocr import PaddleOCR
    except ImportError as e:
        raise ImportError(
            "PaddleOCR is required (auto-install should have run). "
            "On Windows use CPU paddlepaddle from the official CPU index."
        ) from e
    langs = list(languages) if languages else ["en"]
    lang = langs[0] if langs else "en"
    kwargs_list = [
        dict(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
        ),
        dict(lang=lang, use_angle_cls=True, show_log=False, use_gpu=False),
        dict(lang=lang),
    ]
    last_err: Optional[BaseException] = None
    for kwargs in kwargs_list:
        try:
            _paddleocr_reader = PaddleOCR(**kwargs)
            break
        except TypeError as e:
            last_err = e
        except Exception as e:
            last_err = e
    if _paddleocr_reader is None:
        raise RuntimeError(f"Cannot init PaddleOCR: {last_err}")
    return _paddleocr_reader


def get_easyocr_reader(languages: Optional[Sequence[str]] = None):
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    try:
        import easyocr
    except Exception as e:
        # ImportError or OSError (torch shm.dll after paddle on Windows)
        raise ImportError(
            "easyocr is unavailable (optional when using PaddleOCR). "
            f"Underlying error: {type(e).__name__}: {e}"
        ) from e
    langs = list(languages) if languages else ["en"]
    _easyocr_reader = easyocr.Reader(langs, gpu=False, verbose=False)
    return _easyocr_reader


def _poly_to_aabb(poly) -> Optional[Tuple[int, int, int, int]]:
    try:
        if poly is None:
            return None
        if len(poly) == 4 and not hasattr(poly[0], "__len__"):
            return int(poly[0]), int(poly[1]), int(poly[2]), int(poly[3])
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
    except Exception:
        return None


def _boxes_from_paddle_page(page) -> list:
    assert OcrBox is not None
    results = []
    payload = None
    if hasattr(page, "json"):
        raw = page.json
        if isinstance(raw, dict):
            payload = raw.get("res", raw)
    if payload is None and isinstance(page, dict):
        payload = page.get("res", page)
    if isinstance(payload, dict) and (
        "rec_texts" in payload
        or "rec_boxes" in payload
        or "dt_polys" in payload
    ):
        texts = list(payload.get("rec_texts") or [])
        scores = list(payload.get("rec_scores") or [])
        boxes = list(
            payload.get("rec_boxes") or payload.get("dt_polys") or []
        )
        for i, text in enumerate(texts):
            text = (text or "").strip()
            if not text:
                continue
            conf = float(scores[i]) if i < len(scores) else 1.0
            box = _poly_to_aabb(boxes[i]) if i < len(boxes) else None
            if box is None:
                continue
            left, top, right, bottom = box
            if right - left < 2 or bottom - top < 2:
                continue
            results.append(
                OcrBox(
                    text=text,
                    box=(left, top, right, bottom),
                    confidence=conf,
                )
            )
        return results

    if isinstance(page, (list, tuple)):
        for item in page:
            if not item or len(item) < 2:
                continue
            bbox = item[0]
            if len(item) >= 3 and not isinstance(item[1], (list, tuple)):
                text, conf = item[1], float(item[2])
            else:
                info = item[1]
                if isinstance(info, (list, tuple)) and len(info) >= 2:
                    text, conf = info[0], float(info[1])
                else:
                    continue
            text = (text or "").strip()
            if not text:
                continue
            box = _poly_to_aabb(bbox)
            if box is None:
                continue
            left, top, right, bottom = box
            if right - left < 2 or bottom - top < 2:
                continue
            results.append(
                OcrBox(
                    text=text,
                    box=(left, top, right, bottom),
                    confidence=float(conf),
                )
            )
    return results


def ocr_image_paddle(path: Path, *, min_confidence: float = 0.3) -> list:
    reader = get_paddleocr_reader(["en"])
    path = Path(path)
    raw = None
    if hasattr(reader, "predict"):
        try:
            raw = reader.predict(str(path))
        except Exception as e:
            logger.debug(
                "PaddleOCR.predict failed: %s; trying .ocr()", e
            )
            raw = None
    if raw is None and hasattr(reader, "ocr"):
        try:
            raw = reader.ocr(str(path))
        except TypeError:
            raw = reader.ocr(str(path), cls=True)
    if not raw:
        return []
    results = []
    for page in raw:
        if page is None:
            continue
        for ob in _boxes_from_paddle_page(page):
            if ob.confidence < min_confidence:
                continue
            results.append(ob)
    return results


def ocr_image_easyocr(path: Path, *, min_confidence: float = 0.3) -> list:
    reader = get_easyocr_reader(["en"])
    path = Path(path)
    raw = reader.readtext(str(path), detail=1, paragraph=False)
    assert OcrBox is not None
    results = []
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
        results.append(
            OcrBox(
                text=text,
                box=(left, top, right, bottom),
                confidence=conf,
            )
        )
    return results


def ocr_image(
    path: Path,
    *,
    min_confidence: float = 0.3,
    engine: Optional[str] = None,
) -> list:
    global _last_ocr_engine_used
    eng = normalize_ocr_engine(
        engine if engine is not None else OCR_ENGINE
    )
    path = Path(path)
    if eng == "paddle":
        try:
            boxes = ocr_image_paddle(path, min_confidence=min_confidence)
            _last_ocr_engine_used = "paddle"
            return boxes
        except Exception as e:
            logger.warning(
                "PaddleOCR failed (%s); falling back to EasyOCR", e
            )
            try:
                boxes = ocr_image_easyocr(
                    path, min_confidence=min_confidence
                )
                _last_ocr_engine_used = "easyocr"
                return boxes
            except Exception as e2:
                raise RuntimeError(
                    "PaddleOCR failed and EasyOCR fallback also failed.\n"
                    f"Paddle: {e}\nEasyOCR: {e2}"
                ) from e2
    boxes = ocr_image_easyocr(path, min_confidence=min_confidence)
    _last_ocr_engine_used = "easyocr"
    return boxes


def preload_ocr(
    engine: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    def _msg(m: str) -> None:
        if progress_callback:
            progress_callback(m)

    eng = normalize_ocr_engine(
        engine if engine is not None else OCR_ENGINE
    )
    if eng == "paddle":
        _msg("Loading PaddleOCR models (first run downloads)...")
        try:
            get_paddleocr_reader(["en"])
            return "paddle"
        except Exception as e:
            _msg(
                f"PaddleOCR preload failed, falling back to EasyOCR: {e}"
            )
            try:
                get_easyocr_reader(["en"])
                return "easyocr(fallback)"
            except Exception as e2:
                # Torch/EasyOCR often broken after paddle DLL load on Windows.
                raise RuntimeError(
                    "PaddleOCR preload failed and EasyOCR fallback unavailable.\n"
                    f"Paddle: {e}\nEasyOCR: {e2}"
                ) from e2
    _msg("Loading EasyOCR models (first run downloads)...")
    get_easyocr_reader(["en"])
    return "easyocr"
