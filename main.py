#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图 / PDF 导出为多页 PDF — 桌面 Tkinter GUI。

默认：列表项按顺序组成多页 PDF（图片一图一页；PDF 贡献其全部页），
不拼接成巨图，避免 OOM。JPEG 尽量经 img2pdf 原样嵌入；PDF 页经 pypdf
合并，不重新栅格化。

可选：本地 EN→ZH 图片翻译（EasyOCR + Ollama / Argos），将中文绘制到
原图目录下新建子文件夹 ``translated_zh/``（不与原图同级混放，便于清理），再走同一套多页 PDF 导出。

另附「下载视频」窗口：粘贴链接 → yt-dlp 下载（逻辑见 ``video_dl.py``）。
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import queue
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # 无 GUI 环境仍可导入核心导出逻辑（如 pytest）
    tk = None  # type: ignore
    filedialog = messagebox = ttk = None  # type: ignore

try:
    from PIL import Image
except ImportError:
    print("请先安装依赖: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from PIL import ImageTk, ImageDraw, ImageFont
except ImportError:  # headless / minimal Pillow
    ImageTk = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore

try:
    import img2pdf
except ImportError:
    img2pdf = None  # type: ignore

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore


translate_local = None  # type: ignore  # lazy — avoid heavy imports on UI startup


def _ensure_translate_local():
    """Import translate_local on first use (worker thread preferred)."""
    global translate_local
    if translate_local is None:
        import translate_local as _tl

        translate_local = _tl
    return translate_local


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")  # skip .gif by default
PDF_EXT = ".pdf"
MEDIA_EXTS = IMAGE_EXTS + (PDF_EXT,)
PROJECT_DIR = Path(__file__).resolve().parent

ProgressCallback = Callable[[int, int], None]
Img2pdfInput = Union[str, bytes]


def natural_key(name: str):
    """按文件名自然排序（img2 在 img10 前面）。"""
    parts = re.split(r"(\d+)", name.lower())
    key = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return key


def is_pdf(path: Path) -> bool:
    return Path(path).suffix.lower() == PDF_EXT


def is_image(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def list_images_in_folder(
    folder: Path,
    *,
    skip_gif: bool = True,
    include_pdf: bool = True,
    recurse: bool = True,
) -> List[Path]:
    """收集文件夹内常见扩展名图片与 PDF，按相对路径自然排序。

    recurse=True（默认）时递归子目录；False 时仅扫描顶层文件。
    默认跳过 .gif；保留 jpg/jpeg/png/webp/bmp/tif/tiff，以及 .pdf。
    """
    folder = Path(folder)
    allowed = set(IMAGE_EXTS)
    if include_pdf:
        allowed.add(PDF_EXT)
    if not skip_gif:
        allowed = set(allowed) | {".gif"}
    found: List[Path] = []
    skip_dir_names = {"translated_zh"}
    try:
        import translate_local as _tl

        skip_dir_names.add(getattr(_tl, "TRANSLATED_SUBDIR", "translated_zh"))
    except ImportError:
        pass
    iterator = folder.rglob("*") if recurse else folder.iterdir()
    for p in iterator:
        if not p.is_file():
            continue
        # Skip outputs living under translated_zh/ (and any *_zh names)
        if any(part in skip_dir_names for part in p.parts):
            continue
        if p.stem.endswith("_zh"):
            continue
        suf = p.suffix.lower()
        if suf not in allowed:
            continue
        found.append(p)
    found.sort(key=lambda p: natural_key(p.relative_to(folder).as_posix()))
    return found



def decode_drop_path(raw) -> str:
    """Normalize a Windows drop path (bytes or str) to a filesystem str."""
    if isinstance(raw, bytes):
        for enc in ("utf-8", "mbcs", "gbk", "cp936"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def resolve_dropped_paths(
    dropped,
    *,
    recurse: bool = True,
) -> List[Path]:
    """Turn Windows drop items into image/PDF paths for the file list.

    - Files: keep only MEDIA_EXTS (images + PDF; gif skipped like folder scan).
    - Directories: scan with ``list_images_in_folder(..., recurse=recurse)``
      so the GUI ``include_subfolders`` checkbox is honored.
    - Order: preserve drop order; within each folder, natural relative sort.
    - Deduplicate while preserving first occurrence.
    """
    out: List[Path] = []
    seen: Set[Path] = set()

    def _add(p: Path) -> None:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    for raw in dropped or ():
        try:
            path = Path(decode_drop_path(raw))
        except (TypeError, ValueError, OSError):
            continue
        try:
            if path.is_dir():
                for item in list_images_in_folder(path, recurse=recurse):
                    _add(item)
            elif path.is_file():
                suf = path.suffix.lower()
                if suf in MEDIA_EXTS:
                    _add(path)
        except OSError:
            continue
    return out



def drop_error_log_path() -> Path:
    """Log file for drag-drop failures (pythonw has no console)."""
    base = os.environ.get("APPDATA") or os.environ.get("TEMP") or str(Path.cwd())
    return Path(base) / "img-stitch-pdf" / "drop_errors.log"


def log_drop_exception(where: str) -> None:
    """Append traceback for a drop-handler failure."""
    try:
        path = drop_error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} [{where}] ---\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def hook_win_dropfiles(tk_widget, on_files: Callable) -> bool:
    """Enable WM_DROPFILES on a Tk widget with a crash-safe Unicode WndProc.

    Critical: ``on_files`` must NOT call into Tk/Tcl (no ``after``, widget
    updates, etc.). Explorer delivers WM_DROPFILES via the Win32 message pump;
    touching Tk from the subclassed WndProc re-enters Tcl and aborts the
    process (``PyEval_RestoreThread`` / flash-crash). Only enqueue paths;
    drain from a poller started with ``root.after`` *before* any drop.

    Also avoids ``windnd``'s 64-bit bugs (``SetWindowLongPtrA``, byte-sized
    ``DragQueryFileW`` buffers). Uses ``*W`` APIs and keeps the ctypes
    callback alive on the widget.
    """
    if sys.platform != "win32" or tk_widget is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    try:
        hwnd = int(tk_widget.winfo_id())
    except Exception:
        return False
    if not hwnd:
        return False

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    is64 = ctypes.sizeof(ctypes.c_void_p) == 8

    if is64:
        GetWindowLongPtr = user32.GetWindowLongPtrW
        SetWindowLongPtr = user32.SetWindowLongPtrW
    else:
        GetWindowLongPtr = user32.GetWindowLongW
        SetWindowLongPtr = user32.SetWindowLongW

    GetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int]
    GetWindowLongPtr.restype = ctypes.c_void_p
    SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    SetWindowLongPtr.restype = ctypes.c_void_p
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [wintypes.HANDLE]

    GWL_WNDPROC = -4
    WM_DROPFILES = 0x0233
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    old_raw = GetWindowLongPtr(hwnd, GWL_WNDPROC)
    old_proc = int(old_raw) if old_raw else 0
    if not old_proc:
        return False

    def _query_paths(hdrop) -> list:
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(int(count)):
            # First call with NULL returns required character count (without NUL).
            nchars = shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(int(nchars) + 1)
            shell32.DragQueryFileW(hdrop, i, buf, int(nchars) + 1)
            if buf.value:
                out.append(buf.value)
        return out

    @WNDPROC
    def wndproc(hwnd_, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            try:
                paths = _query_paths(wparam)
                try:
                    # Must not touch Tk here — see docstring.
                    on_files(paths)
                except Exception:
                    log_drop_exception("drop_callback")
            except Exception:
                log_drop_exception("DragQueryFileW")
            try:
                shell32.DragFinish(wparam)
            except Exception:
                log_drop_exception("DragFinish")
            return 0
        try:
            return int(
                user32.CallWindowProcW(old_proc, hwnd_, msg, wparam, lparam) or 0
            )
        except Exception:
            log_drop_exception("CallWindowProcW")
            return 0

    # Prevent GC of the callback while the HWND is subclassed.
    refs = getattr(tk_widget, "_win_drop_hook_refs", None)
    if refs is None:
        refs = []
        try:
            tk_widget._win_drop_hook_refs = refs  # type: ignore[attr-defined]
        except Exception:
            pass
    refs.append((wndproc, old_proc, hwnd))

    shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
    new_ptr = ctypes.cast(wndproc, ctypes.c_void_p).value
    SetWindowLongPtr(hwnd, GWL_WNDPROC, new_ptr)
    return True



def move_items_to_index(
    items: Sequence,
    selected_indices: Sequence[int],
    target_1based: int,
) -> List:
    """将选中项移到 1-based 目标位置，返回新列表。

    - selected_indices：0-based，可乱序；按原相对顺序组成一块
    - target_1based：第一块选中项在结果中的目标序号（夹到 1..len）
    - 未选中项保持相对顺序
    """
    items_list = list(items)
    n = len(items_list)
    if n == 0 or not selected_indices:
        return items_list
    try:
        target = int(target_1based)
    except (TypeError, ValueError):
        raise ValueError("目标位置必须是整数") from None
    target = max(1, min(target, n))
    idx_set = {i for i in selected_indices if isinstance(i, int) and 0 <= i < n}
    if not idx_set:
        return items_list
    ordered = sorted(idx_set)
    moving = [items_list[i] for i in ordered]
    remaining = [items_list[i] for i in range(n) if i not in idx_set]
    insert_at = max(0, min(target - 1, len(remaining)))
    return remaining[:insert_at] + moving + remaining[insert_at:]


def default_pdf_name(paths: List[Path], source_folder: Optional[Path]) -> str:
    """默认 PDF 文件名：文件夹名 / 首文件 stem / merge.pdf。"""
    if source_folder is not None:
        name = source_folder.name.strip()
        if name:
            return f"{name}.pdf"
    if paths:
        parents = {p.resolve().parent for p in paths}
        if len(parents) == 1:
            folder_name = next(iter(parents)).name.strip()
            if folder_name:
                return f"{folder_name}.pdf"
        return f"{paths[0].stem}.pdf"
    return "merge.pdf"


def _rgba_to_rgb(img: Image.Image) -> Image.Image:
    """透明通道铺白底后转 RGB。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode == "RGB":
        return img
    return img.convert("RGB")


def _prepare_img2pdf_input(path: Path) -> Img2pdfInput:
    """准备单张图给 img2pdf：JPEG 原路径嵌入；其余转 PNG 字节（不建巨图）。"""
    path = Path(path)
    suf = path.suffix.lower()

    # JPEG：原样嵌入，不解码、不重压缩
    if suf in (".jpg", ".jpeg"):
        return str(path)

    # 非 JPEG：经 Pillow 转 PNG 字节再交给 img2pdf（无损路径，一图一页）
    with Image.open(path) as im:
        im.load()
        rgb = _rgba_to_rgb(im)
        buf = io.BytesIO()
        rgb.save(buf, format="PNG", optimize=False)
        return buf.getvalue()


def images_to_multipage_pdf(
    paths: Sequence[Path],
    out_path: Path,
    progress_callback: Optional[ProgressCallback] = None,
) -> int:
    """将多张图片导出为多页 PDF（一图一页，列表顺序）。返回页数。

    不拼接成单张巨图。优先 img2pdf；不可用时退回 Pillow 多页 PDF。
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("没有可导出的图片")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(paths)

    def report(i: int) -> None:
        if progress_callback is not None:
            progress_callback(i, n)

    if img2pdf is not None:
        prepared: List[Img2pdfInput] = []
        for i, p in enumerate(paths, start=1):
            report(i)
            prepared.append(_prepare_img2pdf_input(p))
        with open(out_path, "wb") as f:
            f.write(img2pdf.convert(prepared))
        return n

    # 无 img2pdf：Pillow 逐页写入，仍不拼接
    pil_images: List[Image.Image] = []
    try:
        for i, p in enumerate(paths, start=1):
            report(i)
            im = Image.open(p)
            im.load()
            pil_images.append(_rgba_to_rgb(im))
        first, rest = pil_images[0], pil_images[1:]
        first.save(out_path, "PDF", save_all=True, append_images=rest, resolution=100.0)
    finally:
        for im in pil_images:
            try:
                im.close()
            except Exception:
                pass
    return n


def _require_pypdf() -> None:
    if PdfWriter is None or PdfReader is None:
        raise ImportError(
            "合并 PDF 需要 pypdf。请执行: pip install pypdf"
        )


def merge_pdf_files(
    pdf_paths: Sequence[Path],
    out_path: Path,
    progress_callback: Optional[ProgressCallback] = None,
) -> int:
    """按列表顺序合并多个 PDF 的全部页（不重新栅格化）。返回总页数。"""
    _require_pypdf()
    paths = [Path(p) for p in pdf_paths]
    if not paths:
        raise ValueError("没有可合并的 PDF")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(paths)
    writer = PdfWriter()
    total_pages = 0

    for i, p in enumerate(paths, start=1):
        if progress_callback is not None:
            progress_callback(i, n)
        reader = PdfReader(str(p))
        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1

    with open(out_path, "wb") as f:
        writer.write(f)
    return total_pages


def _image_to_temp_pdf(path: Path, tmp_dir: Path) -> Path:
    """把单张图片写成临时单页 PDF，供后续与其它 PDF 合并。"""
    tmp_pdf = tmp_dir / f"img_{path.stem}_{id(path)}.pdf"
    images_to_multipage_pdf([path], tmp_pdf)
    return tmp_pdf


def items_to_multipage_pdf(
    paths: Sequence[Path],
    out_path: Path,
    progress_callback: Optional[ProgressCallback] = None,
) -> int:
    """将图片与/或 PDF 按列表顺序导出为一个多页 PDF。返回总页数。

    - 仅图片：走 images_to_multipage_pdf（img2pdf）
    - 仅 PDF：pypdf 合并页，不栅格化
    - 混合：图片先转临时 PDF，再与 PDF 页按顺序合并
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("没有可导出的文件")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pdfs = [p for p in paths if is_pdf(p)]
    images = [p for p in paths if is_image(p)]
    unknown = [p for p in paths if not is_pdf(p) and not is_image(p)]
    if unknown:
        raise ValueError(f"不支持的文件类型: {unknown[0].suffix}")

    n = len(paths)

    def report(i: int) -> None:
        if progress_callback is not None:
            progress_callback(i, n)

    # 纯图片：原有路径
    if not pdfs:
        return images_to_multipage_pdf(paths, out_path, progress_callback=progress_callback)

    # 纯 PDF：直接合并
    if not images:
        return merge_pdf_files(paths, out_path, progress_callback=progress_callback)

    # 混合：逐项生成 PDF 片段再合并
    _require_pypdf()
    writer = PdfWriter()
    total_pages = 0
    with tempfile.TemporaryDirectory(prefix="img_stitch_pdf_") as tmp:
        tmp_dir = Path(tmp)
        for i, p in enumerate(paths, start=1):
            report(i)
            if is_pdf(p):
                reader = PdfReader(str(p))
                for page in reader.pages:
                    writer.add_page(page)
                    total_pages += 1
            else:
                seg = _image_to_temp_pdf(p, tmp_dir)
                reader = PdfReader(str(seg))
                for page in reader.pages:
                    writer.add_page(page)
                    total_pages += 1
        with open(out_path, "wb") as f:
            writer.write(f)
    return total_pages



APP_NAME = "img-stitch-pdf"
CONFIG_FILENAME = "config.json"


def config_dir() -> Path:
    """用户级配置目录：Windows 用 %APPDATA%/img-stitch-pdf，其它用 ~/.config/img-stitch-pdf。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Roaming" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def suggested_default_output_dir() -> Path:
    """首次建议的默认导出目录：~/Documents/PDF导出（跨平台友好）。"""
    docs = Path.home() / "Documents"
    # Windows 上 Documents 可能在 OneDrive 下；仍优先 ~/Documents
    if sys.platform == "win32":
        try:
            import ctypes.wintypes

            CSIDL_PERSONAL = 5  # My Documents
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, buf
            )
            if buf.value:
                docs = Path(buf.value)
        except Exception:
            pass
    return docs / "PDF导出"


def load_config() -> dict:
    """读取 JSON 配置；文件不存在或损坏时返回空 dict。"""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> Path:
    """写入配置文件，自动创建配置目录。返回配置文件路径。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data) if isinstance(data, dict) else {}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def get_output_dir() -> Optional[Path]:
    """返回已配置的 output_dir（Path）；未设置或无效则 None。"""
    raw = load_config().get("output_dir")
    if raw is None or raw == "":
        return None
    try:
        p = Path(str(raw)).expanduser()
    except (TypeError, ValueError):
        return None
    return p


def set_output_dir(path: Union[str, Path]) -> Path:
    """设置并持久化 output_dir，返回规范化 Path。"""
    out = Path(path).expanduser().resolve()
    data = load_config()
    data["output_dir"] = str(out)
    save_config(data)
    return out


DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"


def get_ollama_model_config() -> Optional[str]:
    """Return persisted ``ollama_model`` from config.json, or None if unset."""
    raw = load_config().get("ollama_model")
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None


def set_ollama_model_config(model: str) -> str:
    """Persist ``ollama_model`` in config.json and apply to translate_local if loaded."""
    name = (model or "").strip() or DEFAULT_OLLAMA_MODEL
    data = load_config()
    data["ollama_model"] = name
    save_config(data)
    if translate_local is not None and hasattr(translate_local, "set_ollama_model"):
        translate_local.set_ollama_model(name)
    return name


def list_ollama_models_for_ui() -> List[str]:
    """Model names from Ollama (``/api/tags``, same as ``ollama list``). Empty if down."""
    try:
        tl = _ensure_translate_local()
    except ImportError:
        return []
    try:
        names = list(tl.ollama_list_models())
    except Exception:
        return []
    seen = set()
    out: List[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def resolve_ollama_model_choice(available: Optional[Sequence[str]] = None) -> str:
    """Pick model: config → qwen2.5:14b if present → first available → default."""
    names = list(available) if available is not None else list_ollama_models_for_ui()
    cfg = get_ollama_model_config()
    if cfg:
        if not names or cfg in names:
            return cfg
        for n in names:
            if n.startswith(cfg) or cfg in n:
                return n
        return cfg
    prefer = DEFAULT_OLLAMA_MODEL
    if prefer in names:
        return prefer
    for n in names:
        if n.startswith(prefer) or prefer in n:
            return n
    if names:
        return names[0]
    if translate_local is not None and hasattr(translate_local, "get_ollama_model"):
        return translate_local.get_ollama_model()
    return prefer


def apply_ollama_model_to_runtime(model: Optional[str] = None) -> str:
    """Resolve choice and push selection into config + translate_local."""
    names = list_ollama_models_for_ui()
    chosen = (model or "").strip() or resolve_ollama_model_choice(names)
    set_ollama_model_config(chosen)
    return chosen




def ensure_default_output_dir() -> Path:
    """若尚未配置 output_dir，则创建建议目录并写入配置；返回当前默认目录。"""
    existing = get_output_dir()
    if existing is not None:
        try:
            existing.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return existing
    suggested = suggested_default_output_dir()
    suggested.mkdir(parents=True, exist_ok=True)
    return set_output_dir(suggested)



def find_pythonw() -> Path:
    """查找 pythonw.exe（尽量与当前解释器同目录）。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return candidate
    for name in ("pythonw.exe", "pythonw"):
        which = _which(name)
        if which:
            return Path(which)
    return exe


def _which(cmd: str) -> Optional[str]:
    from shutil import which

    return which(cmd)


def create_desktop_shortcut(
    project_dir: Optional[Path] = None,
    shortcut_name: str = "图片拼接转PDF.lnk",
) -> Path:
    """在用户桌面创建 .lnk：用 pythonw 启动 main.py，工作目录=项目目录。"""
    project_dir = Path(project_dir or PROJECT_DIR).resolve()
    main_py = project_dir / "main.py"
    if not main_py.is_file():
        raise FileNotFoundError(f"找不到 main.py: {main_py}")

    desktop = Path(os.path.join(os.path.expanduser("~"), "Desktop"))
    if sys.platform == "win32":
        try:
            import ctypes.wintypes

            CSIDL_DESKTOP = 0
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_DESKTOP, None, SHGFP_TYPE_CURRENT, buf
            )
            if buf.value:
                desktop = Path(buf.value)
        except Exception:
            pass
        one = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop"
        if not desktop.is_dir() and one.is_dir():
            desktop = one

    desktop.mkdir(parents=True, exist_ok=True)
    lnk_path = desktop / shortcut_name
    pythonw = find_pythonw()

    if sys.platform == "win32":
        ps = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{str(lnk_path).replace("'", "''")}')
