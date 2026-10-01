#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图拼接导出 PDF — 简易 Tkinter GUI。"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import List, Optional

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


def natural_key(name: str):
    """按文件名自然排序键（img2 在 img10 前）。"""
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


def _open_rgb(path: Path) -> Image.Image:
    """打开图片并转为可拼接的 RGB。"""
    img = Image.open(path)
    img.load()
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def stitch_images(
    paths: List[Path],
    direction: str = "vertical",
    target_size: Optional[int] = None,
) -> Image.Image:
    """按方向拼接图片。

    默认（及任意统一尺寸）以图中最大宽/高为基准，只把较小图放大，
    绝不缩小最大的那一张。缩放使用 LANCZOS。
    """
    if not paths:
        raise ValueError("没有可拼接的图片")

    images = [_open_rgb(p) for p in paths]

    if direction == "vertical":
        max_w = max(im.width for im in images)
        # 用户指定尺寸若小于最大宽，仍取最大宽，避免缩小原图
        base_w = max(max_w, target_size) if target_size and target_size > 0 else max_w
        scaled: List[Image.Image] = []
        for im in images:
            if im.width != base_w:
                new_h = max(1, round(im.height * (base_w / im.width)))
                im = im.resize((base_w, new_h), Image.Resampling.LANCZOS)
            scaled.append(im)
        total_h = sum(im.height for im in scaled)
        canvas = Image.new("RGB", (base_w, total_h), (255, 255, 255))
        y = 0
        for im in scaled:
            canvas.paste(im, (0, y))
            y += im.height
        return canvas

    # horizontal
    max_h = max(im.height for im in images)
    base_h = max(max_h, target_size) if target_size and target_size > 0 else max_h
    scaled = []
    for im in images:
        if im.height != base_h:
            new_w = max(1, round(im.width * (base_h / im.height)))
            im = im.resize((new_w, base_h), Image.Resampling.LANCZOS)
        scaled.append(im)
    total_w = sum(im.width for im in scaled)
    canvas = Image.new("RGB", (total_w, base_h), (255, 255, 255))
    x = 0
    for im in scaled:
        canvas.paste(im, (x, 0))
        x += im.width
    return canvas


def _needs_resize(paths: List[Path], direction: str, target_size: Optional[int]) -> bool:
    """判断拼接是否会改变任一原图像素尺寸。"""
    sizes = []
    for p in paths:
        with Image.open(p) as im:
            sizes.append(im.size)
    if direction == "vertical":
        max_w = max(w for w, _h in sizes)
        base_w = max(max_w, target_size) if target_size and target_size > 0 else max_w
        return any(w != base_w for w, _h in sizes) or len(paths) > 1
    max_h = max(h for _w, h in sizes)
    base_h = max(max_h, target_size) if target_size and target_size > 0 else max_h
    return any(h != base_h for _w, h in sizes) or len(paths) > 1


def image_to_pdf(
    img: Image.Image,
    out_path: Path,
    source_paths: Optional[List[Path]] = None,
    direction: str = "vertical",
    target_size: Optional[int] = None,
) -> None:
    """将拼接后的图片写入单页 PDF。

    优先 img2pdf：
    - 仅一张且无需缩放的 JPEG：直接嵌入原文件（不重编码）
    - 其它情况：无损 PNG 再交给 img2pdf，避免 JPEG 二次压缩损失
    无 img2pdf 时回退 Pillow 写 PDF。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 单张原图 JPEG、无需缩放/拼接：原样嵌入
    if (
        img2pdf is not None
        and source_paths
        and len(source_paths) == 1
        and not _needs_resize(source_paths, direction, target_size)
    ):
        src = Path(source_paths[0])
        if src.suffix.lower() in (".jpg", ".jpeg"):
            with open(out_path, "wb") as f:
                f.write(img2pdf.convert(str(src)))
            return

    rgb = img.convert("RGB")

    if img2pdf is not None:
        buf = io.BytesIO()
        # 无损 PNG，避免 JPEG recompress 质量损失
        rgb.save(buf, format="PNG", optimize=True)
        with open(out_path, "wb") as f:
            f.write(img2pdf.convert(buf.getvalue()))
        return

    rgb.save(out_path, "PDF", resolution=100.0)


def default_pdf_name(paths: List[Path], source_folder: Optional[Path]) -> str:
    """默认 PDF 文件名：文件夹名 / 首图 stem / stitch.pdf。"""
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
    return "stitch.pdf"


def find_pythonw() -> Path:
    """查找 pythonw.exe（优先与当前解释器同目录）。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return candidate
    # PATH 上碰运气
    for name in ("pythonw.exe", "pythonw"):
        which = _which(name)
        if which:
            return Path(which)
    return exe  # 回退 python.exe（会闪控制台）


