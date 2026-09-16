# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""决定性验证: 真实浏览器打字 -> 桥接 -> x11vnc -> X, 用 xev 数到底到了多少字符。
覆盖: 长串输入是否被截断(用户说的"输入长度有限制")、/api/paste、/api/type。
"""
import http.client, json, os, re, subprocess, time
from playwright.sync_api import sync_playwright

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
ENV = {**os.environ, "DISPLAY": ":0"}
XEVLOG = f"{ART}/xev_e2e.log"

def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV).stdout.strip()

def presses(log):
    blocks = re.split(r'(?=^KeyPress event|^KeyRelease event)', log, flags=re.M)
    out = []
    for b in blocks:
        if not b.startswith("KeyPress event"): continue
        m = re.search(r"keysym (0x[0-9a-f]+)", b)
        if m: out.append(int(m.group(1), 16))
    return out

c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST","/login",login_body(),
          {"Content-Type":"application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
name, value = cookie.split("=",1)
saved_win = xdo("getwindowfocus")

# 1) X 侧靶子
subprocess.run(["pkill","-f","xev -name LUO_E2E"], capture_output=True)
if os.path.exists(XEVLOG): os.remove(XEVLOG)
xev = subprocess.Popen(["stdbuf","-o0","xev","-name","LUO_E2E"], env=ENV,
                       stdout=open(XEVLOG,"w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    parts = xdo("search","--name","LUO_E2E").split()
    if parts: wid = parts[0]; break
    time.sleep(0.25)
xdo("windowfocus","--sync",wid); time.sleep(0.5)
focused = (xdo("getwindowfocus") == wid)
print("X 侧靶子 xev 拿到焦点:", focused, f"(wid={wid})")
if not focused:
    print("拿不到焦点, 为免误伤你的窗口, 中止"); xev.terminate(); raise SystemExit(1)

def xev_reset():
    open(XEVLOG,"w").close()

try:
    with sync_playwright() as p:
        b = p.firefox.launch(headless=True)
        ctx = b.new_context(viewport={"width":412,"height":900}, device_scale_factor=2.5,
                            is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name":name,"value":value,"domain":"127.0.0.1","path":"/"}])
        page = ctx.new_page()
        page.on("pageerror", lambda e: print("  [pageerror]", e))
        page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
        page.wait_for_timeout(5000)

        # ---- 长 ASCII 输入 ----
        ASCII = "abcdefghijklmnopqrstuvwxyz0123456789" * 4     # 144 字符
        xev_reset()
        page.evaluate("() => document.getElementById('vnc-kbbtn').click()")
        page.wait_for_timeout(300)
        page.evaluate("() => document.getElementById('vnc-kb').focus()")
        page.wait_for_timeout(200)
        page.keyboard.type(ASCII, delay=2)
        time.sleep(2.0)
        got = presses(open(XEVLOG, errors="replace").read())
        want = [ord(ch) for ch in ASCII]
        it = iter(got); hit = sum(1 for w in want if w in it)
        print(f"\n长 ASCII 输入: 期望 {len(want)} 字符, X 收到 {len(got)} 个按键, 按序命中 {hit}")

        # ---- 长中文输入 ----
        CN = "中文输入长度测试" * 8                              # 64 字符
        xev_reset()
        page.keyboard.type(CN, delay=2)
        time.sleep(2.0)
        got2 = presses(open(XEVLOG, errors="replace").read())
        want2 = [0x01000000 | ord(ch) for ch in CN]
        it2 = iter(got2); hit2 = sum(1 for w in want2 if w in it2)
        print(f"长中文输入:   期望 {len(want2)} 字符, X 收到 {len(got2)} 个按键, 按序命中 {hit2}")

        # ---- /api/paste ----
        xev_reset()
        st, body, _ = (lambda cc: (cc[0], cc[1], None))(
            (lambda: (lambda conn: (conn.request("POST","/api/paste",
                json.dumps({"text":"粘贴测试XYZ"}),
                {"Content-Type":"application/json","Cookie":cookie}),
                (lambda rr: (rr.status, rr.read()))(conn.getresponse())))(http.client.HTTPConnection("127.0.0.1",8390,timeout=25)))())
        print(f"\n/api/paste -> {st} {body[:80]}")
        time.sleep(1.2)
        gp = presses(open(XEVLOG, errors="replace").read())
        print(f"   X 侧收到: Control_L={'是' if 0xffe3 in gp else '否'}, v={'是' if 0x76 in gp else '否'}")

        # ---- /api/type ----
        xev_reset()
        conn = http.client.HTTPConnection("127.0.0.1", 8390, timeout=25)
        conn.request("POST","/api/type", json.dumps({"text":"直接键入中文ABC"}),
                     {"Content-Type":"application/json","Cookie":cookie})
        rr = conn.getresponse(); print(f"\n/api/type -> {rr.status} {rr.read()[:80]}")
        time.sleep(1.5)
        gt = presses(open(XEVLOG, errors="replace").read())
        exp = [0x01000000 | ord(ch) for ch in "直接键入中文"] + [ord(ch) for ch in "ABC"]
        it3 = iter(gt); h3 = sum(1 for w in exp if w in it3)
        print(f"   X 侧收到 {len(gt)} 个按键, 期望 {len(exp)} 个字符, 按序命中 {h3}")

        b.close()
finally:
    xev.terminate()
    try: xev.wait(timeout=4)
    except Exception: xev.kill()
    if saved_win: xdo("windowfocus","--sync",saved_win)
    print("\n已关闭靶子并还原焦点")
