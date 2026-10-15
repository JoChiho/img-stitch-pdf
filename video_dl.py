# -*- coding: utf-8 -*-
"""视频下载（yt-dlp）核心逻辑 — 无 Tk 依赖，便于单元测试。

等价命令::

    yt-dlp -f "bv*+ba/b" -P "%USERPROFILE%\\Downloads" --newline [--no-playlist]
           [--cookies-from-browser X | --cookies <规范化副本>] "<URL>"

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
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_FORMAT = "bv*+ba/b"
COOKIE_BROWSER_NONE = "无"
COOKIE_BROWSERS = (COOKIE_BROWSER_NONE, "chrome", "edge", "firefox")
FFMPEG_INSTALL_HINT = "winget install Gyan.FFmpeg"
YTDLP_PIP_PACKAGE = "yt-dlp[default]"  # [default] 带 yt-dlp-ejs（YouTube JS 挑战求解脚本）
DENO_INSTALL_HINT = "winget install DenoLand.Deno"
# YouTube 的 n/sig JS 挑战需要 deno + 求解脚本；允许 yt-dlp 从 GitHub 拉取求解脚本
YOUTUBE_REMOTE_COMPONENTS = "ejs:github"
YOUTUBE_SITES = ("youtube.com", "youtu.be")
# 年龄限制视频：默认客户端只给 360p（18），web_creator/web/mweb 的 https 格式又需要 GVS PO Token；
# web_safari 提供 HLS（m3u8）格式，带登录 Cookies 时无需 PO Token 即可拿到 1080p。
YOUTUBE_PLAYER_CLIENTS = "default,web_safari"
YOUTUBE_EXTRACTOR_ARGS = f"youtube:player_client={YOUTUBE_PLAYER_CLIENTS}"
DENO_MISSING_TIP = (
    f"提示：未找到 deno，YouTube 的 JS 挑战（n challenge）可能无法求解、格式缺失。"
    f"请在 PowerShell 运行：{DENO_INSTALL_HINT}，然后重启本工具。"
)
AGE_VERIFY_TIP = (
    "提示：该视频有年龄限制，YouTube 要求账号年龄验证。账号需在 YouTube 完成年龄验证，"
    "或 Cookies 未登录/已失效；建议用无痕窗口登录后导出并关闭窗口（YouTube 会轮换已打开标签页的 Cookies）。"
)
N_CHALLENGE_TIP = (
    "提示：YouTube JS 挑战（n challenge）求解失败。请先点「更新 yt-dlp」，并确认已安装 deno"
    f"（{DENO_INSTALL_HINT}）；本工具已自动加 --remote-components {YOUTUBE_REMOTE_COMPONENTS}。"
)
PO_TOKEN_TIP = (
    "提示（通常可忽略）：本工具已自动加 --extractor-args "
    f"{YOUTUBE_EXTRACTOR_ARGS}（web_safari HLS 格式无需 PO Token）。部分 YouTube 客户端格式需要 PO Token，已被跳过（通常仍能下载其它格式）；"
    "若全部失败，请更新 yt-dlp 并重新导出 YouTube Cookies。"
)
BOT_CHECK_TIP = (
    "提示：YouTube 要求“登录以确认不是机器人”。请用无痕窗口登录 YouTube，导出 cookies.txt 后关闭窗口，"
    "再在「Cookies 文件」导入 youtube.com 的 Cookies。"
)
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
HTTPONLY_PREFIX = "#HttpOnly_"
COOKIES_NEED_LOGIN_TIP = (
    "提示：这条推文/视频可能需要登录才能看到。请在「Cookies 文件」点「导入…」，"
    "导入浏览器扩展导出的该网站 cookies.txt（Netscape 格式）后重试。"
)
CHROME_COOKIE_DB_TIP = (
    "提示：无法复制 Chrome/Edge 的 Cookie 数据库（浏览器运行中会锁定该文件）。"
    "请完全关闭浏览器（含后台进程）后重试，或改用「Cookies 文件」（浏览器扩展导出 cookies.txt）。"
)
BROWSER_DECRYPT_TIP = (
    "提示：无法解密浏览器 Cookies（新版 Chrome/Edge 常见）。建议改用「Cookies 文件」"
    "（浏览器扩展导出 cookies.txt）。"
)

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


# ---------------------------------------------------------------- cookies.txt


class CookiesFileError(Exception):
    """Cookies 文件不可读 / 无有效条目（消息为中文，不含 cookie 值）。"""


def _decode_cookies_bytes(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def normalize_cookies_text(text: str) -> Tuple[str, int]:
    """Normalize a (browser-extension exported) cookies.txt to strict Netscape format.

    Python's ``http.cookiejar.MozillaCookieJar`` (used by yt-dlp) asserts that the
    2nd field (include-subdomains flag) equals ``domain.startswith('.')`` and
    rejects the whole file otherwise (``invalid Netscape format cookies file``).

    - writes the ``# Netscape HTTP Cookie File`` header
    - keeps only 7-field tab-separated lines (``#HttpOnly_`` lines kept; the domain
      after the prefix decides the flag); other comments / junk are dropped
    - field 2 → ``TRUE`` iff the domain starts with ``.``, else ``FALSE``
    - secure flag upper-cased; expiry coerced to an integer (empty/invalid → 0)
    - BOM (utf-8-sig) and CRLF handled; output uses ``\n``

    Returns ``(text, kept_count)``. Cookie values are copied verbatim, never logged.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    out = [NETSCAPE_HEADER, ""]
    kept = 0
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        prefix = ""
        body = line
        if body.startswith(HTTPONLY_PREFIX):
            prefix, body = HTTPONLY_PREFIX, body[len(HTTPONLY_PREFIX):]
        elif body.lstrip().startswith("#"):
            continue
        fields = body.split("\t")
        if len(fields) != 7:
            continue
        domain, _flag, path, secure, expires, name, value = fields
        domain = domain.strip()
        if not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if secure.strip().upper() == "TRUE" else "FALSE"
        exp = expires.strip()
        try:
            exp = str(max(0, int(float(exp)))) if exp else "0"
        except (ValueError, OverflowError):
            exp = "0"
        path = path.strip() or "/"
        out.append("\t".join([prefix + domain, flag, path, secure, exp, name, value]))
        kept += 1
    return "\n".join(out) + "\n", kept


