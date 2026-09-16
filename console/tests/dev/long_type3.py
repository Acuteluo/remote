#!/usr/bin/env python3
"""长文本丢的是**第几个**字?

各档位都只丢 1 个, 且和 delay 无关 —— 不像速度问题, 更像固定位置掉一个。
这里改用互不相同的字符, 收到后重建序列, 直接指出缺口下标。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, re, subprocess, sys, time

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
LOG = f"{ART}/xev_long3.log"


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True,
                          env=ENV).stdout.strip()


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]

subprocess.run(["pkill", "-f", "xev -name LUO_L3"], capture_output=True)
if os.path.exists(LOG):
    os.remove(LOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_L3"], env=ENV,
                       stdout=open(LOG, "w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    p = xdo("search", "--name", "LUO_L3").split()
    if p:
        wid = p[0]
        break
    time.sleep(0.25)


def grab():
    xdo("windowmap", "--sync", wid); xdo("windowraise", wid)
    xdo("windowactivate", "--sync", wid); xdo("windowfocus", "--sync", wid)


for attempt in range(3):
    for _ in range(5):
        grab(); time.sleep(0.5)
        if xdo("getwindowfocus") == wid:
            break
    if xdo("getwindowfocus") == wid:
        break
    others = [w for w in xdo("search", "--onlyvisible", "--maxdepth", "2", ".*").split()
              if w != wid]
    if others:
        xdo("windowactivate", "--sync", others[0]); time.sleep(0.8)
    grab(); time.sleep(0.8)

if xdo("getwindowfocus") != wid:
    print("!! 靶子没拿到焦点, 中止"); xev.terminate(); sys.exit(1)
print("靶子就绪\n")

# 用互不相同的 CJK 字符, 每个下标一个字
POOL = [chr(0x4E00 + i) for i in range(600)]          # 一, 丁, 丂...
KS = {0x01000000 | ord(ch): i for i, ch in enumerate(POOL)}

try:
    for n in (200, 500, 2000):
        text = "".join(POOL[i % len(POOL)] for i in range(n))
        open(LOG, "w").close()
        cc = http.client.HTTPConnection("127.0.0.1", 8390, timeout=180)
        t0 = time.time()
        cc.request("POST", "/api/type", json.dumps({"text": text}),
                   {"Content-Type": "application/json", "Cookie": CK})
        cc.getresponse().read()
        dt = time.time() - t0
        time.sleep(2.5)
        seq = []
        for b in re.split(r"(?=^KeyPress event)",
                          open(LOG, errors="replace").read(), flags=re.M):
            if not b.startswith("KeyPress event"):
                continue
            m = re.search(r"keysym (0x[0-9a-f]+)", b)
            if m and int(m.group(1), 16) in KS:
                seq.append(KS[int(m.group(1), 16)] % len(POOL))
        # 找缺口: 期望是 0,1,2,...,n-1 (mod 600)
        want = [i % len(POOL) for i in range(n)]
        missing, it = [], iter(seq)
        for i, w in enumerate(want):
            for got in it:
                if got == w:
                    break
                missing.append(("多出/错位", i, got))
            else:
                missing.append(("缺", i, w))
        print(f"  {n:>5d} 字 -> 收到 {len(seq)}/{n}  耗时 {dt:.2f}s")
        if missing:
            for kind, i, v in missing[:6]:
                print(f"        {kind}: 第 {i} 个 (值 {v})")
            if len(missing) > 6:
                print(f"        ... 共 {len(missing)} 处")
        else:
            print("        完整无缺")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
