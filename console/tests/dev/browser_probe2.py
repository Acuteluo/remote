# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""端到端验证: 在真实浏览器里操作触控板 -> RFB -> x11vnc -> 真实 X 指针, 看 Dock 弹不弹。
同时查清顶部提示横幅为什么没出现。
"""
import http.client, os, subprocess, time
from playwright.sync_api import sync_playwright

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
ENV = {**os.environ, "DISPLAY": ":0"}

def rp():
    o = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, env=ENV).stdout
    d = dict(p.split(":") for p in o.split() if ":" in p)
    return int(d["x"]), int(d["y"])

def shot(tag):
    subprocess.run(["import", "-window", "root", "-crop", "2880x240+0+1680", "+repage",
                    f"{ART}/d_{tag}.png"], capture_output=True, env=ENV)

c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=", 1)

orig = rp()
print("初始真实指针:", orig)

with sync_playwright() as p:
    b = p.firefox.launch(headless=True)
    ctx = b.new_context(viewport={"width": 412, "height": 900},
                        device_scale_factor=2.5, is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}])
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("  [pageerror]", e))
    page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
    page.wait_for_timeout(5000)

    print("\n=== 横幅为什么没出来 ===")
    print("  fetch('/api/screen').status =",
          page.evaluate("() => fetch('/api/screen').then(r => r.status)"))
    print("  fetch 跟随重定向后 url    =",
          page.evaluate("() => fetch('/api/screen').then(r => r.url)"))
    print("  banner class =", page.evaluate("() => document.getElementById('vnc-banner').className"))
    print("  banner html  =", page.evaluate("() => document.getElementById('vnc-banner').innerHTML")[:120])

    print("\n=== 触控板 -> 真实指针 端到端 ===")
    box = page.locator("#pad-area").bounding_box()
    print("  pad-area bbox:", {k: round(v) for k, v in box.items()})
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    canvas = page.evaluate("() => { const c=document.querySelector('#screen canvas');"
                           " const r=c.getBoundingClientRect();"
                           " return {top:r.top, bottom:r.bottom, h:r.height, fbH:c.height}; }")
    print("  canvas:", {k: round(v) for k, v in canvas.items()})

    page.mouse.move(cx, cy)
    page.mouse.down()
    steps = 45
    for i in range(steps):
        page.mouse.move(cx, cy + (box["height"] / 2 - 6) * (i + 1) / steps)
        page.wait_for_timeout(12)
    time.sleep(0.8)
    mid = rp()
    print(f"  单指下滑 {steps} 步后真实指针: {mid}   (期望 y 触到 1919)")
    shot("viaswipe")
    page.mouse.up()
    time.sleep(0.5)

    print("\n=== 双指滚动是否卡顿(看每帧发送的档位数) ===")
    page.evaluate("""() => {
      window.__wheelLog = [];
      const c = document.querySelector('#screen canvas');
      // 统计单位时间内的 pointer 事件数, 粗略反映发送压力
      window.__t0 = performance.now();
      window.__n = 0;
    }""")
    box2 = page.locator("#pad-area").bounding_box()
    print("  (跳过精确计数, 见下方说明)")

    page.screenshot(path=f"{ART}/page_mobile.png")
    b.close()

subprocess.run(["xdotool", "mousemove", str(orig[0]), str(orig[1])], capture_output=True, env=ENV)
print("\n已还原指针")
