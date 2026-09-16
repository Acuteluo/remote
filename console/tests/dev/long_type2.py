#!/usr/bin/env python3
"""长文本键入完整性复测(delay 阈值调过之后)。

单独成文件而不用 heredoc: 脚本里要 pkill -f 'xev -name XXX',
若整段代码出现在 bash 的 argv 里, pkill 会把外层 bash 自己一起杀掉。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, re, subprocess, sys, time, collections

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
LOG = f"{ART}/xev_long2.log"


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV).stdout.strip()


c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]

subprocess.run(["pkill", "-f", "xev -name LUO_L2"], capture_output=True)
if os.path.exists(LOG):
    os.remove(LOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_L2"], env=ENV,
                       stdout=open(LOG, "w"), stderr=subprocess.DEVNULL)
wid = ""
for _ in range(40):
    p = xdo("search", "--name", "LUO_L2").split()
    if p:
        wid = p[0]
        break
    time.sleep(0.25)
def grab():
    xdo("windowmap", "--sync", wid); xdo("windowraise", wid)
    xdo("windowactivate", "--sync", wid); xdo("windowfocus", "--sync", wid)


for attempt in range(3):                 # GNOME 防焦点窃取, 要反复争取
    for _ in range(5):
        grab()
        time.sleep(0.5)
        if xdo("getwindowfocus") == wid:
            break
    if xdo("getwindowfocus") == wid:
        break
    # 上一轮 xev 退出后 _NET_ACTIVE_WINDOW 常留着一个死窗口, Mutter 就一直
    # 把焦点弹回它。先随便激活一个活着的窗口把状态洗掉, 再切回靶子。
    others = [w for w in xdo("search", "--onlyvisible", "--maxdepth", "2", ".*").split()
              if w != wid]
    if others:
        xdo("windowactivate", "--sync", others[0])
        time.sleep(0.8)
    grab()
    time.sleep(0.8)
if xdo("getwindowfocus") != wid:
    print("!! 靶子没拿到焦点, 中止"); xev.terminate(); sys.exit(1)
print("靶子就绪\n")

K = 0x01000000 | ord("甲")
try:
    for n in (200, 500, 2000):
        open(LOG, "w").close()
        cc = http.client.HTTPConnection("127.0.0.1", 8390, timeout=180)
        t0 = time.time()
        cc.request("POST", "/api/type", json.dumps({"text": "甲" * n}),
                   {"Content-Type": "application/json", "Cookie": CK})
        rr = cc.getresponse(); body = rr.read().decode("utf-8", "replace")
        dt = time.time() - t0
        time.sleep(2.5)
        h = collections.Counter()
        for b in re.split(r"(?=^KeyPress event)",
                          open(LOG, errors="replace").read(), flags=re.M):
            if b.startswith("KeyPress event"):
                m = re.search(r"keysym (0x[0-9a-f]+)", b)
                if m:
                    h[int(m.group(1), 16)] += 1
        g = h.get(K, 0)
        print(f"  {n:>5d} 字 -> 收到 {g}/{n} "
              f"{'★丢 %d 字' % (n - g) if g < n else '未丢字'}   耗时 {dt:.2f}s   {body[:40]}")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
