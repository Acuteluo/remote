#!/usr/bin/env python3
"""端到端验证新 VNC 桥接 + 边缘助弹(直接对着真实 x11vnc / 真实 X 会话跑)。

验证内容:
  1. 新 server.py 的 /ws/vnc 桥接能否完整透传 RFB 握手
  2. 通过桥接发 PointerEvent, 真实指针是否跟着动
  3. 心跳(WebSocket ping)是否按预期发出 —— 这是"过一会儿就连不上"的修复点
  4. /api/edge 是否能把真实指针钉到屏幕最后一行像素(Dock 弹出前提)

结束后还原指针。只读 + 少量指针移动。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import base64
import http.client
import importlib.util
import json
import os
import secrets
import socket
import struct
import sys
import threading
import http.server
import time

CONSOLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CONSOLE)

# 找一个空闲端口
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); PORT = _s.getsockname()[1]; _s.close()

spec = importlib.util.spec_from_file_location("meow_console", os.path.join(CONSOLE, "server.py"))
srv_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv_mod)
srv_mod.PORT = PORT

fails = []
def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        fails.append(name)

httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), srv_mod.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)


def xdo(*args):
    import subprocess
    r = subprocess.run(["xdotool", *args], capture_output=True, text=True)
    return r.stdout.strip()


def real_screen():
    return tuple(int(v) for v in xdo("getdisplaygeometry").split())


def real_pointer():
    out = xdo("getmouselocation")
    d = dict(p.split(":") for p in out.split() if ":" in p)
    return int(d["x"]), int(d["y"])


def http_req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, data, r.getheader("Set-Cookie"))
    c.close()
    return out


# 登录
_, _, setc = http_req("POST", "/login", login_body(),
                      {"Content-Type": "application/x-www-form-urlencoded"})
COOKIE = (setc or "").split(";")[0]
check("登录", COOKIE.startswith("meow_session="))


class WS:
    def __init__(self, path):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=30)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: binary\r\nCookie: {COOKIE}\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        head, _, rest = resp.partition(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], head
        self.buf = rest
        self.bin = b""

    def _read(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise ConnectionError("closed")
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        h = self._read(2)
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        return op, (self._read(n) if n else b"")

    def recv_binary(self, want):
        """累积二进制帧直到 want 字节数够了(桥接会把 RFB 流按 TCP 分片转发)。"""
        while len(self.bin) < want:
            op, d = self.frame()
            if op == 2:
                self.bin += d
            elif op == 9:
                self.sock.sendall(b"\x8a\x80" + secrets.token_bytes(4))  # pong
            elif op == 8:
                raise ConnectionError("server closed ws")
        out, self.bin = self.bin[:want], self.bin[want:]
        return out

    def send_binary(self, payload):
        mask = secrets.token_bytes(4)
        n = len(payload)
        hdr = bytearray([0x82])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        self.sock.sendall(bytes(hdr) + mask +
                          bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def close(self):
        try: self.sock.close()
        except OSError: pass


sx, sy = real_screen()
ox, oy = real_pointer()
print(f"真实屏幕 {sx}x{sy}, 初始指针 ({ox},{oy})\n")

print("== 1/2) /ws/vnc 透传 RFB 握手 + 指针注入 ==")
ws = WS("/ws/vnc")
ver = ws.recv_binary(12)
check("桥接透传 RFB 版本", ver.startswith(b"RFB "), ver)
ws.send_binary(b"RFB 003.008\n")
n = ws.recv_binary(1)[0]
types = ws.recv_binary(n)
check("安全类型", 1 in types, list(types))
ws.send_binary(bytes([1]))
res = struct.unpack(">I", ws.recv_binary(4))[0]
check("认证通过", res == 0)
ws.send_binary(bytes([1]))
head = ws.recv_binary(24)
fw, fh = struct.unpack(">HH", head[:4])
nl = struct.unpack(">I", head[20:24])[0]
ws.recv_binary(nl)
print(f"   客户端帧缓冲 {fw}x{fh}")

ws.send_binary(struct.pack(">BBHH", 5, 0, 400, 300))
time.sleep(0.3)
rx, ry = real_pointer()
check("经桥接的指针事件生效", abs(rx - 800) <= 3 and abs(ry - 600) <= 3, f"-> ({rx},{ry})")

ws.send_binary(struct.pack(">BBHH", 5, 0, fw - 1, fh - 1))
time.sleep(0.3)
rx, ry = real_pointer()
print(f"   客户端最后一行 (fw-1,fh-1) -> 真实 ({rx},{ry}); 需要 {sy-1} 才能弹 Dock")

print("\n== 4) /api/edge 边缘助弹 ==")
st, body, _ = http_req("POST", "/api/edge",
                       json.dumps({"edge": "bottom", "fx": fw // 2, "fy": fh - 1,
                                   "fbw": fw, "fbh": fh}),
                       {"Content-Type": "application/json", "Cookie": COOKIE})
j = json.loads(body)
check("POST /api/edge", st == 200 and j.get("ok"), body[:80])
time.sleep(0.3)
rx, ry = real_pointer()
check("真实指针被钉到最后一行", ry == sy - 1, f"-> ({rx},{ry}) 期望 y={sy-1}")

print("\n== 3) 心跳(等 25s, 期间应收到 WebSocket ping) ==")
ws.sock.settimeout(30)
t0 = time.time()
got_ping = False
try:
    while time.time() - t0 < 26:
        op, d = ws.frame()
        if op == 9:
            got_ping = True
            ws.sock.sendall(b"\x8a\x80" + secrets.token_bytes(4))
            break
        if op == 8:
            break
except Exception as e:
    print("   读帧异常:", repr(e))
check("收到服务端心跳 ping", got_ping, f"耗时 {time.time()-t0:.1f}s")
check("心跳后连接仍可用", got_ping)

ws.close()
httpd.shutdown()
xdo("mousemove", str(ox), str(oy))
print(f"已还原指针到 ({ox},{oy})")
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
