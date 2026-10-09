# -*- coding: utf-8 -*-
"""video_dl：命令构建、进度解析、URL 拆分、工具定位与下载队列。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

import main
import video_dl


# ------------------------------------------------------------------ URLs


def test_split_urls_lines_whitespace_dedupe_and_noise():
    text = """
    https://x.com/a/status/1
    # comment https://ignored.example
    not a url
    http://example.com/v?id=2   https://youtu.be/abc
    https://x.com/a/status/1
    "https://quoted.example/x"
    ftp://nope.example/file
    """
    assert video_dl.split_urls(text) == [
        "https://x.com/a/status/1",
        "http://example.com/v?id=2",
        "https://youtu.be/abc",
        "https://quoted.example/x",
    ]


def test_split_urls_empty_and_windows_newlines():
    assert video_dl.split_urls("") == []
    assert video_dl.split_urls(None) == []
    assert video_dl.split_urls("https://a.example/1\r\nhttps://b.example/2\r\n") == [
        "https://a.example/1",
        "https://b.example/2",
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://x.com/MicWhyss/status/2107835806723580263", True),
        ("  HTTP://EXAMPLE.COM/x  ", True),
        ("hello world", False),
        ("https://a.example b", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_url(text, expected):
    assert video_dl.looks_like_url(text) is expected


# ------------------------------------------------------------------ command


def test_build_command_matches_original_defaults(tmp_path: Path):
    cmd = video_dl.build_command(["yt-dlp"], "https://x.com/s/1", tmp_path)
    assert cmd == [
        "yt-dlp",
        "-f",
        "bv*+ba/b",
        "-P",
        str(tmp_path),
        "--newline",
        "--no-playlist",
        "https://x.com/s/1",
    ]


def test_build_command_options_and_python_module_base(tmp_path: Path):
    base = ["C:/Python/python.exe", "-m", "yt_dlp"]
    cmd = video_dl.build_command(
        base,
        "https://u.example/v",
        tmp_path,
        no_playlist=False,
        cookies_browser="Edge",
        ffmpeg_location="C:/ffmpeg/bin",
    )
    assert cmd[:3] == base
    assert "--no-playlist" not in cmd
    i = cmd.index("--cookies-from-browser")
    assert cmd[i + 1] == "edge"
    j = cmd.index("--ffmpeg-location")
    assert cmd[j + 1] == "C:/ffmpeg/bin"
    assert cmd[-1] == "https://u.example/v"


@pytest.mark.parametrize("value", [None, "", "无", "none", "safari-unknown"])
def test_build_command_no_cookies_for_none_values(tmp_path: Path, value):
    cmd = video_dl.build_command(["yt-dlp"], "https://a.example", tmp_path, cookies_browser=value)
    assert "--cookies-from-browser" not in cmd


def test_build_command_rejects_empty_base(tmp_path: Path):
    with pytest.raises(ValueError):
        video_dl.build_command([], "https://a.example", tmp_path)


def test_pip_install_command():
    up = video_dl.pip_install_command(upgrade=True)
    assert up[1:] == ["-m", "pip", "install", "-U", "yt-dlp"]
    fresh = video_dl.pip_install_command(upgrade=False)
    assert "-U" not in fresh and fresh[-1] == "yt-dlp"


def test_version_command():
    assert video_dl.version_command(["yt-dlp"]) == ["yt-dlp", "--version"]


# ------------------------------------------------------------------ parsing


def test_parse_progress_full_line():
    info = video_dl.parse_progress("[download]  42.3% of ~  10.00MiB at    1.23MiB/s ETA 00:05")
    assert info == {"percent": 42.3, "total": "10.00MiB", "speed": "1.23MiB/s", "eta": "00:05"}


def test_parse_progress_variants():
    done = video_dl.parse_progress("[download] 100% of   55.02MiB in 00:00:04 at 12.08MiB/s")
    assert done is not None and done["percent"] == 100.0 and done["total"] == "55.02MiB"
    frag = video_dl.parse_progress("[download]   7.0% of ~ 120.50MiB at  2.00MiB/s ETA 01:00 (frag 3/40)")
    assert frag is not None and frag["percent"] == 7.0 and frag["eta"] == "01:00"
    unk = video_dl.parse_progress("[download]   0.0% of    3.00MiB at  Unknown B/s ETA Unknown")
    assert unk is not None and unk["percent"] == 0.0


@pytest.mark.parametrize(
    "line",
    [
        "",
        "[youtube] abc: Downloading webpage",
        "[download] Destination: C:\\Downloads\\a.mp4",
        "WARNING: something 50% odd",
    ],
)
def test_parse_progress_non_progress(line):
    assert video_dl.parse_progress(line) is None


def test_parse_destination():
    assert (
        video_dl.parse_destination("[download] Destination: C:\\D\\v [id].f137.mp4")
        == "C:\\D\\v [id].f137.mp4"
    )
    assert (
        video_dl.parse_destination('[Merger] Merging formats into "C:\\D\\v [id].mp4"')
        == "C:\\D\\v [id].mp4"
    )
    assert (
        video_dl.parse_destination("[download] C:\\D\\v.mp4 has already been downloaded")
        == "C:\\D\\v.mp4"
    )
    assert video_dl.parse_destination("[info] nothing") is None


def test_format_progress_chinese():
    s = video_dl.format_progress({"percent": 5, "total": "1MiB", "speed": "2KiB/s", "eta": "00:01"})
    assert s == "5.0% · 共 1MiB · 速度 2KiB/s · 剩余 00:01"
    assert video_dl.format_progress({"percent": 1.5}) == "1.5%"


# ------------------------------------------------------------------ tools


def test_find_ytdlp_prefers_path():
    assert video_dl.find_ytdlp(which=lambda n: "/bin/yt-dlp", has_module=lambda: True) == [
        "/bin/yt-dlp"
    ]


def test_find_ytdlp_falls_back_to_module_then_none():
    base = video_dl.find_ytdlp(which=lambda n: None, has_module=lambda: True)
    assert base is not None and base[1:] == ["-m", "yt_dlp"]
    assert video_dl.find_ytdlp(which=lambda n: None, has_module=lambda: False) is None


def test_console_python_swaps_pythonw(tmp_path: Path, monkeypatch):
    pyw = tmp_path / "pythonw.exe"
    py = tmp_path / "python.exe"
    pyw.write_bytes(b"")
    py.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(pyw))
    assert video_dl.console_python() == str(py)


def test_find_ffmpeg_path_and_missing(monkeypatch, tmp_path):
    assert video_dl.find_ffmpeg(which=lambda n: "/usr/bin/ffmpeg") == "/usr/bin/ffmpeg"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert video_dl.find_ffmpeg(which=lambda n: None) is None


def test_find_ffmpeg_winget_location(monkeypatch, tmp_path):
    monkeypatch.setattr(video_dl.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    exe = (
        tmp_path / "Microsoft" / "WinGet" / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.0-full_build" / "bin" / "ffmpeg.exe"
    )
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    found = video_dl.find_ffmpeg(which=lambda n: None)
    assert found == str(exe)
    assert video_dl.ffmpeg_location_arg(found, which=lambda n: None) == str(exe.parent)
    assert video_dl.ffmpeg_location_arg(found, which=lambda n: "/x/ffmpeg") is None
    assert video_dl.ffmpeg_location_arg(None) is None


def test_ffmpeg_missing_message_mentions_winget():
    assert "winget install Gyan.FFmpeg" in video_dl.ffmpeg_missing_message()


def test_default_download_dir_windows_userprofile(monkeypatch, tmp_path):
    monkeypatch.setattr(video_dl.sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert video_dl.default_download_dir() == tmp_path / "Downloads"


# ------------------------------------------------------------------ runner


def _fake_ytdlp_base(tmp_path: Path, script: str):
    f = tmp_path / "fake_ytdlp.py"
    f.write_text(script, encoding="utf-8")
    return [sys.executable, str(f)]


def test_download_runner_sequential_events(tmp_path: Path):
    base = _fake_ytdlp_base(
        tmp_path,
        "import sys\n"
        "url = sys.argv[-1]\n"
        "print('[download] Destination: out.mp4', flush=True)\n"
        "for p in (10, 55.5, 100):\n"
        "    print(f'[download] {p}% of 1.00MiB at 1.00MiB/s ETA 00:01', flush=True)\n"
        "sys.exit(3 if url.endswith('bad') else 0)\n",
    )
    events = []
    runner = video_dl.DownloadRunner(
        base,
        ["https://a.example/ok", "https://a.example/bad"],
        tmp_path / "out",
        on_event=events.append,
    )
    ok, failed, cancelled = runner.run()
    assert (ok, failed, cancelled) == (1, 1, False)
    assert (tmp_path / "out").is_dir()
    kinds = [e[0] for e in events]
    assert kinds.count("start") == 2
    progress = [e[3]["percent"] for e in events if e[0] == "progress"]
    assert progress == [10.0, 55.5, 100.0, 10.0, 55.5, 100.0]
    assert ("file", 1, 2, "out.mp4") in events
    done = [e for e in events if e[0] == "item_done"]
    assert [d[4] for d in done] == [0, 3]
    assert events[-1] == ("finished", 1, 1, False)
    cmd_logs = [e[1] for e in events if e[0] == "log" and e[1].startswith("> ")]
    assert len(cmd_logs) == 2 and "--no-playlist" in cmd_logs[0]


def test_download_runner_cancel_kills_process(tmp_path: Path):
    base = _fake_ytdlp_base(
        tmp_path,
        "import time\n"
        "print('[download] 1.0% of 1.00MiB', flush=True)\n"
        "time.sleep(60)\n",
    )
    events = []
    started = threading.Event()

    def on_event(ev):
        events.append(ev)
        if ev[0] == "progress":
            started.set()

    runner = video_dl.DownloadRunner(
        base, ["https://a.example/1", "https://a.example/2"], tmp_path, on_event=on_event
    )
    t = threading.Thread(target=runner.run)
    t.start()
    assert started.wait(30)
    runner.cancel()
    t.join(30)
    assert not t.is_alive()
    assert events[-1][0] == "finished"
    assert events[-1][3] is True  # cancelled
    assert events[-1][1:3] == (0, 2)  # remaining URL counted as not done
    assert sum(1 for e in events if e[0] == "start") == 1  # queue stopped


# ------------------------------------------------------------------ config (main.py)


def test_video_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(video_dl, "default_download_dir", lambda: tmp_path / "Downloads")
    assert main.get_video_download_dir_config() == tmp_path / "Downloads"
    assert main.get_video_no_playlist_config() is True
    assert main.get_video_cookies_browser_config() == "无"

    out = main.set_video_download_dir_config(tmp_path / "vids")
    assert main.get_video_download_dir_config() == out
    assert main.set_video_no_playlist_config(False) is False
    assert main.get_video_no_playlist_config() is False
    assert main.set_video_cookies_browser_config("Chrome") == "chrome"
    assert main.get_video_cookies_browser_config() == "chrome"
    assert main.set_video_cookies_browser_config("无") == "无"
    data = main.load_config()
    assert data["video_download_dir"] == str(out)
    assert data["video_no_playlist"] is False
