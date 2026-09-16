#!/usr/bin/env python3
"""复现/回归: "客户端不再读数据"时, 桥接会不会在 x11vnc 上留下僵尸连接。

用法: DISPLAY=:0 python3 tests/leak_repro.py

背景(2026-09-16 实测): 跑完一轮测试后 `ss -tnp | grep 5900` 看到 7 条 ESTAB,
属主栏是空的, 但 `ss -e` 的 cgroup 写着 meow-console.service —— 也就是说
**socket 是我们的, 但进程里已经没有对应的 fd 了**, 而 x11vnc 认为它们还活着
(`-shared` 下客户端越多共享更新循环越慢 → 所有人一起卡)。控制台自己的
`/api/vnc/clients` 也报 6~7, 而登记表是 0。

假设: 浏览器(手机)卡死/锁屏时不再读 WS 数据 → 服务端 v2w 线程阻塞在
`ws.send_binary()` → 心跳判死 → teardown 关掉 fd, 但那个线程还堵在系统调用里,
内核持有 socket 引用 → **连接一直 ESTAB**。

本脚本就按这个假设做: 连上、要全屏刷新、**收一会儿就故意不再读**,
然后看 x11vnc 上的连接数会不会自己降回去。
"""
import http.client
import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vnc_stability_check as V                    # noqa: E402

OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def wait_drop(before, secs=90):
    """等连接数从 before 降下来; 返回 (用时, 最终值)。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < secs:
        last, _ = V.api_clients()
        if last is not None and last < before:
            return time.time() - t0, last
        time.sleep(1)
    return None, last


def case_no_read():
    """客户端不再读数据(手机卡死/锁屏) —— 服务端必须自己收干净。"""
    print("【1】客户端停止读取(模拟手机卡死)")
    base, _ = V.api_clients()
    print(f"  起始客户端数: {base}")
    b = V.Bridge(timeout=5)
    w, h = b.handshake()
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)
    # 先正常读一会儿, 让画面真的开始流动
    t0 = time.time()
    while time.time() - t0 < 4:
        b.send(req)
        try:
            b.recv_frame()
        except socket.timeout:
            pass
    up, _ = V.api_clients()
    chk("连接建立后客户端数 +1", up == (base or 0) + 1, f"{base} → {up}")

    # 关键: 之后**再也不读**(但 TCP 连接保持打开), 也不回 pong
    print("  现在停止读取, 保持 TCP 连接打开(不回 pong), 最多等 90s...")
    b.reply_ping = False
    dropped, left = wait_drop(up, secs=90)
    chk("服务端把这条连接清掉了(x11vnc 上不再挂着)",
        dropped is not None, f"{dropped:.0f}s 后剩 {left}" if dropped else
        f"90s 后仍是 {left} 个")
    if dropped:
        chk("清理时机合理(≤70s)", dropped <= 70, f"{dropped:.0f}s")
    b.close(graceful=False)
    return base


def case_normal_read():
    """对照组: 正常读、正常关 —— 应该立刻干净。"""
    print("【2】对照组: 正常读取 + 正常关闭")
    base, _ = V.api_clients()
    b = V.Bridge()
    w, h = b.handshake()
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)
    for _ in range(10):
        b.send(req)
        try:
            b.recv_frame()
        except socket.timeout:
            pass
        time.sleep(0.05)
    b.close()
    dt, left = wait_drop(base, secs=10)
    chk("正常关闭后立刻回到原值", left == base, f"{base} → {left}"
        + (f" ({dt:.1f}s)" if dt is not None else ""))
    return base


def main():
    V.CK = V.cookie()
    c0, b0 = V.api_clients()
    print(f"初始: x11vnc 客户端 {c0} 个 / 登记桥接 {b0} 个")
    if c0:
        print("  注: 已存在遗留连接, 下面看的是**增量**")
    print()
    case_normal_read()
    print()
    case_no_read()
    print()
    c1, b1 = V.api_clients()
    print(f"结束: x11vnc 客户端 {c1} 个 / 登记桥接 {b1} 个")
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