$Shortcut.TargetPath = '{str(pythonw).replace("'", "''")}'
$Shortcut.Arguments = '"{str(main_py).replace("'", "''")}"'
$Shortcut.WorkingDirectory = '{str(project_dir).replace("'", "''")}'
$Shortcut.WindowStyle = 1
$Shortcut.Description = '图片/PDF 导出为多页 PDF'
$Shortcut.Save()
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        sh_path = desktop / "图片导出PDF.sh"
        sh_path.write_text(
            f"#!/bin/sh\ncd '{project_dir}' && exec '{sys.executable}' main.py\n",
            encoding="utf-8",
        )
        sh_path.chmod(0o755)
        return sh_path

    return lnk_path



DEFAULT_OCR_ENGINE = "paddle"


def get_ocr_engine_config():
    """Return persisted ``ocr_engine`` from config.json, or None if unset."""
    raw = load_config().get("ocr_engine")
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None


def set_ocr_engine_config(engine: str) -> str:
    """Persist ``ocr_engine`` (paddle|easyocr) and apply to translate_local."""
    name = (engine or "").strip().lower() or DEFAULT_OCR_ENGINE
    if name not in ("paddle", "easyocr"):
        name = DEFAULT_OCR_ENGINE
    data = load_config()
    data["ocr_engine"] = name
    save_config(data)
    if translate_local is not None and hasattr(translate_local, "set_ocr_engine"):
        translate_local.set_ocr_engine(name)
    return name


def resolve_ocr_engine_choice() -> str:
    """Pick OCR engine: config -> translate_local default -> paddle."""
    cfg = get_ocr_engine_config()
    if cfg in ("paddle", "easyocr"):
        return cfg
    if translate_local is not None and hasattr(translate_local, "get_ocr_engine"):
        return translate_local.get_ocr_engine()
    return DEFAULT_OCR_ENGINE


def apply_ocr_engine_to_runtime(engine=None) -> str:
    """Resolve choice and push into config + translate_local."""
    chosen = (engine or "").strip() or resolve_ocr_engine_choice()
    return set_ocr_engine_config(chosen)


def get_include_subfolders_config() -> bool:
    """Return persisted ``include_subfolders`` (default True = recurse)."""
    raw = load_config().get("include_subfolders")
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def set_include_subfolders_config(value: bool) -> bool:
    """Persist ``include_subfolders`` in config.json."""
    flag = bool(value)
    data = load_config()
    data["include_subfolders"] = flag
    save_config(data)
    return flag


DEFAULT_OCR_DOWNSCALE_ENABLED = False
DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE = 1600


def get_ocr_downscale_enabled_config() -> bool:
    """Return persisted ``ocr_downscale`` (default False = off)."""
    raw = load_config().get("ocr_downscale")
    if raw is None:
        return DEFAULT_OCR_DOWNSCALE_ENABLED
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def get_ocr_downscale_max_long_side_config() -> int:
    """Return persisted ``ocr_downscale_max_long_side`` (default 1600)."""
    raw = load_config().get("ocr_downscale_max_long_side")
    if raw is None or raw == "":
        return DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE


def set_ocr_downscale_config(enabled: bool, max_long_side=None):
    """Persist OCR downscale settings and apply to translate_local if loaded."""
    flag = bool(enabled)
    if max_long_side is None:
        side = get_ocr_downscale_max_long_side_config()
    else:
        try:
            side = max(1, int(max_long_side))
        except (TypeError, ValueError):
            side = DEFAULT_OCR_DOWNSCALE_MAX_LONG_SIDE
    data = load_config()
    data["ocr_downscale"] = flag
    data["ocr_downscale_max_long_side"] = side
    save_config(data)
    if translate_local is not None and hasattr(translate_local, "set_ocr_downscale"):
        translate_local.set_ocr_downscale(flag, side)
    return flag, side


def apply_ocr_downscale_to_runtime(enabled=None, max_long_side=None):
    """Resolve choice from args/config and push into config + translate_local."""
    flag = (
        get_ocr_downscale_enabled_config()
        if enabled is None
        else bool(enabled)
    )
    side = (
        get_ocr_downscale_max_long_side_config()
        if max_long_side is None
        else max_long_side
    )
    return set_ocr_downscale_config(flag, side)


# ---------------------------------------------------------------- video download (yt-dlp)


def get_video_download_dir_config() -> Path:
    """Return persisted ``video_download_dir`` (default %USERPROFILE%\\Downloads)."""
    import video_dl

    raw = load_config().get("video_download_dir")
    if raw:
        try:
            return Path(str(raw)).expanduser()
        except (TypeError, ValueError):
            pass
    return video_dl.default_download_dir()


def set_video_download_dir_config(path: Union[str, Path]) -> Path:
    """Persist ``video_download_dir`` in config.json."""
    out = Path(path).expanduser().resolve()
    data = load_config()
    data["video_download_dir"] = str(out)
    save_config(data)
    return out


def get_video_cookies_browser_config() -> str:
    """Return persisted ``video_cookies_browser`` (无/chrome/edge/firefox; default 无)."""
    import video_dl

    raw = load_config().get("video_cookies_browser")
    norm = video_dl.normalize_cookies_browser(raw if isinstance(raw, str) else None)
    return norm or video_dl.COOKIE_BROWSER_NONE


def set_video_cookies_browser_config(value: Optional[str]) -> str:
    import video_dl

    norm = video_dl.normalize_cookies_browser(value) or video_dl.COOKIE_BROWSER_NONE
    data = load_config()
    data["video_cookies_browser"] = norm
    save_config(data)
    return norm


def get_video_no_playlist_config() -> bool:
    """Return persisted ``video_no_playlist`` (default True)."""
    raw = load_config().get("video_no_playlist")
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def set_video_no_playlist_config(value: bool) -> bool:
    flag = bool(value)
    data = load_config()
    data["video_no_playlist"] = flag
    save_config(data)
    return flag


def open_folder_in_explorer(path: Path) -> None:
    """Open *path* in the OS file browser (Explorer / Finder / xdg-open)."""
    p = Path(path)
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


