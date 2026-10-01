#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图拼接导出 PDF — 简易 Tkinter GUI。"""

from __future__ import annotations

import io
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

    vertical: 统一宽度（取最大宽或用户指定），再纵向拼接。
    horizontal: 统一高度（取最大高或用户指定），再横向拼接。
    """
    if not paths:
        raise ValueError("没有可拼接的图片")

    images = [_open_rgb(p) for p in paths]

    if direction == "vertical":
        base_w = target_size if target_size and target_size > 0 else max(im.width for im in images)
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
    base_h = target_size if target_size and target_size > 0 else max(im.height for im in images)
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


def image_to_pdf(img: Image.Image, out_path: Path) -> None:
    """将拼接后的图片写入单页 PDF。优先 img2pdf，否则 Pillow 回退。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    rgb = img.convert("RGB")
    rgb.save(buf, format="JPEG", quality=92, optimize=True)
    jpeg_bytes = buf.getvalue()

    if img2pdf is not None:
        with open(out_path, "wb") as f:
            f.write(img2pdf.convert(jpeg_bytes))
        return

    rgb.save(out_path, "PDF", resolution=100.0)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("图片拼接转 PDF")
        self.geometry("720x520")
        self.minsize(640, 440)

        self.paths: List[Path] = []
        self._build_ui()
        self._set_status("就绪。请添加图片。")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill=tk.X, **pad)

        ttk.Button(top, text="添加图片…", command=self.add_images).pack(side=tk.LEFT, padx=(0, 4))
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
            text="纵向=统一宽度；横向=统一高度。留空则取图片中的最大值。",
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=8, pady=(0, 6))

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, **pad)
        ttk.Button(bottom, text="导出 PDF…", command=self.export_pdf).pack(side=tk.LEFT)
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
        self._refresh_list()
        self._set_status(f"已添加 {added} 张，当前共 {len(self.paths)} 张。")

    def move_selected(self, delta: int) -> None:
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            self._set_status("请先选中一张图片再调整顺序。")
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
        self._refresh_list()
        self._set_status(f"已移除，当前共 {len(self.paths)} 张。")

    def clear_list(self) -> None:
        self.paths.clear()
        self._refresh_list()
        self._set_status("列表已清空。")

    def _parse_target_size(self) -> Optional[int]:
        raw = self.size_var.get().strip()
        if not raw:
            return None
        try:
            val = int(raw)
        except ValueError as exc:
            raise ValueError("统一尺寸必须是正整数（像素）。") from exc
        if val <= 0:
            raise ValueError("统一尺寸必须大于 0。")
        return val

    def export_pdf(self) -> None:
        if not self.paths:
            messagebox.showwarning("提示", "请先添加至少一张图片。")
            self._set_status("错误：列表为空。")
            return
        try:
            target = self._parse_target_size()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            self._set_status(f"错误：{e}")
            return

        out = filedialog.asksaveasfilename(
            title="保存 PDF",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile="stitched.pdf",
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
            image_to_pdf(img, out_path)
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