def _which(cmd: str) -> Optional[str]:
    from shutil import which

    return which(cmd)


def create_desktop_shortcut(
    project_dir: Optional[Path] = None,
    shortcut_name: str = "图片拼接转PDF.lnk",
) -> Path:
    """在用户桌面创建 .lnk，用 pythonw 启动 main.py，工作目录=项目目录。"""
    project_dir = Path(project_dir or PROJECT_DIR).resolve()
    main_py = project_dir / "main.py"
    if not main_py.is_file():
        raise FileNotFoundError(f"找不到 main.py: {main_py}")

    desktop = Path(os.path.join(os.path.expanduser("~"), "Desktop"))
    # Windows 常见：通过 shell 文件夹拿桌面
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
        # OneDrive 桌面兜底
        one = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop"
        if not desktop.is_dir() and one.is_dir():
            desktop = one

    desktop.mkdir(parents=True, exist_ok=True)
    lnk_path = desktop / shortcut_name
    pythonw = find_pythonw()

    if sys.platform == "win32":
        # PowerShell + WScript.Shell 创建 .lnk
        ps = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{str(lnk_path).replace("'", "''")}')
$Shortcut.TargetPath = '{str(pythonw).replace("'", "''")}'
$Shortcut.Arguments = '"{str(main_py).replace("'", "''")}"'
$Shortcut.WorkingDirectory = '{str(project_dir).replace("'", "''")}'
$Shortcut.WindowStyle = 1
$Shortcut.Description = '图片拼接转 PDF'
$Shortcut.Save()
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # 非 Windows：写一个可执行启动脚本到「桌面」同名路径旁作提示
        sh_path = desktop / "图片拼接转PDF.sh"
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
        self.title("图片拼接转 PDF")
        self.geometry("760x540")
        self.minsize(660, 460)

        self.paths: List[Path] = []
        # 若图片主要来自某次「添加文件夹」，用于默认 PDF 名
        self.source_folder: Optional[Path] = None
        self._build_ui()
        self._set_status("请添加图片，或添加整个文件夹。")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        ttk.Button(top, text="添加图片…", command=self.add_images).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="添加文件夹…", command=self.add_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="上移", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="下移", command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="清空列表", command=self.clear_list).pack(side=tk.LEFT, padx=2)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        self.listbox = tk.Listbox(mid, selectmode=tk.EXTENDED, activestyle="dotbox")
        scroll = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        opts = ttk.LabelFrame(self, text="拼接选项")
        opts.pack(fill=tk.X, **pad)

        self.direction = tk.StringVar(value="vertical")
        ttk.Radiobutton(opts, text="纵向拼接（默认）", variable=self.direction, value="vertical").grid(
            row=0, column=0, sticky=tk.W, padx=8, pady=4
        )
        ttk.Radiobutton(opts, text="横向拼接", variable=self.direction, value="horizontal").grid(
            row=0, column=1, sticky=tk.W, padx=8, pady=4
        )

        ttk.Label(opts, text="统一尺寸（像素，可选）:").grid(row=1, column=0, sticky=tk.W, padx=8, pady=4)
        self.size_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.size_var, width=12).grid(
            row=1, column=1, sticky=tk.W, padx=8, pady=4
        )
        ttk.Label(
            opts,
            text="留空=按图中最大边对齐；只放大较小图，绝不缩小最大原图（LANCZOS）。纵向=统一宽度，横向=统一高度。",
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=8, pady=(0, 6))

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, **pad)
        ttk.Button(bottom, text="导出 PDF…", command=self.export_pdf).pack(side=tk.LEFT)
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
        """根据当前列表推断 source_folder（同父目录则保留该文件夹）。"""
        if not self.paths:
            self.source_folder = None
            return
        parents = {p.resolve().parent for p in self.paths}
        if len(parents) == 1:
            self.source_folder = next(iter(parents))
        else:
            self.source_folder = None

    def add_images(self) -> None:
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
        folder = filedialog.askdirectory(title="选择图片文件夹")
        if not folder:
            return
        folder_path = Path(folder)
        images = list_images_in_folder(folder_path)
        if not images:
            messagebox.showwarning("提示", f"该文件夹下没有常见格式的图片：\n{folder_path}")
            self._set_status("文件夹内未找到图片。")
            return
        added = 0
        for path in images:
            if path not in self.paths:
                self.paths.append(path)
                added += 1
        # 若列表全部来自该文件夹，用文件夹名作默认 PDF 名
        self._update_source_folder_from_paths()
        if self.source_folder is None and added == len(images):
            # 刚追加进来的与原有混在一起 → 上面已置 None；若原本为空则应为该文件夹
            pass
        if all(p.resolve().parent == folder_path.resolve() for p in self.paths):
            self.source_folder = folder_path.resolve()
        self._refresh_list()
        self._set_status(
            f"从文件夹添加 {added} 张（自然排序），当前共 {len(self.paths)} 张。默认 PDF 名："
            f"{default_pdf_name(self.paths, self.source_folder)}"
        )

    def move_selected(self, delta: int) -> None:
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
        self.paths.clear()
        self.source_folder = None
        self._refresh_list()
        self._set_status("列表已清空。")

    def _parse_target_size(self) -> Optional[int]:
        raw = self.size_var.get().strip()
        if not raw:
            return None
        try:
            val = int(raw)
        except ValueError as exc:
            raise ValueError("统一尺寸请输入正整数（像素）。") from exc
        if val <= 0:
            raise ValueError("统一尺寸必须大于 0。")
        return val

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
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("导出列表为空。")
            return
        try:
            target = self._parse_target_size()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            self._set_status(f"错误：{e}")
            return

        initial = default_pdf_name(self.paths, self.source_folder)
        initial_dir = None
        if self.source_folder and self.source_folder.is_dir():
            initial_dir = str(self.source_folder)
        elif self.paths:
            initial_dir = str(self.paths[0].parent)

        out = filedialog.asksaveasfilename(
            title="导出 PDF",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile=initial,
            initialdir=initial_dir,
        )
        if not out:
            self._set_status("已取消导出。")
            return

        out_path = Path(out)
        direction = self.direction.get()
        direction_cn = "纵向" if direction == "vertical" else "横向"
        self._set_status(f"正在{direction_cn}拼接 {len(self.paths)} 张图片…")
        try:
            img = stitch_images(self.paths, direction=direction, target_size=target)
            self._set_status("正在写入 PDF…")
            image_to_pdf(
                img,
                out_path,
                source_paths=self.paths,
                direction=direction,
                target_size=target,
            )
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("导出失败", f"{e}")
            self._set_status(f"错误：{e}")
            return

        self._set_status(f"导出成功：{out_path} （{img.width}×{img.height}）")
        messagebox.showinfo("完成", f"已导出 PDF：\n{out_path}")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
