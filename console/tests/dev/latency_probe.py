#!/usr/bin/env python3
"""量化 x11vnc "空闲后变慢" 到底有多严重, 以及怎么修。

测法: 通过 RFB 发一个 PointerEvent, 然后轮询真实指针位置(xdotool), 量到"指针真的
动了"为止的耗时。这样测的是**输入处理延迟** —— 正是"响应慢"里最影响手感的部分,
而且不受画面噪声干扰(桌面永远在闪, 用帧缓冲测会被无关更新污染)。

怀疑对象(x11vnc 默认值):
  -nap     活动少时"打盹", 拉长屏幕轮询间隔
  -sb 60   屏幕静止 60 秒后, 轮询间隔降到约 1.5 秒 —— 主循环睡那么久, 客户端发来的
           指针事件也要等它醒过来才处理, 表现就是"停一会儿再动, 第一下卡一下"

在临时端口跑 x11vnc, 不碰用户正在用的服务。
"""
import os
import socket
import struct
import subprocess
import sys
import time

PORT = 5903
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {**os.environ, "DISPLAY": ":0"}

fails = []
def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)
    if not cond:
        fails.append(name)


def base_args(extra):
    return (["/usr/bin/x11vnc", "-display", ":0", "-noshm", "-forever", "-shared",
             "-threads", "-localhost", "-nopw", "-xkb", "-rfbport", str(PORT),
             "-scale", "1/2", "-quiet"] + extra)


def read_exact(s, n, dl):
    buf = b""
    while len(buf) < n:
        s.settimeout(max(0.05, dl - time.time()))
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


def handshake(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    dl = time.time() + 10
    read_exact(s, 12, dl)
    s.sendall(b"RFB 003.008\n")
    n = read_exact(s, 1, dl)[0]
    types = read_exact(s, n, dl)
    assert 1 in types, types
    s.sendall(bytes([1]))
    assert struct.unpack(">I", read_exact(s, 4, dl))[0] == 0
    s.sendall(bytes([1]))
    h = read_exact(s, 24, dl)
    fw, fh = struct.unpack(">HH", h[:4])
    nl = struct.unpack(">I", h[20:24])[0]
    read_exact(s, nl, dl)
    enc = [7, 16, 5, 0]
    s.sendall(struct.pack(">BBH", 2, 0, len(enc)) +
              b"".join(struct.pack(">i", e) for e in enc))
    return s, fw, fh


def real_pointer():
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True,
                       text=True, env=ENV)
    d = dict(p.split(":") for p in r.stdout.split() if ":" in p)
    return int(d["x"]), int(d["y"])


def pointer(s, x, y):
    s.sendall(struct.pack(">BBHH", 5, 0, x, y))


def drain_quiet(s, quiet=0.3, max_wait=3.0):
    end = time.time() + max_wait
    while time.time() < end:
        s.settimeout(quiet)
        try:
            if not s.recv(262144):
                return
        except socket.timeout:
            return


def input_latency(s, fw, fh, tx, ty, idle=0.0):
    """返回 (毫秒, 真实指针坐标); 超时返回 (None, None)。"""
    if idle:
        time.sleep(idle)
    t0 = time.perf_counter()
    pointer(s, tx, ty)
    want = (tx * 2, ty * 2)                 # -scale 1/2 => 真实坐标 x2
    while time.perf_counter() - t0 < 6.0:
        rx, ry = real_pointer()
        if abs(rx - want[0]) <= 3 and abs(ry - want[1]) <= 3:
            return (time.perf_counter() - t0) * 1000.0, (rx, ry)
    return None, None


def run_config(label, extra, idle=0.0, trials=2):
    proc = subprocess.Popen(base_args(extra), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    time.sleep(2.5)
    if proc.poll() is not None:
        print(f"  [{label}] 启动失败: "
              f"{proc.stderr.read().decode('utf-8','replace')[:160]}", flush=True)
        return None
    lat = []
    try:
        s, fw, fh = handshake(PORT)
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, fw, fh))
        drain_quiet(s, 0.4, 6.0)
        for i in range(trials):
            # 目标交替, 保证每次都是真的移动
            tx = 200 + (i % 2) * 260
            ty = 300 + (i % 2) * 180
            v, pos = input_latency(s, fw, fh, tx, ty, idle=idle)
            if v is not None:
                lat.append(v)
            else:
                print(f"    (第{i+1}次超时未到位)", flush=True)
        s.close()
    except Exception as e:
        print(f"  [{label}] 异常 {e!r}", flush=True)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
    if not lat:
        return None
    lat.sort()
    med = lat[len(lat) // 2]
    print(f"  {label:34s} 中位 {med:8.1f}ms  {['%.0f' % v for v in lat]}  n={len(lat)}",
          flush=True)
    return med


# 基线: xdotool getmouselocation 自身开销, 用来判断测量分辨率
t = time.perf_counter()
for _ in range(10):
    real_pointer()
base_ms = (time.perf_counter() - t) / 10 * 1000
print(f"测量基线: xdotool 轮询一次 {base_ms:.1f}ms (所以 <{base_ms*2:.0f}ms 的差异无意义)\n",
      flush=True)

print("空闲后第一个指针事件的生效延迟:\n", flush=True)
a = run_config("A 默认(nap+sb60) 空闲8s", ["-wait", "5", "-defer", "5"], idle=8.0)
b = run_config("B -sb 4 空闲8s (模拟生产60s后)", ["-wait", "5", "-defer", "5", "-sb", "4"], idle=8.0)
d = run_config("D -sb 0 -nonap 空闲8s", ["-wait", "5", "-defer", "5", "-sb", "0", "-nonap"], idle=8.0)

print("\n再验证生产真实场景(默认 -sb 60, 空闲 65 秒):\n", flush=True)
e = run_config("E 默认(nap+sb60) 空闲65s", ["-wait", "5", "-defer", "5"], idle=65.0, trials=1)
f = run_config("F -sb 0 -nonap 空闲65s", ["-wait", "5", "-defer", "5", "-sb", "0", "-nonap"],
               idle=65.0, trials=1)

print()
if e is not None and f is not None:
    check("生产配置空闲65s后确实很慢", e > 300, f"{e:.0f}ms")
    check("关掉 -sb/-nap 后恢复正常", f < max(60, base_ms * 3), f"{f:.0f}ms")
    check("改善幅度显著", f < e / 3, f"{e:.0f}ms -> {f:.0f}ms")

print("\n" + ("符合预期" if not fails else f"未达预期: {fails}"))
