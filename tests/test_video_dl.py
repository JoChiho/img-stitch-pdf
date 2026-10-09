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


def test_video_cookies_file_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    assert main.get_video_cookies_file_config() is None
    f = tmp_path / "cookies.txt"
    stored = main.set_video_cookies_file_config(f)
    assert stored == str(f)
    assert main.get_video_cookies_file_config() == str(f)
    assert main.load_config()["video_cookies_file"] == str(f)
    assert main.set_video_cookies_file_config(None) is None
    assert main.get_video_cookies_file_config() is None
    assert "video_cookies_file" not in main.load_config()


# ------------------------------------------------------------------ stored cookies (tool folder)


def test_tool_cookies_dir_next_to_module():
    assert video_dl.tool_cookies_dir() == Path(video_dl.__file__).resolve().parent / "cookies"
    assert video_dl.stored_cookies_path().name == "cookies.txt"


def test_import_cookies_moves_and_deletes_source(tmp_path):
    store = tmp_path / "tool" / "cookies"
    src = tmp_path / "Downloads" / "cookies_x.com.txt"
    src.parent.mkdir()
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    res = video_dl.import_cookies_file(src, store)
    assert res.stored == store / "cookies.txt" and res.kept == 4
    assert res.moved and res.source_deleted and res.warning is None
    assert not src.exists()
    assert res.stored.read_bytes() == _EXT_COOKIES.encode("utf-8")
    assert sorted(p.name for p in store.iterdir()) == ["cookies.txt"]  # no temp leftovers
    assert video_dl.is_in_cookies_dir(res.stored, store)
    # per-run normalized temp copy still works from the stored file
    out, kept = video_dl.write_normalized_cookies(res.stored, tmp_path / "tmp")
    assert kept == 4 and res.stored.exists()
    video_dl.remove_file_quietly(out)


def test_import_cookies_replaces_old_stored(tmp_path):
    store = tmp_path / "cookies"
    store.mkdir()
    (store / "cookies.txt").write_text(".old.com\tTRUE\t/\tTRUE\t0\ta\tOLD\n", encoding="utf-8")
    (store / "cookies_legacy.txt").write_text("stale", encoding="utf-8")
    src = tmp_path / "new_cookies.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    res = video_dl.import_cookies_file(src, store)
    assert sorted(p.name for p in store.iterdir()) == ["cookies.txt"]
    assert b"OLD" not in res.stored.read_bytes() and b"SECRET1" in res.stored.read_bytes()
    assert not src.exists()


def test_import_cookies_noop_when_same_file(tmp_path):
    store = tmp_path / "cookies"
    store.mkdir()
    stored = store / "cookies.txt"
    stored.write_bytes(_EXT_COOKIES.encode("utf-8"))
    mtime = stored.stat().st_mtime_ns
    res = video_dl.import_cookies_file(stored, store)
    assert not res.moved and not res.source_deleted and res.kept == 4
    assert stored.exists() and stored.stat().st_mtime_ns == mtime


def test_import_cookies_invalid_keeps_everything(tmp_path):
    store = tmp_path / "cookies"
    store.mkdir()
    (store / "cookies.txt").write_bytes(_EXT_COOKIES.encode("utf-8"))
    bad = tmp_path / "bad.txt"
    bad.write_text("# only comments\nname=SECRETVALUE\n", encoding="utf-8")
    with pytest.raises(video_dl.CookiesFileError) as ei:
        video_dl.import_cookies_file(bad, store)
    assert "SECRETVALUE" not in str(ei.value)
    assert bad.exists() and (store / "cookies.txt").exists()


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


def test_clear_stored_cookies_deletes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    store = tmp_path / "cookies"
    src = tmp_path / "c.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    res = main.import_video_cookies_file(src, store)
    assert main.get_video_cookies_file_config() == str(res.stored)
    removed = main.clear_video_cookies_file(store)
    assert removed and not res.stored.exists()
    assert main.get_video_cookies_file_config() is None
    assert "video_cookies_file" not in main.load_config()


def test_migrate_config_pointing_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    store = tmp_path / "cookies"
    dl = tmp_path / "Downloads"
    dl.mkdir()
    src = dl / "cookies_x.com.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    main.set_video_cookies_file_config(src)
    msgs = main.migrate_video_cookies_file(store, dl)
    assert msgs and not src.exists()
    assert main.get_video_cookies_file_config() == str(store / "cookies.txt")
    assert (store / "cookies.txt").exists()
    assert all("SECRET" not in m for m in msgs)
    assert main.migrate_video_cookies_file(store, dl) == []  # idempotent


def test_migrate_downloads_file_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path / "cfg")
    store = tmp_path / "cookies"
    dl = tmp_path / "Downloads"
    dl.mkdir()
    assert main.migrate_video_cookies_file(store, dl) == []
    src = dl / "cookies_x.com.txt"
    src.write_bytes(_EXT_COOKIES.encode("utf-8"))
    msgs = main.migrate_video_cookies_file(store, dl)
    assert msgs and not src.exists()
    assert main.get_video_cookies_file_config() == str(store / "cookies.txt")


def test_gitignore_excludes_cookies():
    gi = (Path(main.__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in gi.splitlines()}
    assert "cookies/" in lines and "*cookies*.txt" in lines
