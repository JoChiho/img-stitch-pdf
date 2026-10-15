import json
import threading
from pathlib import Path

import pytest

import cookie_grabber as cg
import video_dl


def _c(domain, name, value="v", **kw):
    d = {"domain": domain, "name": name, "value": value, "path": "/", "expires": -1,
         "secure": False, "httpOnly": False, "session": True}
    d.update(kw)
    return d


def test_netscape_line_flags():
    line = cg.cdp_cookie_to_netscape(_c(".youtube.com", "SID", "abc", secure=True, httpOnly=True,
                                         session=False, expires=1999999999.7))
    assert line == "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tabc"
    line = cg.cdp_cookie_to_netscape(_c("www.youtube.com", "X"))
    assert line == "www.youtube.com\tFALSE\t/\tFALSE\t0\tX\tv"


def test_netscape_skips_bad_and_dedups():
    text, n = cg.cookies_to_netscape([_c(".x.com", "a"), _c(".x.com", "a"), _c(".x.com", "b", "x\ty"), _c("", "c")])
    assert n == 1
    assert text.startswith(video_dl.NETSCAPE_HEADER)
    norm, kept = video_dl.normalize_cookies_text(text)
    assert kept == 1


def test_netscape_roundtrip_mozilla(tmp_path):
    import http.cookiejar
    text, n = cg.cookies_to_netscape([_c(".youtube.com", "A", session=False, expires=1999999999, httpOnly=True),
                                      _c("accounts.google.com", "B", secure=True)])
    p = tmp_path / "c.txt"
    p.write_text(text, encoding="utf-8")
    jar = http.cookiejar.MozillaCookieJar(str(p))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert sorted(c.name for c in jar) == ["A", "B"]


def test_domain_filter():
    cs = [_c(".youtube.com", "a"), _c("accounts.google.com", "b"), _c(".notyoutube.com", "c"),
          _c(".twitter.com", "d"), _c("x.com", "e"), _c(".google.co.jp", "f")]
    yt = cg.filter_cookies(cs, cg.SITES["youtube"].domains)
    assert [c["name"] for c in yt] == ["a", "b"]
    x = cg.filter_cookies(cs, cg.SITES["x"].domains)
    assert [c["name"] for c in x] == ["d", "e"]


def test_resolve_site():
    assert cg.resolve_site("YouTube").key == "youtube.com"
    assert cg.resolve_site("https://twitter.com/foo").key == "x.com"
    s = cg.resolve_site("https://www.bilibili.com/video/1")
    assert s.key == "bilibili.com" and s.domains == ("bilibili.com",)
    with pytest.raises(cg.CookieGrabError):
        cg.resolve_site("")


def test_logged_in():
    assert cg.is_logged_in([_c(".x.com", "auth_token")], cg.SITES["x"]) is True
    assert cg.is_logged_in([_c(".x.com", "guest_id")], cg.SITES["x"]) is False
    assert cg.is_logged_in([], cg.resolve_site("bilibili.com")) is None


def test_find_browsers_mocked():
    env = {"PROGRAMFILES": r"C:\PF", "PROGRAMFILES(X86)": r"C:\PF86", "LOCALAPPDATA": r"C:\L"}
    present = {str(Path(r"C:\PF86") / r"Microsoft\Edge\Application\msedge.exe"),
               str(Path(r"C:\L") / r"Google\Chrome\Application\chrome.exe")}
    found = cg.find_browsers(env, exists=lambda p: str(p) in present)
    assert [n for n, _ in found] == ["chrome", "edge"]
    assert str(found[0][1]).endswith("chrome.exe")
    assert cg.find_browsers(env, exists=lambda p: False) == []
    with pytest.raises(cg.CookieGrabError):
        cg.pick_browser(env=env, exists=lambda p: False)
    assert cg.pick_browser("edge", env=env, exists=lambda p: str(p) in present)[0] == "edge"


def test_launch_args(tmp_path):
    a = cg.build_launch_args(Path("chrome.exe"), tmp_path, "https://x.com/login", headless=True)
    assert f"--user-data-dir={tmp_path}" in a and "--headless=new" in a and a[-1] == "https://x.com/login"
    assert "--remote-debugging-port=0" in a


def test_devtools_port_file(tmp_path):
    assert cg.read_devtools_port_file(tmp_path) is None
    (tmp_path / cg.DEVTOOLS_PORT_FILE).write_text("9333\n/devtools/browser/abc\n")
    assert cg.read_devtools_port_file(tmp_path) == "ws://127.0.0.1:9333/devtools/browser/abc"


def test_save_site_cookies(tmp_path):
    res = cg.save_site_cookies(cg.SITES["x"], [_c(".x.com", "auth_token"), _c(".youtube.com", "z")], tmp_path)
    assert res.path == tmp_path / "x.com.txt" and res.count == 1 and res.logged_in is True
    assert [s.site for s in video_dl.list_stored_cookies(tmp_path)] == ["x.com"]
    with pytest.raises(cg.CookieGrabError):
        cg.save_site_cookies(cg.SITES["youtube"], [_c(".x.com", "a")], tmp_path)
    # not logged in: refuse to overwrite the good file
    before = (tmp_path / "x.com.txt").read_bytes()
    with pytest.raises(cg.CookieGrabError):
        cg.save_site_cookies(cg.SITES["x"], [_c(".x.com", "guest_id")], tmp_path)
    assert (tmp_path / "x.com.txt").read_bytes() == before
    r = cg.save_site_cookies(cg.SITES["x"], [_c(".x.com", "guest_id")], tmp_path, allow_anonymous=True)
    assert r.logged_in is False


def test_websocket_cdp_against_fake_server():
    """Minimal RFC6455 server: checks our client handshake/masking/large frames."""
    import base64, hashlib, socket, struct
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    big = [_c(".youtube.com", f"n{i}", "v" * 50) for i in range(2000)]

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        key = [l.split(b": ")[1] for l in buf.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        b1, b2 = conn.recv(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", conn.recv(2))[0]
        mask = conn.recv(4)
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        req = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        assert req["method"] == "Storage.getCookies"
        out = json.dumps({"id": req["id"], "result": {"cookies": big}}).encode()
        conn.sendall(b"\x81\x7f" + struct.pack(">Q", len(out)) + out)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    c = cg.CDPConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
    got = c.call("Storage.getCookies")["cookies"]
    c.close()
    t.join(5)
    srv.close()
    assert len(got) == 2000
