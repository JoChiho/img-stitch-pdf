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
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# Set by bind_ocr_box() from translate_local.OcrBox
OcrBox = None

_easyocr_reader = None
_paddleocr_reader = None
_last_ocr_engine_used: Optional[str] = None
_reader_lock = threading.Lock()
_paddleocr_init_count = 0
_easyocr_init_count = 0

OCR_ENGINES = ("paddle", "easyocr")
DEFAULT_OCR_ENGINE = "paddle"
_ocr_env = (os.environ.get("IMG_STITCH_OCR_ENGINE") or DEFAULT_OCR_ENGINE).strip().lower()
OCR_ENGINE = _ocr_env if _ocr_env in OCR_ENGINES else DEFAULT_OCR_ENGINE

DEFAULT_OCR_DOWNSCALE_ENABLED = False
DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE = 1600

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


OCR_DOWNSCALE_ENABLED = _env_flag(
    "IMG_STITCH_OCR_DOWNSCALE", DEFAULT_OCR_DOWNSCALE_ENABLED
)
OCR_DOWNSCALE_MAX_LONG_SIDE = _env_int(
    "IMG_STITCH_OCR_DOWNSCALE_MAX", DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE
)

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


def get_ocr_downscale_enabled() -> bool:
    return bool(OCR_DOWNSCALE_ENABLED)


def get_ocr_downscale_max_long_side() -> int:
    return int(OCR_DOWNSCALE_MAX_LONG_SIDE)


def set_ocr_downscale_enabled(enabled: bool) -> bool:
    global OCR_DOWNSCALE_ENABLED
    OCR_DOWNSCALE_ENABLED = bool(enabled)
    logger.info("OCR downscale enabled=%s", OCR_DOWNSCALE_ENABLED)
    return OCR_DOWNSCALE_ENABLED


def set_ocr_downscale_max_long_side(value: int) -> int:
    global OCR_DOWNSCALE_MAX_LONG_SIDE
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE
    OCR_DOWNSCALE_MAX_LONG_SIDE = max(1, n)
    logger.info("OCR downscale max_long_side=%s", OCR_DOWNSCALE_MAX_LONG_SIDE)
    return OCR_DOWNSCALE_MAX_LONG_SIDE


def set_ocr_downscale(enabled: bool, max_long_side: Optional[int] = None) -> Tuple[bool, int]:
    """Set both downscale flags; omit max_long_side to keep current value."""
    set_ocr_downscale_enabled(enabled)
    if max_long_side is not None:
        set_ocr_downscale_max_long_side(max_long_side)
    return get_ocr_downscale_enabled(), get_ocr_downscale_max_long_side()


def prepare_image_for_ocr(
    path: Path,
    *,
    enabled: Optional[bool] = None,
    max_long_side: Optional[int] = None,
) -> Tuple[Path, float, float, Optional[Path]]:
    """Optionally downscale ``path`` for OCR.

    Returns ``(ocr_path, scale_x, scale_y, temp_path)``.
    Multiply OCR box coordinates by ``scale_x`` / ``scale_y`` to map back to
    the original image. Caller must delete ``temp_path`` when not None.
    """
    path = Path(path)
    use = OCR_DOWNSCALE_ENABLED if enabled is None else bool(enabled)
    limit = (
        OCR_DOWNSCALE_MAX_LONG_SIDE
        if max_long_side is None
        else max(1, int(max_long_side))
    )
    if not use:
        return path, 1.0, 1.0, None
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            long_side = max(w, h)
            if long_side <= limit:
                return path, 1.0, 1.0, None
            scale = limit / float(long_side)
            nw = max(1, int(round(w * scale)))
            nh = max(1, int(round(h * scale)))
            rgb = im.convert("RGB")
            resized = rgb.resize((nw, nh), Image.Resampling.LANCZOS)
            fd, tmp_name = tempfile.mkstemp(prefix="ocr_ds_", suffix=".png")
            os.close(fd)
            tmp_path = Path(tmp_name)
            resized.save(tmp_path, format="PNG")
            sx = w / float(nw)
            sy = h / float(nh)
            logger.info(
                "OCR downscale %s: %sx%s -> %sx%s (max_long=%s)",
                path.name,
                w,
                h,
                nw,
                nh,
                limit,
            )
            return tmp_path, sx, sy, tmp_path
    except Exception as e:
        logger.warning("OCR downscale skipped for %s: %s", path, e)
        return path, 1.0, 1.0, None


