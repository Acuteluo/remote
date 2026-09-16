#!/usr/bin/env python3
"""诊断: 记录客户端实际发出的 RFB 指针消息坐标, 与真实指针对照。"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, os, subprocess, time
from playwright.sync_api import sync_playwright

ENV = {**os.environ, "DISPLAY": ":0"}


def rp():
    o = subprocess.run(["xdotool", "getmouselocation"], capture_output=True,
                       text=True, env=ENV).stdout
    d = dict(p.split(":") for p in o.split() if ":" in p)
    return int(d["x"]), int(d["y"])


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=", 1)

INIT = """
window.__ptrs = [];
const _send = WebSocket.prototype.send;
WebSocket.prototype.send = function (data) {
  try {
    if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
      const u8 = data instanceof ArrayBuffer ? new Uint8Array(data)
                : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      if (u8[0] === 5 && u8.length >= 6) {
        window.__ptrs.push([(u8[2] << 8) | u8[3], (u8[4] << 8) | u8[5]]);
        if (window.__ptrs.length > 400) window.__ptrs.shift();
      }
    }
  } catch (e) {}
  return _send.apply(this, arguments);
};
"""

with sync_playwright() as p:
    b = p.firefox.launch(headless=True)
    ctx = b.new_context(viewport={"width": 412, "height": 900},
                        device_scale_factor=2.5, is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}])
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("  [pageerror]", e))
    page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
    page.wait_for_timeout(5000)

    canvas = page.evaluate("() => { const c=document.querySelector('#screen canvas');"
                           " const r=c.getBoundingClientRect();"
                           " return {top:r.top, bottom:r.bottom, left:r.left, right:r.right,"
                           " w:Math.round(r.width), h:Math.round(r.height),"
                           " fbW:c.width, fbH:c.height}; }")
    print("canvas:", {k: (round(v, 1) if isinstance(v, float) else v) for k, v in canvas.items()})
    box = page.locator("#pad-area").bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    print(f"pad 中心 ({cx:.0f},{cy:.0f})  bbox={ {k: round(v) for k, v in box.items()} }")

    page.evaluate("""() => {
      window.__pe = (t, i, x, y) => document.getElementById('pad-area')
        .dispatchEvent(new PointerEvent(t, { pointerId: i, clientX: x, clientY: y,
          bubbles: true, cancelable: true, pointerType: 'touch',
          isPrimary: i === 1, buttons: t === 'pointerup' ? 0 : 1 }));
    }""")

    def drag(total_dy, steps, pause_ms=14):
        page.evaluate("() => { window.__ptrs = []; }")
        page.evaluate("([cx, cy]) => window.__pe('pointerdown', 1, cx, cy)", [cx, cy])
        for i in range(1, steps + 1):
            page.evaluate("([cx, cy, dy]) => window.__pe('pointermove', 1, cx, cy + dy)",
                          [cx, cy, total_dy * i / steps])
            page.wait_for_timeout(pause_ms)
        page.evaluate("([cx, cy]) => window.__pe('pointerup', 1, cx, cy)", [cx, cy + total_dy])
        time.sleep(0.9)
        return page.evaluate("() => window.__ptrs.slice(-8)")

    print("\n--- 分步拖到底边(每步 14ms, 共 40 步 x 12px = 480px) ---")
    last = drag(480, 40)
    print("  最后 8 条指针消息:", last, "  (期望 y=960)")
    print("  真实指针:", rp())

    print("\n--- 继续再拖 60px ---")
    last = drag(60, 10)
    print("  最后 8 条:", last, "  真实指针:", rp())

    print("\n--- 轻微回弹 -10px ---")
    last = drag(-10, 5)
    print("  最后 8 条:", last, "  真实指针:", rp())

    print("\n--- 明确退开 -120px ---")
    last = drag(-120, 15)
    print("  最后 8 条:", last, "  真实指针:", rp())

    b.close()
