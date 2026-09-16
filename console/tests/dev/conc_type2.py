#!/usr/bin/env python3
"""并发键入复测: 分别统计各批次命中数, 而不是只看总数。

conc_type.py 只看总数和"A 在前"的按序命中, 一旦两批顺序互换就会误判成丢字。
这里改成"每个字符出现次数"的直方图, 顺序和内容分开看。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, re, subprocess, sys, threading, time, collections

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
XEVLOG = f"{ART}/xev_conc2.log"


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV).stdout.strip()


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]


def post(path, obj, timeout=180):
    cc = http.client.HTTPConnection("127.0.0.1", 8390, timeout=timeout)
    cc.request("POST", path, json.dumps(obj),
               {"Content-Type": "application/json", "Cookie": CK})
    rr = cc.getresponse()
    return rr.status, rr.read().decode("utf-8", "replace")


def hist():
    h = collections.Counter()
    try:
        log = open(XEVLOG, errors="replace").read()
    except OSError:
        return h
    for b in re.split(r"(?=^KeyPress event|^KeyRelease event)", log, flags=re.M):
        if not b.startswith("KeyPress event"):
            continue
        m = re.search(r"keysym (0x[0-9a-f]+)", b)
        if m:
            h[int(m.group(1), 16)] += 1
    return h


saved = xdo("getwindowfocus")
subprocess.run(["pkill", "-f", "xev -name LUO_CONC2"], capture_output=True)
if os.path.exists(XEVLOG):
    os.remove(XEVLOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_CONC2"], env=ENV,
                       stdout=open(XEVLOG, "w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    p = xdo("search", "--name", "LUO_CONC2").split()
    if p:
        wid = p[0]
        break
    time.sleep(0.25)
# GNOME 有"防焦点窃取", 单次 windowfocus 常被驳回来, 要反复争取
for _ in range(15):
    xdo("windowmap", "--sync", wid)
    xdo("windowraise", wid)
    xdo("windowactivate", "--sync", wid)
    xdo("windowfocus", "--sync", wid)
    time.sleep(0.4)
    if xdo("getwindowfocus") == wid:
        break
if xdo("getwindowfocus") != wid:
    print("!! 靶子没拿到焦点, 中止"); xev.terminate(); sys.exit(1)
print(f"靶子就绪 (wid={wid})\n")

MARK = {"A": 0x01000000 | ord("甲"), "B": 0x01000000 | ord("乙"),
        "C": 0x01000000 | ord("丙"), "D": 0x01000000 | ord("丁")}
TEXT = {k: chr(v & 0xFFFFFF) for k, v in MARK.items()}

try:
    def run(label, batches, settle=2.5):
        open(XEVLOG, "w").close()
        want = collections.Counter()
        for n in batches:
            want[MARK[n]] += 60
        ths = []
        t0 = time.time()
        for n in batches:
            t = threading.Thread(target=lambda x=TEXT[n] * 60: post("/api/type", {"text": x}))
            t.start(); ths.append(t)
        for t in ths:
            t.join()
        dt = time.time() - t0
        time.sleep(settle)
        got = hist()
        parts, lost = [], 0
        for n in batches:
            k = MARK[n]
            g, w = got.get(k, 0), want[k]
            lost += max(0, w - g)
            parts.append(f"{n}: {g}/{w}")
        total = sum(got.get(MARK[n], 0) for n in batches)
        print(f"  {label}")
        print(f"    各批 {'  '.join(parts)}   合计 {total}/{sum(want.values())}"
              f"   耗时 {dt:.2f}s   {'★丢 %d 字' % lost if lost else '未丢字'}")
        return lost

    print("=== 1. 单批 60 字(基线) ===")
    run("单批", ["A"])
    print("\n=== 2. 两路并发(各 60 字) ===")
    run("两路", ["A", "B"])
    print("\n=== 3. 三路并发(各 60 字) ===")
    run("三路", ["A", "B", "C"])
    print("\n=== 4. 四路并发(各 60 字, 极端) ===")
    run("四路", ["A", "B", "C", "D"])
    print("\n=== 5. 长文本 2000 字单批 ===")
    open(XEVLOG, "w").close()
    t0 = time.time()
    st, body = post("/api/type", {"text": "甲" * 2000})
    print(f"    返回 {st} {body[:60]}  耗时 {time.time()-t0:.2f}s")
    time.sleep(2.0)
    print(f"    X 收到 甲 x{hist().get(MARK['A'], 0)} / 2000")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
    if saved:
        xdo("windowfocus", "--sync", saved)
    print("\n已关闭靶子并还原焦点")