def _scale_ocr_boxes(boxes: list, scale_x: float, scale_y: float) -> list:
    if scale_x == 1.0 and scale_y == 1.0:
        return boxes
    assert OcrBox is not None
    out = []
    for ob in boxes:
        left, top, right, bottom = ob.box
        out.append(
            OcrBox(
                text=ob.text,
                box=(
                    int(round(left * scale_x)),
                    int(round(top * scale_y)),
                    int(round(right * scale_x)),
                    int(round(bottom * scale_y)),
                ),
                confidence=ob.confidence,
            )
        )
    return out


def _cleanup_temp(temp_path: Optional[Path]) -> None:
    if temp_path is None:
        return
    try:
        Path(temp_path).unlink(missing_ok=True)
    except TypeError:
        # Python <3.8 missing_ok
        try:
            Path(temp_path).unlink()
        except OSError:
            pass
    except OSError:
        pass


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


def ocr_reader_cache_stats() -> dict:
    """Return process-level OCR reader cache / cold-start counters."""
    return {
        "paddle_cached": _paddleocr_reader is not None,
        "easyocr_cached": _easyocr_reader is not None,
        "paddle_init_count": _paddleocr_init_count,
        "easyocr_init_count": _easyocr_init_count,
        "engine": OCR_ENGINE,
        "last_used": _last_ocr_engine_used,
    }


def reset_ocr_readers_for_tests() -> None:
    """Clear cached readers (tests only)."""
    global _paddleocr_reader, _easyocr_reader
    global _paddleocr_init_count, _easyocr_init_count, _last_ocr_engine_used
    with _reader_lock:
        _paddleocr_reader = None
        _easyocr_reader = None
        _paddleocr_init_count = 0
        _easyocr_init_count = 0
        _last_ocr_engine_used = None


def get_paddleocr_reader(languages: Optional[Sequence[str]] = None):
    """Return the process-wide PaddleOCR instance (create once, then reuse)."""
    global _paddleocr_reader, _paddleocr_init_count
    if _paddleocr_reader is not None:
        return _paddleocr_reader
    with _reader_lock:
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
        logger.info("OCR cold-start: creating PaddleOCR reader (lang=%s)", lang)
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
        _paddleocr_init_count += 1
        logger.info(
            "OCR ready: PaddleOCR reader cached (init_count=%s)",
            _paddleocr_init_count,
        )
        return _paddleocr_reader


def get_easyocr_reader(languages: Optional[Sequence[str]] = None):
    """Return the process-wide EasyOCR Reader (create once, then reuse)."""
    global _easyocr_reader, _easyocr_init_count
    if _easyocr_reader is not None:
        return _easyocr_reader
    with _reader_lock:
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
        logger.info("OCR cold-start: creating EasyOCR Reader (langs=%s)", langs)
        _easyocr_reader = easyocr.Reader(langs, gpu=False, verbose=False)
        _easyocr_init_count += 1
        logger.info(
            "OCR ready: EasyOCR reader cached (init_count=%s)",
            _easyocr_init_count,
        )
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
    ocr_path, sx, sy, tmp = prepare_image_for_ocr(path)
    try:
        if eng == "paddle":
            try:
                boxes = ocr_image_paddle(ocr_path, min_confidence=min_confidence)
                _last_ocr_engine_used = "paddle"
                return _scale_ocr_boxes(boxes, sx, sy)
            except Exception as e:
                logger.warning(
                    "PaddleOCR failed (%s); falling back to EasyOCR", e
                )
                try:
                    boxes = ocr_image_easyocr(
                        ocr_path, min_confidence=min_confidence
                    )
                    _last_ocr_engine_used = "easyocr"
                    return _scale_ocr_boxes(boxes, sx, sy)
                except Exception as e2:
                    raise RuntimeError(
                        "PaddleOCR failed and EasyOCR fallback also failed.\n"
                        f"Paddle: {e}\nEasyOCR: {e2}"
                    ) from e2
        boxes = ocr_image_easyocr(ocr_path, min_confidence=min_confidence)
        _last_ocr_engine_used = "easyocr"
        return _scale_ocr_boxes(boxes, sx, sy)
    finally:
        _cleanup_temp(tmp)


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