def write_normalized_cookies(src, dest_dir=None) -> Tuple[Path, int]:
    """Write a normalized temp copy of cookies file *src*; returns ``(tmp_path, kept)``.

    The copy goes to *dest_dir* (default: ``%TEMP%``); the caller deletes it after
    the run (yt-dlp also writes cookies back to it, so the user's file stays untouched).
    """
    p = Path(src)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise CookiesFileError(f"无法读取 Cookies 文件：{p}（{e.strerror or e}）") from None
    raw_text = _decode_cookies_bytes(data)
    text, kept = normalize_cookies_text(raw_text)
    if kept == 0:
        raise CookiesFileError(cookies_error_message(p, analyze_cookies_text(raw_text)))
    try:
        if dest_dir is not None:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="img-stitch-pdf_cookies_", suffix=".txt",
            dir=str(dest_dir) if dest_dir is not None else None,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        raise CookiesFileError(f"无法写入临时 Cookies 文件：{e}") from None
    return Path(tmp), kept


# ---------------------------------------------------------------- stored cookies (per site)

COOKIES_DIRNAME = "cookies"
LEGACY_STORED_NAME = "cookies.txt"  # single-file layout before per-site storage
COOKIES_AUTO_HINT = "下载时按链接网站自动使用对应 Cookies"

