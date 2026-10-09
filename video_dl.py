# -*- coding: utf-8 -*-
"""视频下载（yt-dlp）核心逻辑 — 无 Tk 依赖，便于单元测试。

等价命令::

    yt-dlp -f "bv*+ba/b" -P "%USERPROFILE%\\Downloads" --newline [--no-playlist]
           [--cookies-from-browser X] "<URL>"

GUI（main.py 的「下载视频」对话框）只通过 :class:`DownloadRunner` 的事件
回调（放入 queue，由 Tk 主线程 ``after()`` 轮询）获取进度，后台线程绝不触碰 Tk。
"""

from __future__ import annotations

import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_FORMAT = "bv*+ba/b"
COOKIE_BROWSER_NONE = "无"
COOKIE_BROWSERS = (COOKIE_BROWSER_NONE, "chrome", "edge", "firefox")
FFMPEG_INSTALL_HINT = "winget install Gyan.FFmpeg"
YTDLP_PIP_PACKAGE = "yt-dlp"

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_PROGRESS_RE = re.compile(
    r"^\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%"
    r"(?:\s+of\s+~?\s*(?P<total>\S+))?"
    r"(?:\s+at\s+(?P<speed>\S+))?"
    r"(?:\s+ETA\s+(?P<eta>\S+))?"
)
_DEST_RES = (
    re.compile(r'^\[Merger\] Merging formats into "(?P<path>.+)"\s*$'),
    re.compile(r"^\[download\] Destination: (?P<path>.+?)\s*$"),
    re.compile(r"^\[download\] (?P<path>.+?) has already been downloaded"),
    re.compile(r'^\[(?:ExtractAudio|VideoConvertor|VideoRemuxer)\] Destination: (?P<path>.+?)\s*$'),
)

EventCallback = Callable[[tuple], None]


# ---------------------------------------------------------------- URLs


def looks_like_url(text: Optional[str]) -> bool:
    """True if *text* (stripped) is a single http(s) URL."""
    if not text:
        return False
    return bool(_URL_RE.match(text.strip()))


def split_urls(text: Optional[str]) -> List[str]:
    """Split pasted text into http(s) URLs (one per line / whitespace), deduped in order.

    Blank lines, ``#`` comment lines and non-URL tokens are ignored.
    """
    if not text:
        return []
    out: List[str] = []
    seen = set()
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for tok in line.split():
            tok = tok.strip().strip("\"'<>")
            if not looks_like_url(tok):
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out


# ---------------------------------------------------------------- paths / tools


def default_download_dir() -> Path:
    """%USERPROFILE%\\Downloads（Windows）或 ~/Downloads。"""
    base = os.environ.get("USERPROFILE") if sys.platform == "win32" else None
    home = Path(base) if base else Path.home()
    return home / "Downloads"


def normalize_cookies_browser(value: Optional[str]) -> Optional[str]:
    """'无' / '' / None → None；其它返回小写浏览器名（仅限支持列表）。"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v or v in (COOKIE_BROWSER_NONE, "none", "no"):
        return None
    return v if v in COOKIE_BROWSERS else None


def console_python() -> str:
    """Prefer python.exe over pythonw.exe for child processes (stdout pipes)."""
    exe = sys.executable or "python"
    p = Path(exe)
    if p.name.lower() == "pythonw.exe":
        cand = p.with_name("python.exe")
        if cand.is_file():
            return str(cand)
    return exe


def find_ytdlp(
    which: Callable[[str], Optional[str]] = shutil.which,
    has_module: Optional[Callable[[], bool]] = None,
) -> Optional[List[str]]:
    """Return the base command for yt-dlp, or None if unavailable.

    Order: ``yt-dlp`` on PATH → ``<python> -m yt_dlp`` (if the module is importable).
    """
    exe = which("yt-dlp")
    if exe:
        return [exe]
    if has_module is None:
        importlib.invalidate_caches()  # pick up a fresh `pip install yt-dlp`
        has_module = lambda: importlib.util.find_spec("yt_dlp") is not None  # noqa: E731
    try:
        if has_module():
            return [console_python(), "-m", "yt_dlp"]
    except (ImportError, ValueError):
        pass
    return None


def _winget_ffmpeg_candidates() -> List[str]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    out: List[str] = []
    link = Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    if link.is_file():
        out.append(str(link))
    pattern = str(
        Path(local) / "Microsoft" / "WinGet" / "Packages" / "*FFmpeg*" / "*" / "bin" / "ffmpeg.exe"
    )
    out.extend(sorted(glob.glob(pattern)))
    return out


def find_ffmpeg(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Locate ffmpeg: PATH first, then (Windows) winget install locations.

    winget 刚装完时当前进程的 PATH 可能还没刷新，所以额外搜索 WinGet 目录。
    """
    exe = which("ffmpeg")
    if exe:
        return exe
    if sys.platform == "win32":
        for cand in _winget_ffmpeg_candidates():
            return cand
    return None


