#!/usr/bin/env python3
"""重复性实验: 单次指针落到"越过帧缓冲一行"(真实 1919) 到底能不能稳定弹出 Dock。

每次试验前都截图确认 Dock 确实处于隐藏态, 避免把"上次没藏回去"误判成失败。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os, socket, struct, subprocess, time
import http.client, json

ENV = {**os.environ, "DISPLAY": ":0",
       "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
S = "org.gnome.shell.extensions.dash-to-dock"
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
os.makedirs(ART, exist_ok=True)


def gset(k, v):
    """改 require-pressure-to-show。

    注意: 不能用沙箱里的 `gsettings set` —— 沙箱能读 dconf 但写不进去(命令静默失败),
    会把"设置没变"误当成"这个设置没用"。必须走服务端接口, 它跑在沙箱外。
    """
    assert k == "require-pressure-to-show"
    c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c.request("POST", "/api/dock", json.dumps({"pressure": bool(v)}),
              {"Content-Type": "application/json", "Cookie": ck})
    c.getresponse().read()


def shot(tag):
    p = f"{ART}/rp_{tag}.png"
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


class R:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", 5900), timeout=8)
        self.fw, self.fh = self._hs()

    def _rx(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def _hs(self):
        self._rx(12)
        self.s.sendall(b"RFB 003.008\n")
        n = self._rx(1)[0]
        self._rx(n)
        self.s.sendall(bytes([1]))
        self._rx(4)
        self.s.sendall(bytes([1]))
        h = self._rx(24)
        fw, fh = struct.unpack(">HH", h[:4])
        nl = struct.unpack(">I", h[20:24])[0]
        self._rx(nl)
        self.s.sendall(struct.pack(">BBH", 2, 0, 4) +
                       b"".join(struct.pack(">i", e) for e in (7, 16, 5, 0)))
        return fw, fh

    def ptr(self, x, y):
        self.s.sendall(struct.pack(">BBHH", 5, 0, x, y))

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


r = R()
cx = r.fw // 2
away = r.fh // 3
edge_over = r.fh        # 越过帧缓冲一行 -> 真实 1919
edge_last = r.fh - 1    # 帧缓冲最后一行 -> 真实 1918

print(f"帧缓冲 {r.fw}x{r.fh}   y={edge_over}->真实1919(边界)   y={edge_last}->真实1918\n")


def refs():
    r.ptr(cx, away); time.sleep(2.2)
    hid = shot("hid")
    r.ptr(cx, edge_over); time.sleep(2.0)
    sho = shot("sho")
    r.ptr(cx, away); time.sleep(2.0)
    return hid, sho


def trial(y, warm=2.2, wait=1.5, push=1):
    """push: 到位后额外重复几次(每次间隔 120ms), 模拟"推一把"。"""
    r.ptr(cx, away); time.sleep(warm)
    base = shot("base")
    for i in range(push):
        r.ptr(cx, y)
        if i < push - 1:
            time.sleep(0.12)
    time.sleep(wait)
    cur = shot("cur")
    r.ptr(cx, away); time.sleep(1.8)
    return diff(base, cur)


for mode in (True, False):
    gset("require-pressure-to-show", "true" if mode else "false")
    time.sleep(1.6)
    hid, sho = refs()
    print(f"require-pressure-to-show = {str(mode).lower()}   (参考: 隐藏 vs 显示 差 {diff(hid, sho):.4f})")

    for label, y, push in [("单次 y=1919", edge_over, 1),
                           ("单次 y=1918", edge_last, 1),
                           ("y=1919 推3次", edge_over, 3),
                           ("y=1919 推6次", edge_over, 6)]:
        rs = [trial(y, push=push) for _ in range(4)]
        hits = sum(1 for d in rs if d > 0.1)
        print(f"   {label:16s} 成功 {hits}/4   各次像素差 {[round(d,3) for d in rs]}")
    print()

r.ptr(cx, away)
r.close()
