"""命令行获取 Cookies（始终使用 Microsoft Edge，专用配置 browser_profile_edge\\）。

  python get_cookies.py login  youtube|x|<网址>   # 第 1 步：打开普通 Edge 窗口，自己登录后关闭窗口
  python get_cookies.py export youtube|x|<网址>   # 第 2 步：无界面读取该配置的 Cookies 并保存

结果保存为 cookies\\<网站>.txt（Netscape 格式），下载视频时自动使用。不打印 Cookie 值。
"""
from __future__ import annotations

import argparse
import sys

import cookie_grabber as cg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用工具专用 Edge 配置登录并导出网站 Cookies（Netscape 格式）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="打开普通 Edge 窗口（无调试端口），自己登录后关闭窗口")
    p_login.add_argument("site", help="youtube / x / 网址")
    p_exp = sub.add_parser("export", help="登录窗口关闭后，无界面读取 Cookies 并保存")
    p_exp.add_argument("site", help="youtube / x / 网址")
    p_exp.add_argument("--allow-anonymous", action="store_true", help="未登录也保存（默认不保存，避免覆盖）")
    for p in (p_login, p_exp):
        p.add_argument("--browser", choices=("edge",), default="edge", help="仅支持 edge")
    a = ap.parse_args(argv)
    try:
        spec = cg.resolve_site(a.site)
        if a.cmd == "login":
            cg.launch_login(spec, browser=a.browser, log=print)
            print(f"完成后运行：python get_cookies.py export {a.site}")
            return 0
        res = cg.export_cookies(spec, browser=a.browser, log=print, allow_anonymous=a.allow_anonymous)
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
