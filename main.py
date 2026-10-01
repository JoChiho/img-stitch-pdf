#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图 / PDF 导出为多页 PDF — 桌面 Tkinter GUI。

默认：列表项按顺序组成多页 PDF（图片一图一页；PDF 贡献其全部页），
不拼接成巨图，避免 OOM。JPEG 尽量经 img2pdf 原样嵌入；PDF 页经 pypdf
合并，不重新栅格化。

可选：本地 EN→ZH 图片翻译（EasyOCR + Argos Translate），将中文绘制到
新文件（原图旁 ``*_zh``），再走同一套多页 PDF 导出。
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

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
    import img2pdf
except ImportError:
    img2pdf = None  # type: ignore

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore

try:
    import translate_local
except ImportError:
    translate_local = None  # type: ignore


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
) -> List[Path]:
    """递归收集文件夹（含子目录）内常见扩展名图片与 PDF，按相对路径自然排序。

    默认跳过 .gif；保留 jpg/jpeg/png/webp/bmp/tif/tiff，以及 .pdf。
    """
    folder = Path(folder)
    allowed = set(IMAGE_EXTS)
    if include_pdf:
        allowed.add(PDF_EXT)
    if not skip_gif:
        allowed = set(allowed) | {".gif"}
    found: List[Path] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf not in allowed:
            continue
        found.append(p)
    found.sort(key=lambda p: natural_key(p.relative_to(folder).as_posix()))
    return found


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


