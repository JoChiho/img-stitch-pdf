# -*- coding: utf-8 -*-
"""核心逻辑单元测试：排序、列目录、默认文件名、多页 PDF、PDF 合并。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

import main


def test_natural_key_orders_numeric_parts():
    names = ["img10.png", "img2.png", "img1.png", "img20.png"]
    sorted_names = sorted(names, key=main.natural_key)
    assert sorted_names == ["img1.png", "img2.png", "img10.png", "img20.png"]


def test_list_images_in_folder_recursive_natural_sort(tmp_path: Path):
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "a10.png").write_bytes(b"x")
    (tmp_path / "a2.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.jpg").write_bytes(b"x")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "z.webp").write_bytes(b"x")

    found = main.list_images_in_folder(tmp_path)
    rels = [p.relative_to(tmp_path).as_posix() for p in found]
    assert rels == ["a2.png", "a10.png", "b.jpg", "sub/deep/z.webp", "sub/nested.jpg"]


def test_list_images_skips_gif_by_default(tmp_path: Path):
    (tmp_path / "keep.png").write_bytes(b"x")
    (tmp_path / "skip.gif").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "also.gif").write_bytes(b"x")
    (tmp_path / "sub" / "ok.jpg").write_bytes(b"x")

    found = main.list_images_in_folder(tmp_path)
    names = [p.name for p in found]
    assert names == ["keep.png", "ok.jpg"]
    assert all(p.suffix.lower() != ".gif" for p in found)

    found_with_gif = main.list_images_in_folder(tmp_path, skip_gif=False)
    names_gif = sorted(p.name for p in found_with_gif)
    assert names_gif == ["also.gif", "keep.png", "ok.jpg", "skip.gif"]



def test_list_images_skips_translated_zh_subdir(tmp_path: Path):
    (tmp_path / "keep.png").write_bytes(b"x")
    sub = tmp_path / "translated_zh"
    sub.mkdir()
    (sub / "keep_zh.png").write_bytes(b"x")
    (tmp_path / "old_zh.jpg").write_bytes(b"x")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "ok.webp").write_bytes(b"x")
    nest_tr = tmp_path / "nested" / "translated_zh"
    nest_tr.mkdir()
    (nest_tr / "ok_zh.webp").write_bytes(b"x")

    found = main.list_images_in_folder(tmp_path)
    names = [p.relative_to(tmp_path).as_posix() for p in found]
    assert names == ["keep.png", "nested/ok.webp"]
    assert all("translated_zh" not in n for n in names)
    assert all(not Path(n).stem.endswith("_zh") for n in names)


def test_list_folder_picks_up_pdf(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "skip.gif").write_bytes(b"x")

    found = main.list_images_in_folder(tmp_path)
    rels = [p.relative_to(tmp_path).as_posix() for p in found]
    assert rels == ["a.png", "b.pdf", "sub/c.pdf"]

    no_pdf = main.list_images_in_folder(tmp_path, include_pdf=False)
    assert [p.name for p in no_pdf] == ["a.png"]


def test_move_items_to_index_single_and_multi():
    items = ["a", "b", "c", "d", "e"]
    assert main.move_items_to_index(items, [2], 1) == ["c", "a", "b", "d", "e"]
    assert main.move_items_to_index(items, [0], 3) == ["b", "c", "a", "d", "e"]
    assert main.move_items_to_index(items, [0], 5) == ["b", "c", "d", "e", "a"]
    assert main.move_items_to_index(items, [2, 3], 1) == ["c", "d", "a", "b", "e"]
    assert main.move_items_to_index(items, [0, 1], 4) == ["c", "d", "e", "a", "b"]
    # clamp / empty / invalid indices
    assert main.move_items_to_index(items, [2], 99) == ["a", "b", "d", "e", "c"]
    assert main.move_items_to_index(items, [], 1) == items
    assert main.move_items_to_index(items, [9], 1) == items


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
    assert main.default_pdf_name([], None) == "merge.pdf"


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


def _make_tiny_pdf(path: Path, pages: int = 1) -> Path:
    """用 Pillow 写出极小多页 PDF（测试用）。"""
    images = [
        Image.new("RGB", (20, 16), (30 + i * 20, 80, 120)) for i in range(pages)
    ]
    first, rest = images[0], images[1:]
    first.save(path, "PDF", save_all=True, append_images=rest, resolution=72.0)
    for im in images:
        im.close()
    return path


@pytest.mark.parametrize("n", [5, 50])
def test_images_to_multipage_pdf_page_count(tmp_path: Path, n: int):
    paths = _make_images(tmp_path, n, kind="mix")
    out = tmp_path / "out.pdf"
    progress: list[tuple[int, int]] = []

    pages = main.images_to_multipage_pdf(
        paths, out, progress_callback=lambda i, total: progress.append((i, total))
    )

    assert out.is_file()
    assert pages == n
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


def test_merge_pdf_files_page_count(tmp_path: Path):
    pdf_a = _make_tiny_pdf(tmp_path / "a.pdf", pages=2)
    pdf_b = _make_tiny_pdf(tmp_path / "b.pdf", pages=3)
    out = tmp_path / "merged.pdf"
    progress: list[tuple[int, int]] = []

    total = main.merge_pdf_files(
        [pdf_a, pdf_b],
        out,
        progress_callback=lambda i, n: progress.append((i, n)),
    )
    assert total == 5
    assert len(PdfReader(str(out)).pages) == 5
    assert progress == [(1, 2), (2, 2)]


def test_items_to_multipage_pdf_pdfs_only(tmp_path: Path):
    pdfs = [
        _make_tiny_pdf(tmp_path / "one.pdf", pages=1),
        _make_tiny_pdf(tmp_path / "two.pdf", pages=2),
        _make_tiny_pdf(tmp_path / "three.pdf", pages=1),
    ]
    out = tmp_path / "all_pdf.pdf"
    pages = main.items_to_multipage_pdf(pdfs, out)
    assert pages == 4
    assert len(PdfReader(str(out)).pages) == 4


def test_items_to_multipage_pdf_mixed_image_and_pdf(tmp_path: Path):
    img = tmp_path / "front.png"
    Image.new("RGB", (10, 12), (10, 80, 120)).save(img, format="PNG")
    pdf = _make_tiny_pdf(tmp_path / "doc.pdf", pages=2)
    img2 = tmp_path / "tail.jpg"
    Image.new("RGB", (14, 12), (200, 10, 10)).save(img2, format="JPEG", quality=90)

    out = tmp_path / "mixed.pdf"
    # order: image(1) + pdf(2) + image(1) => 4 pages
    pages = main.items_to_multipage_pdf([img, pdf, img2], out)
    assert pages == 4
    assert len(PdfReader(str(out)).pages) == 4


def test_items_to_multipage_pdf_images_only_delegates(tmp_path: Path):
    paths = _make_images(tmp_path, 4, kind="mix")
    out = tmp_path / "imgs.pdf"
    pages = main.items_to_multipage_pdf(paths, out)
    assert pages == 4
    assert len(PdfReader(str(out)).pages) == 4



def test_suggested_default_output_dir_ends_with_chinese_folder():
    suggested = main.suggested_default_output_dir()
    assert suggested.name == "PDF导出"
    assert suggested.parent.name in ("Documents", "文档", "My Documents") or (
        "Documents" in suggested.parent.as_posix()
        or "文档" in suggested.parent.as_posix()
    )


def test_config_load_save_roundtrip(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    assert main.load_config() == {}
    assert main.get_output_dir() is None

    out = tmp_path / "exports"
    saved = main.set_output_dir(out)
    assert saved == out.resolve()
    assert main.config_path() == cfg_dir / "config.json"
    assert main.config_path().is_file()

    loaded = main.load_config()
    assert loaded["output_dir"] == str(out.resolve())
    assert main.get_output_dir() == out.resolve()


def test_config_load_handles_missing_and_corrupt(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    assert main.load_config() == {}

    path = main.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    assert main.load_config() == {}

    path.write_text('"just a string"', encoding="utf-8")
    assert main.load_config() == {}


def test_ensure_default_output_dir_creates_and_persists(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    suggested = tmp_path / "Documents" / "PDF导出"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(main, "suggested_default_output_dir", lambda: suggested)

    assert main.get_output_dir() is None
    got = main.ensure_default_output_dir()
    assert got == suggested.resolve()
    assert suggested.is_dir()
    assert main.get_output_dir() == suggested.resolve()

    # second call keeps existing setting even if suggested would differ
    other = tmp_path / "other_out"
    other.mkdir()
    main.set_output_dir(other)
    got2 = main.ensure_default_output_dir()
    assert got2 == other.resolve()


def test_get_output_dir_empty_string_is_none(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    main.save_config({"output_dir": ""})
    assert main.get_output_dir() is None


def test_ollama_model_config_roundtrip(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    assert main.get_ollama_model_config() is None
    saved = main.set_ollama_model_config("qwen2.5:7b")
    assert saved == "qwen2.5:7b"
    assert main.get_ollama_model_config() == "qwen2.5:7b"
    assert main.load_config()["ollama_model"] == "qwen2.5:7b"


def test_resolve_ollama_model_prefers_config_then_14b(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(main, "list_ollama_models_for_ui", lambda: ["llama3:8b", "qwen2.5:14b"])
    # No config → prefer 14b when present
    assert main.resolve_ollama_model_choice() == "qwen2.5:14b"
    main.set_ollama_model_config("llama3:8b")
    assert main.resolve_ollama_model_choice() == "llama3:8b"


def test_resolve_ollama_model_first_available_without_14b(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(main, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(main, "list_ollama_models_for_ui", lambda: ["phi3:mini", "llama3:8b"])
    assert main.resolve_ollama_model_choice() == "phi3:mini"