# well-known video sites: preferred when picking the site key of a cookies file
KNOWN_VIDEO_SITES = (
    "youtube.com", "x.com", "bilibili.com", "tiktok.com", "douyin.com", "instagram.com",
    "facebook.com", "vimeo.com", "twitch.tv", "nicovideo.jp", "reddit.com", "weibo.com",
    "pixiv.net", "dailymotion.com", "threads.net", "niconico.jp", "iqiyi.com", "youku.com",
)
SITE_ALIASES = {
    "twitter.com": "x.com",
    "twimg.com": "x.com",
    "t.co": "x.com",
    "youtu.be": "youtube.com",
    "youtube-nocookie.com": "youtube.com",
    "ytimg.com": "youtube.com",
    "b23.tv": "bilibili.com",
    "bilivideo.com": "bilibili.com",
    "fb.watch": "facebook.com",
    "nico.ms": "nicovideo.jp",
    "redd.it": "reddit.com",
    "weibo.cn": "weibo.com",
    "instagr.am": "instagram.com",
}
# second-level public suffixes → registrable domain keeps three labels
_MULTI_PART_SUFFIXES = {
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.cn", "net.cn", "org.cn", "com.tw", "com.hk", "com.au", "net.au", "co.kr",
    "com.br", "co.in", "co.nz", "com.sg",
}
_SITE_KEY_SAFE = re.compile(r"[^a-z0-9.\-]")


def registrable_domain(host: Optional[str]) -> Optional[str]:
    """``.www.Music.YouTube.com`` → ``youtube.com``; ``news.bbc.co.uk`` → ``bbc.co.uk``."""
    if not host:
        return None
    h = str(host).strip().lower()
    if h.startswith(HTTPONLY_PREFIX.lower()):
        h = h[len(HTTPONLY_PREFIX):]
    h = h.split("@")[-1].split(":")[0].strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h or re.fullmatch(r"[\d.]+", h):
        return h or None
    labels = [x for x in h.split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def site_key_for_host(host: Optional[str]) -> Optional[str]:
    """Host / cookie domain → site key with aliases (twitter.com → x.com, youtu.be → youtube.com)."""
    reg = registrable_domain(host)
    if not reg:
        return None
    return SITE_ALIASES.get(reg, reg)


def site_for_url(url: Optional[str]) -> Optional[str]:
    """``https://m.youtube.com/watch?v=1`` → ``youtube.com``; None if no host."""
    if not url:
        return None
    from urllib.parse import urlsplit

    try:
        host = urlsplit(str(url).strip()).hostname
    except ValueError:
        return None
    return site_key_for_host(host)


def detect_cookies_site(domains: Sequence[str]) -> Optional[str]:
    """Pick the site key for a cookies file from its cookie domains.

    Known video sites win (so ``google.com`` cookies inside a YouTube export do not
    hijack the key); otherwise the most frequent registrable domain. Ties → alphabetical.
    """
    counts: Dict[str, int] = {}
    for d in domains:
        k = site_key_for_host(d)
        if k:
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None
    known = {k: v for k, v in counts.items() if k in KNOWN_VIDEO_SITES}
    pool = known or counts
    return sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def safe_site_filename(site: str) -> str:
    key = _SITE_KEY_SAFE.sub("_", str(site).strip().lower()).strip(".") or "unknown"
    return key + ".txt"


class CookiesInfo:
    """Parsed summary of a cookies file (never holds cookie values)."""

    def __init__(self, kept: int, domains: List[str], content_lines: int) -> None:
        self.kept = kept
        self.domains = domains
        self.content_lines = content_lines  # non-blank, non-comment lines
        self.site = detect_cookies_site(domains)


def analyze_cookies_text(text: str) -> CookiesInfo:
    if text.startswith("\ufeff"):
        text = text[1:]
    domains: List[str] = []
    content = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        body = line
        if body.startswith(HTTPONLY_PREFIX):
            body = body[len(HTTPONLY_PREFIX):]
        elif s.startswith("#"):
            continue
        content += 1
        fields = body.rstrip("\r\n").split("\t")
        if len(fields) == 7 and fields[0].strip():
            domains.append(fields[0].strip())
    return CookiesInfo(len(domains), domains, content)


def _read_cookies_info(p: Path) -> CookiesInfo:
    try:
        data = p.read_bytes()
    except OSError as e:
        raise CookiesFileError(f"无法读取 Cookies 文件：{p}（{e.strerror or e}）") from None
    return analyze_cookies_text(_decode_cookies_bytes(data))


def cookies_error_message(p, info: CookiesInfo) -> str:
    """Chinese error for a file without valid cookie lines (empty vs. wrong format)."""
    if info.content_lines == 0:
        return (
            f"这个 Cookies 文件是空的（只有文件头，没有任何 Cookie）。请在已登录该网站的页面上重新导出。\n文件：{p}"
        )
    return (
        f"Cookies 文件格式不对：{p}\n没有找到 7 列、Tab 分隔的 Cookie 行；"
        "需要 Netscape 格式（浏览器扩展导出的 cookies.txt）。"
    )


def tool_cookies_dir() -> Path:
    """``<工具文件夹>\\cookies``（与 main.py / video_dl.py 同级；已在 .gitignore 中排除）。"""
    return Path(__file__).resolve().parent / COOKIES_DIRNAME


def _store(store_dir) -> Path:
    return Path(store_dir) if store_dir is not None else tool_cookies_dir()


def _same_file(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def validate_cookies_file(src) -> int:
    """Return the number of valid cookie lines in *src*; raise :class:`CookiesFileError` if none."""
    p = Path(src)
    info = _read_cookies_info(p)
    if info.kept == 0:
        raise CookiesFileError(cookies_error_message(p, info))
    return info.kept


class StoredCookies:
    """One stored per-site cookies file: ``site``, ``path``, ``mtime``, ``count``."""

    def __init__(self, site: str, path: Path, mtime: float, count: int) -> None:
        self.site, self.path, self.mtime, self.count = site, path, mtime, count


def stored_cookies_path_for_site(site: str, store_dir=None) -> Path:
    return _store(store_dir) / safe_site_filename(site)


def stored_cookies_for_site(site: Optional[str], store_dir=None) -> Optional[Path]:
    if not site:
        return None
    p = stored_cookies_path_for_site(site, store_dir)
    return p if p.is_file() else None


def list_stored_cookies(store_dir=None) -> List[StoredCookies]:
    d = _store(store_dir)
    out: List[StoredCookies] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.txt")):
        if not f.is_file() or f.name.startswith(".") or f.name == LEGACY_STORED_NAME:
            continue
        try:
            info = _read_cookies_info(f)
            mtime = f.stat().st_mtime
        except (CookiesFileError, OSError):
            continue
        out.append(StoredCookies(f.stem, f, mtime, info.kept))
    return out


class CookiesImportResult:
    """Outcome of :func:`import_cookies_file`（不含 cookie 值）。"""

    def __init__(self, site: str, stored: Path, kept: int, *, moved: bool, source_deleted: bool,
                 replaced: bool = False, warning: Optional[str] = None) -> None:
        self.site = site
        self.stored = stored
        self.kept = kept
        self.moved = moved  # False = source already was the stored file (no-op)
        self.source_deleted = source_deleted
        self.replaced = replaced  # an older file for the same site was overwritten
        self.warning = warning


def _atomic_write(dest: Path, data: bytes) -> None:
    tmp: Optional[str] = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".import_", suffix=".tmp", dir=str(dest.parent))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, dest)
        tmp = None
    finally:
        if tmp:
            remove_file_quietly(tmp)