class App(tk.Tk if tk is not None else object):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.title("图片 / PDF 导出 PDF")
        self.geometry("820x520")
        self.minsize(700, 420)

        self.paths: List[Path] = []
        self.source_folder: Optional[Path] = None
        self._exporting = False
        self._export_btn: Optional[ttk.Button] = None
        self._translate_export_btn: Optional[ttk.Button] = None
        self.output_dir: Path = ensure_default_output_dir()
        self.direct_export_var: Optional[tk.BooleanVar] = None
        self.translate_then_export_var: Optional[tk.BooleanVar] = None
        self.output_dir_label_var: Optional[tk.StringVar] = None

        self._build_ui()
        self._refresh_output_dir_label()
        self._set_status(
            "请添加图片、PDF 或文件夹（含子目录）。可用「移到第…位」或双击调整顺序。"
            f"默认导出目录：{self.output_dir}"
        )

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        ttk.Button(top, text="添加图片…", command=self.add_images).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(top, text="添加 PDF…", command=self.add_pdfs).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(top, text="添加文件夹…", command=self.add_folder).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(top, text="上移", command=lambda: self.move_selected(-1)).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(top, text="下移", command=lambda: self.move_selected(1)).pack(
            side=tk.LEFT, padx=2
        )
        self.move_pos_var = tk.StringVar(value="1")
        self.move_pos_entry = ttk.Entry(top, width=5, textvariable=self.move_pos_var)
        self.move_pos_entry.pack(side=tk.LEFT, padx=(8, 2))
        ttk.Button(top, text="移到第…位", command=self.move_selected_to_position).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(top, text="清空列表", command=self.clear_list).pack(
            side=tk.LEFT, padx=2
        )

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        self.listbox = tk.Listbox(mid, selectmode=tk.EXTENDED, activestyle="dotbox")
        scroll = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-Button-1>", self._on_list_double_click)

        hint = ttk.LabelFrame(self, text="导出说明")
        hint.pack(fill=tk.X, **pad)
        ttk.Label(
            hint,
            text="生成多页 PDF：列表顺序 = 页序。图片各占一页（JPEG 尽量原样嵌入）；"
            "PDF 文件贡献其全部页并直接合并（不重新栅格化）。可混合图片与 PDF。"
            "勾选或点「本地翻译后导出」：对图片做本地 EN→ZH（原图旁生成 *_zh，不覆盖原图），"
            "跳过 PDF/GIF，再导出多页 PDF。首次需下载 OCR/翻译模型。",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=8, pady=6)

        out_row = ttk.Frame(self)
        out_row.pack(fill=tk.X, **pad)
        ttk.Label(out_row, text="默认导出目录：").pack(side=tk.LEFT)
        self.output_dir_label_var = tk.StringVar(value="")
        ttk.Label(
            out_row,
            textvariable=self.output_dir_label_var,
            foreground="#333333",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            out_row, text="设定默认导出文件夹…", command=self.set_default_output_folder
        ).pack(side=tk.RIGHT)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, **pad)
        self._export_btn = ttk.Button(
            bottom, text="生成 PDF…", command=self.export_pdf
        )
        self._export_btn.pack(side=tk.LEFT)
        self.direct_export_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bottom,
            text="直接导出到默认文件夹",
            variable=self.direct_export_var,
        ).pack(side=tk.LEFT, padx=8)
        self.translate_then_export_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bottom,
            text="本地翻译后导出",
            variable=self.translate_then_export_var,
        ).pack(side=tk.LEFT, padx=4)
        self._translate_export_btn = ttk.Button(
            bottom, text="本地翻译后导出…", command=self.export_pdf_translated
        )
        self._translate_export_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="创建桌面快捷方式", command=self.create_shortcut).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(bottom, text="退出", command=self.destroy).pack(side=tk.RIGHT)

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, side=tk.BOTTOM, padx=4, pady=4
        )

    def _set_status(self, msg: str) -> None:
        self.status.set(msg)
        self.update_idletasks()

    def _refresh_list(self, keep_selection: bool = False) -> None:
        sel = list(self.listbox.curselection()) if keep_selection else []
        self.listbox.delete(0, tk.END)
        for i, path in enumerate(self.paths, start=1):
            kind = "PDF" if is_pdf(path) else "图"
            self.listbox.insert(tk.END, f"{i}. [{kind}] {path.name}  —  {path}")
        for idx in sel:
            if 0 <= idx < len(self.paths):
                self.listbox.selection_set(idx)

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
        self._refresh_list()
        self._set_status(f"已添加 {added} 张图片，当前共 {len(self.paths)} 项。")

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
        self._refresh_list()
        self._set_status(f"已添加 {added} 个 PDF，当前共 {len(self.paths)} 项。")

    def add_folder(self) -> None:
        if self._exporting:
            return
        folder = filedialog.askdirectory(title="选择文件夹（图片与 PDF）")
        if not folder:
            return
        folder_path = Path(folder)
        items = list_images_in_folder(folder_path)
        if not items:
            messagebox.showwarning(
                "提示",
                f"该文件夹内没有常见格式的图片或 PDF：\n{folder_path}",
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
        self._refresh_list()
        self._set_status(
            f"从文件夹（含所有子目录）添加了 {added} 项（图片+PDF，自然排序），"
            f"当前共 {len(self.paths)} 项。"
            f"默认 PDF 名：{default_pdf_name(self.paths, self.source_folder)}"
        )

    def move_selected(self, delta: int) -> None:
        if self._exporting:
            return
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            self._set_status("请只选择一项再上下移动；多选请用「移到第…位」。")
            return
        i = sel[0]
        j = i + delta
        if j < 0 or j >= len(self.paths):
            return
        self.paths[i], self.paths[j] = self.paths[j], self.paths[i]
        self._refresh_list()
        self.listbox.selection_set(j)
        self._set_status("已调整顺序。")

    def move_selected_to_position(self, target_1based: Optional[int] = None) -> None:
        """将当前选中项移到指定 1-based 位置（支持多选，保持相对顺序）。"""
        if self._exporting:
            return
        sel = list(self.listbox.curselection())
        if not sel:
            self._set_status("请先选择要移动的项。")
            return
        if target_1based is None:
            raw = (self.move_pos_var.get() or "").strip()
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
        self._refresh_list()
        n = len(self.paths)
        start = max(0, min(int(target_1based) - 1, n - len(sel)))
        for k in range(len(sel)):
            idx = start + k
            if 0 <= idx < n:
                self.listbox.selection_set(idx)
        if sel:
            self.listbox.see(start)
        self._set_status(f"已移到第 {start + 1} 位起（共 {len(sel)} 项）。")

    def _on_list_double_click(self, _event=None) -> None:
        """双击：弹出对话框输入新的序号。"""
        if self._exporting:
            return
        sel = list(self.listbox.curselection())
        if not sel:
            return
        current = sel[0] + 1
        dialog = tk.Toplevel(self)
        dialog.title("调整顺序")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text=f"当前第 {current} 位，移到第几位？（1–{len(self.paths)}）").pack(
            padx=12, pady=(12, 4)
        )
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
        sel = sorted(self.listbox.curselection(), reverse=True)
        if not sel:
            self._set_status("请先选中要移除的项。")
            return
        for i in sel:
            del self.paths[i]
        self._update_source_folder_from_paths()
        self._refresh_list()
        self._set_status(f"已移除，当前共 {len(self.paths)} 项。")

    def clear_list(self) -> None:
        if self._exporting:
            return
        self.paths.clear()
        self.source_folder = None
        self._refresh_list()
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
        """本地 EN→ZH 翻译图片为 *_zh 新文件，再导出多页 PDF（后台线程）。"""
        if self._exporting:
            return
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("导出列表为空。")
            return
        if translate_local is None:
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
        self._set_exporting(True)
        self._set_status(
            f"检查/安装翻译依赖后开始本地翻译… 0/{image_count}"
        )

        def worker() -> None:
            try:
                def on_dep(msg: str) -> None:
                    self.after(0, lambda m=msg: self._set_status(m))

                # Auto-install missing packages into this interpreter (no manual pip)
                translate_local.ensure_deps(progress_callback=on_dep)
                on_dep("正在准备 OCR / 翻译模型（首次可能下载）…")
                try:
                    translate_local.preload_models(progress_callback=on_dep)
                except Exception as warm_err:
                    # Non-fatal: translate_image_paths will surface real failures
                    on_dep(f"模型预加载提示：{warm_err}")

                def on_tr(i: int, n: int, msg: str) -> None:
                    self.after(
                        0,
                        lambda i=i, n=n, msg=msg: self._set_status(
                            f"本地翻译 {i}/{n}：{msg}"
                        ),
                    )

                translated, skipped_pdfs, warnings = (
                    translate_local.translate_image_paths(
                        paths_snapshot, progress_callback=on_tr
                    )
                )
                if not translated:
                    raise RuntimeError(
                        "没有成功翻译任何图片。"
                        + (" ".join(warnings) if warnings else "")
                    )

                def on_progress(i: int, n: int) -> None:
                    self.after(
                        0, lambda i=i, n=n: self._set_status(f"正在导出 {i}/{n}…")
                    )

                page_count = items_to_multipage_pdf(
                    translated, out_path, progress_callback=on_progress
                )
                note_parts = [
                    f"已生成 {len(translated)} 张翻译图（原图旁 *_zh，未覆盖原图）"
                ]
                if skipped_pdfs:
                    note_parts.append(f"跳过 PDF {len(skipped_pdfs)} 个")
                if warnings:
                    note_parts.append("；".join(warnings[:5]))
                note = "。".join(note_parts)
            except Exception as e:
                traceback.print_exc()
                err = e
                self.after(0, lambda: self._on_export_error(err))
            else:
                self.after(
                    0,
                    lambda: self._on_export_success(
                        out_path, page_count, extra=note
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

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
        self._set_status(f"正在导出多页 PDF：0/{len(paths_snapshot)}…")

        def worker() -> None:
            try:

                def on_progress(i: int, n: int) -> None:
                    self.after(
                        0, lambda i=i, n=n: self._set_status(f"正在导出 {i}/{n}…")
                    )

                page_count = items_to_multipage_pdf(
                    paths_snapshot, out_path, progress_callback=on_progress
                )
            except Exception as e:
                traceback.print_exc()
                err = e
                self.after(0, lambda: self._on_export_error(err))
            else:
                self.after(
                    0,
                    lambda: self._on_export_success(
                        out_path, page_count, extra=translate_note
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _set_exporting(self, running: bool) -> None:
        self._exporting = running
        state = tk.DISABLED if running else tk.NORMAL
        if self._export_btn is not None:
            self._export_btn.configure(state=state)
        if self._translate_export_btn is not None:
            self._translate_export_btn.configure(state=state)

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
