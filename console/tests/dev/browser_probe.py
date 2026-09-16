# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""用真实浏览器(Firefox/Playwright)加载远程桌面页, 抓运行时错误 + 验证交互。

为什么需要: 前端逻辑只有真跑起来才知道对不对 —— 一个顶层异常就会让整页失效,
而接口/静态文件检查都看不出来。
"""
import http.client, sys, os
from playwright.sync_api import sync_playwright

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
os.makedirs(ART, exist_ok=True)

c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=", 1)
print("cookie:", cookie[:28], "...")

with sync_playwright() as p:
    b = p.firefox.launch(headless=True)
    ctx = b.new_context(viewport={"width": 412, "height": 900},
                        device_scale_factor=2.5, is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}])
    page = ctx.new_page()
    errs, logs = [], []
    page.on("pageerror", lambda e: errs.append(("pageerror", str(e))))
    page.on("console", lambda m: logs.append((m.type, m.text)))

    page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
    page.wait_for_timeout(5000)

    print("\n=== 未捕获异常 ===")
    print("  无" if not errs else "")
    for t, m in errs: print(f"  [{t}] {m[:300]}")
    print("=== console 错误/警告 ===")
    bad = [(t, m) for t, m in logs if t in ("error", "warning")]
    print("  无" if not bad else "")
    for t, m in bad[:10]: print(f"  [{t}] {m[:250]}")

    print("\n=== 页面状态 ===")
    for sel in ["#vnc-banner", "#keybar", "#vnc-keybtn", "#pad-area", "#vnc-kb",
                "#screen canvas"]:
        print(f"  {sel:20s} count={page.locator(sel).count()}")
    print("  banner 可见 :", page.locator("#vnc-banner").is_visible())
    print("  banner 文本 :", page.locator("#vnc-banner").inner_text()[:150])
    print("  keybar 可见 :", page.locator("#keybar").is_visible(), "(期望 False, 默认收起)")
    print("  帧缓冲尺寸  :", page.evaluate(
        "() => { const c=document.querySelector('#screen canvas');"
        " return c ? c.width+'x'+c.height : 'no canvas'; }"))
    print("  画布 CSS 尺寸:", page.evaluate(
        "() => { const c=document.querySelector('#screen canvas');"
        " if(!c) return 'none'; const r=c.getBoundingClientRect();"
        " return Math.round(r.width)+'x'+Math.round(r.height)+' @'+Math.round(r.top); }"))

    # 模拟: 打开软键盘 -> 点快捷键条上的键 -> 看焦点还在不在输入框上
    print("\n=== 焦点保护测试 ===")
    page.locator("#vnc-keybtn").click()          # 展开快捷键条
    page.wait_for_timeout(300)
    print("  展开后 keybar 可见:", page.locator("#keybar").is_visible())
    page.evaluate("() => document.getElementById('vnc-kbbtn').click()")
    page.wait_for_timeout(300)
    print("  点键盘按钮后 activeElement:", page.evaluate("() => document.activeElement.id"))
    page.evaluate("() => document.getElementById('vnc-kbbtn').click()")
    page.wait_for_timeout(300)
    print("  再点一次后 activeElement:", page.evaluate("() => document.activeElement.id"),
          "(期望空 => 关掉了)")

    page.screenshot(path=f"{ART}/page_mobile.png")
    print(f"\n截图: {ART}/page_mobile.png")
    b.close()
