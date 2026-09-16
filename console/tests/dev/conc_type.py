#!/usr/bin/env python3
"""验证并发 /api/type 会不会丢字 —— "输入长度有限制"的可疑根因。

前端是 80ms 防抖攒批, 如果用户打字没停顿, 上一批请求还没返回就又发一批,
两个 xdotool type 进程会交错执行, 各自临时重映射 keycode, 互相踩。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, re, subprocess, sys, threading, time

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
XEVLOG = f"{ART}/xev_conc.log"


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV).stdout.strip()


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]


def post(path, obj, timeout=120):
    cc = http.client.HTTPConnection("127.0.0.1", 8390, timeout=timeout)
    cc.request("POST", path, json.dumps(obj),
               {"Content-Type": "application/json", "Cookie": CK})
    rr = cc.getresponse()
    return rr.status, rr.read().decode("utf-8", "replace")


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
subprocess.run(["pkill", "-f", "xev -name LUO_CONC"], capture_output=True)
if os.path.exists(XEVLOG):
    os.remove(XEVLOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_CONC"], env=ENV,
                       stdout=open(XEVLOG, "w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    p = xdo("search", "--name", "LUO_CONC").split()
    if p:
        wid = p[0]
        break
    time.sleep(0.25)
xdo("windowfocus", "--sync", wid); time.sleep(0.5)
if xdo("getwindowfocus") != wid:
    print("!! 靶子没拿到焦点, 中止"); xev.terminate(); sys.exit(1)
print(f"靶子就绪 (wid={wid})\n")

try:
    A = "甲" * 60     # 0x01000000|0x7532
    B = "乙" * 60     # 0x01000000|0x4e59
    want = [0x01000000 | ord(ch) for ch in (A + B)]

    print("=== 顺序发送(对照组) ===")
    open(XEVLOG, "w").close()
    t0 = time.time()
    post("/api/type", {"text": A}); post("/api/type", {"text": B})
    dt = time.time() - t0
    time.sleep(1.0)
    got = presses()
    it = iter(got); hit = sum(1 for w in want if w in it)
    print(f"  发 120 字 -> X 收到 {len(got)} 个按键, 按序命中 {hit}/120, 耗时 {dt:.2f}s")

    print("\n=== 并发发送(模拟打字不停顿) ===")
    open(XEVLOG, "w").close()
    res = {}
    ths = []
    for name, txt in (("A", A), ("B", B)):
        t = threading.Thread(target=lambda n=name, x=txt: res.__setitem__(n, post("/api/type", {"text": x})))
        t.start(); ths.append(t)
    t0 = time.time()
    for t in ths:
        t.join()
    dt = time.time() - t0
    time.sleep(1.0)
    got = presses()
    it = iter(got); hit = sum(1 for w in want if w in it)
    print(f"  并发发 120 字 -> X 收到 {len(got)} 个按键, 按序命中 {hit}/120, 耗时 {dt:.2f}s")
    if len(got) < 120 or hit < 120:
        print("  ★ 并发确实丢字/乱序 —— 这就是'输入长度有限制'")
    else:
        print("  并发未丢字")

    print("\n=== 三路并发 ===")
    open(XEVLOG, "w").close()
    ths = []
    for txt in (A[:40], B[:40], A[40:80]):
        t = threading.Thread(target=lambda x=txt: post("/api/type", {"text": x}))
        t.start(); ths.append(t)
    for t in ths:
        t.join()
    time.sleep(1.0)
    got = presses()
    print(f"  三路各 40 字(共 120) -> X 收到 {len(got)} 个按键 "
          f"{'★丢字' if len(got) < 120 else '未丢'}")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
    if saved:
        xdo("windowfocus", "--sync", saved)
    print("\n已关闭靶子并还原焦点")
