#!/usr/bin/env python3
"""长文本专项: 1) /api/type 到达率(数 X 侧 xev 收到的 keysym)  2) /api/paste 耗时。

"输入长度有限制" 和 "一粘贴就卡死" 这两条都要在这里定量。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, re, subprocess, sys, time

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
os.makedirs(ART, exist_ok=True)
XEVLOG = f"{ART}/xev_long.log"
B = "127.0.0.1"


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV).stdout.strip()


def login():
    c = http.client.HTTPConnection(B, 8390, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse(); r.read()
    return (r.getheader("Set-Cookie") or "").split(";")[0]


CK = login()


def post(path, obj, timeout=180):
    c = http.client.HTTPConnection(B, 8390, timeout=timeout)
    t0 = time.time()
    c.request("POST", path, json.dumps(obj),
              {"Content-Type": "application/json", "Cookie": CK})
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    return time.time() - t0, r.status, body


def presses():
    out = []
    try:
        log = open(XEVLOG, errors="replace").read()
    except OSError:
        return out
    for b in re.split(r"(?=^KeyPress event|^KeyRelease event)", log, flags=re.M):
        if not b.startswith("KeyPress event"):
            continue
        m = re.search(r"keysym (0x[0-9a-f]+)", b)
        if m:
            out.append(int(m.group(1), 16))
    return out


saved = xdo("getwindowfocus")
subprocess.run(["pkill", "-f", "xev -name LUO_LONG"], capture_output=True)
if os.path.exists(XEVLOG):
    os.remove(XEVLOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_LONG"], env=ENV,
                       stdout=open(XEVLOG, "w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    parts = xdo("search", "--name", "LUO_LONG").split()
    if parts:
        wid = parts[0]
        break
    time.sleep(0.25)
xdo("windowfocus", "--sync", wid); time.sleep(0.5)
if xdo("getwindowfocus") != wid:
    print("!! 靶子窗口没拿到焦点, 为免误伤你的窗口, 中止")
    xev.terminate()
    sys.exit(1)
print(f"靶子就绪 (wid={wid})\n")

try:
    # ---------- 1) /api/type 到达率 ----------
    print("=== /api/type 长文本到达率 ===")
    for n in (30, 100, 200, 400):
        text = ("中文长度测试" * ((n // 6) + 1))[:n]
        open(XEVLOG, "w").close()
        dt, st, body = post("/api/type", {"text": text})
        time.sleep(1.2)
        got = presses()
        want = [0x01000000 | ord(ch) for ch in text]
        it = iter(got)
        hit = sum(1 for w in want if w in it)
        print(f"  发 {n:4d} 字 -> X 收到 {len(got):4d} 个按键, 按序命中 {hit:4d}/{n}"
              f"   耗时 {dt:5.2f}s   {body[:60]}")

    # ---------- 2) /api/paste 耗时 ----------
    print("\n=== /api/paste 耗时(剪贴板+Ctrl+V, 关注是否卡死) ===")
    for n in (10, 100, 500, 2000, 8000):
        text = ("粘贴耗时测试内容ABC" * ((n // 9) + 1))[:n]
        dt, st, body = post("/api/paste", {"text": text})
        flag = "  <== 卡顿" if dt > 2 else ""
        print(f"  文本 {n:5d} 字 -> 耗时 {dt:6.2f}s  {body[:70]}{flag}")

    # ---------- 3) /api/clipboard 写入耗时 ----------
    print("\n=== /api/clipboard 写入耗时 ===")
    for n in (10, 500, 8000):
        text = ("剪贴板写入测试XY" * ((n // 8) + 1))[:n]
        dt, st, body = post("/api/clipboard", {"text": text})
        flag = "  <== 卡顿" if dt > 2 else ""
        print(f"  文本 {n:5d} 字 -> 耗时 {dt:6.2f}s  {body[:70]}{flag}")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
    if saved:
        xdo("windowfocus", "--sync", saved)
    print("\n已关闭靶子并还原焦点")
