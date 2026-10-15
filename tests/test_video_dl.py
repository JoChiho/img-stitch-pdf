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
    assert up[1:] == ["-m", "pip", "install", "-U", "yt-dlp[default]"]
    fresh = video_dl.pip_install_command(upgrade=False)
    assert "-U" not in fresh and fresh[-1] == "yt-dlp[default]"


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


# ------------------------------------------------------------------ cookies.txt


_EXT_COOKIES = (
    "\ufeff# Exported by a browser extension\r\n"
    ".x.com\tFALSE\t/\tTRUE\t1790000000\tauth_token\tSECRET1\r\n"
    "x.com\tTRUE\t/\tfalse\t1790000000.5\tlang\ten\r\n"
    "#HttpOnly_.x.com\tFALSE\t/\tTRUE\t1790000000\tkdt\tSECRET2\r\n"
    "#HttpOnly_api.x.com\tTRUE\t/\tTRUE\t\tsess\tSECRET3\r\n"
    "\r\n"
    "garbage line without tabs\r\n"
    "too\tfew\tfields\r\n"
)


def _cookie_rows(text):
    return [ln.split("\t") for ln in text.splitlines() if "\t" in ln]


def test_normalize_cookies_header_and_flags():
    text, kept = video_dl.normalize_cookies_text(_EXT_COOKIES)
    assert kept == 4
    lines = text.splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    assert "\r" not in text and "\ufeff" not in text
    assert "Exported by" not in text and "garbage" not in text
    rows = _cookie_rows(text)
    assert [r[0] for r in rows] == [".x.com", "x.com", "#HttpOnly_.x.com", "#HttpOnly_api.x.com"]
    # FALSE -> TRUE when domain starts with '.', TRUE -> FALSE otherwise
    assert [r[1] for r in rows] == ["TRUE", "FALSE", "TRUE", "FALSE"]
    assert [r[3] for r in rows] == ["TRUE", "FALSE", "TRUE", "TRUE"]
    assert [r[4] for r in rows] == ["1790000000", "1790000000", "1790000000", "0"]
    assert [r[6] for r in rows] == ["SECRET1", "en", "SECRET2", "SECRET3"]


def test_normalize_cookies_loads_in_mozilla_cookiejar(tmp_path):
    import http.cookiejar

    text, _ = video_dl.normalize_cookies_text(_EXT_COOKIES)
    f = tmp_path / "c.txt"
    f.write_text(text, encoding="utf-8")
    jar = http.cookiejar.MozillaCookieJar(str(f))
    jar.load(ignore_discard=True, ignore_expires=True)  # raised LoadError before the fix
    names = sorted(c.name for c in jar)
    assert names == ["auth_token", "kdt", "lang", "sess"]
    raw = tmp_path / "raw.txt"
    raw.write_text(_EXT_COOKIES.replace("\ufeff", ""), encoding="utf-8")
    with pytest.raises(http.cookiejar.LoadError):
        http.cookiejar.MozillaCookieJar(str(raw)).load(ignore_discard=True, ignore_expires=True)


def test_normalize_cookies_already_valid_is_stable():
    once, kept = video_dl.normalize_cookies_text(_EXT_COOKIES)
    twice, kept2 = video_dl.normalize_cookies_text(once)
    assert once == twice and kept == kept2 == 4


def test_write_normalized_cookies_temp_copy(tmp_path):
    src = tmp_path / "cookies_x.com.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    out, kept = video_dl.write_normalized_cookies(src, tmp_path / "tmp")
    assert kept == 4 and out.parent == tmp_path / "tmp" and out != src
    assert out.read_text(encoding="utf-8").startswith("# Netscape HTTP Cookie File\n")
    assert src.read_bytes() == _EXT_COOKIES.encode("utf-8")  # original untouched
    video_dl.remove_file_quietly(out)
    assert not out.exists()


