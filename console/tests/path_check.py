#!/usr/bin/env python3
"""真实链路自检: 走 lan_forward(局域网 IP / Tailscale), 不是回环。

用法: DISPLAY=:0 python3 tests/path_check.py

为什么必须单独测: 手机连的是 `lan_forward.py` 镜像出来的 `网卡IP:8390`,
不是 127.0.0.1。回环全绿不代表手机那条路没问题 —— 转发层多一次
recv/sendall, 端口也可能没被镜像。

地址自动从 `ip -4 -o addr` 里取(排除 lo), 换机器/换网卡不用改脚本。
复用 vnc_stability_check 里的最小 WS 客户端(Bridge) —— 它按模块全局
HOST/PORT 建连, 所以这里改 V.HOST 就能换目标地址。
"""
import http.client
import os
import re
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vnc_stability_check as V                    # noqa: E402

OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def local_ips():
    """本机非回环 IPv4(带网卡名), 排除 lo。"""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for ln in out.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", ln)
        if m and m.group(1) != "lo":
            found.append((m.group(1), m.group(2)))
    return found


def http_rtt(host, n=20):
    out = []
    for _ in range(n):
        c = http.client.HTTPConnection(host, 8390, timeout=5)
        t0 = time.perf_counter()
        c.request("GET", "/api/health?t=%d" % time.time_ns())
        c.getresponse().read()
        out.append((time.perf_counter() - t0) * 1000)
        c.close()
        time.sleep(0.03)
    return out


def bridge_rtt(host, n=12, box=64):
    V.HOST = host
    b = V.Bridge()
    w, h = b.handshake()
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, box, box)
    out = []
    b.settimeout(3)
    for _ in range(n):
        t0 = time.perf_counter()
        b.send(req)
        try:
            b.recv_frame()
        except (socket.timeout, ConnectionError):
            continue
        out.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.08)
    b.close()
    return out, (w, h)


def bridge_rate(host, secs=8.0):
    V.HOST = host
    b = V.Bridge()
    w, h = b.handshake()
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)
    stop = V.threading.Event()

    def feeder():
        while not stop.is_set():
            try:
                b.send(req)
            except OSError:
                return
            stop.wait(0.04)

    V.threading.Thread(target=feeder, daemon=True).start()
    total, frames = 0, 0
    t0 = time.time()
    b.settimeout(5)
    while time.time() - t0 < secs:
        try:
            op, pl = b.recv_frame()
        except socket.timeout:
            continue
        except ConnectionError:
            break
        if op == 0x2:
            total += len(pl)
            frames += 1
    dt = time.time() - t0
    stop.set()
    b.close()
    return total / dt / 1024, frames / dt


def hold(host, secs=20):
    """挂着不动, 看会不会掉。"""
    V.HOST = host
    b = V.Bridge()
    b.handshake()
    drops, pings = 0, 0
    t0 = time.time()
    b.settimeout(5)
    while time.time() - t0 < secs:
        try:
            b.recv_frame()
        except socket.timeout:
            continue
        except ConnectionError:
            drops += 1
            break
    pings = b.pings
    b.close()
    return drops, pings, time.time() - t0


def main():
    ips = local_ips()
    print("本机地址: " + ", ".join(f"{n}={ip}" for n, ip in ips) or "  ★ 没找到非回环地址")
    if not ips:
        print("  ★ 没有可用网卡, 跳过")
        return 1
    V.CK = V.cookie()          # cookie 不绑 host, 回环登录一次就够
    # 基线: 你自己手机/电脑上可能正连着远程桌面 —— 那时客户端数本来就不是 0,
    # 所以判断"有没有留残留"只能跟基线比, 不能硬要求 0。
    base0, _ = V.api_clients()
    print(f"起始 x11vnc 客户端数: {base0}" + ("  (你自己连着的, 属正常)" if base0 else ""))

    print("【1】HTTP RTT: 回环 vs 网卡地址(手机走的就是下面这些)")
    base = http_rtt("127.0.0.1")
    chk("回环 HTTP 可达", bool(base), f"中位 {V.pct(base, 50):.1f}ms")
    per_ip = {}
    for name, ip in ips:
        s = http_rtt(ip)
        per_ip[ip] = s
        chk(f"{name} ({ip}) HTTP 可达且不慢", bool(s) and V.pct(s, 50) < 25,
            f"中位 {V.pct(s, 50):.1f}ms  p95 {V.pct(s, 95):.1f}ms" if s else "不通")

    print("【2】桥接往返(左上角 64x64): 各条路径对比")
    for name, ip in ips:
        s, (w, h) = bridge_rtt(ip)
        chk(f"{name} 桥接往返正常", bool(s) and V.pct(s, 50) < 100,
            f"中位 {V.pct(s, 50):.1f}ms  最快 {min(s):.1f}ms" if s else "没采到")

    print("【3】经网卡的吞吐(每 40ms 要一次全屏刷新)")
    for name, ip in ips[:1]:
        kbs, fps = bridge_rate(ip, 8.0)
        chk(f"{name} 吞吐 > 1 MB/s", kbs > 1024,
            f"{kbs / 1024:.1f} MB/s, {fps:.1f} 帧/s")

    print("【4】挂着不动 20 秒(看会不会掉)")
    for name, ip in ips:
        drops, pings, held = hold(ip, 20)
        extra = f"坚持 {held:.0f}s, 心跳 {pings} 次"
        if drops and 4 <= held <= 6:
            extra += "  ← 5 秒左右断说明 dsh-lan-forward 还是旧代码, 跑: systemctl --user restart dsh-lan-forward"
        chk(f"{name} 20s 不掉线", drops == 0, extra)

    c, br = V.api_clients()
    chk("测试后没留残留连接", (c or 0) <= (base0 or 0), f"clients={c} (基线 {base0})")
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
