# -*- coding: utf-8 -*-
"""图集下载（gallery-dl）核心逻辑 — 无 Tk 依赖，便于单元测试。

等价命令::

    gallery-dl -d "%USERPROFILE%\\Downloads" [--cookies <规范化副本>] "<URL>"

未登录 E-Hentai/ExHentai 时多为网页预览图（.webp）；本模块可边下边把
``.webp`` 转成 ``.jpg``（Pillow，quality=95，透明底铺白）并删除原 webp。

GUI（main.py「下载图集」对话框）只通过 :class:`DownloadRunner` 事件回调
（放入 queue，由 Tk 主线程 ``after()`` 轮询）获取进度。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import video_dl as _vd

# Re-export URL helpers so callers / tests can use gallery_fetch.split_urls etc.
looks_like_url = _vd.looks_like_url
split_urls = _vd.split_urls
default_download_dir = _vd.default_download_dir
console_python = _vd.console_python
kill_process_tree = _vd.kill_process_tree
popen_kwargs = _vd.popen_kwargs
run_capture = _vd.run_capture
stream_command = _vd.stream_command
write_normalized_cookies = _vd.write_normalized_cookies
remove_file_quietly = _vd.remove_file_quietly
CookiesFileError = _vd.CookiesFileError
site_for_url = _vd.site_for_url
stored_cookies_for_site = _vd.stored_cookies_for_site
tool_cookies_dir = _vd.tool_cookies_dir
normalize_cookies_browser = _vd.normalize_cookies_browser

GALLERY_DL_PIP_PACKAGE = "gallery-dl"
EHENTAI_SITES = ("e-hentai.org", "exhentai.org")
WEBP_EXT = ".webp"
JPG_QUALITY = 95
COOKIES_ORIGINALS_TIP = (
    "提示：未登录时 E-Hentai/ExHentai 多半只能下到网页预览图（.webp）。"
    "要原图（jpg/png）请先在「Cookies 文件」导入已登录 e-hentai.org / exhentai.org 的 Cookies，"
    "或在「下载视频」窗口用「打开 Edge 登录…」登录后导出。"
)
WEBP_CONVERT_TIP = (
    "提示：已开启「webp 转 jpg」：每出现一张 .webp 会转成同名 .jpg（质量 95）并删除原 webp。"
)

# gallery-dl PipeOutput: success → "{path}\n"; skip → "#{path}\n"
# Terminal progress on stderr: " 42%  1.23MB  500kB/s "
_PROGRESS_RE = re.compile(
    r"^\s*(?P<pct>\d{1,3})%\s+(?P<downloaded>\S+)\s+(?P<speed>\S+)\s*$"
)
_SKIP_PREFIX = "#"
_IMAGE_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif", ".jxl", ".bmp", ".tif", ".tiff")
# Optional "# num/total" style (some extractors / -v)
_COUNT_RE = re.compile(r"^#?\s*(?P<cur>\d+)\s*/\s*(?P<total>\d+)\b")

EventCallback = Callable[[tuple], None]


# ---------------------------------------------------------------- sites / URLs


def is_ehentai_url(url: Optional[str]) -> bool:
    """True for e-hentai.org / exhentai.org gallery (or related) URLs."""
    return site_for_url(url) in EHENTAI_SITES


def is_gallery_url(url: Optional[str]) -> bool:
    """Heuristic: EH / ExH gallery links (extend later for other gallery-dl sites)."""
    return is_ehentai_url(url)


# ---------------------------------------------------------------- tools


def find_gallery_dl(
    which: Callable[[str], Optional[str]] = shutil.which,
    has_module: Optional[Callable[[], bool]] = None,
) -> Optional[List[str]]:
    """Return base command for gallery-dl, or None if unavailable.

    Order: ``gallery-dl`` on PATH → ``<python> -m gallery_dl``.
    """
    exe = which("gallery-dl")
    if exe:
        return [exe]
    if has_module is None:
        importlib.invalidate_caches()
        has_module = lambda: importlib.util.find_spec("gallery_dl") is not None  # noqa: E731
    try:
        if has_module():
            return [console_python(), "-m", "gallery_dl"]
    except (ImportError, ValueError):
        pass
    return None


def version_command(base: Sequence[str]) -> List[str]:
    return list(base) + ["--version"]


def pip_install_command(upgrade: bool = True) -> List[str]:
    cmd = [console_python(), "-m", "pip", "install"]
    if upgrade:
        cmd.append("-U")
    cmd.append(GALLERY_DL_PIP_PACKAGE)
    return cmd


def probe_tools() -> Dict[str, object]:
    """Locate gallery-dl (+version). Safe to call from a worker thread."""
    base = find_gallery_dl()
    version = None
    if base:
        rc, out = run_capture(version_command(base), timeout=60)
        if rc == 0 and out:
            version = out.splitlines()[-1].strip()
        elif rc == 127:
            base = None
    return {"gallery_dl": base, "gallery_dl_version": version}


# ---------------------------------------------------------------- command


def build_command(
    base: Sequence[str],
    url: str,
    out_dir,
    *,
    cookies_file=None,
    cookies_browser: Optional[str] = None,
) -> List[str]:
    """Build ``gallery-dl -d <outdir> [--cookies …] URL``.

    *cookies_file* takes priority over *cookies_browser*; never both.
    """
    if not base:
        raise ValueError("gallery-dl command is empty")
    cmd = list(base) + ["-d", str(out_dir)]
    browser = normalize_cookies_browser(cookies_browser)
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    elif browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)
    return cmd


# ---------------------------------------------------------------- parsing


def parse_progress(line: str) -> Optional[Dict[str, object]]:
    """Parse gallery-dl stderr-style progress: `` 42%  1.23MB  500kB/s ``."""
    if not line:
        return None
    # strip ANSI
    clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip("\r\n")
    m = _PROGRESS_RE.match(clean)
    if not m:
        return None
    return {
        "percent": float(m.group("pct")),
        "downloaded": m.group("downloaded"),
        "speed": m.group("speed"),
    }


def parse_file_path(line: str) -> Optional[str]:
    """Extract a downloaded (or skipped) file path from a gallery-dl output line.

    PipeOutput success: bare path. Skip: ``#path`` or ``# path``.
    """
    if not line:
        return None
    clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    if not clean:
        return None
    # count-only lines like "# 3/1202"
    if _COUNT_RE.match(clean):
        return None
    if clean.startswith(_SKIP_PREFIX):
        clean = clean[len(_SKIP_PREFIX) :].lstrip()
    # must look like a path with an image extension
    lower = clean.lower()
    if not any(lower.endswith(ext) for ext in _IMAGE_EXTS):
        return None
    # reject URLs
    if lower.startswith("http://") or lower.startswith("https://"):
        return None
    return clean


def parse_count(line: str) -> Optional[Tuple[int, int]]:
    """Parse ``# 3/1202`` or ``3/1202`` style counters if present."""
    if not line:
        return None
    clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    m = _COUNT_RE.match(clean)
    if not m:
        return None
    return int(m.group("cur")), int(m.group("total"))


