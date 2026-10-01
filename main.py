#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图导出为多页 PDF — 桌面 Tkinter GUI。

默认：每张图一页，不拼接成巨图，避免 OOM。
JPEG 尽量经 img2pdf 原样嵌入；其余走无损 PNG 路径。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image
except ImportError:
    print("请先安装依赖: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    import img2pdf
except ImportError:
    img2pdf = None  # type: ignore


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
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


def list_images_in_folder(folder: Path) -> List[Path]:
    """收集文件夹内常见扩展名图片，按文件名自然排序。"""
    folder = Path(folder)
    found: List[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            found.append(p)
    found.sort(key=lambda p: natural_key(p.name))
    return found


def default_pdf_name(paths: List[Path], source_folder: Optional[Path]) -> str:
    """默认 PDF 文件名：文件夹名 / 首图 stem / images.pdf。"""
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
    return "images.pdf"


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
) -> None:
    """将多张图片导出为多页 PDF（一图一页，列表顺序）。

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
        return

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
    shortcut_name: str = "图片导出PDF.lnk",
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
$Shortcut.Description = '图片导出为多页 PDF'
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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("图片导出 PDF")
        self.geometry("760x520")
        self.minsize(660, 420)

        self.paths: List[Path] = []
        self.source_folder: Optional[Path] = None
        self._exporting = False
        self._export_btn: Optional[ttk.Button] = None

        self._build_ui()
        self._set_status("请添加图片，或「添加文件夹」。默认：多页 PDF，一图一页。")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        ttk.Button(top, text="添加图片…", command=self.add_images).pack(
            side=tk.LEFT, padx=(0, 4)
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

        hint = ttk.LabelFrame(self, text="导出说明")
        hint.pack(fill=tk.X, **pad)
        ttk.Label(
            hint,
            text="生成多页 PDF：列表中每张图片对应一页，保留原始像素。"
            "JPEG 尽量原样嵌入；PNG 等走无损路径。不会拼接成单张巨图。",
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=8, pady=6)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, **pad)
        self._export_btn = ttk.Button(
            bottom, text="生成 PDF…", command=self.export_pdf
        )
        self._export_btn.pack(side=tk.LEFT)
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
            self.listbox.insert(tk.END, f"{i}. {path.name}  —  {path}")
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
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif"),
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
        self._set_status(f"已添加 {added} 张，当前共 {len(self.paths)} 张。")

    def add_folder(self) -> None:
        if self._exporting:
            return
        folder = filedialog.askdirectory(title="选择图片文件夹")
        if not folder:
            return
        folder_path = Path(folder)
        images = list_images_in_folder(folder_path)
        if not images:
            messagebox.showwarning(
                "提示", f"该文件夹内没有常见格式的图片：\n{folder_path}"
            )
            self._set_status("文件夹内未找到图片。")
            return
        added = 0
        for path in images:
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        self._update_source_folder_from_paths()
        if all(p.resolve().parent == folder_path.resolve() for p in self.paths):
            self.source_folder = folder_path.resolve()
        self._refresh_list()
        self._set_status(
            f"从文件夹添加 {added} 张（自然排序），当前共 {len(self.paths)} 张。"
            f"默认 PDF 名：{default_pdf_name(self.paths, self.source_folder)}"
        )

    def move_selected(self, delta: int) -> None:
        if self._exporting:
            return
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            self._set_status("请只选中一张图片再调整顺序。")
            return
        i = sel[0]
        j = i + delta
        if j < 0 or j >= len(self.paths):
            return
        self.paths[i], self.paths[j] = self.paths[j], self.paths[i]
        self._refresh_list()
        self.listbox.selection_set(j)
        self._set_status("已调整顺序。")

    def remove_selected(self) -> None:
        if self._exporting:
            return
        sel = sorted(self.listbox.curselection(), reverse=True)
        if not sel:
            self._set_status("请先选中要移除的图片。")
            return
        for i in sel:
            del self.paths[i]
        self._update_source_folder_from_paths()
        self._refresh_list()
        self._set_status(f"已移除，当前共 {len(self.paths)} 张。")

    def clear_list(self) -> None:
        if self._exporting:
            return
        self.paths.clear()
        self.source_folder = None
        self._refresh_list()
        self._set_status("列表已清空。")

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
        if self._exporting:
            return
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("导出列表为空。")
            return

        initial = default_pdf_name(self.paths, self.source_folder)
        initial_dir = None
        if self.source_folder and self.source_folder.is_dir():
            initial_dir = str(self.source_folder)
        elif self.paths:
            initial_dir = str(self.paths[0].parent)

        out = filedialog.asksaveasfilename(
            title="保存 PDF",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile=initial,
            initialdir=initial_dir,
        )
        if not out:
            self._set_status("已取消导出。")
            return

        out_path = Path(out)
        paths_snapshot = list(self.paths)
        self._set_exporting(True)
        self._set_status(f"正在导出多页 PDF：0/{len(paths_snapshot)}…")

        def worker() -> None:
            try:

                def on_progress(i: int, n: int) -> None:
                    self.after(
                        0, lambda i=i, n=n: self._set_status(f"正在导出 {i}/{n}…")
                    )

                images_to_multipage_pdf(
                    paths_snapshot, out_path, progress_callback=on_progress
                )
            except Exception as e:
                traceback.print_exc()
                err = e
                self.after(0, lambda: self._on_export_error(err))
            else:
                self.after(0, lambda: self._on_export_success(out_path, len(paths_snapshot)))

        threading.Thread(target=worker, daemon=True).start()

    def _set_exporting(self, running: bool) -> None:
        self._exporting = running
        if self._export_btn is not None:
            self._export_btn.configure(state=tk.DISABLED if running else tk.NORMAL)

    def _on_export_success(self, out_path: Path, page_count: int) -> None:
        self._set_exporting(False)
        self._set_status(f"导出成功：{out_path}（{page_count} 页）")
        messagebox.showinfo("完成", f"已导出多页 PDF（{page_count} 页）：\n{out_path}")

    def _on_export_error(self, err: BaseException) -> None:
        self._set_exporting(False)
        self._set_status(f"失败：{err}")
        messagebox.showerror("导出失败", f"{err}")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
