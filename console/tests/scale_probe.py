#!/usr/bin/env python3
"""实证 x11vnc -scale 1/2 的指针坐标误差 —— 这是 Dock 弹不出来的根因。

思路: 连上正在跑的 x11vnc, 按 RFB 协议发一个 PointerEvent 到"客户端帧缓冲的最后
一行", 然后用 xdotool 读回真实指针坐标。如果真实坐标落在屏幕最后一行之外,
就说明 Ubuntu Dock 的驻留判定 (y == monitor.height - 1, 严格相等) 永远不可能满足。

只做只读验证 + 一次指针移动, 结束后把指针还原。
"""
import socket
import struct
import subprocess
import sys
import time

HOST, PORT = "127.0.0.1", 5900


def real_screen():
    out = subprocess.run(["xdotool", "getdisplaygeometry"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]), int(out[1])


def real_pointer():
    out = subprocess.run(["xdotool", "getmouselocation"],
                         capture_output=True, text=True).stdout
    d = dict(p.split(":") for p in out.split() if ":" in p)
    return int(d["x"]), int(d["y"])


def read_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("server closed")
        buf += c
    return buf


def main():
    sx, sy = real_screen()
    ox, oy = real_pointer()
    print(f"真实屏幕: {sx}x{sy}   当前指针: ({ox},{oy})")

    s = socket.create_connection((HOST, PORT), timeout=8)
    ver = read_exact(s, 12)
    print(f"服务器版本: {ver!r}")
    s.sendall(b"RFB 003.008\n")

    n = read_exact(s, 1)[0]
    types = read_exact(s, n)
    print(f"安全类型: {list(types)}")
    if 1 not in types:
        print("没有 None 认证, 放弃")
        return 1
    s.sendall(bytes([1]))                       # None
    res = struct.unpack(">I", read_exact(s, 4))[0]
    if res != 0:
        print(f"认证失败 {res}")
        return 1

    s.sendall(bytes([1]))                       # shared
    head = read_exact(s, 24)
    fw, fh = struct.unpack(">HH", head[:4])
    namelen = struct.unpack(">I", head[20:24])[0]
    name = read_exact(s, namelen).decode("utf-8", "replace")
    print(f"客户端帧缓冲: {fw}x{fh}  桌面名: {name!r}")
    print(f"=> 缩放比: x {sx/fw:.4f}  y {sy/fh:.4f}")

    def pointer(x, y, mask=0):
        s.sendall(struct.pack(">BBHH", 5, mask, x, y))
        time.sleep(0.25)

    results = []
    for label, (cx, cy) in [
        ("客户端最后一行 (fw-1, fh-1)", (fw - 1, fh - 1)),
        ("客户端最右一列 (fw-1, fh-1)", (fw - 1, fh - 1)),
    ]:
        pointer(cx, cy)
        rx, ry = real_pointer()
        results.append((label, cx, cy, rx, ry))
        print(f"  发 ({cx},{cy})  ->  真实指针 ({rx},{ry})")

    pointer(fw // 2, fh - 1)
    rx, ry = real_pointer()
    print(f"\n关键判定: 客户端能寻址的最大 y = {fh-1}, 换算到真实屏幕得到 y = {ry}")
    print(f"          Dock 驻留要求真实 y == {sy-1}")
    ok = (ry == sy - 1)
    print(f"          -> {'可达' if ok else f'差 {sy-1-ry} 像素, 永远弹不出 Dock'}")

    # 服务端 xdotool 钉到真实最后一行, 验证修复思路可行
    subprocess.run(["xdotool", "mousemove", str(sx // 2), str(sy - 1)],
                   capture_output=True)
    time.sleep(0.2)
    nx, ny = real_pointer()
    print(f"\n边缘助弹验证: xdotool mousemove -> 真实指针 ({nx},{ny}) "
          f"=> {'命中最后一行' if ny == sy - 1 else '仍未命中'}")

    s.close()
    # 还原指针
    subprocess.run(["xdotool", "mousemove", str(ox), str(oy)], capture_output=True)
    print(f"已还原指针到 ({ox},{oy})")
    return 0 if not ok else 0


if __name__ == "__main__":
    sys.exit(main())