def format_progress(info: Dict[str, object]) -> str:
    parts = [f"{float(info['percent']):.0f}%"]
    if info.get("downloaded"):
        parts.append(f"已下 {info['downloaded']}")
    if info.get("speed"):
        parts.append(f"速度 {info['speed']}")
    return " · ".join(parts)


# ---------------------------------------------------------------- webp → jpg


def convert_webp_to_jpg(
    src,
    *,
    quality: int = JPG_QUALITY,
    delete_src: bool = True,
) -> Optional[Path]:
    """Convert one ``.webp`` to ``.jpg`` (RGB, white background for alpha).

    Returns the jpg path on success, None if skipped (not webp / missing) or on error.
    Never raises for ordinary I/O / decode failures.
    """
    p = Path(src)
    if p.suffix.lower() != WEBP_EXT:
        return None
    if not p.is_file():
        return None
    dest = p.with_suffix(".jpg")
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(p) as im:
            # Handle animated / multi-frame: use first frame
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                rgb = bg
            else:
                rgb = im.convert("RGB")
            rgb.save(dest, "JPEG", quality=int(quality), optimize=True)
    except Exception:
        return None
    if delete_src:
        try:
            p.unlink()
        except OSError:
            pass
    return dest


def convert_webp_tree(
    root,
    *,
    quality: int = JPG_QUALITY,
    delete_src: bool = True,
) -> Tuple[int, int]:
    """Convert all ``.webp`` under *root* (recursive). Returns ``(ok, failed)``."""
    root_p = Path(root)
    if not root_p.is_dir():
        return 0, 0
    ok = failed = 0
    for p in sorted(root_p.rglob("*.webp")):
        if convert_webp_to_jpg(p, quality=quality, delete_src=delete_src) is not None:
            ok += 1
        else:
            # still exists and is webp → failed; missing → ignore
            if p.is_file():
                failed += 1
    return ok, failed