def test_write_normalized_cookies_errors_do_not_leak_values(tmp_path):
    with pytest.raises(video_dl.CookiesFileError):
        video_dl.write_normalized_cookies(tmp_path / "missing.txt", tmp_path)
    bad = tmp_path / "bad.txt"
    bad.write_text("# only comments\nname=SECRETVALUE\n", encoding="utf-8")
    with pytest.raises(video_dl.CookiesFileError) as ei:
        video_dl.write_normalized_cookies(bad, tmp_path)
    assert "SECRETVALUE" not in str(ei.value)


def test_build_command_cookies_file_has_priority(tmp_path):
    cmd = video_dl.build_command(
        ["yt-dlp"], "https://x.com/s/1", tmp_path, cookies_browser="chrome", cookies_file="C:/t/c.txt"
    )
    i = cmd.index("--cookies")
    assert cmd[i + 1] == "C:/t/c.txt"
    assert "--cookies-from-browser" not in cmd
    assert cmd[-1] == "https://x.com/s/1"
    cmd2 = video_dl.build_command(["yt-dlp"], "https://x.com/s/1", tmp_path, cookies_browser="chrome")
    assert "--cookies" not in cmd2 and "--cookies-from-browser" in cmd2


def test_output_hints():
    nv = "ERROR: [twitter] 1: No video could be found in this tweet"
    assert video_dl.output_hint(nv, has_cookies=False) == video_dl.COOKIES_NEED_LOGIN_TIP
    assert "Cookies 文件" in video_dl.COOKIES_NEED_LOGIN_TIP
    assert video_dl.output_hint(nv, has_cookies=True) is None
    chrome = "ERROR: Could not copy Chrome cookie database. See  https://github.com/yt-dlp/yt-dlp/issues/7271"
    tip = video_dl.output_hint(chrome, has_cookies=True)
    assert tip == video_dl.CHROME_COOKIE_DB_TIP and "关闭浏览器" in tip and "Cookies 文件" in tip
    assert video_dl.output_hint("[info] ok", has_cookies=False) is None


_COOKIE_ECHO_SCRIPT = (
    "import sys\n"
    "args = sys.argv[1:]\n"
    "if '--cookies' in args:\n"
    "    p = args[args.index('--cookies') + 1]\n"
    "    first = open(p, encoding='utf-8').readline().strip()\n"
    "    print('COOKIES ' + first, flush=True)\n"
    "    print('BROWSER ' + str('--cookies-from-browser' in args), flush=True)\n"
    "    sys.exit(0)\n"
    "print('ERROR: [twitter] 1: No video could be found in this tweet', flush=True)\n"
    "sys.exit(1)\n"
)


def test_download_runner_uses_normalized_temp_cookies(tmp_path):
    base = _fake_ytdlp_base(tmp_path, _COOKIE_ECHO_SCRIPT)
    src = tmp_path / "cookies_x.com.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    tmpdir = tmp_path / "cookie_tmp"
    events = []
    runner = video_dl.DownloadRunner(
        base,
        ["https://x.com/a/status/1"],
        tmp_path / "out",
        cookies_browser="chrome",
        cookies_file=src,
        cookies_tmp_dir=tmpdir,
        on_event=events.append,
    )
    assert runner.run() == (1, 0, False)
    logs = [e[1] for e in events if e[0] == "log"]
    assert "COOKIES # Netscape HTTP Cookie File" in logs
    assert "BROWSER False" in logs
    assert any("忽略「浏览器 Cookies」" in s for s in logs)
    assert list(tmpdir.iterdir()) == []  # temp copy deleted after run
    joined = "\n".join(logs)
    for secret in ("SECRET1", "SECRET2", "SECRET3"):
        assert secret not in joined
    assert video_dl.COOKIES_NEED_LOGIN_TIP not in logs


def test_download_runner_no_video_hint_without_cookies(tmp_path):
    base = _fake_ytdlp_base(tmp_path, _COOKIE_ECHO_SCRIPT)
    events = []
    runner = video_dl.DownloadRunner(
        base, ["https://x.com/a/status/1"], tmp_path / "out", on_event=events.append
    )
    assert runner.run() == (0, 1, False)
    logs = [e[1] for e in events if e[0] == "log"]
    assert logs.count(video_dl.COOKIES_NEED_LOGIN_TIP) == 1


