#!/usr/bin/env python3
"""量一下 x11vnc 到底能出多少帧 / 多少带宽 —— 排查"画面卡"用。

用法:
    DISPLAY=:0 python3 tests/vnc_rate.py [秒数] [host] [port]

为什么需要它: 手机上说"延时 <50ms 但画面很卡"时, 要区分三种原因 ——
  1) x11vnc 自己产帧太慢(屏幕太大 / 没开 XDamage 只能全屏扫描)
  2) 网络带宽不够(走 DERP 中继 / 弱网)
  3) 手机端解码跟不上
这个脚本量的是第 1 种(服务端产能): 直接连 127.0.0.1:5900, 不经过网络。

**故意不解析 VNC 消息**: 逐条解析编码(Tight/ZRLE/Hextile…)太容易写错,
写错就会得出假结论。改成"发一次全屏请求 → 读到安静为止", 按**静默间隔**
切分每一帧, 只数字节和时间 —— 简单且不会骗人。

收尾会主动断开 TCP, x11vnc 看到 EOF 会自己清理, 不留僵尸客户端。
"""
import socket
import struct
import sys
import time

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
PORT = int(sys.argv[3] if len(sys.argv) > 3 else 5900)
IDLE = 0.35            # 多久没有新数据就算这一帧结束了


def recvn(s, n, deadline):
    buf = b""
    while len(buf) < n:
        if time.time() > deadline:
            raise TimeoutError(f"只读到 {len(buf)}/{n} 字节")
        d = s.recv(n - len(buf))
        if not d:
            raise ConnectionError("对端关闭")
        buf += d
    return buf


def handshake(s):
    deadline = time.time() + 15
    ver = recvn(s, 12, deadline)
    s.sendall(b"RFB 003.008\n")
    ntypes = recvn(s, 1, deadline)[0]
    if ntypes == 0:
        ln = struct.unpack(">I", recvn(s, 4, deadline))[0]
        raise RuntimeError("服务端拒绝: " + recvn(s, ln, deadline).decode("utf-8", "replace"))
    types = recvn(s, ntypes, deadline)
    if 1 not in types:
        raise RuntimeError(f"需要密码, 这个脚本不支持 (types={list(types)})")
    s.sendall(bytes([1]))
    if struct.unpack(">I", recvn(s, 4, deadline))[0] != 0:
        raise RuntimeError("认证失败")
    s.sendall(bytes([1]))                       # ClientInit: shared
    w, h = struct.unpack(">HH", recvn(s, 4, deadline))
    pf = recvn(s, 16, deadline)
    nl = struct.unpack(">I", recvn(s, 4, deadline))[0]
    name = recvn(s, nl, deadline).decode("utf-8", "replace")
    encs = [7, 16, 5, 1, 0, -223]               # 接近 noVNC 会协商的
    s.sendall(struct.pack(">BBH", 2, 0, len(encs)) +
              b"".join(struct.pack(">i", e) for e in encs))
    return w, h, pf[0], name


def read_until_quiet(s, timeout_idle=IDLE, hard=20.0):
    """读到安静为止, 返回 (字节数, 耗时)。"""
    n = 0
    t0 = time.time()
    last = t0
    while time.time() - last < timeout_idle and time.time() - t0 < hard:
        try:
            d = s.recv(262144)
            if not d:
                break
            n += len(d)
            last = time.time()
        except socket.timeout:
            continue
    return n, time.time() - t0


def main():
    try:
        s = socket.create_connection((HOST, PORT), timeout=10)
    except OSError as e:
        print(f"  ★ 连不上 {HOST}:{PORT}  {e}")
        return 1
    s.settimeout(IDLE)
    try:
        w, h, bpp, name = handshake(s)
    except (RuntimeError, TimeoutError, ConnectionError) as e:
        print(f"  ★ 握手失败: {e}")
        return 1
    print(f"  桌面: {w}x{h}  {bpp}bpp  \"{name}\"   ({w * h / 1e6:.1f} Mpx)")

    req_full = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)   # incremental=0
    req_inc = struct.pack(">BBHHHH", 3, 1, 0, 0, w, h)    # incremental=1

    # 先单独量一帧有多大(用静默切分, 这里慢一点无所谓)
    print()
    print("  【A】单帧大小(整屏全画一次)")
    s.sendall(req_full)
    n, dt = read_until_quiet(s, timeout_idle=0.5)
    print(f"      {n / 1024:.0f} KB / {dt:.2f}s")
    frame_kb = n / 1024 or 1

    # 真正的吞吐: **独立线程定时补请求 + 墙钟计时**。
    # 三个坑都踩过, 记下来:
    #  1) 用"读到安静为止"切帧 → 静默阈值本身就是下限, 测出来的帧率被压在 1/阈值 附近;
    #  2) 用"收够 N 字节才补请求" → 一帧不到 N 字节就永远不补, 每次都要等超时;
    #  3) 在主循环里检查计时器 → recv 一阻塞, 计时器就没人看, 又混进空转时间。
    # 现在由独立线程每 40ms 补一次请求, 主循环只数字节。
    print()
    print(f"  【B】连续整屏吞吐(墙钟 {SECS:.0f}s, 独立线程每 40ms 补请求)")
    import threading
    sent = [0]
    quit_flag = threading.Event()

    def feeder():
        while not quit_flag.is_set():
            try:
                s.sendall(req_full)
                sent[0] += 1
            except OSError:
                return
            quit_flag.wait(0.04)

    total = 0
    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    t0 = time.time()
    while time.time() - t0 < SECS:
        try:
            d = s.recv(262144)
            if not d:
                break
            total += len(d)
        except socket.timeout:
            pass
    dt = time.time() - t0
    quit_flag.set()
    print(f"      收到 {total / 1024:.0f} KB / {dt:.1f}s  =  {total / dt / 1024:.0f} KB/s")
    print(f"      发出请求 {sent[0]} 次  →  单帧约 {total / 1024 / max(sent[0], 1):.0f} KB")

    # 增量: 看画面实际变化量(静止时接近 0 是正常的)
    print()
    print("  【C】增量刷新(画面实际变化量)")
    inc_total, inc_n = 0, 0
    t_end = time.time() + SECS / 2
    while time.time() < t_end:
        s.sendall(req_inc)
        n, _ = read_until_quiet(s, timeout_idle=0.2)
        inc_total += n
        inc_n += 1
    print(f"      请求 {inc_n} 次, 收到 {inc_total / 1024:.0f} KB"
          f"  (画面静止时会很小, 正常)")

    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
