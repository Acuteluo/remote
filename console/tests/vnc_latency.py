#!/usr/bin/env python3
"""量 VNC 的**响应延迟**：发一个小区域请求，测到第一个响应字节要多久。

用法:
    DISPLAY=:0 python3 tests/vnc_latency.py [次数]

对比两条路:
  直连 127.0.0.1:5900        —— x11vnc 本身的响应延迟
  经 /ws/vnc 桥接            —— 加上控制台转发后的延迟
两者之差 = 桥接引入的额外延迟。这是"是不是我加的代码让它变慢了"的直接证据。
"""
import base64
import http.client
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import login_body                      # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 12
PORT = 8390
HOST = "127.0.0.1"


def stats(name, samples):
    if not samples:
        print(f"  {name}: 没采到")
        return None
    s = sorted(samples)
    avg = sum(s) / len(s)
    print(f"  {name:22s} 中位 {s[len(s) // 2]:6.1f} ms   "
          f"平均 {avg:6.1f} ms   最快 {s[0]:5.1f}   最慢 {s[-1]:6.1f}")
    return avg


def raw_vnc_latency():
    """直连 5900，量小区域请求的响应延迟。"""
    s = socket.create_connection((HOST, 5900), timeout=10)
    s.settimeout(10)
    s.recv(12)
    s.sendall(b"RFB 003.008\n")
    n = s.recv(1)[0]
    s.recv(n)
    s.sendall(bytes([1]))
    s.recv(4)
    s.sendall(bytes([1]))
    hdr = b""
    while len(hdr) < 24:
        hdr += s.recv(24 - len(hdr))
    w, h = struct.unpack(">HH", hdr[:4])
    nl = struct.unpack(">I", hdr[20:24])[0]
    s.recv(nl)
    encs = [7, 16, 5, 1, 0, -223]
    s.sendall(struct.pack(">BBH", 2, 0, len(encs)) +
              b"".join(struct.pack(">i", e) for e in encs))
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, 64, 64)      # 只问左上角 64x64
    out = []
    s.settimeout(3)
    for _ in range(N):
        t0 = time.time()
        s.sendall(req)
        try:
            d = s.recv(65536)
        except socket.timeout:
            continue
        if d:
            out.append((time.time() - t0) * 1000)
        time.sleep(0.08)
    s.close()
    return out, (w, h)


def bridge_latency(w, h):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]

    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws/vnc HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nCookie: {ck}\r\n"
               f"Origin: http://{HOST}:{PORT}\r\n\r\n").encode())
    if b"101" not in s.recv(4096).split(b"\r\n")[0]:
        return []
    s.settimeout(3)

    def send(payload):
        m = os.urandom(4)
        s.sendall(bytes([0x82, 0x80 | len(payload)]) + m +
                  bytes(b ^ m[i % 4] for i, b in enumerate(payload)))

    def recv_frame(buf):
        while True:
            if len(buf) >= 2:
                ln = buf[1] & 0x7F
                off = None
                if ln < 126:
                    off = 2
                elif ln == 126 and len(buf) >= 4:
                    ln = struct.unpack(">H", buf[2:4])[0]
                    off = 4
                elif ln == 127 and len(buf) >= 10:
                    ln = struct.unpack(">Q", buf[2:10])[0]
                    off = 10
                if off is not None and len(buf) >= off + ln:
                    return buf[off:off + ln], buf[off + ln:]
            d = s.recv(262144)
            if not d:
                raise ConnectionError("closed")
            buf += d

    buf = b""
    try:
        _, buf = recv_frame(buf)                 # RFB 版本
        send(b"RFB 003.008\n")
        time.sleep(0.25)
        _, buf = recv_frame(buf)                 # 安全类型
        send(bytes([1]))
        time.sleep(0.25)
        _, buf = recv_frame(buf)                 # SecurityResult
        send(bytes([1]))                         # ClientInit
        time.sleep(0.35)
        _, buf = recv_frame(buf)                 # ServerInit
        encs = [7, 16, 5, 1, 0, -223]
        send(struct.pack(">BBH", 2, 0, len(encs)) +
             b"".join(struct.pack(">i", e) for e in encs))
    except Exception as e:
        print("  桥接握手失败:", type(e).__name__, e)
        s.close()
        return []

    req = struct.pack(">BBHHHH", 3, 0, 0, 0, 64, 64)
    out = []
    for _ in range(N):
        t0 = time.time()
        send(req)
        try:
            _, buf = recv_frame(buf)
        except (socket.timeout, ConnectionError):
            continue
        out.append((time.time() - t0) * 1000)
        time.sleep(0.08)
    try:
        send(b"")
        s.sendall(bytes([0x88, 0x80]) + os.urandom(4))
    except OSError:
        pass
    s.close()
    return out


def main():
    a, (w, h) = raw_vnc_latency()
    b = bridge_latency(w, h)
    print(f"  桌面 {w}x{h}，每次请求左上角 64x64，各测 {N} 次")
    print()
    ra = stats("直连 x11vnc", a)
    rb = stats("经 /ws/vnc 桥接", b)
    if ra and rb:
        print()
        print(f"  → 桥接引入的额外延迟: {rb - ra:+.1f} ms")
        print("     (差值很小说明控制台转发不是瓶颈; 延迟大头在手机↔PC 链路上)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