def test_download_runner_bad_cookies_file_fails_cleanly(tmp_path):
    base = _fake_ytdlp_base(tmp_path, _COOKIE_ECHO_SCRIPT)
    events = []
    runner = video_dl.DownloadRunner(
        base,
        ["https://x.com/a/status/1", "https://x.com/a/status/2"],
        tmp_path / "out",
        cookies_file=tmp_path / "nope.txt",
        on_event=events.append,
    )
    assert runner.run() == (0, 2, False)
    assert not any(e[0] == "start" for e in events)
    assert events[-1] == ("finished", 0, 2, False)


# ------------------------------------------------------------------ per-site stored cookies

_YT_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.haxx.se/rfc/cookie_spec.html\n"
    "# This is a generated file! Do not edit.\n\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tYTSECRET1\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\tHSID\tYTSECRET2\n"
    "www.youtube.com\tFALSE\t/\tTRUE\t1790000000\tPREF\tYTSECRET3\n"
    ".google.com\tTRUE\t/\tTRUE\t1790000000\tNID\tG1\n"
    ".google.com\tTRUE\t/\tTRUE\t1790000000\tSID\tG2\n"
    ".google.com\tTRUE\t/\tTRUE\t1790000000\tAPISID\tG3\n"
    ".google.com\tTRUE\t/\tTRUE\t1790000000\tSAPISID\tG4\n"
)
_EMPTY_EXPORT = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.haxx.se/rfc/cookie_spec.html\n"
    "# This is a generated file! Do not edit.\n\n"
)


@pytest.mark.parametrize(
    "host,key",
    [
        (".x.com", "x.com"),
        ("#HttpOnly_.x.com", "x.com"),
        ("twitter.com", "x.com"),
        ("mobile.twitter.com", "x.com"),
        ("www.youtube.com", "youtube.com"),
        ("m.youtube.com", "youtube.com"),
        ("music.youtube.com", "youtube.com"),
        ("youtu.be", "youtube.com"),
        (".bilibili.com", "bilibili.com"),
        ("b23.tv", "bilibili.com"),
        ("www.nicovideo.jp", "nicovideo.jp"),
        ("news.yahoo.co.jp", "yahoo.co.jp"),
        ("www.bbc.co.uk", "bbc.co.uk"),
        ("", None),
    ],
)
def test_site_key_for_host_aliases(host, key):
    assert video_dl.site_key_for_host(host) == key


def test_detect_site_youtube_ignores_google_noise():
    info = video_dl.analyze_cookies_text(_YT_COOKIES)
    assert info.kept == 7
    assert info.site == "youtube.com"  # google.com (4 lines) does not hijack


def test_detect_site_x_and_fallback_most_frequent():
    assert video_dl.analyze_cookies_text(_EXT_COOKIES).site == "x.com"
    assert video_dl.detect_cookies_site([".a.example.org", "b.example.org", "c.other.net"]) == "example.org"
    assert video_dl.detect_cookies_site([".twitter.com", ".google.com", ".google.com"]) == "x.com"
    assert video_dl.detect_cookies_site([]) is None


@pytest.mark.parametrize(
    "url,key",
    [
        ("https://x.com/MicWhyss/status/2106320134461919264", "x.com"),
        ("https://twitter.com/a/status/1", "x.com"),
        ("https://www.youtube.com/watch?v=abc", "youtube.com"),
        ("https://youtu.be/abc", "youtube.com"),
        ("https://m.youtube.com/shorts/abc", "youtube.com"),
        ("https://music.youtube.com/watch?v=1", "youtube.com"),
        ("https://www.bilibili.com/video/BV1", "bilibili.com"),
        ("https://b23.tv/xyz", "bilibili.com"),
        ("not a url", None),
    ],
)
def test_site_for_url(url, key):
    assert video_dl.site_for_url(url) == key


