# -*- coding: utf-8 -*-
"""gallery_fetch：命令构建、webp→jpg、URL 识别与工具定位。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import gallery_fetch as gf
import video_dl


# ------------------------------------------------------------------ URLs


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://e-hentai.org/g/4307284/8ac2334f3c/", True),
        ("https://exhentai.org/g/1/abcdef12/", True),
        ("http://g.e-hentai.org/g/1/abcdef12/", True),
        ("https://www.youtube.com/watch?v=1", False),
        ("https://x.com/a/status/1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_ehentai_url(url, expected):
    assert gf.is_ehentai_url(url) is expected


def test_split_urls_reuses_video_dl():
    text = "https://e-hentai.org/g/1/abc/\n# skip\nhttps://exhentai.org/g/2/def/"
    assert gf.split_urls(text) == [
        "https://e-hentai.org/g/1/abc/",
        "https://exhentai.org/g/2/def/",
    ]


def test_site_for_url_ehentai_keys():
    assert video_dl.site_for_url("https://e-hentai.org/g/1/a/") == "e-hentai.org"
    assert video_dl.site_for_url("https://exhentai.org/g/1/a/") == "exhentai.org"
    assert "e-hentai.org" in video_dl.KNOWN_VIDEO_SITES
    assert "exhentai.org" in video_dl.KNOWN_VIDEO_SITES


# ------------------------------------------------------------------ command


def test_build_command_defaults(tmp_path: Path):
    cmd = gf.build_command(["gallery-dl"], "https://e-hentai.org/g/1/a/", tmp_path)
    assert cmd == [
        "gallery-dl",
        "-d",
        str(tmp_path),
        "https://e-hentai.org/g/1/a/",
    ]


def test_build_command_cookies_file_over_browser(tmp_path: Path):
    base = ["C:/Python/python.exe", "-m", "gallery_dl"]
    cmd = gf.build_command(
        base,
        "https://exhentai.org/g/1/a/",
        tmp_path,
        cookies_file=tmp_path / "c.txt",
        cookies_browser="edge",
    )
    assert cmd[:3] == base
    i = cmd.index("--cookies")
    assert cmd[i + 1] == str(tmp_path / "c.txt")
    assert "--cookies-from-browser" not in cmd


def test_build_command_browser_only(tmp_path: Path):
    cmd = gf.build_command(
        ["gallery-dl"], "https://e-hentai.org/g/1/a/", tmp_path, cookies_browser="Chrome"
    )
    i = cmd.index("--cookies-from-browser")
    assert cmd[i + 1] == "chrome"


def test_build_command_rejects_empty_base(tmp_path: Path):
    with pytest.raises(ValueError):
        gf.build_command([], "https://e-hentai.org/g/1/a/", tmp_path)


def test_pip_and_version_commands():
    up = gf.pip_install_command(upgrade=True)
    assert up[1:] == ["-m", "pip", "install", "-U", "gallery-dl"]
    assert gf.version_command(["gallery-dl"]) == ["gallery-dl", "--version"]


# ------------------------------------------------------------------ tools


def test_find_gallery_dl_prefers_path():
    assert gf.find_gallery_dl(which=lambda n: "/bin/gallery-dl", has_module=lambda: True) == [
        "/bin/gallery-dl"
    ]


def test_find_gallery_dl_falls_back_to_module_then_none():
    base = gf.find_gallery_dl(which=lambda n: None, has_module=lambda: True)
    assert base is not None and base[1:] == ["-m", "gallery_dl"]
    assert gf.find_gallery_dl(which=lambda n: None, has_module=lambda: False) is None


# ------------------------------------------------------------------ parsing


def test_parse_progress():
    info = gf.parse_progress(" 42%  1.23MB  500kB/s ")
    assert info == {"percent": 42.0, "downloaded": "1.23MB", "speed": "500kB/s"}
    assert gf.parse_progress("# file.webp") is None
    assert gf.parse_progress("") is None


def test_parse_file_path():
    assert gf.parse_file_path(r"C:\D\a.webp") == r"C:\D\a.webp"
    assert gf.parse_file_path("#C:\\D\\a.webp") == r"C:\D\a.webp"
    assert gf.parse_file_path("# C:\\D\\a.jpg") == r"C:\D\a.jpg"
    assert gf.parse_file_path("# 3/1202") is None
    assert gf.parse_file_path("https://e-hentai.org/g/1/a/") is None
    assert gf.parse_file_path("[info] hello") is None


def test_parse_count():
    assert gf.parse_count("# 3/1202") == (3, 1202)
    assert gf.parse_count("12/100 done") == (12, 100)
    assert gf.parse_count("# file.webp") is None


# ------------------------------------------------------------------ webp → jpg


def _make_webp(path: Path, *, mode="RGB", size=(16, 10), color=(10, 20, 30)) -> Path:
    if mode == "RGBA":
        im = Image.new("RGBA", size, color + (128,))
    else:
        im = Image.new("RGB", size, color)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "WEBP")
    return path


def test_convert_webp_to_jpg_rgb(tmp_path: Path):
    src = _make_webp(tmp_path / "a.webp")
    dest = gf.convert_webp_to_jpg(src)
    assert dest == tmp_path / "a.jpg"
    assert dest.is_file() and not src.exists()
    with Image.open(dest) as im:
        assert im.format == "JPEG" and im.mode == "RGB" and im.size == (16, 10)


def test_convert_webp_to_jpg_rgba_white_bg(tmp_path: Path):
    src = _make_webp(tmp_path / "t.webp", mode="RGBA", color=(255, 0, 0))
    dest = gf.convert_webp_to_jpg(src, delete_src=True)
    assert dest is not None and dest.is_file() and not src.exists()
    with Image.open(dest) as im:
        # semi-transparent red over white → pinkish, not pure black
        px = im.getpixel((0, 0))
        assert px[0] > 100 and px[1] > 50  # blended toward white


def test_convert_webp_skips_non_webp(tmp_path: Path):
    p = tmp_path / "a.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(p)
    assert gf.convert_webp_to_jpg(p) is None
    assert p.exists()


def test_convert_webp_tree_and_watcher(tmp_path: Path):
    d = tmp_path / "exhentai" / "g"
    _make_webp(d / "1.webp")
    _make_webp(d / "2.webp")
    ok, failed = gf.convert_webp_tree(tmp_path)
    assert (ok, failed) == (2, 0)
    assert list(d.glob("*.webp")) == []
    assert len(list(d.glob("*.jpg"))) == 2

    # watcher picks up a new file
    events = []
    w = gf.WebpWatcher(tmp_path, interval=0.25, on_converted=lambda s, d_: events.append((s.name, d_.name)))
    w.start()
    _make_webp(d / "3.webp")
    deadline = time.time() + 5
    while time.time() < deadline and w.converted < 1:
        time.sleep(0.1)
    w.stop(final_sweep=True)
    assert w.converted >= 1
    assert not (d / "3.webp").exists()
    assert (d / "3.jpg").exists()


# ------------------------------------------------------------------ runner


def _fake_gdl_base(tmp_path: Path, script: str):
    f = tmp_path / "fake_gdl.py"
    f.write_text(script, encoding="utf-8")
    return [sys.executable, str(f)]


def test_download_runner_events_and_webp_convert(tmp_path: Path):
    out = tmp_path / "out"
    # fake gallery-dl writes a webp then prints its path
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "out = None\n"
        "args = sys.argv[1:]\n"
        "if '-d' in args:\n"
        "    out = Path(args[args.index('-d')+1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "p = out / 't.webp'\n"
        "Image.new('RGB', (8, 8), (9, 9, 9)).save(p, 'WEBP')\n"
        "print(str(p), flush=True)\n"
        "print(' 50%  1.00MB  1.00MB/s ', flush=True)\n"
        "sys.exit(0)\n"
    )
    base = _fake_gdl_base(tmp_path, script)
    events = []
    runner = gf.DownloadRunner(
        base,
        ["https://e-hentai.org/g/1/a/"],
        out,
        convert_webp=True,
        cookies_dir=tmp_path / "cookies",
        on_event=events.append,
    )
    (tmp_path / "cookies").mkdir()
    assert runner.run() == (1, 0, False)
    kinds = [e[0] for e in events]
    assert "start" in kinds and "file" in kinds and "converted" in kinds and "finished" in kinds
    assert list(out.glob("*.jpg")) and not list(out.glob("*.webp"))
    logs = "\n".join(e[1] for e in events if e[0] == "log")
    assert "webp" in logs.lower() or "jpg" in logs.lower()
    assert ("原图" in logs) or ("Cookies" in logs)


def test_download_runner_cancel(tmp_path: Path):
    base = _fake_gdl_base(
        tmp_path,
        "import time\nprint(' 1%  1B  1B/s ', flush=True)\ntime.sleep(60)\n",
    )
    events = []
    started = threading.Event()

    def on_event(ev):
        events.append(ev)
        if ev[0] == "progress":
            started.set()

    runner = gf.DownloadRunner(
        base, ["https://e-hentai.org/g/1/a/"], tmp_path, convert_webp=False, on_event=on_event
    )
    t = threading.Thread(target=runner.run)
    t.start()
    assert started.wait(30)
    runner.cancel()
    t.join(30)
    assert not t.is_alive()
    assert events[-1][0] == "finished" and events[-1][3] is True
