#!/usr/bin/env python3
"""严格对照: require-pressure-to-show 两种取值 x 两种指针注入方式(xdotool / RFB)。

设置通过服务端接口写(沙箱里 gsettings 写不进去)。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, os, socket, struct, subprocess, time

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")


def api(method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    if method == "GET":
        c.request("POST", "/login", login_body(),
                  {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse(); r.read()
        ck = (r.getheader("Set-Cookie") or "").split(";")[0]
        c.request("GET", "/api/dock", None, {"Cookie": ck})
        return json.loads(c.getresponse().read())
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse(); r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c.request("POST", "/api/dock", json.dumps(body),
              {"Content-Type": "application/json", "Cookie": ck})
    return json.loads(c.getresponse().read())


def xdo(*a):
    subprocess.run(["xdotool", *a], env=ENV, capture_output=True)


def shot(tag):
    p = f"{ART}/ab2_{tag}.png"
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
        self._rx(12); self.s.sendall(b"RFB 003.008\n")
        n = self._rx(1)[0]; self._rx(n); self.s.sendall(bytes([1]))
        self._rx(4); self.s.sendall(bytes([1]))
        h = self._rx(24); fw, fh = struct.unpack(">HH", h[:4])
        nl = struct.unpack(">I", h[20:24])[0]; self._rx(nl)
        self.s.sendall(struct.pack(">BBH", 2, 0, 4) +
                       b"".join(struct.pack(">i", e) for e in (7, 16, 5, 0)))
        return fw, fh

    def ptr(self, x, y):
        self.s.sendall(struct.pack(">BBHH", 5, 0, x, y))


r = R()
print(f"帧缓冲 {r.fw}x{r.fh}\n")

for pressure in (True, False):
    print(f"=== require-pressure-to-show = {pressure} ===")
    api("POST", {"pressure": pressure})
    time.sleep(1.6)
    print("   接口确认:", api().get("pressure"))

    # --- xdotool ---
    xdo("mousemove", "1440", "900"); time.sleep(2.2)
    a = shot("xdo_hid")
    xdo("mousemove", "1440", "1919"); time.sleep(2.2)
    d_xdo = diff(a, shot("xdo_on"))
    xdo("mousemove", "1440", "900"); time.sleep(2.0)
    print(f"   xdotool 1440,1919 -> 像素差 {d_xdo:.4f}  "
          f"{'★弹出' if d_xdo > 0.1 else '无'}")

    # --- RFB ---
    r.ptr(720, 320); time.sleep(2.2)
    b = shot("rfb_hid")
    r.ptr(720, r.fh); time.sleep(2.2)
    d_rfb = diff(b, shot("rfb_on"))
    r.ptr(720, 320); time.sleep(1.8)
    print(f"   RFB   720,960(真实1919) -> 像素差 {d_rfb:.4f}  "
          f"{'★弹出' if d_rfb > 0.1 else '无'}")
    print()

r.s.close()