def import_cookies_file(src, store_dir=None) -> CookiesImportResult:
    """Move cookies file *src* into ``cookies\\<site>.txt`` (site detected from its domains).

    validate → detect site → atomic write (temp + ``os.replace``; overwrites only that
    site's old file) → delete *src*. If *src* is locked the stored copy is kept and a
    Chinese ``warning`` is returned. Raises :class:`CookiesFileError` (never cookie values).
    """
    sp = Path(src).expanduser()
    try:
        data = sp.read_bytes()
    except OSError as e:
        raise CookiesFileError(f"无法读取 Cookies 文件：{sp}（{e.strerror or e}）") from None
    info = analyze_cookies_text(_decode_cookies_bytes(data))
    if info.kept == 0:
        raise CookiesFileError(cookies_error_message(sp, info))
    if not info.site:
        raise CookiesFileError(f"无法从 Cookies 文件识别网站（域名为空）：{sp}")
    d = _store(store_dir)
    dest = d / safe_site_filename(info.site)
    if dest.exists() and _same_file(sp, dest):
        return CookiesImportResult(info.site, dest, info.kept, moved=False, source_deleted=False)
    replaced = dest.exists()
    try:
        _atomic_write(dest, data)
    except OSError as e:
        raise CookiesFileError(f"无法保存 Cookies 文件到工具文件夹：{d}（{e.strerror or e}）") from None
    warning = None
    deleted = not sp.exists()
    if not deleted:
        try:
            sp.unlink()
            deleted = True
        except OSError as e:
            warning = (
                f"警告：已保存到工具文件夹，但无法删除原 Cookies 文件：{sp}（{e.strerror or e}）。"
                "可能被其他程序占用，请稍后手动删除（其中含登录凭据）。"
            )
    return CookiesImportResult(info.site, dest, info.kept, moved=True, source_deleted=deleted,
                               replaced=replaced, warning=warning)