def test_empty_file_message_vs_wrong_format(tmp_path):
    empty = tmp_path / "cookies_www.youtube.com.txt"
    empty.write_text(_EMPTY_EXPORT, encoding="utf-8")
    with pytest.raises(video_dl.CookiesFileError) as ei:
        video_dl.import_cookies_file(empty, tmp_path / "store")
    msg = str(ei.value)
    assert "这个 Cookies 文件是空的（只有文件头，没有任何 Cookie）。请在已登录该网站的页面上重新导出。" in msg
    assert empty.exists() and not (tmp_path / "store").exists()  # untouched
    bad = tmp_path / "bad.txt"
    bad.write_text("# header\nx.com TRUE / TRUE 0 a SECRETVALUE\n", encoding="utf-8")
    with pytest.raises(video_dl.CookiesFileError) as ei2:
        video_dl.validate_cookies_file(bad)
    assert "格式不对" in str(ei2.value) and "空的" not in str(ei2.value)
    assert "SECRETVALUE" not in str(ei2.value)
    with pytest.raises(video_dl.CookiesFileError) as ei3:
        video_dl.write_normalized_cookies(empty, tmp_path)
    assert "空的" in str(ei3.value)


def test_import_per_site_replace_and_delete(tmp_path):
    store = tmp_path / "cookies"
    x1 = tmp_path / "cookies_x.com.txt"
    x1.write_bytes(_EXT_COOKIES.encode("utf-8"))
    r1 = video_dl.import_cookies_file(x1, store)
    assert r1.site == "x.com" and r1.stored == store / "x.com.txt" and r1.kept == 4
    assert r1.moved and r1.source_deleted and not r1.replaced and not x1.exists()
    yt = tmp_path / "cookies_www.youtube.com.txt"
    yt.write_text(_YT_COOKIES, encoding="utf-8")
    r2 = video_dl.import_cookies_file(yt, store)
    assert r2.site == "youtube.com" and r2.stored == store / "youtube.com.txt"
    assert sorted(p.name for p in store.iterdir()) == ["x.com.txt", "youtube.com.txt"]
    # replace X only
    yt_before = (store / "youtube.com.txt").read_bytes()
    x2 = tmp_path / "new_x.txt"
    x2.write_text(_EXT_COOKIES.replace("SECRET1", "NEWSECRET"), encoding="utf-8")
    r3 = video_dl.import_cookies_file(x2, store)
    assert r3.replaced and r3.site == "x.com"
    assert b"NEWSECRET" in (store / "x.com.txt").read_bytes()
    assert (store / "youtube.com.txt").read_bytes() == yt_before
    assert sorted(p.name for p in store.iterdir()) == ["x.com.txt", "youtube.com.txt"]
    listed = {s.site: s.count for s in video_dl.list_stored_cookies(store)}
    assert listed == {"x.com": 4, "youtube.com": 7}
    # re-import the stored file itself is a no-op
    r4 = video_dl.import_cookies_file(store / "x.com.txt", store)
    assert not r4.moved and (store / "x.com.txt").exists()
    # delete only youtube
    assert video_dl.delete_stored_cookies("youtube.com", store) is True
    assert sorted(p.name for p in store.iterdir()) == ["x.com.txt"]
    assert video_dl.delete_stored_cookies("youtube.com", store) is False


def test_import_cookies_source_delete_failure_warns(tmp_path, monkeypatch):
    store = tmp_path / "cookies"
    src = tmp_path / "locked_cookies.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    real_unlink = Path.unlink

    def fake_unlink(self, *a, **k):
        if self == src:
            raise PermissionError(13, "被占用")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    res = video_dl.import_cookies_file(src, store)
    assert res.stored.exists() and src.exists()
    assert not res.source_deleted and res.warning and "警告" in res.warning
    assert "SECRET" not in res.warning


def test_tool_cookies_dir_next_to_module():
    assert video_dl.tool_cookies_dir() == Path(video_dl.__file__).resolve().parent / "cookies"