class WebpWatcher:
    """Background poller: convert new ``.webp`` files under *root* as they appear."""

    def __init__(
        self,
        root,
        *,
        interval: float = 0.6,
        quality: int = JPG_QUALITY,
        on_converted: Optional[Callable[[Path, Path], None]] = None,
    ) -> None:
        self.root = Path(root)
        self.interval = max(0.2, float(interval))
        self.quality = quality
        self.on_converted = on_converted
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: set = set()
        self.converted = 0
        self.failed = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="webp-watcher")
        self._thread.start()

    def stop(self, final_sweep: bool = True) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=30)
        if final_sweep:
            self._sweep()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._sweep()

    def _sweep(self) -> None:
        if not self.root.is_dir():
            return
        try:
            files = list(self.root.rglob("*.webp"))
        except OSError:
            return
        for p in files:
            key = str(p)
            # skip if already handled this path in this session and gone
            if key in self._seen and not p.is_file():
                continue
            # wait until file size stable (download may still be writing)
            try:
                s1 = p.stat().st_size
            except OSError:
                continue
            time.sleep(0.05)
            try:
                s2 = p.stat().st_size
            except OSError:
                continue
            if s1 != s2 or s2 == 0:
                continue
            dest = convert_webp_to_jpg(p, quality=self.quality, delete_src=True)
            self._seen.add(key)
            if dest is not None:
                self.converted += 1
                if self.on_converted is not None:
                    try:
                        self.on_converted(p, dest)
                    except Exception:
                        pass
            else:
                if p.is_file():
                    self.failed += 1


# ---------------------------------------------------------------- runner