def ffmpeg_missing_message() -> str:
    return (
        "未找到 ffmpeg：最佳视频+音频（bv*+ba）需要 ffmpeg 合并，否则可能只能下载单一格式或合并失败。\n"
        f"安装方法（PowerShell）：{FFMPEG_INSTALL_HINT}  （装好后重开本程序）"
    )


def ffmpeg_location_arg(ffmpeg_path: Optional[str], which=shutil.which) -> Optional[str]:
    """If ffmpeg was found outside PATH, return its folder for ``--ffmpeg-location``."""
    if not ffmpeg_path:
        return None
    if which("ffmpeg"):
        return None
    return str(Path(ffmpeg_path).parent)


# ---------------------------------------------------------------- commands


def build_command(
    base: Sequence[str],
    url: str,
    out_dir,
    *,
    no_playlist: bool = True,
    cookies_browser: Optional[str] = None,
    ffmpeg_location: Optional[str] = None,
    fmt: str = DEFAULT_FORMAT,
) -> List[str]:
    """Build ``yt-dlp -f bv*+ba/b -P <dir> --newline [--no-playlist] [--cookies-from-browser X] URL``."""
    if not base:
        raise ValueError("yt-dlp command is empty")
    cmd = list(base) + ["-f", fmt, "-P", str(out_dir), "--newline"]
    if no_playlist:
        cmd.append("--no-playlist")
    browser = normalize_cookies_browser(cookies_browser)
    if browser:
        cmd += ["--cookies-from-browser", browser]
    if ffmpeg_location:
        cmd += ["--ffmpeg-location", str(ffmpeg_location)]
    cmd.append(url)
    return cmd


def version_command(base: Sequence[str]) -> List[str]:
    return list(base) + ["--version"]


def pip_install_command(upgrade: bool = True) -> List[str]:
    cmd = [console_python(), "-m", "pip", "install"]
    if upgrade:
        cmd.append("-U")
    cmd.append(YTDLP_PIP_PACKAGE)
    return cmd


# ---------------------------------------------------------------- parsing


def parse_progress(line: str) -> Optional[Dict[str, object]]:
    """Parse a ``yt-dlp --newline`` progress line.

    ``[download]  42.3% of ~ 10.00MiB at  1.23MiB/s ETA 00:05`` →
    ``{"percent": 42.3, "total": "10.00MiB", "speed": "1.23MiB/s", "eta": "00:05"}``.
    Returns None for non-progress lines.
    """
    if not line:
        return None
    m = _PROGRESS_RE.match(line.strip())
    if not m:
        return None
    try:
        pct = float(m.group("pct"))
    except (TypeError, ValueError):
        return None
    pct = max(0.0, min(100.0, pct))
    return {
        "percent": pct,
        "total": m.group("total"),
        "speed": m.group("speed"),
        "eta": m.group("eta"),
    }


def parse_destination(line: str) -> Optional[str]:
    """Return output file path mentioned in a yt-dlp log line (Destination / Merger)."""
    if not line:
        return None
    s = line.strip()
    for rx in _DEST_RES:
        m = rx.match(s)
        if m:
            return m.group("path").strip()
    return None


def format_progress(info: Dict[str, object]) -> str:
    parts = [f"{float(info['percent']):.1f}%"]
    if info.get("total"):
        parts.append(f"共 {info['total']}")
    if info.get("speed"):
        parts.append(f"速度 {info['speed']}")
    if info.get("eta"):
        parts.append(f"剩余 {info['eta']}")
    return " · ".join(parts)


# ---------------------------------------------------------------- processes