class VideoDownloadDialog(tk.Toplevel if tk is not None else object):  # type: ignore[misc]
    """「下载视频」窗口：粘贴链接 → yt-dlp 顺序下载。

    后台线程只往 ``self._events`` 放元组；Tk 只在主线程经 ``after()`` 轮询处理
    （Tk 不可重入，worker 里碰 Tk 会导致闪退）。
    """

    POLL_MS = 100

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        import video_dl

        self._vd = video_dl
        self.app = app
        self.title("下载视频（yt-dlp）")
        self.geometry("820x600")
        self.minsize(640, 460)
        self.transient(app)
        try:
            self.configure(background=app.UI_BG)
        except tk.TclError:
            pass

        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._runner = None
        self._busy = False  # download / pip in progress
        self._pip_cancel = threading.Event()
        self._ytdlp_base: Optional[List[str]] = None
        self._ffmpeg: Optional[str] = None
        self._ffmpeg_warned = False
        self._asked_install = False
        self._poll_job: Optional[str] = None
        self._last_file: Optional[str] = None

        self.out_dir_var = tk.StringVar(value=str(get_video_download_dir_config()))
        self.no_playlist_var = tk.BooleanVar(value=get_video_no_playlist_config())
        self.cookies_var = tk.StringVar(value=get_video_cookies_browser_config())
        self.tool_var = tk.StringVar(value="正在检测 yt-dlp / ffmpeg…")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="")

        self._build()
        self._prefill_from_clipboard()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(self.POLL_MS, self._poll_events)
        self._probe_tools_async()

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        app = self.app
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        url_outer, url_fr = app._make_section(root, "视频链接（每行一个，按顺序下载）")
        url_outer.pack(fill=tk.X, **pad)
        txt_fr = ttk.Frame(url_fr, style="Card.TFrame")
        txt_fr.pack(fill=tk.X)
        self.url_text = tk.Text(txt_fr, height=5, wrap=tk.NONE, undo=True)
        self.url_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        url_btns = ttk.Frame(txt_fr, style="Card.TFrame")
        url_btns.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))
        ttk.Button(url_btns, text="粘贴", command=self._paste).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(url_btns, text="清空", command=self._clear_urls).pack(fill=tk.X)

        opt_outer, opt_fr = app._make_section(root, "选项")
        opt_outer.pack(fill=tk.X, **pad)
        row1 = ttk.Frame(opt_fr, style="Card.TFrame")
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="保存到：", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.out_dir_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(row1, text="选择文件夹…", command=self._choose_dir).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="打开文件夹", command=self._open_dir).pack(side=tk.LEFT, padx=2)
        row2 = ttk.Frame(opt_fr, style="Card.TFrame")
        row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(
            row2,
            text="只下载单个视频（--no-playlist）",
            variable=self.no_playlist_var,
            command=lambda: set_video_no_playlist_config(bool(self.no_playlist_var.get())),
        ).pack(side=tk.LEFT)
        ttk.Label(row2, text="浏览器 Cookies：", style="Card.TLabel").pack(
            side=tk.LEFT, padx=(16, 0)
        )
        combo = ttk.Combobox(
            row2,
            textvariable=self.cookies_var,
            values=list(self._vd.COOKIE_BROWSERS),
            width=10,
            state="readonly",
        )
        combo.pack(side=tk.LEFT)
        combo.bind(
            "<<ComboboxSelected>>",
            lambda e: set_video_cookies_browser_config(self.cookies_var.get()),
        )
        row3 = ttk.Frame(opt_fr, style="Card.TFrame")
        row3.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row3, textvariable=self.tool_var, style="Muted.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self._install_btn = ttk.Button(
            row3, text="安装 yt-dlp", command=lambda: self._pip_ytdlp(upgrade=False)
        )
        self._update_btn = ttk.Button(
            row3, text="更新 yt-dlp", command=lambda: self._pip_ytdlp(upgrade=True)
        )
        self._update_btn.pack(side=tk.RIGHT, padx=2)

        run_outer, run_fr = app._make_section(root, "下载", export=True)
        run_outer.pack(fill=tk.BOTH, expand=True, **pad)
        prow = ttk.Frame(run_fr, style="Card.TFrame")
        prow.pack(fill=tk.X)
        ttk.Progressbar(
            prow, variable=self.progress_var, maximum=100.0, mode="determinate"
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(prow, textvariable=self.progress_text_var, style="Card.TLabel", width=46).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        log_fr = ttk.Frame(run_fr, style="Card.TFrame")
        log_fr.pack(fill=tk.BOTH, expand=True, pady=(6, 4))
        self.log_text = tk.Text(log_fr, height=10, wrap=tk.WORD, state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_fr, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.LEFT, fill=tk.Y)
        brow = ttk.Frame(run_fr, style="Card.TFrame")
        brow.pack(fill=tk.X)
        self._download_btn = ttk.Button(
            brow, text="下载", command=self._start_download
        )
        self._download_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._cancel_btn = ttk.Button(
            brow, text="取消", command=self._cancel, state=tk.DISABLED
        )
        self._cancel_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(brow, text="关闭", command=self._on_close).pack(side=tk.RIGHT)

    # ------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        # keep the widget light on long downloads
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _emit(self, event: tuple) -> None:
        """Thread-safe: worker threads only enqueue."""
        self._events.put(event)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._download_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._cancel_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)
        st = tk.DISABLED if busy else tk.NORMAL
        self._update_btn.configure(state=st)
        self._install_btn.configure(state=st)

    def _read_clipboard(self) -> str:
        try:
            return self.clipboard_get()
        except tk.TclError:
            return ""

    def _prefill_from_clipboard(self) -> None:
        clip = self._read_clipboard()
        urls = self._vd.split_urls(clip)
        if urls:
            self.url_text.insert("1.0", "\n".join(urls) + "\n")

    def _paste(self) -> None:
        clip = self._read_clipboard()
        urls = self._vd.split_urls(clip)
        if not urls:
            messagebox.showinfo("提示", "剪贴板里没有 http(s) 链接。", parent=self)
            return
        existing = self._vd.split_urls(self.url_text.get("1.0", tk.END))
        new = [u for u in urls if u not in existing]
        cur = self.url_text.get("1.0", "end-1c")
        prefix = "\n" if cur.strip() and not cur.endswith("\n") else ""
        if new:
            self.url_text.insert(tk.END, prefix + "\n".join(new) + "\n")
        self.url_text.see(tk.END)

    def _clear_urls(self) -> None:
        self.url_text.delete("1.0", tk.END)

    def _current_out_dir(self) -> Path:
        raw = (self.out_dir_var.get() or "").strip()
        return Path(raw).expanduser() if raw else self._vd.default_download_dir()

    def _choose_dir(self) -> None:
        cur = self._current_out_dir()
        chosen = filedialog.askdirectory(
            title="选择视频保存文件夹",
            initialdir=str(cur) if cur.is_dir() else str(Path.home()),
            parent=self,
        )
        if not chosen:
            return
        out = set_video_download_dir_config(chosen)
        self.out_dir_var.set(str(out))

    def _open_dir(self) -> None:
        d = self._current_out_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            open_folder_in_explorer(d)
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self)

    # ------------------------------------------------------------ tools
    def _probe_tools_async(self) -> None:
        vd = self._vd

        def worker() -> None:
            try:
                info = vd.probe_tools()
            except Exception as e:  # pragma: no cover - defensive
                info = {"ytdlp": None, "ytdlp_version": None, "ffmpeg": None, "error": str(e)}
            self._emit(("tools", info))

        threading.Thread(target=worker, daemon=True, name="ytdlp-probe").start()

    def _apply_tools(self, info: dict) -> None:
        vd = self._vd
        self._ytdlp_base = info.get("ytdlp")  # type: ignore[assignment]
        self._ffmpeg = info.get("ffmpeg")  # type: ignore[assignment]
        if self._ytdlp_base:
            how = (
                "PATH"
                if len(self._ytdlp_base) == 1
                else "python -m yt_dlp"
            )
            ver = info.get("ytdlp_version") or "版本未知"
            ytxt = f"yt-dlp {ver}（{how}）"
            self._install_btn.pack_forget()
        else:
            ytxt = "未找到 yt-dlp"
            if not self._install_btn.winfo_manager():
                self._install_btn.pack(side=tk.RIGHT, padx=2)
        ftxt = "ffmpeg ✓" if self._ffmpeg else f"ffmpeg 未找到（{vd.FFMPEG_INSTALL_HINT}）"
        self.tool_var.set(f"{ytxt} · {ftxt}")
        if not self._ytdlp_base:
            self._log("未找到 yt-dlp。点「安装 yt-dlp」可一键 pip install yt-dlp。")
            if not self._busy and not self._asked_install:
                self._asked_install = True
                if messagebox.askyesno(
                    "未找到 yt-dlp",
                    "没有找到 yt-dlp。\n现在用 pip 安装吗？（pip install yt-dlp）",
                    parent=self,
                ):
                    self._pip_ytdlp(upgrade=False)
        if not self._ffmpeg:
            self._log(vd.ffmpeg_missing_message())

    def _pip_ytdlp(self, upgrade: bool) -> None:
        if self._busy:
            return
        vd = self._vd
        cmd = vd.pip_install_command(upgrade=upgrade)
        self._set_busy(True)
        self._pip_cancel.clear()
        self.progress_text_var.set("正在更新 yt-dlp…" if upgrade else "正在安装 yt-dlp…")
        self._log("> " + subprocess.list2cmdline(cmd))

        def worker() -> None:
            try:
                rc = vd.stream_command(
                    cmd, lambda line: self._emit(("log", line)), self._pip_cancel
                )
            except Exception as e:
                self._emit(("log", f"pip 失败：{e}"))
                rc = -1
            self._emit(("pip_done", rc, upgrade))

        threading.Thread(target=worker, daemon=True, name="ytdlp-pip").start()

    # ------------------------------------------------------------ download
    def _start_download(self) -> None:
        if self._busy:
            return
        vd = self._vd
        urls = vd.split_urls(self.url_text.get("1.0", tk.END))
        if not urls:
            messagebox.showwarning("提示", "请先粘贴视频链接（http/https，每行一个）。", parent=self)
            return
        if not self._ytdlp_base:
            if messagebox.askyesno(
                "未找到 yt-dlp", "没有找到 yt-dlp，现在用 pip 安装吗？", parent=self
            ):
                self._pip_ytdlp(upgrade=False)
            return
        if not self._ffmpeg and not self._ffmpeg_warned:
            self._ffmpeg_warned = True
            if not messagebox.askyesno(
                "未找到 ffmpeg",
                vd.ffmpeg_missing_message() + "\n\n仍然继续下载吗？",
                parent=self,
            ):
                return
        out_dir = self._current_out_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("目录错误", f"无法创建保存目录：{e}", parent=self)
            return
        set_video_download_dir_config(out_dir)
        set_video_no_playlist_config(bool(self.no_playlist_var.get()))
        cookies = set_video_cookies_browser_config(self.cookies_var.get())

        self._runner = vd.DownloadRunner(
            self._ytdlp_base,
            urls,
            out_dir,
            no_playlist=bool(self.no_playlist_var.get()),
            cookies_browser=cookies,
            ffmpeg_location=vd.ffmpeg_location_arg(self._ffmpeg),
            on_event=self._emit,
        )
        self._last_file = None
        self.progress_var.set(0.0)
        self.progress_text_var.set(f"准备下载 {len(urls)} 个链接…")
        self._set_busy(True)
        self._log(f"开始下载 {len(urls)} 个链接 → {out_dir}")
        runner = self._runner

        def worker() -> None:
            try:
                runner.run()
            except Exception as e:
                self._emit(("log", f"下载出错：{e}"))
                self._emit(("finished", 0, len(urls), runner.cancelled))

        threading.Thread(target=worker, daemon=True, name="ytdlp-download").start()
        self.app._set_status(f"视频下载中（{len(urls)} 个链接）…")

    def _cancel(self) -> None:
        if not self._busy:
            return
        self.progress_text_var.set("正在取消…")
        self._pip_cancel.set()
        runner = self._runner
        if runner is not None:
            # taskkill may block briefly; never block Tk
            threading.Thread(target=runner.cancel, daemon=True, name="ytdlp-cancel").start()

    # ------------------------------------------------------------ events (main thread)
    def _poll_events(self) -> None:
        try:
            for _ in range(500):
                ev = self._events.get_nowait()
                try:
                    self._handle_event(ev)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self._poll_job = self.after(self.POLL_MS, self._poll_events)
        except tk.TclError:
            self._poll_job = None

    def _handle_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self._log(str(ev[1]))
        elif kind == "tools":
            self._apply_tools(ev[1])
        elif kind == "start":
            _, idx, n, url = ev
            self.progress_var.set(0.0)
            self.progress_text_var.set(f"[{idx}/{n}] 正在解析…")
            self._log(f"—— [{idx}/{n}] {url}")
        elif kind == "progress":
            _, idx, n, info = ev
            self.progress_var.set(float(info["percent"]))
            self.progress_text_var.set(f"[{idx}/{n}] {self._vd.format_progress(info)}")
        elif kind == "file":
            self._last_file = str(ev[3])
        elif kind == "item_done":
            _, idx, n, url, rc = ev
            if rc == 0:
                self.progress_var.set(100.0)
                self._log(f"✓ [{idx}/{n}] 完成")
            elif self._runner is not None and self._runner.cancelled:
                self._log(f"✗ [{idx}/{n}] 已取消")
            else:
                self._log(f"✗ [{idx}/{n}] 失败（退出码 {rc}）")
        elif kind == "finished":
            _, ok, failed, cancelled = ev
            self._runner = None
            self._set_busy(False)
            if cancelled:
                msg = f"已取消。成功 {ok} 个，未完成 {failed} 个。"
            else:
                msg = f"下载结束：成功 {ok} 个，失败 {failed} 个。"
            self.progress_text_var.set(msg)
            self._log(msg)
            self.app._set_status(f"视频{msg}")
        elif kind == "pip_done":
            _, rc, upgrade = ev
            self._set_busy(False)
            what = "更新" if upgrade else "安装"
            if rc == 0:
                self.progress_text_var.set(f"yt-dlp {what}完成")
                self._log(f"yt-dlp {what}完成，重新检测…")
            else:
                self.progress_text_var.set(f"yt-dlp {what}失败（{rc}）")
                self._log(f"yt-dlp {what}失败（退出码 {rc}）")
            self.tool_var.set("正在检测 yt-dlp / ffmpeg…")
            self._probe_tools_async()

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno(
                "正在下载", "仍有任务在运行，取消并关闭窗口吗？", parent=self
            ):
                return
            self._pip_cancel.set()
            runner = self._runner
            if runner is not None:
                threading.Thread(target=runner.cancel, daemon=True, name="ytdlp-cancel").start()
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self.app._video_dialog = None
        self.destroy()


