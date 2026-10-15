"""命令行获取 Cookies：python get_cookies.py youtube|x|<网址> [--refresh] [--browser chrome|edge]

不带 --refresh：用工具专用浏览器配置（browser_profile\\）打开网站窗口，登录后回到命令行按回车导出。
带 --refresh：无界面启动同一配置直接导出（需要之前已登录过）。
结果保存为 cookies\\<网站>.txt（Netscape 格式），下载视频时自动使用。不打印 Cookie 值。
"""
from __future__ import annotations

import argparse
import sys

import cookie_grabber as cg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用工具专用浏览器配置导出网站 Cookies（Netscape 格式）")
    ap.add_argument("site", help="youtube / x / 网址")
    ap.add_argument("--refresh", action="store_true", help="无界面直接刷新（已登录过时）")
    ap.add_argument("--browser", choices=("chrome", "edge"), default=None)
    ap.add_argument("--allow-anonymous", action="store_true", help="未登录也保存（默认不保存，避免覆盖）")
    a = ap.parse_args(argv)
    try:
        spec = cg.resolve_site(a.site)
        if a.refresh:
            res = cg.refresh_headless(spec, browser=a.browser, log=print, allow_anonymous=a.allow_anonymous)
        else:
            s = cg.BrowserSession(browser=a.browser, log=print)
            s.start(spec.login_url)
            input(f"请在打开的浏览器窗口中登录 {spec.label}（首次需要；如提示年龄验证请完成），完成后按回车导出… ")
            res = s.export(spec, allow_anonymous=a.allow_anonymous)
    except cg.CookieGrabError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(res.summary())
    print(f"文件：{res.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
