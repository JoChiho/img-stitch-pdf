# -*- coding: utf-8 -*-
"""核心逻辑单元测试：排序、列目录、默认文件名、多页 PDF。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

import main


def test_natural_key_orders_numeric_parts():
    names = ["img10.png", "img2.png", "img1.png", "img20.png"]
    sorted_names = sorted(names, key=main.natural_key)
    assert sorted_names == ["img1.png", "img2.png", "img10.png", "img20.png"]


def test_list_images_in_folder(tmp_path: Path):
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "a10.png").write_bytes(b"x")
    (tmp_path / "a2.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.jpg").write_bytes(b"x")

    found = main.list_images_in_folder(tmp_path)
    names = [p.name for p in found]
    assert names == ["a2.png", "a10.png", "b.jpg"]
    assert all(p.parent == tmp_path for p in found)


def test_default_pdf_name_from_folder():
    folder = Path("/tmp/my_album")
    assert main.default_pdf_name([], folder) == "my_album.pdf"


def test_default_pdf_name_from_common_parent(tmp_path: Path):
    p1 = tmp_path / "shot1.jpg"
    p2 = tmp_path / "shot2.jpg"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x")
    assert main.default_pdf_name([p1, p2], None) == f"{tmp_path.name}.pdf"


def test_default_pdf_name_fallback_stem_and_empty(tmp_path: Path):
    a = tmp_path / "only.png"
    b = tmp_path / "other" / "x.png"
    b.parent.mkdir()
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    assert main.default_pdf_name([a, b], None) == "only.pdf"
    assert main.default_pdf_name([], None) == "images.pdf"


def _make_images(folder: Path, n: int, *, kind: str = "mix") -> list[Path]:
    paths: list[Path] = []
    for i in range(n):
        if kind == "jpeg" or (kind == "mix" and i % 2 == 0):
            path = folder / f"img{i}.jpg"
            Image.new("RGB", (12 + i, 10 + (i % 3)), (i * 3 % 256, 40, 80)).save(
                path, format="JPEG", quality=90
            )
        else:
            path = folder / f"img{i}.png"
            Image.new("RGB", (10 + i, 12), (10, i * 5 % 256, 120)).save(path, format="PNG")
        paths.append(path)
    return paths


@pytest.mark.parametrize("n", [5, 50])
def test_images_to_multipage_pdf_page_count(tmp_path: Path, n: int):
    paths = _make_images(tmp_path, n, kind="mix")
    out = tmp_path / "out.pdf"
    progress: list[tuple[int, int]] = []

    main.images_to_multipage_pdf(
        paths, out, progress_callback=lambda i, total: progress.append((i, total))
    )

    assert out.is_file()
    reader = PdfReader(str(out))
    assert len(reader.pages) == n
    assert progress[-1] == (n, n)
    assert [p[0] for p in progress] == list(range(1, n + 1))


def test_multipage_pdf_does_not_call_stitch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """确保导出路径不走巨图拼接。"""
    paths = _make_images(tmp_path, 5, kind="jpeg")
    out = tmp_path / "out.pdf"

    def boom(*_a, **_k):
        raise AssertionError("must not stitch into one canvas")

    # 旧版若仍存在 stitch_images，不得被调用；新版可无此符号
    if hasattr(main, "stitch_images"):
        monkeypatch.setattr(main, "stitch_images", boom)

    main.images_to_multipage_pdf(paths, out)
    assert len(PdfReader(str(out)).pages) == 5


def test_jpeg_embed_path_produces_valid_pdf(tmp_path: Path):
    paths = _make_images(tmp_path, 3, kind="jpeg")
    out = tmp_path / "jpeg.pdf"
    main.images_to_multipage_pdf(paths, out)
    assert len(PdfReader(str(out)).pages) == 3
