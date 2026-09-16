# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""用注入脚本统计前端到底往 WebSocket 发了多少 RFB 消息, 并直接派发 PointerEvent
模拟单指/双指手势, 判断触控板逻辑本身对不对(不依赖 Playwright 的多点触摸能力)。"""
import http.client, os, subprocess, time, json
from playwright.sync_api import sync_playwright

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
ENV = {**os.environ, "DISPLAY": ":0"}
def rp():
    o = subprocess.run(["xdotool","getmouselocation"],capture_output=True,text=True,env=ENV).stdout
    d = dict(p.split(":") for p in o.split() if ":" in p); return int(d["x"]),int(d["y"])

c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST","/login",login_body(),
          {"Content-Type":"application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=",1)
orig = rp(); print("初始真实指针:", orig)

INIT = """
window.__msgs = [];
const _send = WebSocket.prototype.send;
WebSocket.prototype.send = function (data) {
  try {
    if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
      const u8 = data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      window.__msgs.push({ t: performance.now(), n: u8.length, op: u8[0] });
    }
  } catch (e) {}
  return _send.apply(this, arguments);
};
"""

with sync_playwright() as p:
    b = p.firefox.launch(headless=True)
    ctx = b.new_context(viewport={"width":412,"height":900}, device_scale_factor=2.5,
                        is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name":name,"value":value,"domain":"127.0.0.1","path":"/"}])
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("  [pageerror]", e))
    page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
    page.wait_for_timeout(5000)
    print("VNC 已连接:", page.evaluate("() => !!document.querySelector('#screen canvas').width"))

    # 事件计数 + 手势模拟(直接派发 PointerEvent)
    page.evaluate("""() => {
      const pad = document.getElementById('pad-area');
      window.__ev = {down:0, move:0, up:0};
      for (const t of ['pointerdown','pointermove','pointerup']) {
        pad.addEventListener(t, () => window.__ev[t.replace('pointer','')]++, true);
      }
      window.__pe = (type, id, x, y) => pad.dispatchEvent(new PointerEvent(type, {
        pointerId: id, clientX: x, clientY: y, bubbles: true, cancelable: true,
        pointerType: 'touch', isPrimary: id === 1, buttons: type === 'pointerup' ? 0 : 1,
      }));
    }""")

    box = page.locator("#pad-area").bounding_box()
    cx = box["x"] + box["width"]/2
    cy = box["y"] + box["height"]/2
    print(f"pad-area 中心: ({cx:.0f},{cy:.0f})  bbox={ {k:round(v) for k,v in box.items()} }")

    # ---- 单指下滑 ----
    page.evaluate("() => { window.__msgs = []; window.__ev={down:0,move:0,up:0}; }")
    page.evaluate("""([cx, cy]) => {
      window.__pe('pointerdown', 1, cx, cy);
      for (let i = 1; i <= 40; i++) window.__pe('pointermove', 1, cx, cy + i * 2);
      window.__pe('pointerup', 1, cx, cy + 80);
    }""", [cx, cy])
    time.sleep(0.8)
    ev = page.evaluate("() => window.__ev")
    msgs = page.evaluate("() => window.__msgs.map(m => m.op)")
    print(f"\n单指: 事件 down/move/up = {ev['down']}/{ev['move']}/{ev['up']}")
    print(f"      发出的 RFB 消息类型分布: "
          f"pointer(5)={msgs.count(5)}  wheel?(5)  其他={sorted(set(msgs))}")
    print(f"      真实指针 -> {rp()}")

    # ---- 双指滚动 ----
    page.evaluate("() => { window.__msgs = []; window.__ev={down:0,move:0,up:0}; }")
    page.evaluate("""([cx, cy]) => {
      window.__pe('pointerdown', 1, cx - 40, cy);
      window.__pe('pointerdown', 2, cx + 40, cy);
      for (let i = 1; i <= 30; i++) {
        window.__pe('pointermove', 1, cx - 40, cy - i * 6);
        window.__pe('pointermove', 2, cx + 40, cy - i * 6);
      }
      window.__pe('pointerup', 1, cx - 40, cy - 180);
      window.__pe('pointerup', 2, cx + 40, cy - 180);
    }""", [cx, cy])
    time.sleep(1.0)
    ev2 = page.evaluate("() => window.__ev")
    msgs2 = page.evaluate("() => window.__msgs")
    ops = [m["op"] for m in msgs2]
    # 看每 16ms 窗口内发了多少条(反映是否集中爆发)
    ts = sorted(m["t"] for m in msgs2)
    bursts = []
    if ts:
        start = ts[0]; cnt = 0
        for t in ts:
            if t - start > 16.7:
                bursts.append(cnt); start = t; cnt = 0
            cnt += 1
        bursts.append(cnt)
    print(f"\n双指: 事件 down/move/up = {ev2['down']}/{ev2['move']}/{ev2['up']}")
    print(f"      消息总数 {len(msgs2)}, 类型 {sorted(set(ops))}")
    print(f"      每 ~16ms 帧内消息数: 最大 {max(bursts) if bursts else 0}, 平均 {sum(bursts)/len(bursts) if bursts else 0:.1f}")
    print(f"      真实指针 -> {rp()}")

    b.close()
subprocess.run(["xdotool","mousemove",str(orig[0]),str(orig[1])],capture_output=True,env=ENV)
print("已还原指针")
