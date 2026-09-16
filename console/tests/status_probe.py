#!/usr/bin/env python3
"""连一次 /ws/status, 打印手机端实际看到的状态(CPU/进程 TOP), 用来核实 Xorg 占用。"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import base64, http.client, json, os, socket, struct, time

B = ("127.0.0.1", 8390)

c = http.client.HTTPConnection(*B, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
ck = (r.getheader("Set-Cookie") or "").split(";")[0]

s = socket.create_connection(B, timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(("GET /ws/status HTTP/1.1\r\nHost: 127.0.0.1:8390\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\nCookie: %s\r\n\r\n" % (key, ck)).encode())

buf = b""
while b"\r\n\r\n" not in buf:
    d = s.recv(4096)
    if not d:
        raise SystemExit("握手失败")
    buf += d
head, rest = buf.split(b"\r\n\r\n", 1)
print("握手:", head.split(b"\r\n")[0].decode())


def read_frame():
    global rest
    while len(rest) < 2:
        rest += s.recv(65536)
    b0, b1 = rest[0], rest[1]
    op = b0 & 0x0F
    n = b1 & 0x7F
    off = 2
    if n == 126:
        while len(rest) < 4:
            rest += s.recv(65536)
        n = struct.unpack(">H", rest[2:4])[0]; off = 4
    elif n == 127:
        while len(rest) < 10:
            rest += s.recv(65536)
        n = struct.unpack(">Q", rest[2:10])[0]; off = 10
    while len(rest) < off + n:
        rest += s.recv(65536)
    payload = rest[off:off + n]
    rest = rest[off + n:]
    return op, payload


got = 0
t0 = time.time()
while got < 3 and time.time() - t0 < 20:
    op, pl = read_frame()
    if op == 0x9:                      # ping -> pong
        s.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
        continue
    if op != 0x1:
        continue
    got += 1
    d = json.loads(pl.decode("utf-8", "replace"))
    print(f"\n--- 第 {got} 帧 ---")
    cpu = d.get("cpu") or {}
    print("总体 CPU:", json.dumps(cpu, ensure_ascii=False)[:300])
    print("负载:", d.get("load"), " 内存:", json.dumps(d.get("mem"), ensure_ascii=False)[:160])
    print("温度:", json.dumps(d.get("temp"), ensure_ascii=False)[:160])
    procs = d.get("procs") or []
    print(f"进程 TOP ({len(procs)} 条):")
    for p in procs[:14]:
        if isinstance(p, dict):
            print("   ", json.dumps(p, ensure_ascii=False)[:150])
        else:
            print("   ", p)
s.close()