def test_migration_legacy_to_site_and_drops_config(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    data = main.load_config()
    data["video_cookies_file"] = str(tmp_path / "cookies" / "cookies.txt")
    main.save_config(data)
    store = tmp_path / "cookies"
    store.mkdir()
    (store / "cookies.txt").write_bytes(_EXT_COOKIES.encode("utf-8"))
    msgs = main.migrate_video_cookies_store(store)
    assert msgs and "x.com.txt" in msgs[0]
    assert all("SECRET" not in m for m in msgs)
    assert sorted(p.name for p in store.iterdir()) == ["x.com.txt"]
    assert "video_cookies_file" not in main.load_config()
    assert main.migrate_video_cookies_store(store) == []  # idempotent


def test_migration_does_not_touch_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    dl = tmp_path / "Downloads"
    dl.mkdir()
    src = dl / "cookies_x.com.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    monkeypatch.setattr(video_dl, "default_download_dir", lambda: dl)
    assert main.migrate_video_cookies_store(tmp_path / "cookies") == []
    assert src.exists()  # no auto-import any more


def test_migration_empty_legacy_kept(tmp_path):
    store = tmp_path / "cookies"
    store.mkdir()
    (store / "cookies.txt").write_text(_EMPTY_EXPORT, encoding="utf-8")
    msgs = video_dl.migrate_legacy_cookies(store)
    assert msgs and "空的" in msgs[0]
    assert (store / "cookies.txt").exists()


_SITE_ECHO_SCRIPT = (
    "import sys\n"
    "args = sys.argv[1:]\n"
    "url = args[-1]\n"
    "if '--cookies' in args:\n"
    "    p = args[args.index('--cookies') + 1]\n"
    "    names = [l.split('\\t')[5] for l in open(p, encoding='utf-8') if l.count('\\t') == 6]\n"
    "    print('URL ' + url + ' FILE ' + ','.join(sorted(names)[:1]), flush=True)\n"
    "elif '--cookies-from-browser' in args:\n"
    "    print('URL ' + url + ' BROWSER ' + args[args.index('--cookies-from-browser') + 1], flush=True)\n"
    "else:\n"
    "    print('URL ' + url + ' NONE', flush=True)\n"
    "sys.exit(0)\n"
)


def test_download_runner_mixed_queue_picks_cookies_per_url(tmp_path):
    base = _fake_ytdlp_base(tmp_path, _SITE_ECHO_SCRIPT)
    store = tmp_path / "cookies"
    store.mkdir()
    (store / "x.com.txt").write_bytes(_EXT_COOKIES.encode("utf-8"))
    (store / "youtube.com.txt").write_text(_YT_COOKIES, encoding="utf-8")
    tmpdir = tmp_path / "cookie_tmp"
    urls = [
        "https://x.com/a/status/1",
        "https://www.youtube.com/watch?v=1",
        "https://twitter.com/b/status/2",
        "https://youtu.be/2",
        "https://www.bilibili.com/video/BV1",
        "https://vimeo.com/1",
    ]
    events = []
    runner = video_dl.DownloadRunner(
        base, urls, tmp_path / "out", cookies_dir=store, cookies_tmp_dir=tmpdir,
        cookies_browser="firefox", on_event=events.append,
    )
    assert runner.run() == (6, 0, False)
    logs = [e[1] for e in events if e[0] == "log"]
    assert "URL https://x.com/a/status/1 FILE auth_token" in logs
    assert "URL https://www.youtube.com/watch?v=1 FILE APISID" in logs
    assert "URL https://twitter.com/b/status/2 FILE auth_token" in logs
    assert "URL https://youtu.be/2 FILE APISID" in logs
    assert "URL https://www.bilibili.com/video/BV1 BROWSER firefox" in logs
    assert "URL https://vimeo.com/1 BROWSER firefox" in logs
    joined = "\n".join(logs)
    assert "使用 x.com 的 Cookies" in joined and "使用 youtube.com 的 Cookies" in joined
    for secret in ("SECRET1", "SECRET2", "SECRET3", "YTSECRET1", "G1\n"):
        assert secret not in joined
    assert list(tmpdir.iterdir()) == []  # temp copies deleted


def test_download_runner_no_cookies_when_nothing_matches(tmp_path):
    base = _fake_ytdlp_base(tmp_path, _SITE_ECHO_SCRIPT)
    store = tmp_path / "cookies"
    store.mkdir()
    events = []
    runner = video_dl.DownloadRunner(
        base, ["https://x.com/a/status/1"], tmp_path / "out", cookies_dir=store,
        cookies_tmp_dir=tmp_path / "t", on_event=events.append,
    )
    assert runner.run() == (1, 0, False)
    logs = [e[1] for e in events if e[0] == "log"]
    assert "URL https://x.com/a/status/1 NONE" in logs


def test_gitignore_excludes_cookies():

    gi = (Path(main.__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in gi.splitlines()}
    assert "cookies/" in lines and "*cookies*.txt" in lines


# ------------------------------------------------------------------ YouTube JS challenges


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "https://m.youtube.com/watch?v=1",
    "https://youtu.be/jNQXAC9IVRw",
    "https://music.youtube.com/watch?v=1",
])
def test_build_command_youtube_adds_remote_components(tmp_path, url):
    cmd = video_dl.build_command(["yt-dlp"], url, tmp_path)
    i = cmd.index("--remote-components")
    assert cmd[i + 1] == video_dl.YOUTUBE_REMOTE_COMPONENTS == "ejs:github"
    assert cmd[-1] == url
    assert "--js-runtimes" not in cmd


