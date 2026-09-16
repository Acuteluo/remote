# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""端到端验证贴边锁 + Dock 弹出(带节奏的拖拽, 模拟真实手指)。

1) 拖到底边      -> 真实指针 1919, Dock 弹出
2) 回弹 10px     -> 仍在 1919(Dock 不被抖掉)
3) 明确退开      -> 指针离开, Dock 收起
"""
import http.client, os, subprocess, time
from playwright.sync_api import sync_playwright

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
ENV = {**os.environ, "DISPLAY": ":0"}


def rp():
    o = subprocess.run(["xdotool", "getmouselocation"], capture_output=True,
                       text=True, env=ENV).stdout
    d = dict(p.split(":") for p in o.split() if ":" in p)
    return int(d["x"]), int(d["y"])


def shot(tag):
    p = f"{ART}/lt_{tag}.png"
    subprocess.run(["import", "-window", "root", "-crop", "2880x240+0+1680",
                    "+repage", p], env=ENV, capture_output=True)
    return p


def diff(a, b):
    r = subprocess.run(["convert", a, b, "-compose", "difference", "-composite",
                        "-format", "%[fx:mean]", "info:"], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return -1.0


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=", 1)

with sync_playwright() as p:
    b = p.firefox.launch(headless=True)
    ctx = b.new_context(viewport={"width": 412, "height": 900},
                        device_scale_factor=2.5, is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}])
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("  [pageerror]", e))
    page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
    page.wait_for_timeout(5000)

    box = page.locator("#pad-area").bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.evaluate("""() => {
      window.__pe = (t, i, x, y) => document.getElementById('pad-area')
        .dispatchEvent(new PointerEvent(t, { pointerId: i, clientX: x, clientY: y,
          bubbles: true, cancelable: true, pointerType: 'touch',
          isPrimary: i === 1, buttons: t === 'pointerup' ? 0 : 1 }));
    }""")

    def drag(total_dy, steps, pause=14):
        page.evaluate("([cx, cy]) => window.__pe('pointerdown', 1, cx, cy)", [cx, cy])
        for i in range(1, steps + 1):
            page.evaluate("([cx, cy, dy]) => window.__pe('pointermove', 1, cx, cy + dy)",
                          [cx, cy, total_dy * i / steps])
            page.wait_for_timeout(pause)
        page.evaluate("([cx, cy]) => window.__pe('pointerup', 1, cx, cy)", [cx, cy + total_dy])

    # 先退到上方, 作为"未弹出"基线
    drag(-500, 40)
    time.sleep(2.2)
    base = shot("base")
    print("基线(指针在上方):", rp(), f"  参考图 {os.path.basename(base)}")

    drag(560, 45)                       # 拖到底边
    time.sleep(1.8)
    p1, s1 = rp(), shot("on")
    print(f"\n1) 拖到底边:   指针 {p1}   y=1919? {'✓' if p1[1] == 1919 else '✗'}")
    print(f"   底部画面差 {diff(base, s1):.4f}   (>0.1 = Dock 弹出)")

    drag(-10, 5)                        # 轻微回弹(滞回内)
    time.sleep(1.2)
    p2, s2 = rp(), shot("jitter")
    print(f"\n2) 回弹 10px:  指针 {p2}   仍 y=1919? {'✓' if p2[1] == 1919 else '✗'}")
    print(f"   底部画面差 {diff(base, s2):.4f}   (应仍 >0.1, 没被抖掉)")

    drag(-140, 16)                      # 明确退开
    time.sleep(2.0)
    p3, s3 = rp(), shot("off")
    print(f"\n3) 退开 140px: 指针 {p3}   已离开? {'✓' if p3[1] < 1800 else '✗'}")
    print(f"   底部画面差 {diff(base, s3):.4f}   (应回到 ~0, Dock 收起)")

    b.close()

print("\n生成对照图 …")
for tag, lab, col in [("base", "1) finger away from bottom - no dock", "#ff5555"),
                      ("on", "2) touchpad dragged to bottom edge -> DOCK APPEARS", "#55ff55"),
                      ("jitter", "3) finger jittered 10px back - dock STAYS (edge latch)", "#55ff55"),
                      ("off", "4) finger clearly retreated - dock hides", "#ffaa55")]:
    subprocess.run(["convert", f"{ART}/lt_{tag}.png", "-resize", "1000x",
                    "-bordercolor", col, "-border", "2", "-background", "#111",
                    "-fill", "#ddd", "-pointsize", "22", "label:" + lab,
                    "-gravity", "center", "-append", f"{ART}/lab_{tag}.png"], env=ENV)
subprocess.run(["convert", f"{ART}/lab_base.png", f"{ART}/lab_on.png",
                f"{ART}/lab_jitter.png", f"{ART}/lab_off.png", "-append",
                f"{ART}/dock_latch_result.png"], env=ENV)
subprocess.run(["identify", f"{ART}/dock_latch_result.png"], env=ENV)