def delete_stored_cookies(site: str, store_dir=None) -> bool:
    """Delete ``cookies\\<site>.txt`` only; other sites untouched. True if removed."""
    p = stored_cookies_path_for_site(site, store_dir)
    try:
        p.unlink()
        return True
    except OSError:
        return False


def migrate_legacy_cookies(store_dir=None) -> List[str]:
    """``cookies\\cookies.txt`` (old single-file layout) → ``cookies\\<site>.txt``.

    Returns Chinese log lines (never cookie values). Does not raise.
    """
    d = _store(store_dir)
    legacy = d / LEGACY_STORED_NAME
    msgs: List[str] = []
    if not legacy.is_file():
        return msgs
    try:
        info = _read_cookies_info(legacy)
    except CookiesFileError as e:
        return [f"迁移 Cookies 文件失败：{e}"]
    if info.kept == 0 or not info.site:
        return [f"迁移 Cookies 文件跳过：{cookies_error_message(legacy, info)}"]
    dest = d / safe_site_filename(info.site)
    if dest.exists():
        return [f"迁移 Cookies 文件跳过：{dest.name} 已存在，保留旧文件 {legacy.name}（可手动删除）。"]
    try:
        os.replace(legacy, dest)
    except OSError as e:
        return [f"迁移 Cookies 文件失败：{legacy} → {dest.name}（{e.strerror or e}）"]
    msgs.append(f"已迁移 Cookies 文件：{legacy.name} → {dest.name}（网站 {info.site}，{info.kept} 条）")
    return msgs


def remove_file_quietly(path) -> None:
    if not path:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def output_hint(line: str, *, has_cookies: bool) -> Optional[str]:
    """Chinese hint for well-known yt-dlp failures in an output *line*, else None."""
    if not line:
        return None
    if "Could not copy Chrome cookie database" in line:
        return CHROME_COOKIE_DB_TIP
    if "Failed to decrypt with DPAPI" in line:
        return BROWSER_DECRYPT_TIP
    if "No video could be found" in line and not has_cookies:
        return COOKIES_NEED_LOGIN_TIP
    if "age-restricted" in line and "age-verification" in line:
        return AGE_VERIFY_TIP
    if "n challenge solving failed" in line:
        return N_CHALLENGE_TIP
    if "PO Token" in line and ("WARNING" in line or "ERROR" in line):
        return PO_TOKEN_TIP
    if "Sign in to confirm you" in line and "not a bot" in line:
        return BOT_CHECK_TIP
    return None


def is_youtube_url(url: Optional[str]) -> bool:
    return site_for_url(url) in YOUTUBE_SITES


def _winget_deno_candidates() -> List[str]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    pats = [
        os.path.join(local, "Microsoft", "WinGet", "Packages", "DenoLand.Deno*", "deno.exe"),
        os.path.join(local, "Microsoft", "WinGet", "Links", "deno.exe"),
    ]
    out: List[str] = []
    for pat in pats:
        out += sorted(glob.glob(pat))
    home = os.path.expanduser("~")
    out.append(os.path.join(home, ".deno", "bin", "deno.exe"))
    return out


def deno_runtime_arg(deno: Optional[str], which=shutil.which) -> Optional[str]:
    """Path for ``--js-runtimes deno:<path>`` only when deno is found but not on PATH."""
    if not deno or which("deno"):
        return None
    return deno