def test_build_command_non_youtube_has_no_remote_components(tmp_path):
    cmd = video_dl.build_command(["yt-dlp"], "https://x.com/a/status/1", tmp_path)
    assert "--remote-components" not in cmd


def test_build_command_youtube_deno_path(tmp_path):
    cmd = video_dl.build_command(["yt-dlp"], "https://youtu.be/x", tmp_path, deno_path=r"C:\d\deno.exe")
    i = cmd.index("--js-runtimes")
    assert cmd[i + 1] == r"deno:C:\d\deno.exe"
    cmd2 = video_dl.build_command(["yt-dlp"], "https://x.com/a", tmp_path, deno_path=r"C:\d\deno.exe")
    assert "--js-runtimes" not in cmd2


def test_deno_runtime_arg_and_find_deno():
    assert video_dl.deno_runtime_arg(r"C:\d\deno.exe", which=lambda n: None) == r"C:\d\deno.exe"
    assert video_dl.deno_runtime_arg(r"C:\d\deno.exe", which=lambda n: "deno.exe") is None
    assert video_dl.deno_runtime_arg(None, which=lambda n: None) is None
    assert video_dl.find_deno(which=lambda n: r"C:\x\deno.exe") == r"C:\x\deno.exe"


def test_youtube_output_hints():
    h = video_dl.output_hint
    age = "[youtube] A8J2vHug9U0: This video is age-restricted and YouTube is requiring account age-verification; some formats may be missing"
    assert h(age, has_cookies=True) == video_dl.AGE_VERIFY_TIP
    assert "无痕窗口" in video_dl.AGE_VERIFY_TIP and "年龄验证" in video_dl.AGE_VERIFY_TIP
    n = "WARNING: [youtube] X: n challenge solving failed: Some formats may be missing."
    assert h(n, has_cookies=True) == video_dl.N_CHALLENGE_TIP
    po = "WARNING: [youtube] X: web_creator client https formats require a GVS PO Token which was not provided."
    assert h(po, has_cookies=True) == video_dl.PO_TOKEN_TIP
    for q in ("\u2019", "'"):
        bot = f"ERROR: [youtube] X: Sign in to confirm you{q}re not a bot."
        assert h(bot, has_cookies=False) == video_dl.BOT_CHECK_TIP
    assert h("ERROR: [youtube] X: Sorry, this content is age-restricted", has_cookies=True) is None
    assert "DenoLand.Deno" in video_dl.DENO_MISSING_TIP
