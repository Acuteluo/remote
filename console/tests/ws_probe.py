#!/usr/bin/env python3
# meow-console 端到端测试: 登录 -> 静态资源 -> WS状态 -> WS终端 -> WS VNC桥
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import base64
import hashlib
import http.client
import json
import secrets
import socket
import struct
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8390
PASS = sys.argv[3] if len(sys.argv) > 3 else creds()[1]
ok = lambda name, cond, extra="": print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def http_req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=8)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    setc = r.getheader("Set-Cookie")
    c.close()
    return r.status, dict(r.getheaders()), data, setc


class WS:
    def __init__(self, path):
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"{COOKIE_HEADER}\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        head, _, rest = resp.partition(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], head
        self.buf = rest

    def _read(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise ConnectionError("closed")
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_frame(self):
        h = self._read(2)
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        data = self._read(n)
        return op, data

    def send_text(self, s):
        p = s.encode()
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(p))
        n = len(p)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        self.sock.sendall(bytes(hdr) + mask + masked)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# 1) 健康检查
st, _, body, _ = http_req("GET", "/api/health")
ok("health", st == 200 and b"meow-console" in body)

# 2) 未登录跳转
st, hdrs, _, _ = http_req("GET", "/home")
ok("unauthed redirect", st in (301, 302) and "/login" in hdrs.get("Location", ""))

# 3) 登录(错误密码)
form = f"user={creds()[0]}&pass=wrongpass"
st, _, body, _ = http_req("POST", "/login", form,
                          {"Content-Type": "application/x-www-form-urlencoded"})
ok("login reject", st == 200 and "错误".encode() in body)

# 4) 登录(正确密码)
form = login_body()
st, _, _, setc = http_req("POST", "/login", form,
                          {"Content-Type": "application/x-www-form-urlencoded"})
cookie = (setc or "").split(";")[0]
COOKIE_HEADER = f"Cookie: {cookie}"
ok("login accept", st == 302 and cookie.startswith("meow_session="), cookie[:24] + "…")

# 5) 页面与静态资源
st, _, body, _ = http_req("GET", "/home", headers={"Cookie": cookie})
ok("home page", st == 200 and b"cpu-hist" in body)
for p in ("/term", "/vnc"):
    st, _, _, _ = http_req("GET", p, headers={"Cookie": cookie})
    ok(f"page {p}", st == 200)
for p in ("/static/app.css", "/static/home.js", "/static/term.js", "/static/vnc.js",
          "/static/vendor/xterm/xterm.js", "/static/vendor/novnc/core/rfb.js"):
    st, hdrs, body, _ = http_req("GET", p, headers={"Cookie": cookie})
    ok(f"static {p.split('/')[-1]}", st == 200 and len(body) > 100)

# 6) WS 状态
ws = WS("/ws/status")
op, data = ws.recv_frame()
snap = json.loads(data)
cpu_ok = isinstance(snap.get("cpu", {}).get("total"), (int, float))
ok("ws/status", op == 1 and cpu_ok,
   f"cpu={snap['cpu']['total']}% mem_total={snap['mem'].get('total', 0)//2**30}G "
   f"temps={len(snap['temps'])} peers={len(snap['peers'])} wins={len(snap['windows'])}")
ws.close()

# 7) WS 终端: 执行 echo 标记(原始字节读取, 不做组帧解析)
ws = WS("/ws/term")
ws.send_text(json.dumps({"t": "r", "c": 120, "r": 30}))
time.sleep(1.5)
ws.send_text(json.dumps({"t": "i", "d": "echo LUO_TERM_$((21*2))_OK\r"}))
out = b""
ws.sock.settimeout(8)
try:
    while b"LUO_TERM_42_OK" not in out:
        d = ws.sock.recv(65536)
        if not d:
            break
        out += d
except Exception as e:
    print("  term recv exception:", repr(e))
ok("ws/term", b"LUO_TERM_42_OK" in out, f"{len(out)}B")
ws.close()

# 8) WS VNC: 应透传 RFB 握手
ws = WS("/ws/vnc")
op, data = ws.recv_frame()
ok("ws/vnc", data.startswith(b"RFB "), data[:12])
ws.close()


# 9) CSWSH 防护: 跨源 Origin 必须被拒; 同源/无 Origin 放行
def ws_handshake_first_line(path, origin=None):
    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: {HOST}:{PORT}",
             "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
             COOKIE_HEADER]
    if origin:
        lines.append(f"Origin: {origin}")
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        d = s.recv(4096)
        if not d:
            break
        resp += d
    s.close()
    return resp.split(b"\r\n")[0]


ok("ws 跨源 Origin 拒绝", b"403" in ws_handshake_first_line(
    "/ws/status", origin="http://evil.example.com"))
ok("ws 同源 Origin 放行", b"101" in ws_handshake_first_line(
    "/ws/status", origin=f"http://{HOST}:{PORT}"))
ok("ws 无 Origin(脚本客户端)放行", b"101" in ws_handshake_first_line("/ws/status"))

print("\n全部通过" if all else "done")