def find_deno(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """Locate deno (JS runtime yt-dlp uses for YouTube challenges)."""
    p = which("deno")
    if p:
        return p
    for c in _winget_deno_candidates():
        if os.path.isfile(c):
            return c
    return None


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
    cookies_file=None,
    deno_path: Optional[str] = None,
) -> List[str]:
    """Build ``yt-dlp -f bv*+ba/b -P <dir> --newline [--no-playlist] [cookies] URL``.

    *cookies_file* (``--cookies``) takes priority over *cookies_browser*
    (``--cookies-from-browser``); the two are never passed together.
    """
    if not base:
        raise ValueError("yt-dlp command is empty")
    cmd = list(base) + ["-f", fmt, "-P", str(out_dir), "--newline"]
    if no_playlist:
        cmd.append("--no-playlist")
    browser = normalize_cookies_browser(cookies_browser)
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    elif browser:
        cmd += ["--cookies-from-browser", browser]
    if ffmpeg_location:
        cmd += ["--ffmpeg-location", str(ffmpeg_location)]
    if is_youtube_url(url):
        cmd += ["--remote-components", YOUTUBE_REMOTE_COMPONENTS]
        cmd += ["--extractor-args", YOUTUBE_EXTRACTOR_ARGS]
        if deno_path:
            cmd += ["--js-runtimes", f"deno:{deno_path}"]
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
    return {"ytdlp": base, "ytdlp_version": version, "ffmpeg": ffmpeg, "deno": find_deno()}


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
        cookies_file=None,
        cookies_tmp_dir=None,
        cookies_dir=None,
        deno_path: Optional[str] = None,
        on_event: EventCallback,
    ) -> None:
        self.deno_path = deno_path
        self.base = list(base)
        self.urls = list(urls)
        self.out_dir = Path(out_dir)
        self.no_playlist = no_playlist
        self.cookies_browser = cookies_browser
        self.ffmpeg_location = ffmpeg_location
        self.cookies_file = str(cookies_file) if cookies_file else None
        self.cookies_tmp_dir = cookies_tmp_dir
        # per-site store (cookies\<site>.txt); None disables the automatic lookup
        self.cookies_dir = Path(cookies_dir) if cookies_dir is not None else None
        self.on_event = on_event
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._has_cookies = False

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
        tmp_cookies: Optional[Path] = None  # explicit cookies_file override (all URLs)
        browser = normalize_cookies_browser(self.cookies_browser)
        if self.cookies_file:
            try:
                tmp_cookies, kept = write_normalized_cookies(self.cookies_file, self.cookies_tmp_dir)
            except CookiesFileError as e:
                self.on_event(("log", str(e)))
                self.on_event(("finished", 0, n, False))
                return 0, n, False
            self.on_event(
                ("log", f"Cookies 文件：已整理为标准 Netscape 格式临时副本（{kept} 条），运行结束后自动删除。")
            )
            if browser:
                self.on_event(("log", "已选择 Cookies 文件，忽略「浏览器 Cookies」设置（两者不同时使用）。"))
        site_tmp: Dict[str, Optional[Path]] = {}  # site -> normalized temp copy (None = failed)
        try:
            for idx, url in enumerate(self.urls, start=1):
                if self.cancelled:
                    break
                cfile, cbrowser = self._cookies_for_url(url, tmp_cookies, browser, site_tmp)
                self._has_cookies = bool(cfile or cbrowser)
                cmd = build_command(
                    self.base,
                    url,
                    self.out_dir,
                    no_playlist=self.no_playlist,
                    cookies_browser=cbrowser,
                    ffmpeg_location=self.ffmpeg_location,
                    cookies_file=cfile,
                    deno_path=self.deno_path,
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
        cancelled = self.cancelled
        if cancelled:
            failed = n - ok  # include URLs never started
        self.on_event(("finished", ok, failed, cancelled))
        return ok, failed, cancelled

    def _cookies_for_url(self, url, override, browser, site_tmp):
        """Return ``(cookies_file, cookies_browser)`` for *url*; logs the site, never values."""
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
            self.on_event(("log", f"启动 yt-dlp 失败：{e}"))
            return -1
        with self._lock:
            self._proc = proc
        if self.cancelled:  # cancel raced with start
            kill_process_tree(proc)
        hints_shown = set()
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
                hint = output_hint(line, has_cookies=self._has_cookies)
                if hint and hint not in hints_shown:
                    hints_shown.add(hint)
                    self.on_event(("log", hint))
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