class DownloadRunner:
    """Sequential gallery-dl queue for a worker thread.

    Events:

    - ``("log", text)``
    - ``("start", idx, n, url)``
    - ``("progress", idx, n, info_dict)``
    - ``("file", idx, n, path)``
    - ``("converted", idx, n, jpg_path)``
    - ``("item_done", idx, n, url, returncode)``
    - ``("finished", ok, failed, cancelled)``
    """

    def __init__(
        self,
        base: Sequence[str],
        urls: Sequence[str],
        out_dir,
        *,
        cookies_browser: Optional[str] = None,
        cookies_file=None,
        cookies_tmp_dir=None,
        cookies_dir=None,
        convert_webp: bool = True,
        on_event: EventCallback,
    ) -> None:
        self.base = list(base)
        self.urls = list(urls)
        self.out_dir = Path(out_dir)
        self.cookies_browser = cookies_browser
        self.cookies_file = str(cookies_file) if cookies_file else None
        self.cookies_tmp_dir = cookies_tmp_dir
        self.cookies_dir = Path(cookies_dir) if cookies_dir is not None else None
        self.convert_webp = bool(convert_webp)
        self.on_event = on_event
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._watcher: Optional[WebpWatcher] = None

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

        if self.convert_webp:
            self.on_event(("log", WEBP_CONVERT_TIP))
            self._watcher = WebpWatcher(
                self.out_dir,
                on_converted=lambda _src, dest: self.on_event(("converted", 0, n, str(dest))),
            )
            self._watcher.start()

        tmp_cookies: Optional[Path] = None
        browser = normalize_cookies_browser(self.cookies_browser)
        if self.cookies_file:
            try:
                tmp_cookies, kept = write_normalized_cookies(self.cookies_file, self.cookies_tmp_dir)
            except CookiesFileError as e:
                self.on_event(("log", str(e)))
                self._stop_watcher()
                self.on_event(("finished", 0, n, False))
                return 0, n, False
            self.on_event(
                ("log", f"Cookies 文件：已整理为标准 Netscape 格式临时副本（{kept} 条），运行结束后自动删除。")
            )
            if browser:
                self.on_event(("log", "已选择 Cookies 文件，忽略「浏览器 Cookies」设置。"))

        site_tmp: Dict[str, Optional[Path]] = {}
        eh_tip_shown = False
        try:
            for idx, url in enumerate(self.urls, start=1):
                if self.cancelled:
                    break
                cfile, cbrowser = self._cookies_for_url(url, tmp_cookies, browser, site_tmp)
                if is_ehentai_url(url) and not cfile and not cbrowser and not eh_tip_shown:
                    self.on_event(("log", COOKIES_ORIGINALS_TIP))
                    eh_tip_shown = True
                cmd = build_command(
                    self.base,
                    url,
                    self.out_dir,
                    cookies_file=cfile,
                    cookies_browser=cbrowser,
                )
                self.on_event(("start", idx, n, url))
                self.on_event(("log", "> " + subprocess.list2cmdline(cmd)))
                rc = self._run_one(cmd, idx, n)
                self.on_event(("item_done", idx, n, url, rc))
                if rc == 0 and not self.cancelled:
                    ok += 1
                else:
                    failed += 1
        finally:
            remove_file_quietly(tmp_cookies)
            for t in site_tmp.values():
                remove_file_quietly(t)
            self._stop_watcher()
        cancelled = self.cancelled
        if cancelled:
            failed = n - ok
        self.on_event(("finished", ok, failed, cancelled))
        return ok, failed, cancelled

    def _stop_watcher(self) -> None:
        w = self._watcher
        self._watcher = None
        if w is not None:
            w.stop(final_sweep=True)
            if w.converted or w.failed:
                self.on_event(
                    ("log", f"webp→jpg：成功 {w.converted} 张" + (f"，失败 {w.failed} 张" if w.failed else ""))
                )

    def _cookies_for_url(self, url, override, browser, site_tmp):
        if override:
            return override, None
        site = site_for_url(url)
        stored = stored_cookies_for_site(site, self.cookies_dir) if self.cookies_dir else None
        if stored is not None:
            if site not in site_tmp:
                try:
                    tmp, kept = write_normalized_cookies(stored, self.cookies_tmp_dir)
                    site_tmp[site] = tmp
                    self.on_event(("log", f"Cookies：使用 {site} 的 Cookies 文件（{kept} 条，临时副本，运行结束后删除）"))
                except CookiesFileError as e:
                    site_tmp[site] = None
                    self.on_event(("log", f"{site} 的 Cookies 文件不可用：{e}"))
            else:
                if site_tmp[site] is not None:
                    self.on_event(("log", f"Cookies：使用 {site} 的 Cookies 文件"))
            if site_tmp.get(site) is not None:
                return site_tmp[site], None
        if browser:
            self.on_event(("log", f"Cookies：{site or '该链接'} 没有已存的 Cookies 文件，使用浏览器 Cookies（{browser}）"))
            return None, browser
        if self.cookies_dir is not None:
            self.on_event(("log", f"Cookies：{site or '该链接'} 没有已存的 Cookies 文件，不使用 Cookies"))
        return None, None

    def _run_one(self, cmd: List[str], idx: int, n: int) -> int:
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs())
        except (OSError, ValueError) as e:
            self.on_event(("log", f"启动 gallery-dl 失败：{e}"))
            return -1
        with self._lock:
            self._proc = proc
        if self.cancelled:
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
                path = parse_file_path(line)
                if path:
                    self.on_event(("file", idx, n, path))
                    # convert immediately if watcher might lag; watcher also catches orphans
                    if self.convert_webp and path.lower().endswith(WEBP_EXT):
                        dest = convert_webp_to_jpg(path)
                        if dest is not None:
                            self.on_event(("converted", idx, n, str(dest)))
                    continue
                # count lines → soft progress
                cnt = parse_count(line)
                if cnt is not None:
                    cur, total = cnt
                    pct = (100.0 * cur / total) if total else 0.0
                    self.on_event(("progress", idx, n, {"percent": pct, "downloaded": f"{cur}/{total}", "speed": ""}))
                    continue
                self.on_event(("log", line))
        finally:
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            with self._lock:
                self._proc = None
        return rc
