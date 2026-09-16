#!/usr/bin/env python3
"""量一下 /ws/vnc 桥接的吞吐 —— 排查"是不是控制台转发的瓶颈"。

用法:
    DISPLAY=:0 python3 tests/vnc_bridge_rate.py [秒数] [port]

原理: 直接连控制台的 /ws/vnc, 在 WS 里塞 RFB 的全屏刷新请求, 数收到的字节。
和 vnc_rate.py(直连 5900) 的数字对比, 就能知道瓶颈在 x11vnc 还是在桥接。
"""
import base64
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import login_body                      # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
PORT = int(sys.argv[2] if len(sys.argv) > 2 else 8390)
HOST = "127.0.0.1"
W, H = 1440, 960


def ws_send(s, payload, opcode=0x2):
    """客户端 -> 服务端必须加掩码。"""
    m = os.urandom(4)
    n = len(payload)
    h = bytearray([0x80 | opcode])
    if n < 126:
        h.append(0x80 | n)
    elif n < 65536:
        h.append(0x80 | 126)
        h += struct.pack(">H", n)
    else:
        h.append(0x80 | 127)
        h += struct.pack(">Q", n)
    s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload)))


def ws_recv(s, buf):
    """读一个完整帧, 返回 (payload, 剩余 buf)。"""
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


def main():
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    if not ck:
        print("  ★ 登录失败")
        return 1

    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws/vnc HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nCookie: {ck}\r\n"
               f"Origin: http://{HOST}:{PORT}\r\n\r\n").encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        print("  ★ 握手失败:", resp.split(b"\r\n")[0])
        return 1
    print("  /ws/vnc 已连上")
    s.settimeout(0.5)

    # 读掉 RFB 版本(注意要剥掉 WS 帧头, 不能直接看裸字节)
    buf = b""
    try:
        first, buf = ws_recv(s, buf)
    except Exception as e:
        print("  ★ 没收到 RFB 版本:", type(e).__name__)
        s.close()
        return 1
    print(f"  服务端版本: {first!r}")
    if not first.startswith(b"RFB 003."):
        print("  ★ 收到的不是 RFB 版本")
        s.close()
        return 1

    # 完整握手: 版本 -> 安全类型 -> SecurityResult -> ClientInit -> ServerInit
    ws_send(s, b"RFB 003.008\n", 0x2)         # 1) 回版本
    time.sleep(0.3)
    try:
        _, buf = ws_recv(s, buf)              # 2) [ntypes][types...]
    except Exception:
        pass
    ws_send(s, bytes([1]), 0x2)               # 3) 选 None(1)
    time.sleep(0.3)
    try:
        _, buf = ws_recv(s, buf)              # 4) SecurityResult(4 字节)
    except Exception:
        pass
    ws_send(s, bytes([1]), 0x2)               # 5) ClientInit(shared=1) —— 少了这步 x11vnc 会直接断
    time.sleep(0.4)
    try:
        si, buf = ws_recv(s, buf)             # 6) ServerInit
        print(f"  ServerInit {len(si)} 字节, 桌面 {struct.unpack('>HH', si[:4])}"
              if len(si) >= 4 else f"  ServerInit {len(si)} 字节")
    except Exception as e:
        print("  ★ 没拿到 ServerInit:", type(e).__name__)

    encs = [7, 16, 5, 1, 0, -223]             # 和 noVNC 协商的接近
    ws_send(s, struct.pack(">BBH", 2, 0, len(encs)) +
            b"".join(struct.pack(">i", e) for e in encs), 0x2)

    req = struct.pack(">BBHHHH", 3, 0, 0, 0, W, H)   # 全屏非增量
    ws_send(s, req, 0x2)

    total = 0
    frames = 0
    sent = [0]
    quit_flag = threading.Event()

    def feeder():
        while not quit_flag.is_set():
            try:
                ws_send(s, req, 0x2)
                sent[0] += 1
            except OSError:
                return
            quit_flag.wait(0.04)

    t0 = time.time()
    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    while time.time() - t0 < SECS:
        try:
            payload, buf = ws_recv(s, buf)
        except socket.timeout:
            continue
        except ConnectionError:
            print("  ★ 连接被关闭")
            break
        total += len(payload)
        frames += 1
    dt = time.time() - t0
    quit_flag.set()
    try:
        ws_send(s, b"", 0x8)              # 发 close 帧, 别在 x11vnc 上留僵尸
    except OSError:
        pass
    s.close()
    print(f"  收到 {total / 1024:.0f} KB / {dt:.1f}s  =  {total / dt / 1024:.0f} KB/s"
          f"   ({frames} 个 WS 帧, 发请求 {sent[0]} 次)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