class App(tk.Tk if tk is not None else object):  # type: ignore[misc]
    """桌面 GUI：分区操作、列表/缩略图视图、多页 PDF 导出与本地翻译。"""

    VIEW_LIST = "list"
    VIEW_THUMB = "thumb"
    THUMB_PX = 128
    THUMB_PAD = 8
    THUMB_COLS_MIN = 2

    def __init__(self) -> None:
        super().__init__()
        self.title("图片 / PDF 导出 PDF")
        self.geometry("1040x720")
        self.minsize(900, 580)

        self.paths: List[Path] = []
        self.source_folder: Optional[Path] = None
        self._exporting = False
        self._export_btn: Optional[ttk.Button] = None
        self._translate_export_btn: Optional[ttk.Button] = None
        self._ocr_export_btn: Optional[ttk.Button] = None
        self._ocr_import_btn: Optional[ttk.Button] = None
        self._ocr_translate_btn: Optional[ttk.Button] = None
        self._last_ocr_en_txt: Optional[Path] = None
        self._video_dialog: Optional["VideoDownloadDialog"] = None
        self.output_dir: Path = ensure_default_output_dir()
        self.direct_export_var: Optional[tk.BooleanVar] = None
        self.translate_then_export_var: Optional[tk.BooleanVar] = None
        self.include_subfolders_var: Optional[tk.BooleanVar] = None
        self.output_dir_label_var: Optional[tk.StringVar] = None
        self.ollama_model_var: Optional[tk.StringVar] = None
        self._ollama_model_combo: Optional[ttk.Combobox] = None
        self.ocr_engine_var: Optional[tk.StringVar] = None
        self._ocr_engine_combo: Optional[ttk.Combobox] = None
        self.ocr_downscale_var: Optional[tk.BooleanVar] = None
        self.ocr_downscale_max_var: Optional[tk.StringVar] = None
        self._ocr_downscale_entry: Optional[ttk.Entry] = None

        self.view_mode_var: Optional[tk.StringVar] = None
        self._selected: Set[int] = set()
        self._sel_anchor: Optional[int] = None
        self._thumb_cache: Dict[str, "ImageTk.PhotoImage"] = {}
        self._thumb_widgets: List[dict] = []
        self._thumb_load_job: Optional[str] = None
        self._thumb_pending: List[int] = []
        self._placeholder_photo: Optional["ImageTk.PhotoImage"] = None
        self._pdf_placeholder_photo: Optional["ImageTk.PhotoImage"] = None

        self.listbox: Optional[tk.Listbox] = None
        self._list_frame: Optional[ttk.Frame] = None
        self._thumb_outer: Optional[ttk.Frame] = None
        self._thumb_canvas: Optional[tk.Canvas] = None
        self._thumb_inner: Optional[ttk.Frame] = None
        self._thumb_scroll: Optional[ttk.Scrollbar] = None
        self._view_container: Optional[ttk.Frame] = None
        self._count_var: Optional[tk.StringVar] = None
        self.move_pos_var: Optional[tk.StringVar] = None
        self.move_pos_entry: Optional[ttk.Entry] = None
        self.status: Optional[tk.StringVar] = None

        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._cancel_btn: Optional[ttk.Button] = None
        self._heartbeat_job: Optional[str] = None
        self._status_base: str = ""
        self._job_started_monotonic: float = 0.0

        self._build_ui()
        self._refresh_output_dir_label()
        # Do NOT block Tk startup with Ollama HTTP or translate_local import.
        self.after(150, self._refresh_ollama_model_combo_async)
        self.after(200, self._poll_ui_queue)
        apply_ocr_engine_to_runtime(resolve_ocr_engine_choice())
        apply_ocr_downscale_to_runtime()
        self._update_count_label()
        self._set_status(
            "请添加图片、PDF 或文件夹，也可拖放到文件列表。添加后可切换「列表 / 缩略图」视图；"
            "用「移到第…位」或双击调整顺序。"
            f" 默认导出目录：{self.output_dir}"
        )

    # ------------------------------------------------------------------ UI
    # Soft accent palette (not garish): soft blue / teal / slate
    UI_BG = "#f4f7fb"
    UI_CARD = "#ffffff"
    UI_HEADER_BG = "#e8f0fe"
    UI_ACCENT = "#4a7fd4"
    UI_ACCENT_SOFT = "#5b8fa8"
    UI_MUTED = "#5f6b7a"
    UI_BORDER = "#d5dde8"
    UI_EXPORT_BG = "#e8f5ef"
    UI_EXPORT_ACCENT = "#3d8b6e"
    UI_HINT_BG = "#f0f4f8"

    def _apply_ttk_theme(self) -> None:
        """clam theme + light accent styles for clearer section scanning."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = self.UI_BG
        card = self.UI_CARD
        accent = self.UI_ACCENT
        muted = self.UI_MUTED
        border = self.UI_BORDER
        self.configure(background=bg)
        style.configure(".", background=bg, foreground="#1f2933")
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure(
            "Header.TLabel",
            background=self.UI_HEADER_BG,
            foreground=accent,
            font=("Segoe UI", 10, "bold"),
            padding=(10, 5),
        )
        style.configure(
            "HeaderExport.TLabel",
            background=self.UI_EXPORT_BG,
            foreground=self.UI_EXPORT_ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(10, 5),
        )
        style.configure(
            "Muted.TLabel",
            background=card,
            foreground=muted,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Card.TLabel",
            background=card,
            foreground="#1f2933",
        )
        style.configure(
            "Hint.TLabel",
            background=self.UI_HINT_BG,
            foreground=muted,
            font=("Segoe UI", 9),
        )
        style.configure(
            "TLabelframe",
            background=card,
            bordercolor=border,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=card,
            foreground=accent,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Accent.TLabelframe",
            background=card,
            bordercolor=accent,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Accent.TLabelframe.Label",
            background=card,
            foreground=accent,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Export.TLabelframe",
            background=card,
            bordercolor=self.UI_EXPORT_ACCENT,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Export.TLabelframe.Label",
            background=card,
            foreground=self.UI_EXPORT_ACCENT,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("TButton", padding=(8, 4))
        style.configure("TCheckbutton", background=card)
        style.configure("TRadiobutton", background=card)
        style.configure("TCombobox", padding=2)
        style.map(
            "TButton",
            background=[("active", self.UI_HEADER_BG)],
        )

    def _make_section(
        self,
        parent,
        title: str,
        *,
        export: bool = False,
    ):
        """Card with colored header strip + content frame (easier to scan)."""
        outer = ttk.Frame(parent, style="Card.TFrame")
        header_bg = self.UI_EXPORT_BG if export else self.UI_HEADER_BG
        accent = self.UI_EXPORT_ACCENT if export else self.UI_ACCENT
        # Colored left bar + header
        head = tk.Frame(outer, background=header_bg, highlightthickness=0)
        head.pack(fill=tk.X)
        bar = tk.Frame(head, background=accent, width=4)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        hdr_style = "HeaderExport.TLabel" if export else "Header.TLabel"
        ttk.Label(head, text=title, style=hdr_style).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        body = ttk.Frame(outer, style="Card.TFrame", padding=(8, 6, 8, 6))
        body.pack(fill=tk.BOTH, expand=True)
        # Subtle bottom border via tk frame
        tk.Frame(outer, background=self.UI_BORDER, height=1).pack(fill=tk.X)
        return outer, body

    def _build_ui(self) -> None:
        self._apply_ttk_theme()
        pad = {"padx": 10, "pady": 5}
        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # --- 添加 ---
        add_outer, add_fr = self._make_section(root, "添加")
        add_outer.pack(fill=tk.X, **pad)
        ttk.Button(add_fr, text="添加图片…", command=self.add_images).pack(
            side=tk.LEFT, padx=(0, 4), pady=2
        )
        ttk.Button(add_fr, text="添加 PDF…", command=self.add_pdfs).pack(
            side=tk.LEFT, padx=4, pady=2
        )
        ttk.Button(add_fr, text="添加文件夹…", command=self.add_folder).pack(
            side=tk.LEFT, padx=4, pady=2
        )
        self.include_subfolders_var = tk.BooleanVar(
            value=get_include_subfolders_config()
        )
        ttk.Checkbutton(
            add_fr,
            text="包含子文件夹",
            variable=self.include_subfolders_var,
            command=self._on_include_subfolders_toggle,
        ).pack(side=tk.LEFT, padx=(8, 4), pady=2)
        self._count_var = tk.StringVar(value="共 0 项")
        ttk.Label(add_fr, textvariable=self._count_var, style="Card.TLabel").pack(
            side=tk.RIGHT, padx=(10, 0), pady=2
        )

        # --- 排序 ---
        sort_outer, sort_fr = self._make_section(root, "排序")
        sort_outer.pack(fill=tk.X, **pad)
        ttk.Button(sort_fr, text="上移", command=lambda: self.move_selected(-1)).pack(
            side=tk.LEFT, padx=(0, 4), pady=2
        )
        ttk.Button(sort_fr, text="下移", command=lambda: self.move_selected(1)).pack(
            side=tk.LEFT, padx=4, pady=2
        )
        self.move_pos_var = tk.StringVar(value="1")
        self.move_pos_entry = ttk.Entry(sort_fr, width=5, textvariable=self.move_pos_var)
        self.move_pos_entry.pack(side=tk.LEFT, padx=(10, 2), pady=2)
        ttk.Button(
            sort_fr, text="移到第…位", command=self.move_selected_to_position
        ).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(sort_fr, text="移除选中", command=self.remove_selected).pack(
            side=tk.LEFT, padx=4, pady=2
        )
        ttk.Button(sort_fr, text="清空列表", command=self.clear_list).pack(
            side=tk.LEFT, padx=4, pady=2
        )

        # --- 视图 ---
        view_outer, view_bar = self._make_section(root, "视图")
        view_outer.pack(fill=tk.X, **pad)
        self.view_mode_var = tk.StringVar(value=self.VIEW_LIST)
        ttk.Radiobutton(
            view_bar,
            text="列表",
            value=self.VIEW_LIST,
            variable=self.view_mode_var,
            command=self._on_view_mode_change,
        ).pack(side=tk.LEFT, padx=(0, 4), pady=2)
        ttk.Radiobutton(
            view_bar,
            text="缩略图",
            value=self.VIEW_THUMB,
            variable=self.view_mode_var,
            command=self._on_view_mode_change,
        ).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Label(
            view_bar,
            text="添加后可随时切换；缩略图支持多选后移除/移位。",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=10, pady=2)

        # --- 内容区（列表 / 缩略图）---
        list_outer, list_body = self._make_section(root, "文件列表")
        list_outer.pack(fill=tk.BOTH, expand=True, **pad)
        ttk.Label(
            list_body,
            text="可将图片 / PDF / 文件夹拖放到下方列表（子文件夹随「包含子文件夹」）",
            style="Muted.TLabel",
        ).pack(fill=tk.X, pady=(0, 4))
        self._view_container = ttk.Frame(list_body, style="Card.TFrame")
        self._view_container.pack(fill=tk.BOTH, expand=True)

        self._list_frame = ttk.Frame(self._view_container, style="Card.TFrame")
        self.listbox = tk.Listbox(
            self._list_frame,
            selectmode=tk.EXTENDED,
            activestyle="dotbox",
            background="#ffffff",
            foreground="#1f2933",
            selectbackground="#4a7fd4",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground=self.UI_BORDER,
            relief=tk.FLAT,
            borderwidth=0,
        )
        list_scroll = ttk.Scrollbar(
            self._list_frame, orient=tk.VERTICAL, command=self.listbox.yview
        )
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-Button-1>", self._on_list_double_click)
        self.listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        self._thumb_outer = ttk.Frame(self._view_container, style="Card.TFrame")
        self._thumb_canvas = tk.Canvas(
            self._thumb_outer,
            highlightthickness=0,
            background="#eef2f7",
        )
        self._thumb_scroll = ttk.Scrollbar(
            self._thumb_outer, orient=tk.VERTICAL, command=self._thumb_canvas.yview
        )
        self._thumb_inner = ttk.Frame(self._thumb_canvas, style="Card.TFrame")
        self._thumb_inner.bind(
            "<Configure>",
            lambda e: self._thumb_canvas.configure(
                scrollregion=self._thumb_canvas.bbox("all")
            ),
        )
        self._thumb_window = self._thumb_canvas.create_window(
            (0, 0), window=self._thumb_inner, anchor="nw"
        )
        self._thumb_canvas.configure(yscrollcommand=self._thumb_scroll.set)
        self._thumb_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._thumb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._thumb_canvas.bind("<Configure>", self._on_thumb_canvas_configure)
        # Mouse wheel (Windows / macOS / Linux)
        self._thumb_canvas.bind("<Enter>", self._bind_thumb_wheel)
        self._thumb_canvas.bind("<Leave>", self._unbind_thumb_wheel)
        self._thumb_inner.bind("<Enter>", self._bind_thumb_wheel)
        self._thumb_inner.bind("<Leave>", self._unbind_thumb_wheel)

        self._show_list_view()

        self._setup_file_drop()

        # --- 设定 ---
        settings_outer, settings_fr = self._make_section(root, "设定")
        settings_outer.pack(fill=tk.X, **pad)
        ttk.Label(settings_fr, text="默认导出目录：", style="Card.TLabel").pack(
            side=tk.LEFT, padx=(0, 0), pady=2
        )
        self.output_dir_label_var = tk.StringVar(value="")
        ttk.Label(
            settings_fr,
            textvariable=self.output_dir_label_var,
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, pady=2)
        ttk.Button(
            settings_fr,
            text="设定默认导出文件夹…",
            command=self.set_default_output_folder,
        ).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(
            settings_fr, text="下载视频…", command=self.open_video_download
        ).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(
            settings_fr, text="创建桌面快捷方式", command=self.create_shortcut
        ).pack(side=tk.LEFT, padx=(4, 0), pady=2)

        # --- 翻译 ---
        tr_outer, tr_fr = self._make_section(root, "翻译")
        tr_outer.pack(fill=tk.X, **pad)
        tr_row1 = ttk.Frame(tr_fr, style="Card.TFrame")
        tr_row1.pack(fill=tk.X)
        ttk.Label(tr_row1, text="Ollama 模型：", style="Card.TLabel").pack(
            side=tk.LEFT, padx=(0, 0), pady=2
        )
        self.ollama_model_var = tk.StringVar(value=resolve_ollama_model_choice())
        self._ollama_model_combo = ttk.Combobox(
            tr_row1,
            textvariable=self.ollama_model_var,
            width=28,
            state="readonly",
        )
        self._ollama_model_combo.pack(side=tk.LEFT, padx=(0, 4), pady=2)
        self._ollama_model_combo.bind(
            "<<ComboboxSelected>>", self._on_ollama_model_selected
        )
        ttk.Button(
            tr_row1, text="刷新模型列表", command=self._refresh_ollama_model_combo_async
        ).pack(side=tk.LEFT, padx=2, pady=2)
        ttk.Label(tr_row1, text="OCR：", style="Card.TLabel").pack(
            side=tk.LEFT, padx=(12, 0), pady=2
        )
        self.ocr_engine_var = tk.StringVar(value=resolve_ocr_engine_choice())
        self._ocr_engine_combo = ttk.Combobox(
            tr_row1,
            textvariable=self.ocr_engine_var,
            values=["paddle", "easyocr"],
            width=10,
            state="readonly",
        )
        self._ocr_engine_combo.pack(side=tk.LEFT, padx=(0, 4), pady=2)
        self._ocr_engine_combo.bind(
            "<<ComboboxSelected>>", self._on_ocr_engine_selected
        )
        ttk.Label(
            tr_row1,
            text="（默认 paddle，失败回退 easyocr）",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=6, pady=2)

        tr_row2 = ttk.Frame(tr_fr, style="Card.TFrame")
        tr_row2.pack(fill=tk.X, pady=(4, 0))
        self.ocr_downscale_var = tk.BooleanVar(
            value=get_ocr_downscale_enabled_config()
        )
        ttk.Checkbutton(
            tr_row2,
            text="OCR降采样",
            variable=self.ocr_downscale_var,
            command=self._on_ocr_downscale_toggle,
        ).pack(side=tk.LEFT, padx=(0, 4), pady=2)
        ttk.Label(tr_row2, text="最长边：", style="Card.TLabel").pack(
            side=tk.LEFT, padx=(8, 0), pady=2
        )
        self.ocr_downscale_max_var = tk.StringVar(
            value=str(get_ocr_downscale_max_long_side_config())
        )
        self._ocr_downscale_entry = ttk.Entry(
            tr_row2, width=7, textvariable=self.ocr_downscale_max_var
        )
        self._ocr_downscale_entry.pack(side=tk.LEFT, padx=(0, 4), pady=2)
        self._ocr_downscale_entry.bind(
            "<FocusOut>", self._on_ocr_downscale_max_commit
        )
        self._ocr_downscale_entry.bind(
            "<Return>", self._on_ocr_downscale_max_commit
        )
        ttk.Label(
            tr_row2,
            text="（默认关；开启后 OCR 前缩小到最长边，默认 1600）",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=6, pady=2)
        self._sync_ocr_downscale_entry_state()

        # --- 导出 ---
        export_outer, export_fr = self._make_section(root, "导出", export=True)
        export_outer.pack(fill=tk.X, **pad)
        self._export_btn = ttk.Button(
            export_fr, text="生成 PDF…", command=self.export_pdf
        )
        self._export_btn.pack(side=tk.LEFT, padx=(0, 4), pady=2)
        self.direct_export_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            export_fr,
            text="直接导出到默认文件夹",
            variable=self.direct_export_var,
        ).pack(side=tk.LEFT, padx=6, pady=2)
        self.translate_then_export_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            export_fr,
            text="本地翻译后导出",
            variable=self.translate_then_export_var,
        ).pack(side=tk.LEFT, padx=4, pady=2)
        self._translate_export_btn = ttk.Button(
            export_fr, text="本地翻译后导出…", command=self.export_pdf_translated
        )
        self._translate_export_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._ocr_export_btn = ttk.Button(
            export_fr,
            text="导出英文OCR文本",
            command=self.export_ocr_english_txt,
        )
        self._ocr_export_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._ocr_translate_btn = ttk.Button(
            export_fr,
            text="本地翻译OCR文本",
            command=self.translate_ocr_english_txt,
        )
        self._ocr_translate_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._ocr_import_btn = ttk.Button(
            export_fr,
            text="导入译文并生成图/PDF",
            command=self.import_translated_txt,
        )
        self._ocr_import_btn.pack(side=tk.LEFT, padx=4, pady=2)
        self._cancel_btn = ttk.Button(
            export_fr, text="取消", command=self.cancel_export, state=tk.DISABLED
        )
        self._cancel_btn.pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(export_fr, text="退出", command=self.destroy).pack(
            side=tk.RIGHT, padx=(4, 0), pady=2
        )

        # Compact hint under export (was separate LabelFrame)
        hint = tk.Frame(root, background=self.UI_HINT_BG)
        hint.pack(fill=tk.X, padx=10, pady=(0, 4))
        ttk.Label(
            hint,
            text="多页 PDF：列表顺序 = 页序。图片各占一页（JPEG 尽量原样嵌入）；"
            "PDF 直接合并页。翻译后写入 translated_zh/（不覆盖原图），跳过 PDF/GIF。"
            "也可「导出英文OCR文本」→「本地翻译OCR文本」（或外部翻译）→「导入译文并生成图/PDF」（===PAGE NNN===）。",
            style="Hint.TLabel",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=10, pady=6)

        self.status = tk.StringVar(value="")
        status_fr = tk.Frame(self, background=self.UI_HEADER_BG)
        status_fr.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(status_fr, background=self.UI_ACCENT, width=4).pack(
            side=tk.LEFT, fill=tk.Y
        )
        ttk.Label(
            status_fr,
            text="状态",
            style="Header.TLabel",
        ).pack(side=tk.LEFT, padx=(4, 6))
        ttk.Label(
            status_fr,
            textvariable=self.status,
            relief=tk.FLAT,
            anchor=tk.W,
            padding=(6, 6),
            background=self.UI_BG,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _update_count_label(self) -> None:
        if self._count_var is None:
            return
        n = len(self.paths)
        n_img = sum(1 for p in self.paths if is_image(p))
        n_pdf = sum(1 for p in self.paths if is_pdf(p))
        self._count_var.set(f"共 {n} 项（图 {n_img} / PDF {n_pdf}）")

    # ---------------------------------------------------------- view modes
    def _current_view(self) -> str:
        if self.view_mode_var is None:
            return self.VIEW_LIST
        return self.view_mode_var.get() or self.VIEW_LIST

    def _on_view_mode_change(self) -> None:
        # Radio already updated view_mode_var; sync selection from the panel still mapped.
        if self._list_frame is not None and self._list_frame.winfo_ismapped():
            if self.listbox is not None:
                try:
                    self._selected = set(self.listbox.curselection())
                except tk.TclError:
                    pass
        if self._current_view() == self.VIEW_THUMB:
            self._show_thumb_view()
        else:
            self._show_list_view()
        self._refresh_view(keep_selection=True)
        mode_zh = "缩略图" if self._current_view() == self.VIEW_THUMB else "列表"
        self._set_status(f"已切换到「{mode_zh}」视图，共 {len(self.paths)} 项。")

    def _show_list_view(self) -> None:
        if self._thumb_outer is not None:
            self._thumb_outer.pack_forget()
        if self._list_frame is not None:
            self._list_frame.pack(fill=tk.BOTH, expand=True)

    def _show_thumb_view(self) -> None:
        if self._list_frame is not None:
            self._list_frame.pack_forget()
        if self._thumb_outer is not None:
            self._thumb_outer.pack(fill=tk.BOTH, expand=True)

    def _get_selection(self) -> List[int]:
        if self._current_view() == self.VIEW_LIST and self.listbox is not None:
            try:
                return list(self.listbox.curselection())
            except tk.TclError:
                return sorted(self._selected)
        return sorted(i for i in self._selected if 0 <= i < len(self.paths))

    def _set_selection(
        self, indices: Sequence[int], *, see: Optional[int] = None
    ) -> None:
        valid = {i for i in indices if isinstance(i, int) and 0 <= i < len(self.paths)}
        self._selected = valid
        if self.listbox is not None:
            self.listbox.selection_clear(0, tk.END)
            for idx in valid:
                self.listbox.selection_set(idx)
            if see is not None and 0 <= see < len(self.paths):
                self.listbox.see(see)
        self._paint_thumb_selection()
        if see is not None and self._current_view() == self.VIEW_THUMB:
            self._thumb_see(see)

    def _on_listbox_select(self, _event=None) -> None:
        if self.listbox is None:
            return
        try:
            self._selected = set(self.listbox.curselection())
        except tk.TclError:
            pass

    def _refresh_list(self, keep_selection: bool = False) -> None:
        """兼容旧名：刷新当前视图。"""
        self._refresh_view(keep_selection=keep_selection)

    def _refresh_view(self, keep_selection: bool = False) -> None:
        if keep_selection:
            sel = self._get_selection()
            if not sel:
                sel = sorted(self._selected)
        else:
            sel = sorted(i for i in self._selected if 0 <= i < len(self.paths))
        if self.listbox is not None:
            self.listbox.delete(0, tk.END)
            for i, path in enumerate(self.paths, start=1):
                kind = "PDF" if is_pdf(path) else "图"
                self.listbox.insert(tk.END, f"{i}. [{kind}] {path.name}  —  {path}")
        if self._current_view() == self.VIEW_THUMB:
            self._rebuild_thumb_grid()
        self._set_selection(sel)
        self._update_count_label()

    # ------------------------------------------------------- thumbnails
    def _ensure_placeholders(self) -> None:
        if self._placeholder_photo is not None:
            return
        if ImageTk is None or ImageDraw is None:
            raise RuntimeError("缩略图需要 Pillow 的 ImageTk / ImageDraw 支持。")
        size = self.THUMB_PX
        # Generic image placeholder
        img = Image.new("RGB", (size, size), (230, 230, 230))
        draw = ImageDraw.Draw(img)
        draw.rectangle([4, 4, size - 5, size - 5], outline=(160, 160, 160), width=2)
        draw.line([(20, size - 30), (size // 2, 40), (size - 20, size - 30)], fill=(160, 160, 160), width=2)
        self._placeholder_photo = ImageTk.PhotoImage(img)
        # PDF placeholder
        pdf = Image.new("RGB", (size, size), (245, 235, 230))
        d2 = ImageDraw.Draw(pdf)
        d2.rectangle([12, 8, size - 12, size - 8], fill=(255, 255, 255), outline=(180, 60, 60), width=2)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        text = "PDF"
        if font is not None:
            bbox = d2.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            d2.text(((size - tw) / 2, (size - th) / 2), text, fill=(180, 60, 60), font=font)
        else:
            d2.text((size // 2 - 10, size // 2 - 5), text, fill=(180, 60, 60))
        self._pdf_placeholder_photo = ImageTk.PhotoImage(pdf)

    def _thumb_cache_key(self, path: Path) -> str:
        try:
            st = path.stat()
            return f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{self.THUMB_PX}"
        except OSError:
            return f"{path}|missing|{self.THUMB_PX}"

    def _make_thumb_image(self, path: Path) -> "ImageTk.PhotoImage":
        self._ensure_placeholders()
        key = self._thumb_cache_key(path)
        cached = self._thumb_cache.get(key)
        if cached is not None:
            return cached
        if is_pdf(path):
            assert self._pdf_placeholder_photo is not None
            self._thumb_cache[key] = self._pdf_placeholder_photo
            return self._pdf_placeholder_photo
        if not is_image(path):
            assert self._placeholder_photo is not None
            self._thumb_cache[key] = self._placeholder_photo
            return self._placeholder_photo
        try:
            with Image.open(path) as im:
                im.load()
                im = im.convert("RGB") if im.mode not in ("RGB", "L") else im.convert("RGB")
                im.thumbnail((self.THUMB_PX, self.THUMB_PX), getattr(Image, "Resampling", Image).LANCZOS)
                canvas = Image.new("RGB", (self.THUMB_PX, self.THUMB_PX), (245, 245, 245))
                ox = (self.THUMB_PX - im.width) // 2
                oy = (self.THUMB_PX - im.height) // 2
                canvas.paste(im, (ox, oy))
                photo = ImageTk.PhotoImage(canvas)
        except Exception:
            assert self._placeholder_photo is not None
            photo = self._placeholder_photo
        self._thumb_cache[key] = photo
        # Bound cache size
        if len(self._thumb_cache) > 400:
            # drop arbitrary older keys (keep placeholders)
            drop = [k for k in self._thumb_cache if k not in (key,)]
            for k in drop[:100]:
                self._thumb_cache.pop(k, None)
        return photo

    def _on_thumb_canvas_configure(self, event) -> None:
        if self._thumb_canvas is None:
            return
        self._thumb_canvas.itemconfigure(self._thumb_window, width=event.width)
        # Re-flow columns when width changes
        if self._current_view() == self.VIEW_THUMB and self._thumb_widgets:
            self._reflow_thumb_grid()

    def _thumb_cols(self) -> int:
        if self._thumb_canvas is None:
            return self.THUMB_COLS_MIN
        w = max(self._thumb_canvas.winfo_width(), 200)
        cell = self.THUMB_PX + 24 + self.THUMB_PAD * 2
        return max(self.THUMB_COLS_MIN, w // cell)

    def _rebuild_thumb_grid(self) -> None:
        if self._thumb_inner is None:
            return
        self._cancel_thumb_loads()
        for child in self._thumb_inner.winfo_children():
            child.destroy()
        self._thumb_widgets = []
        self._ensure_placeholders()
        assert self._placeholder_photo is not None
        for i, path in enumerate(self.paths):
            cell = ttk.Frame(self._thumb_inner, padding=4)
            border = tk.Frame(cell, bd=2, relief=tk.GROOVE, background="#e8e8e8")
            border.pack(fill=tk.BOTH, expand=True)
            img_lbl = tk.Label(
                border,
                image=self._placeholder_photo
                if not is_pdf(path)
                else self._pdf_placeholder_photo,
                background="#f0f0f0",
                width=self.THUMB_PX,
                height=self.THUMB_PX,
            )
            img_lbl.pack(padx=2, pady=2)
            kind = "PDF" if is_pdf(path) else "图"
            name = path.name
            if len(name) > 22:
                name = name[:10] + "…" + name[-9:]
            caption = f"{i + 1}. [{kind}] {name}"
            cap_lbl = tk.Label(
                border,
                text=caption,
                wraplength=self.THUMB_PX + 8,
                justify=tk.CENTER,
                background="#e8e8e8",
                font=("TkDefaultFont", 8),
            )
            cap_lbl.pack(fill=tk.X, padx=2, pady=(0, 2))
            for w in (cell, border, img_lbl, cap_lbl):
                w.bind("<Button-1>", lambda e, idx=i: self._on_thumb_click(e, idx))
                w.bind("<Double-Button-1>", lambda e, idx=i: self._on_thumb_double_click(idx))
                w.bind("<Control-Button-1>", lambda e, idx=i: self._on_thumb_click(e, idx))
                w.bind("<Shift-Button-1>", lambda e, idx=i: self._on_thumb_click(e, idx))
            self._thumb_widgets.append(
                {
                    "index": i,
                    "path": path,
                    "cell": cell,
                    "border": border,
                    "img_lbl": img_lbl,
                    "cap_lbl": cap_lbl,
                    "loaded": False,
                }
            )
        self._reflow_thumb_grid()
        self._paint_thumb_selection()
        self._thumb_pending = list(range(len(self.paths)))
        self._schedule_thumb_loads()

    def _reflow_thumb_grid(self) -> None:
        if not self._thumb_widgets:
            return
        cols = self._thumb_cols()
        for i, info in enumerate(self._thumb_widgets):
            r, c = divmod(i, cols)
            info["cell"].grid(row=r, column=c, padx=self.THUMB_PAD, pady=self.THUMB_PAD, sticky="n")
        if self._thumb_canvas is not None:
            self._thumb_canvas.update_idletasks()
            self._thumb_canvas.configure(scrollregion=self._thumb_canvas.bbox("all"))

    def _paint_thumb_selection(self) -> None:
        for info in self._thumb_widgets:
            idx = info["index"]
            border: tk.Frame = info["border"]
            cap: tk.Label = info["cap_lbl"]
            if idx in self._selected:
                border.configure(background="#3a7cff", bd=3, relief=tk.SOLID)
                cap.configure(background="#3a7cff", foreground="white")
            else:
                border.configure(background="#e8e8e8", bd=2, relief=tk.GROOVE)
                cap.configure(background="#e8e8e8", foreground="black")

    def _on_thumb_click(self, event, idx: int) -> None:
        if self._exporting:
            return
        ctrl = bool(event.state & 0x0004) or bool(event.state & 0x0008)  # Control / Command
        shift = bool(event.state & 0x0001)
        if shift and self._sel_anchor is not None:
            a, b = sorted((self._sel_anchor, idx))
            self._selected = set(range(a, b + 1))
        elif ctrl:
            if idx in self._selected:
                self._selected.discard(idx)
            else:
                self._selected.add(idx)
            self._sel_anchor = idx
        else:
            self._selected = {idx}
            self._sel_anchor = idx
        self._set_selection(self._selected, see=idx)

    def _on_thumb_double_click(self, idx: int) -> None:
        if self._exporting:
            return
        self._selected = {idx}
        self._sel_anchor = idx
        self._set_selection(self._selected)
        # Temporarily ensure listbox also has selection for dialog flow
        self._on_list_double_click()

    def _thumb_see(self, idx: int) -> None:
        if not (0 <= idx < len(self._thumb_widgets)) or self._thumb_canvas is None:
            return
        cell = self._thumb_widgets[idx]["cell"]
        self._thumb_canvas.update_idletasks()
        y = cell.winfo_y()
        h = max(self._thumb_inner.winfo_height(), 1) if self._thumb_inner else 1
        ch = max(self._thumb_canvas.winfo_height(), 1)
        # fraction so cell is in view
        top = max(0.0, (y - 10) / max(h - ch, 1))
        self._thumb_canvas.yview_moveto(top)

    def _cancel_thumb_loads(self) -> None:
        if self._thumb_load_job is not None:
            try:
                self.after_cancel(self._thumb_load_job)
            except Exception:
                pass
            self._thumb_load_job = None
        self._thumb_pending = []

    def _schedule_thumb_loads(self) -> None:
        if self._thumb_load_job is not None:
            return
        self._thumb_load_job = self.after(10, self._load_next_thumbs)

    def _load_next_thumbs(self) -> None:
        self._thumb_load_job = None
        if self._current_view() != self.VIEW_THUMB:
            return
        batch = 6
        loaded_any = False
        while batch > 0 and self._thumb_pending:
            idx = self._thumb_pending.pop(0)
            batch -= 1
            if idx >= len(self._thumb_widgets):
                continue
            info = self._thumb_widgets[idx]
            if info.get("loaded"):
                continue
            path = info["path"]
            photo = self._make_thumb_image(path)
            try:
                info["img_lbl"].configure(image=photo)
                info["img_lbl"].image = photo  # keep ref
                info["loaded"] = True
                loaded_any = True
            except tk.TclError:
                pass
        if self._thumb_pending:
            self._thumb_load_job = self.after(15, self._load_next_thumbs)
        elif loaded_any:
            pass

    def _on_thumb_mousewheel(self, event) -> None:
        if self._thumb_canvas is None:
            return
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            self._thumb_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            self._thumb_canvas.yview_scroll(1, "units")

    def _bind_thumb_wheel(self, _event=None) -> None:
        if self._thumb_canvas is None:
            return
        # Windows / macOS
        self.bind_all("<MouseWheel>", self._on_thumb_mousewheel)
        # Linux
        self.bind_all("<Button-4>", self._on_thumb_mousewheel)
        self.bind_all("<Button-5>", self._on_thumb_mousewheel)

    def _unbind_thumb_wheel(self, _event=None) -> None:
        try:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")
        except tk.TclError:
            pass

    # ----------------------------------------------------------- status
    def _set_status(self, msg: str) -> None:
        if self.status is not None:
            self.status.set(msg)
        self.update_idletasks()

    def _update_source_folder_from_paths(self) -> None:
        """根据当前列表推断 source_folder（同一目录则视为该文件夹）。"""
        if not self.paths:
            self.source_folder = None
            return
        parents = {p.resolve().parent for p in self.paths}
        if len(parents) == 1:
            self.source_folder = next(iter(parents))
        else:
            self.source_folder = None


    def _setup_file_drop(self) -> None:
        """Enable Windows shell drag-and-drop onto the file list / window.

        Prefer ``tkinterdnd2`` (OLE IDropTarget via tkdnd) when installed —
        callbacks run inside Tcl and are safe. Fallback: subclass HWND with
        WM_DROPFILES, but never call Tk from the WndProc (enqueue only; a
        poller started here drains onto the Tk thread). Calling ``after`` from
        WndProc aborts the process under real Explorer drops.
        """
        if sys.platform != "win32":
            return

        self._drop_path_queue: queue.Queue = queue.Queue()

        def _poll_drop_queue() -> None:
            try:
                while True:
                    paths = self._drop_path_queue.get_nowait()
                    self._safe_add_dropped_paths(paths)
            except queue.Empty:
                pass
            except Exception:
                log_drop_exception("poll_drop_queue")
            try:
                self.after(100, _poll_drop_queue)
            except Exception:
                pass

        if self._try_setup_tkdnd_drop():
            return

        def _callback(files) -> None:
            # WndProc context: enqueue only — never touch Tk here.
            try:
                self._drop_path_queue.put(list(files) if files else [])
            except Exception:
                log_drop_exception("drop_queue_put")

        # Hook classic tk HWNDs only (ttk Frames are unnecessary and riskier).
        targets = []
        for w in (
            self,
            self.listbox,
            getattr(self, "_thumb_canvas", None),
        ):
            if w is not None and w not in targets:
                targets.append(w)

        hooked = 0
        for w in targets:
            try:
                if hook_win_dropfiles(w, _callback):
                    hooked += 1
            except Exception:
                log_drop_exception("_setup_file_drop_target")
        if hooked == 0:
            try:
                path = drop_error_log_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        "\n--- %s [hook_win_dropfiles: no targets hooked] ---\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S")
                    )
            except Exception:
                pass
            return

        self.after(100, _poll_drop_queue)

    def _try_setup_tkdnd_drop(self) -> bool:
        """Register OLE drop targets via tkinterdnd2/tkdnd if available."""
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
        except ImportError:
            return False
        try:
            TkinterDnD._require(self)
        except Exception:
            log_drop_exception("tkdnd_require")
            return False

        def on_drop(event):
            try:
                data = getattr(event, "data", None) or ""
                paths = list(self.tk.splitlist(data)) if data else []
                # <<Drop>> runs inside Tcl — after() is safe here.
                self.after(0, lambda p=paths: self._safe_add_dropped_paths(p))
            except Exception:
                log_drop_exception("tkdnd_drop")
            return "copy"

        targets = []
        for w in (
            self,
            getattr(self, "_view_container", None),
            getattr(self, "_list_frame", None),
            self.listbox,
            getattr(self, "_thumb_canvas", None),
            getattr(self, "_thumb_outer", None),
        ):
            if w is not None and w not in targets:
                targets.append(w)

        hooked = 0
        for w in targets:
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", on_drop)
                hooked += 1
            except Exception:
                log_drop_exception("tkdnd_register_target")
        if hooked == 0:
            return False
        try:
            self._tkdnd_drop_hooked = hooked  # type: ignore[attr-defined]
        except Exception:
            pass
        return True


    def _safe_add_dropped_paths(self, dropped) -> None:
        """Tk-thread wrapper: never let drop handling kill the process."""
        try:
            self.add_dropped_paths(dropped)
        except Exception:
            log_drop_exception("add_dropped_paths")
            try:
                self._set_status(
                    "拖放处理失败，详见 %APPDATA%/img-stitch-pdf/drop_errors.log"
                )
            except Exception:
                pass

    def add_dropped_paths(self, dropped) -> None:
        """Add image/PDF files and folder contents from a Windows file drop."""
        if self._exporting:
            return
        recurse = True
        if self.include_subfolders_var is not None:
            recurse = bool(self.include_subfolders_var.get())
            set_include_subfolders_config(recurse)
        items = resolve_dropped_paths(dropped, recurse=recurse)
        if not items:
            scope = "含所有子目录" if recurse else "仅顶层"
            self._set_status(
                f"拖放未加入任何图片或 PDF（文件夹按「包含子文件夹」={scope} 扫描）。"
            )
            return
        added = 0
        for path in items:
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        # If every drop root that was a folder is a common ancestor, prefer it.
        folder_roots: List[Path] = []
        for raw in dropped or ():
            try:
                p = Path(decode_drop_path(raw))
                if p.is_dir():
                    folder_roots.append(p.resolve())
            except OSError:
                continue
        if len(folder_roots) == 1:
            root = folder_roots[0]

            def _under_root(p: Path) -> bool:
                try:
                    p.resolve().relative_to(root)
                    return True
                except ValueError:
                    return False

            if self.paths and all(_under_root(p) for p in self.paths):
                self.source_folder = root
            else:
                self._update_source_folder_from_paths()
        else:
            self._update_source_folder_from_paths()
        self._refresh_view()
        scope = "含所有子目录" if recurse else "仅顶层"
        self._set_status(
            f"拖放添加了 {added} 项（图片+PDF；文件夹扫描：{scope}），"
            f"当前共 {len(self.paths)} 项。"
        )

    def add_images(self) -> None:

        if self._exporting:
            return
        files = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if not files:
            return
        added = 0
        for f in files:
            path = Path(f)
            if path.suffix.lower() not in IMAGE_EXTS:
                continue
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        self._update_source_folder_from_paths()
        self._refresh_view()
        self._set_status(f"已添加 {added} 张图片，当前共 {len(self.paths)} 项。可切换「列表 / 缩略图」。")

    def add_pdfs(self) -> None:
        if self._exporting:
            return
        files = filedialog.askopenfilenames(
            title="选择 PDF",
            filetypes=[
                ("PDF 文件", "*.pdf"),
                ("所有文件", "*.*"),
            ],
        )
        if not files:
            return
        added = 0
        for f in files:
            path = Path(f)
            if not is_pdf(path):
                continue
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        self._update_source_folder_from_paths()
        self._refresh_view()
        self._set_status(f"已添加 {added} 个 PDF，当前共 {len(self.paths)} 项。可切换「列表 / 缩略图」。")

    def _on_include_subfolders_toggle(self) -> None:
        if self.include_subfolders_var is None:
            return
        set_include_subfolders_config(bool(self.include_subfolders_var.get()))

    def add_folder(self) -> None:
        if self._exporting:
            return
        folder = filedialog.askdirectory(title="选择文件夹（图片与 PDF）")
        if not folder:
            return
        folder_path = Path(folder)
        recurse = True
        if self.include_subfolders_var is not None:
            recurse = bool(self.include_subfolders_var.get())
            set_include_subfolders_config(recurse)
        items = list_images_in_folder(folder_path, recurse=recurse)
        if not items:
            scope = "（含所有子目录）" if recurse else "（仅顶层）"
            messagebox.showwarning(
                "提示",
                f"该文件夹{scope}内没有常见格式的图片或 PDF：\n{folder_path}",
            )
            self._set_status("文件夹内未找到图片或 PDF。")
            return
        added = 0
        for path in items:
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        root = folder_path.resolve()

        def _under_root(p: Path) -> bool:
            try:
                p.resolve().relative_to(root)
                return True
            except ValueError:
                return False

        if self.paths and all(_under_root(p) for p in self.paths):
            self.source_folder = root
        else:
            self._update_source_folder_from_paths()
        self._refresh_view()
        scope = "含所有子目录" if recurse else "仅顶层"
        self._set_status(
            f"从文件夹（{scope}）添加了 {added} 项（图片+PDF，自然排序），"
            f"当前共 {len(self.paths)} 项。"
            f"默认 PDF 名：{default_pdf_name(self.paths, self.source_folder)}"
            " 可切换「列表 / 缩略图」。"
        )

    def move_selected(self, delta: int) -> None:
        if self._exporting:
            return
        sel = self._get_selection()
        if len(sel) != 1:
            self._set_status("请只选择一项再上下移动；多选请用「移到第…位」。")
            return
        i = sel[0]
        j = i + delta
        if j < 0 or j >= len(self.paths):
            return
        self.paths[i], self.paths[j] = self.paths[j], self.paths[i]
        self._selected = {j}
        self._sel_anchor = j
        self._refresh_view(keep_selection=True)
        self._set_selection([j], see=j)
        self._set_status("已调整顺序。")

    def move_selected_to_position(self, target_1based: Optional[int] = None) -> None:
        """将当前选中项移到指定 1-based 位置（支持多选，保持相对顺序）。"""
        if self._exporting:
            return
        sel = self._get_selection()
        if not sel:
            self._set_status("请先选择要移动的项。")
            return
        if target_1based is None:
            raw = (self.move_pos_var.get() if self.move_pos_var else "") or ""
            raw = raw.strip()
            try:
                target_1based = int(raw)
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的目标位置（正整数）。")
                self._set_status("目标位置无效。")
                return
        try:
            self.paths = move_items_to_index(self.paths, sel, target_1based)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            self._set_status(str(e))
            return
        n = len(self.paths)
        start = max(0, min(int(target_1based) - 1, n - len(sel)))
        new_sel = list(range(start, start + len(sel)))
        self._selected = set(new_sel)
        self._sel_anchor = start if new_sel else None
        self._refresh_view(keep_selection=True)
        self._set_selection(new_sel, see=start if new_sel else None)
        self._set_status(f"已移到第 {start + 1} 位起（共 {len(sel)} 项）。")

    def _on_list_double_click(self, _event=None) -> None:
        """双击：弹出对话框输入新的序号。"""
        if self._exporting:
            return
        sel = self._get_selection()
        if not sel:
            return
        current = sel[0] + 1
        dialog = tk.Toplevel(self)
        dialog.title("调整顺序")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(
            dialog, text=f"当前第 {current} 位，移到第几位？（1–{len(self.paths)}）"
        ).pack(padx=12, pady=(12, 4))
        var = tk.StringVar(value=str(current))
        entry = ttk.Entry(dialog, width=8, textvariable=var)
        entry.pack(padx=12, pady=4)
        entry.select_range(0, tk.END)
        entry.focus_set()

        def apply() -> None:
            raw = (var.get() or "").strip()
            try:
                target = int(raw)
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的正整数。", parent=dialog)
                return
            dialog.destroy()
            if self.move_pos_var is not None:
                self.move_pos_var.set(str(target))
            self.move_selected_to_position(target)

        def cancel() -> None:
            dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(pady=8)
        ttk.Button(btns, text="确定", command=apply).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="取消", command=cancel).pack(side=tk.LEFT, padx=4)
        dialog.bind("<Return>", lambda e: apply())
        dialog.bind("<Escape>", lambda e: cancel())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.winfo_rootx() + 80}+{self.winfo_rooty() + 120}")

    def remove_selected(self) -> None:
        if self._exporting:
            return
        sel = sorted(self._get_selection(), reverse=True)
        if not sel:
            self._set_status("请先选中要移除的项。")
            return
        for i in sel:
            del self.paths[i]
        self._selected.clear()
        self._sel_anchor = None
        self._update_source_folder_from_paths()
        self._refresh_view()
        self._set_status(f"已移除，当前共 {len(self.paths)} 项。")

    def clear_list(self) -> None:
        if self._exporting:
            return
        self.paths.clear()
        self.source_folder = None
        self._selected.clear()
        self._sel_anchor = None
        self._cancel_thumb_loads()
        self._thumb_cache.clear()
        self._refresh_view()
        self._set_status("列表已清空。")

    def _refresh_output_dir_label(self) -> None:
        if self.output_dir_label_var is not None:
            self.output_dir_label_var.set(str(self.output_dir))

    def set_default_output_folder(self) -> None:
        """弹出目录选择，持久化默认导出文件夹。"""
        if self._exporting:
            return
        initial = str(self.output_dir) if self.output_dir.is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(
            title="设定默认导出文件夹",
            initialdir=initial,
        )
        if not chosen:
            self._set_status("未更改默认导出目录。")
            return
        self.output_dir = set_output_dir(chosen)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showwarning("提示", f"目录已保存，但创建失败：{e}")
        self._refresh_output_dir_label()
        self._set_status(f"默认导出目录已设为：{self.output_dir}")

    def _export_initial_dir(self) -> str:
        """保存对话框的初始目录：优先已配置的默认导出文件夹。"""
        if self.output_dir is not None:
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            if self.output_dir.is_dir():
                return str(self.output_dir)
        if self.source_folder and self.source_folder.is_dir():
            return str(self.source_folder)
        if self.paths:
            return str(self.paths[0].parent)
        return str(Path.home())

    def _post_ui(self, fn, *args, **kwargs) -> None:
        """Enqueue a callable to run on the Tk main thread (thread-safe)."""
        self._ui_queue.put((fn, args, kwargs))

    def _poll_ui_queue(self) -> None:
        """Drain background→UI messages; never call Tk from worker threads."""
        try:
            while True:
                fn, args, kwargs = self._ui_queue.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self.after(100, self._poll_ui_queue)
        except tk.TclError:
            pass

    def _set_status_threadsafe(self, msg: str) -> None:
        self._post_ui(self._set_status_from_worker, msg)

    def _set_status_from_worker(self, msg: str) -> None:
        self._status_base = msg
        self._set_status(msg)

    def _start_heartbeat(self) -> None:
        self._job_started_monotonic = time.monotonic()
        self._schedule_heartbeat()

    def _schedule_heartbeat(self) -> None:
        if not self._exporting:
            return
        elapsed = int(time.monotonic() - self._job_started_monotonic)
        base = self._status_base or "处理中"
        # Heartbeat keeps Windows from marking the app Not Responding during
        # long Ollama waits (e.g. qwen2.5:32b).
        if elapsed > 0:
            self._set_status(f"{base} · 已运行 {elapsed}s")
        try:
            self.after(3000, self._schedule_heartbeat)
        except tk.TclError:
            pass

    def cancel_export(self) -> None:
        """Request cancel of the running OCR/translate/PDF worker."""
        if not self._exporting:
            return
        self._cancel_event.set()
        self._status_base = "正在取消…"
        self._set_status("正在取消…（等待当前步骤结束）")

    def _cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _refresh_ollama_model_combo_async(self) -> None:
        """Fetch Ollama tags on a background thread; apply on UI thread."""
        self._set_status("正在刷新 Ollama 模型列表…")

        def worker() -> None:
            try:
                names = list_ollama_models_for_ui()
            except Exception:
                names = []
            self._post_ui(self._apply_ollama_model_names, names)

        threading.Thread(target=worker, daemon=True, name="ollama-tags").start()

    def _apply_ollama_model_names(self, names: List[str]) -> None:
        """UI-thread: fill combobox from background-fetched names."""
        chosen = resolve_ollama_model_choice(names)
        values = list(names)
        if chosen and chosen not in values:
            values = [chosen] + values
        if not values:
            values = [chosen or DEFAULT_OLLAMA_MODEL]
        if self._ollama_model_combo is not None:
            self._ollama_model_combo["values"] = values
        if self.ollama_model_var is not None:
            self.ollama_model_var.set(chosen)
        set_ollama_model_config(chosen)
        self._set_status(f"Ollama 模型：{chosen}（共 {len(names)} 个可用）")

    def _refresh_ollama_model_combo(self) -> None:
        """Compat wrapper — prefer async to avoid blocking Tk."""
        self._refresh_ollama_model_combo_async()

    def _on_ollama_model_selected(self, _event=None) -> None:
        if self.ollama_model_var is None:
            return
        name = (self.ollama_model_var.get() or "").strip()
        if not name:
            return
        # Persist only — do not block UI with another /api/tags round-trip.
        set_ollama_model_config(name)
        self._set_status(f"已选择 Ollama 模型：{name}（已写入配置）")

    def _on_ocr_engine_selected(self, _event=None) -> None:
        if self.ocr_engine_var is None:
            return
        name = (self.ocr_engine_var.get() or "").strip().lower()
        if not name:
            return
        apply_ocr_engine_to_runtime(name)
        self._set_status(f"已选择 OCR 引擎：{name}（已写入配置）")


    def _sync_ocr_downscale_entry_state(self) -> None:
        """Enable max-long-side entry only when downscale checkbox is on."""
        if self._ocr_downscale_entry is None or self.ocr_downscale_var is None:
            return
        state = tk.NORMAL if bool(self.ocr_downscale_var.get()) else tk.DISABLED
        self._ocr_downscale_entry.configure(state=state)

    def _on_ocr_downscale_toggle(self) -> None:
        if self.ocr_downscale_var is None:
            return
        enabled = bool(self.ocr_downscale_var.get())
        side = self._parse_ocr_downscale_max()
        apply_ocr_downscale_to_runtime(enabled, side)
        self._sync_ocr_downscale_entry_state()
        if enabled:
            self._set_status(
                f"已开启 OCR 降采样（最长边 {side}，已写入配置）"
            )
        else:
            self._set_status("已关闭 OCR 降采样（已写入配置）")

    def _parse_ocr_downscale_max(self) -> int:
        raw = ""
        if self.ocr_downscale_max_var is not None:
            raw = (self.ocr_downscale_max_var.get() or "").strip()
        try:
            side = max(1, int(raw))
        except (TypeError, ValueError):
            side = get_ocr_downscale_max_long_side_config()
        if self.ocr_downscale_max_var is not None:
            self.ocr_downscale_max_var.set(str(side))
        return side

    def _on_ocr_downscale_max_commit(self, _event=None) -> None:
        enabled = (
            bool(self.ocr_downscale_var.get())
            if self.ocr_downscale_var is not None
            else get_ocr_downscale_enabled_config()
        )
        side = self._parse_ocr_downscale_max()
        apply_ocr_downscale_to_runtime(enabled, side)
        self._set_status(f"OCR 降采样最长边已设为 {side}（已写入配置）")

    def export_ocr_english_txt(self) -> None:

        """OCR all listed images → one UTF-8 .txt with ===PAGE NNN=== keys."""
        if self._exporting:
            return
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("导出列表为空。")
            return
        try:
            tl = _ensure_translate_local()
        except ImportError:
            messagebox.showerror(
                "缺少模块",
                "未找到 translate_local.py。请确认项目文件完整。",
            )
            return

        image_count = sum(
            1
            for p in self.paths
            if is_image(p) and not tl.is_already_translated_name(p)
        )
        if image_count == 0:
            messagebox.showwarning(
                "提示",
                "当前列表没有可 OCR 的图片（PDF/GIF/译图会跳过）。",
            )
            return

        default_name = "ocr_english.txt"
        if self.source_folder is not None and self.source_folder.name.strip():
            default_name = f"{self.source_folder.name.strip()}_ocr_en.txt"
        out = filedialog.asksaveasfilename(
            title="保存英文 OCR 文本",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=default_name,
            initialdir=self._export_initial_dir(),
        )
        if not out:
            self._set_status("已取消 OCR 文本导出。")
            return
        out_path = Path(out)

        paths_snapshot = list(self.paths)
        root = self.source_folder
        ocr_choice = (
            (self.ocr_engine_var.get() or "").strip()
            if self.ocr_engine_var is not None
            else ""
        )
        self._set_exporting(True)
        self._status_base = f"准备 OCR 导出… 0/{image_count}"
        self._set_status(self._status_base)

        def worker() -> None:
            note = ""
            try:
                tl = _ensure_translate_local()

                def on_dep(msg: str) -> None:
                    self._set_status_threadsafe(msg)

                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if ocr_choice:
                    apply_ocr_engine_to_runtime(ocr_choice)
                else:
                    apply_ocr_engine_to_runtime()
                apply_ocr_downscale_to_runtime(
                    bool(self.ocr_downscale_var.get())
                    if self.ocr_downscale_var is not None
                    else None,
                    self._parse_ocr_downscale_max()
                    if self.ocr_downscale_max_var is not None
                    else None,
                )
                # OCR-only export: still use ensure_deps (keeps Ollama path intact
                # for the separate「本地翻译后导出」button).
                tl.ensure_deps(progress_callback=on_dep)
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                try:
                    tl._ocr_backend.preload_ocr(
                        tl.get_ocr_engine(), progress_callback=on_dep
                    )
                except Exception as warm_err:
                    on_dep(f"OCR 预加载提示：{warm_err}")
                if self._cancelled():
                    raise tl.CancelledError("已取消")

                def on_tr(i: int, n: int, msg: str) -> None:
                    self._set_status_threadsafe(f"OCR 导出 {i}/{n}：{msg}")

                text_body, image_paths, warnings = tl.export_ocr_text_for_paths(
                    paths_snapshot,
                    root=root,
                    progress_callback=on_tr,
                    cancel_check=self._cancelled,
                )
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if not image_paths:
                    raise RuntimeError(
                        "没有可 OCR 的图片。"
                        + (" ".join(warnings) if warnings else "")
                    )
                out_path.write_text(text_body, encoding="utf-8")
                note_parts = [
                    f"已写入 {len(image_paths)} 页 OCR 文本（UTF-8，===PAGE NNN===）"
                ]
                if warnings:
                    note_parts.append("；".join(warnings[:5]))
                note = "。".join(note_parts)
            except Exception as e:
                traceback.print_exc()
                is_cancel = type(e).__name__ == "CancelledError" or (
                    self._cancelled()
                )
                if is_cancel:
                    self._post_ui(self._on_export_cancelled, str(e))
                else:
                    self._post_ui(self._on_export_error, e)
            else:
                def done() -> None:
                    self._set_exporting(False)
                    self._last_ocr_en_txt = out_path
                    self._set_status(f"OCR 文本已保存：{out_path} — {note}")
                    messagebox.showinfo(
                        "完成",
                        f"已导出英文 OCR 文本：\n{out_path}\n\n{note}\n\n"
                        "可点「本地翻译OCR文本」用 Ollama 翻成中文 TXT，"
                        "或自行翻译后使用「导入译文并生成图/PDF」。"
                        "请保留 ===PAGE NNN=== 与 path: 行。",
                    )

                self._post_ui(done)

        threading.Thread(target=worker, daemon=True, name="ocr-export-txt").start()

    def translate_ocr_english_txt(self) -> None:
        """Pick EN OCR TXT (===PAGE NNN===) → Ollama EN→ZH → save ZH TXT for import."""
        if self._exporting:
            return
        try:
            tl = _ensure_translate_local()
        except ImportError:
            messagebox.showerror(
                "缺少模块",
                "未找到 translate_local.py。请确认项目文件完整。",
            )
            return

        initial = self._export_initial_dir()
        if self._last_ocr_en_txt is not None and self._last_ocr_en_txt.is_file():
            use_last = messagebox.askyesno(
                "本地翻译 OCR 文本",
                f"使用最近导出的英文 OCR 文本？\n{self._last_ocr_en_txt}\n\n"
                "选「否」可重新选择文件。",
            )
            if use_last:
                src_path = self._last_ocr_en_txt
            else:
                src = filedialog.askopenfilename(
                    title="选择英文 OCR 文本（含 ===PAGE NNN===）",
                    filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
                    initialdir=str(self._last_ocr_en_txt.parent),
                )
                if not src:
                    self._set_status("已取消本地翻译。")
                    return
                src_path = Path(src)
        else:
            src = filedialog.askopenfilename(
                title="选择英文 OCR 文本（含 ===PAGE NNN===）",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
                initialdir=initial,
            )
            if not src:
                self._set_status("已取消本地翻译。")
                return
            src_path = Path(src)

        try:
            preview = src_path.read_text(encoding="utf-8")
            blocks = tl.parse_ocr_export_text(preview)
        except Exception as e:
            messagebox.showerror("解析失败", f"无法解析 OCR 文本：\n{e}")
            self._set_status(f"解析 OCR 文本失败：{e}")
            return
        if not blocks:
            messagebox.showwarning("提示", "文件没有有效页块。")
            return

        default_name = src_path.stem
        if default_name.endswith("_ocr_en"):
            default_name = default_name[: -len("_ocr_en")] + "_ocr_zh"
        elif default_name.endswith("_en"):
            default_name = default_name[: -len("_en")] + "_zh"
        else:
            default_name = default_name + "_zh"
        default_name = default_name + ".txt"
        out = filedialog.asksaveasfilename(
            title="保存中文 OCR 译文文本",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=default_name,
            initialdir=str(src_path.parent),
        )
        if not out:
            self._set_status("已取消保存译文。")
            return
        out_path = Path(out)

        model_choice = (
            (self.ollama_model_var.get() or "").strip()
            if self.ollama_model_var is not None
            else ""
        )
        page_n = len(blocks)
        self._set_exporting(True)
        self._status_base = f"本地翻译 OCR 文本：0/{page_n}"
        self._set_status(self._status_base)

        def worker() -> None:
            note = ""
            try:
                tl = _ensure_translate_local()

                def on_dep(msg: str) -> None:
                    self._set_status_threadsafe(msg)

                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if model_choice:
                    set_ollama_model_config(model_choice)
                else:
                    apply_ollama_model_to_runtime()
                # TXT→TXT needs MT only (Ollama preferred).
                tl.ensure_deps(progress_callback=on_dep)
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                _get = getattr(tl, "_get_translator", None)
                if callable(_get):
                    _get()
                backend = getattr(tl, "_mt_backend", None) or "unknown"
                if getattr(tl, "_translate_fn", None) is None:
                    raise RuntimeError(
                        f"翻译器未就绪（backend={backend}）。"
                        "请确认 Ollama 正在运行（ollama serve）且已 pull 所选模型，"
                        "或已安装 Argos en→zh 语言包。"
                    )

                def on_tr(i: int, n: int, msg: str) -> None:
                    self._set_status_threadsafe(f"本地翻译 {i}/{n}：{msg}")

                dst, zh_blocks, warnings = tl.translate_ocr_export_file(
                    src_path,
                    out_path,
                    progress_callback=on_tr,
                    cancel_check=self._cancelled,
                )
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                note_parts = [
                    f"已写入 {len(zh_blocks)} 页中文 TXT（UTF-8，===PAGE NNN===）：{dst}"
                ]
                if warnings:
                    note_parts.append("；".join(warnings[:5]))
                note = " · ".join(note_parts)
            except Exception as e:
                traceback.print_exc()
                is_cancel = type(e).__name__ == "CancelledError" or (
                    self._cancelled()
                )
                if is_cancel:
                    self._post_ui(self._on_export_cancelled, str(e))
                else:
                    self._post_ui(self._on_export_error, e)
            else:
                def done() -> None:
                    self._set_exporting(False)
                    self._last_ocr_en_txt = src_path
                    self._set_status(note)
                    messagebox.showinfo(
                        "完成",
                        f"{note}\n\n"
                        "下一步：点「导入译文并生成图/PDF」选择该中文 TXT"
                        "（列表顺序须与导出时一致）。",
                    )

                self._post_ui(done)

        threading.Thread(
            target=worker, daemon=True, name="ocr-translate-txt"
        ).start()

    def import_translated_txt(self) -> None:
        """Import translated .txt with ===PAGE NNN=== → caption images; optional PDF."""
        if self._exporting:
            return
        if not self.paths:
            messagebox.showwarning(
                "提示",
                "请先添加至少一张图片（顺序须与导出 OCR 文本时一致）。",
            )
            self._set_status("导入列表为空。")
            return
        try:
            tl = _ensure_translate_local()
        except ImportError:
            messagebox.showerror(
                "缺少模块",
                "未找到 translate_local.py。请确认项目文件完整。",
            )
            return

        txt = filedialog.askopenfilename(
            title="选择译文文本（须含 ===PAGE NNN===）",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialdir=self._export_initial_dir(),
        )
        if not txt:
            self._set_status("已取消导入译文。")
            return
        txt_path = Path(txt)
        try:
            content = txt_path.read_text(encoding="utf-8")
            blocks = tl.parse_ocr_export_text(content)
        except Exception as e:
            messagebox.showerror("解析失败", f"无法解析译文文件：\n{e}")
            self._set_status(f"解析译文失败：{e}")
            return
        if not blocks:
            messagebox.showwarning("提示", "译文文件没有有效页块。")
            return

        want_pdf = messagebox.askyesno(
            "导入译文",
            f"已解析 {len(blocks)} 个页块（===PAGE NNN===）。\n\n"
            "将按页序号映射到当前列表中的图片，在下方追加中文译文条，"
            "写入 translated_zh/（不覆盖原图）。\n\n"
            "是否在完成后额外导出多页 PDF？",
        )

        out_path: Optional[Path] = None
        if want_pdf:
            out_path = self._ask_pdf_out_path(
                default_name=default_pdf_name(self.paths, self.source_folder)
            )
            if out_path is None:
                want_pdf = False

        paths_snapshot = list(self.paths)
        self._set_exporting(True)
        self._status_base = f"导入译文并生成图… 0/{len(blocks)}"
        self._set_status(self._status_base)

        def worker() -> None:
            note = ""
            try:
                tl = _ensure_translate_local()

                def on_tr(i: int, n: int, msg: str) -> None:
                    self._set_status_threadsafe(f"写译文图 {i}/{n}：{msg}")

                if self._cancelled():
                    raise tl.CancelledError("已取消")
                translated, warnings = tl.apply_translated_captions(
                    paths_snapshot,
                    blocks,
                    progress_callback=on_tr,
                    cancel_check=self._cancelled,
                )
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if not translated:
                    raise RuntimeError(
                        "没有生成任何译文图。"
                        + (" ".join(warnings) if warnings else "")
                    )

                page_count = 0
                if want_pdf and out_path is not None:
                    def on_progress(i: int, n: int) -> None:
                        self._set_status_threadsafe(f"正在导出 PDF {i}/{n}…")

                    page_count = items_to_multipage_pdf(
                        translated, out_path, progress_callback=on_progress
                    )

                note_parts = [
                    f"已生成 {len(translated)} 张译文图（translated_zh/）"
                ]
                if want_pdf and out_path is not None:
                    note_parts.append(f"并导出 PDF {page_count} 页：{out_path}")
                if warnings:
                    note_parts.append("；".join(warnings[:5]))
                note = "。".join(note_parts)
            except Exception as e:
                traceback.print_exc()
                is_cancel = type(e).__name__ == "CancelledError" or (
                    self._cancelled()
                )
                if is_cancel:
                    self._post_ui(self._on_export_cancelled, str(e))
                else:
                    self._post_ui(self._on_export_error, e)
            else:
                def done() -> None:
                    self._set_exporting(False)
                    self._set_status(note)
                    detail = note
                    if want_pdf and out_path is not None:
                        detail = f"{detail}\n\nPDF：{out_path}"
                    messagebox.showinfo("完成", detail)

                self._post_ui(done)

        threading.Thread(target=worker, daemon=True, name="ocr-import-txt").start()


    def open_video_download(self) -> None:
        """打开「下载视频」窗口（yt-dlp：粘贴链接 → 下载）；已打开则置前。"""
        dlg = self._video_dialog
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    dlg.deiconify()
                    dlg.lift()
                    dlg.focus_set()
                    return
            except tk.TclError:
                pass
        try:
            self._video_dialog = VideoDownloadDialog(self)
        except Exception as e:
            traceback.print_exc()
            self._video_dialog = None
            messagebox.showerror("打开失败", f"无法打开下载视频窗口：{e}")

    def create_shortcut(self) -> None:
        try:
            lnk = create_desktop_shortcut(PROJECT_DIR)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("创建失败", str(e))
            self._set_status(f"创建快捷方式失败：{e}")
            return
        self._set_status(f"已创建桌面快捷方式：{lnk}")
        messagebox.showinfo("完成", f"已创建桌面快捷方式：\n{lnk}")

    def export_pdf(self) -> None:
        """普通导出；若勾选「本地翻译后导出」则走翻译管线。"""
        if self._exporting:
            return
        translate = bool(
            self.translate_then_export_var is not None
            and self.translate_then_export_var.get()
        )
        if translate:
            self.export_pdf_translated()
            return
        self._start_pdf_export(list(self.paths), translate_note=None)

    def export_pdf_translated(self) -> None:
        """本地 EN→ZH 翻译图片到 translated_zh/ 子文件夹，再导出多页 PDF（后台线程）。"""
        if self._exporting:
            return
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("导出列表为空。")
            return
        try:
            _ensure_translate_local()
        except ImportError:
            messagebox.showerror(
                "缺少模块",
                "未找到 translate_local.py。请确认项目文件完整。",
            )
            return

        image_count = sum(1 for p in self.paths if is_image(p))
        pdf_count = sum(1 for p in self.paths if is_pdf(p))
        if image_count == 0:
            messagebox.showwarning(
                "提示",
                "翻译模式只处理图片，当前列表没有可翻译的图片。\n"
                "PDF 不会被翻译；请添加 jpg/png 等图片后再试。",
            )
            self._set_status("翻译模式：无可用图片。")
            return
        if pdf_count:
            ok = messagebox.askokcancel(
                "翻译模式",
                f"列表中有 {pdf_count} 个 PDF，翻译模式将跳过它们，"
                f"仅翻译 {image_count} 张图片后导出 PDF。\n\n是否继续？",
            )
            if not ok:
                self._set_status("已取消翻译导出。")
                return

        out_path = self._ask_pdf_out_path(
            default_name=default_pdf_name(self.paths, self.source_folder)
        )
        if out_path is None:
            return

        paths_snapshot = list(self.paths)
        model_choice = (
            (self.ollama_model_var.get() or "").strip()
            if self.ollama_model_var is not None
            else ""
        )
        ocr_choice = (
            (self.ocr_engine_var.get() or "").strip()
            if self.ocr_engine_var is not None
            else ""
        )
        self._set_exporting(True)
        self._status_base = f"检查/安装翻译依赖后开始本地翻译… 0/{image_count}"
        self._set_status(self._status_base)

        def worker() -> None:
            page_count = 0
            note = ""
            try:
                tl = _ensure_translate_local()

                def on_dep(msg: str) -> None:
                    self._set_status_threadsafe(msg)

                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if model_choice:
                    set_ollama_model_config(model_choice)
                else:
                    apply_ollama_model_to_runtime()
                if ocr_choice:
                    apply_ocr_engine_to_runtime(ocr_choice)
                else:
                    apply_ocr_engine_to_runtime()
                apply_ocr_downscale_to_runtime(
                    bool(self.ocr_downscale_var.get())
                    if self.ocr_downscale_var is not None
                    else None,
                    self._parse_ocr_downscale_max()
                    if self.ocr_downscale_max_var is not None
                    else None,
                )
                tl.ensure_deps(progress_callback=on_dep)
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                on_dep("正在准备 OCR / 翻译模型（首次可能下载）…")
                try:
                    tl.preload_models(progress_callback=on_dep)
                except Exception as warm_err:
                    on_dep(f"模型预加载提示：{warm_err}")
                if self._cancelled():
                    raise tl.CancelledError("已取消")

                def on_tr(i: int, n: int, msg: str) -> None:
                    self._set_status_threadsafe(f"本地翻译 {i}/{n}：{msg}")

                translated, skipped_pdfs, warnings = tl.translate_image_paths(
                    paths_snapshot,
                    progress_callback=on_tr,
                    cancel_check=self._cancelled,
                )
                if self._cancelled():
                    raise tl.CancelledError("已取消")
                if not translated:
                    raise RuntimeError(
                        "没有成功翻译任何图片。"
                        + (" ".join(warnings) if warnings else "")
                    )

                def on_progress(i: int, n: int) -> None:
                    self._set_status_threadsafe(f"正在导出 {i}/{n}…")

                page_count = items_to_multipage_pdf(
                    translated, out_path, progress_callback=on_progress
                )
                note_parts = [
                    f"已生成 {len(translated)} 张译图（写入 translated_zh/ 子文件夹，未覆盖原图）",
                ]
                if skipped_pdfs:
                    note_parts.append(f"跳过 PDF {len(skipped_pdfs)} 个")
                if warnings:
                    note_parts.append("；".join(warnings[:5]))
                note = "。".join(note_parts)
            except Exception as e:
                is_cancel = type(e).__name__ == "CancelledError" or (
                    translate_local is not None
                    and isinstance(e, getattr(translate_local, "CancelledError", ()))
                )
                if is_cancel:
                    self._post_ui(self._on_export_cancelled, str(e))
                    return
                traceback.print_exc()
                self._post_ui(self._on_export_error, e)
            else:
                self._post_ui(
                    self._on_export_success, out_path, page_count, note
                )

        self._worker_thread = threading.Thread(
            target=worker, daemon=True, name="translate-export"
        )
        self._worker_thread.start()

    def _ask_pdf_out_path(self, default_name: str) -> Optional[Path]:
        """Resolve output PDF path via direct-export flag or save dialog."""
        initial_dir = self._export_initial_dir()
        direct = bool(
            self.direct_export_var is not None and self.direct_export_var.get()
        )
        if direct:
            out_path = Path(initial_dir) / default_name
            if out_path.suffix.lower() != ".pdf":
                out_path = out_path.with_suffix(".pdf")
            return out_path
        out = filedialog.asksaveasfilename(
            title="保存 PDF",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile=default_name,
            initialdir=initial_dir,
        )
        if not out:
            self._set_status("已取消导出。")
            return None
        return Path(out)

    def _start_pdf_export(
        self, paths_snapshot: List[Path], translate_note: Optional[str]
    ) -> None:
        if not paths_snapshot:
            messagebox.showwarning("提示", "请先添加至少一张图片或一个 PDF。")
            self._set_status("导出列表为空。")
            return
        out_path = self._ask_pdf_out_path(
            default_name=default_pdf_name(paths_snapshot, self.source_folder)
        )
        if out_path is None:
            return
        self._set_exporting(True)
        self._status_base = f"正在导出多页 PDF：0/{len(paths_snapshot)}…"
        self._set_status(self._status_base)

        def worker() -> None:
            page_count = 0
            try:
                def on_progress(i: int, n: int) -> None:
                    if self._cancelled():
                        raise RuntimeError("已取消导出")
                    self._set_status_threadsafe(f"正在导出 {i}/{n}…")

                page_count = items_to_multipage_pdf(
                    paths_snapshot, out_path, progress_callback=on_progress
                )
                if self._cancelled():
                    raise RuntimeError("已取消导出")
            except Exception as e:
                if "已取消" in str(e):
                    self._post_ui(self._on_export_cancelled, str(e))
                    return
                traceback.print_exc()
                self._post_ui(self._on_export_error, e)
            else:
                self._post_ui(
                    self._on_export_success, out_path, page_count, translate_note
                )

        self._worker_thread = threading.Thread(
            target=worker, daemon=True, name="pdf-export"
        )
        self._worker_thread.start()

    def _set_exporting(self, running: bool) -> None:
        self._exporting = running
        state = tk.DISABLED if running else tk.NORMAL
        if self._export_btn is not None:
            self._export_btn.configure(state=state)
        if self._translate_export_btn is not None:
            self._translate_export_btn.configure(state=state)
        if self._ocr_export_btn is not None:
            self._ocr_export_btn.configure(state=state)
        if self._ocr_import_btn is not None:
            self._ocr_import_btn.configure(state=state)
        if self._ocr_translate_btn is not None:
            self._ocr_translate_btn.configure(state=state)
        if self._cancel_btn is not None:
            self._cancel_btn.configure(
                state=tk.NORMAL if running else tk.DISABLED
            )
        if running:
            self._cancel_event.clear()
            self._start_heartbeat()
        else:
            self._cancel_event.clear()

    def _on_export_success(
        self, out_path: Path, page_count: int, extra: Optional[str] = None
    ) -> None:
        self._set_exporting(False)
        msg = f"导出成功：{out_path}（{page_count} 页）"
        if extra:
            msg = f"{msg} — {extra}"
        self._set_status(msg)
        detail = f"已导出多页 PDF（{page_count} 页）：\n{out_path}"
        if extra:
            detail = f"{detail}\n\n{extra}"
        messagebox.showinfo("完成", detail)

    def _on_export_cancelled(self, detail: str = "") -> None:
        self._set_exporting(False)
        msg = (detail or "").strip() or "已取消"
        self._set_status(msg)
        messagebox.showinfo("已取消", msg)

    def _on_export_error(self, err: BaseException) -> None:
        self._set_exporting(False)
        self._set_status(f"失败：{err}")
        messagebox.showerror("导出失败", f"{err}")


def main() -> None:
    if tk is None:
        raise SystemExit("需要 tkinter 才能启动界面。请安装 python3-tk 或使用带 Tk 的 Python。")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