def popen_kwargs() -> dict:
    """Common Popen kwargs: no console window on Windows, UTF-8 merged output."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    kw = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def kill_process_tree(proc: Optional[subprocess.Popen]) -> None:
    """Terminate *proc* and its children (yt-dlp may spawn ffmpeg)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=15,
            )
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_capture(cmd: Sequence[str], timeout: float = 60) -> Tuple[int, str]:
    """Run a short command (e.g. ``--version``) and return (returncode, output)."""
    kw = popen_kwargs()
    kw.pop("bufsize", None)
    try:
        cp = subprocess.run(list(cmd), timeout=timeout, **kw)
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "超时"
    return cp.returncode, (cp.stdout or "").strip()


def probe_tools() -> Dict[str, object]:
    """Locate yt-dlp (+version) and ffmpeg. Safe to call from a worker thread."""
    base = find_ytdlp()
    version = None
    if base:
        rc, out = run_capture(version_command(base), timeout=60)
        if rc == 0 and out:
            version = out.splitlines()[-1].strip()
        elif rc == 127:  # executable vanished / not runnable
            base = None
    ffmpeg = find_ffmpeg()
    return {"ytdlp": base, "ytdlp_version": version, "ffmpeg": ffmpeg}


class DownloadRunner:
    """Sequential yt-dlp queue for a worker thread.

    Events passed to ``on_event`` (caller should push them into a queue for Tk):

    - ``("log", text)``
    - ``("start", idx, n, url)``
    - ``("progress", idx, n, info_dict)``
    - ``("file", idx, n, path)``
    - ``("item_done", idx, n, url, returncode)``
    - ``("finished", ok, failed, cancelled)``
    """

    def __init__(
        self,
        base: Sequence[str],
        urls: Sequence[str],
        out_dir,
        *,
        no_playlist: bool = True,
        cookies_browser: Optional[str] = None,
        ffmpeg_location: Optional[str] = None,
        on_event: EventCallback,
    ) -> None:
        self.base = list(base)
        self.urls = list(urls)
        self.out_dir = Path(out_dir)
        self.no_playlist = no_playlist
        self.cookies_browser = cookies_browser
        self.ffmpeg_location = ffmpeg_location
        self.on_event = on_event
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._proc
        kill_process_tree(proc)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> Tuple[int, int, bool]:
        ok = failed = 0
        n = len(self.urls)
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.on_event(("log", f"无法创建输出目录：{e}"))
            self.on_event(("finished", 0, n, False))
            return 0, n, False
        for idx, url in enumerate(self.urls, start=1):
            if self.cancelled:
                break
            cmd = build_command(
                self.base,
                url,
                self.out_dir,
                no_playlist=self.no_playlist,
                cookies_browser=self.cookies_browser,
                ffmpeg_location=self.ffmpeg_location,
            )
            self.on_event(("start", idx, n, url))
            self.on_event(("log", "> " + subprocess.list2cmdline(cmd)))
            rc = self._run_one(cmd, idx, n)
            self.on_event(("item_done", idx, n, url, rc))
            if rc == 0 and not self.cancelled:
                ok += 1
            else:
                failed += 1
        cancelled = self.cancelled
        if cancelled:
            failed = n - ok  # include URLs never started
        self.on_event(("finished", ok, failed, cancelled))
        return ok, failed, cancelled

    def _run_one(self, cmd: List[str], idx: int, n: int) -> int:
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs())
        except (OSError, ValueError) as e:
            self.on_event(("log", f"启动 yt-dlp 失败：{e}"))
            return -1
        with self._lock:
            self._proc = proc
        if self.cancelled:  # cancel raced with start
            kill_process_tree(proc)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                info = parse_progress(line)
                if info is not None:
                    self.on_event(("progress", idx, n, info))
                    continue
                dest = parse_destination(line)
                if dest:
                    self.on_event(("file", idx, n, dest))
                self.on_event(("log", line))
        finally:
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            with self._lock:
                self._proc = None
        return rc


def stream_command(
    cmd: Sequence[str],
    on_line: Callable[[str], None],
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Run *cmd* (e.g. pip install) streaming merged output lines to *on_line*."""
    try:
        proc = subprocess.Popen(list(cmd), **popen_kwargs())
    except (OSError, ValueError) as e:
        on_line(f"启动失败：{e}")
        return -1
    assert proc.stdout is not None
    for raw in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            kill_process_tree(proc)
            break
        line = raw.rstrip("\r\n")
        if line:
            on_line(line)
    return proc.wait()
