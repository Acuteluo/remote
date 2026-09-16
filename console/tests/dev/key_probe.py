#!/usr/bin/env python3
"""验证"键盘输入好像有长度限制"—— 实际是 x11vnc 在高频输入下丢键。

x11vnc 文档(-pointer_mode, 默认 2):
    "Note that modes 2, 3, 4 will skip -input_skip keyboard events"
即: 默认配置下, 为了让画面跟上, x11vnc 会**丢弃键盘事件**。快速敲/粘贴一长串时,
部分字符就没了 —— 用起来就像"输入有长度上限"。
另外文档明确: 这些模式 "are not available in -threads mode"。

本测试: 通过 RFB 连发一串字符(和前端 sendChars 一样的发法), 用 xev 窗口统计到底
收到了几个, 对比三种配置。

安全: 输入靶子是 xev 窗口, 发按键前必须确认焦点已落在它上面, 否则直接跳过。
"""
import os
import re
import socket
import struct
import subprocess
import sys
import time

PORT = 5906
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {**os.environ, "DISPLAY": ":0"}
TEXT = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
fails = []

def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)
    if not cond:
        fails.append(name)

def args(extra):
    return (["/usr/bin/x11vnc", "-display", ":0", "-noshm", "-forever", "-shared",
             "-localhost", "-nopw", "-xkb", "-rfbport", str(PORT), "-scale", "1/2",
             "-quiet"] + extra)

def rx(s, n, dl):
    b = b""
    while len(b) < n:
        s.settimeout(max(.05, dl - time.time()))
        c = s.recv(n - len(b))
        if not c: raise ConnectionError
        b += c
    return b

def handshake():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10); dl = time.time() + 10
    rx(s, 12, dl); s.sendall(b"RFB 003.008\n")
    n = rx(s, 1, dl)[0]; rx(s, n, dl); s.sendall(bytes([1]))
    assert struct.unpack(">I", rx(s, 4, dl))[0] == 0
    s.sendall(bytes([1])); h = rx(s, 24, dl)
    fw, fh = struct.unpack(">HH", h[:4]); nl = struct.unpack(">I", h[20:24])[0]; rx(s, nl, dl)
    return s, fw, fh

def send_text(s, text):
    """和 static/vnc.js 的 sendChars 完全同构: 每字符一组 key down/up, 无间隔。"""
    buf = bytearray()
    for ch in text:
        ks = ord(ch)
        buf += struct.pack(">BBHI", 4, 1, 0, ks)
        buf += struct.pack(">BBHI", 4, 0, 0, ks)
    s.sendall(bytes(buf))

def parse_presses(log):
    blocks = re.split(r'(?=^KeyPress event|^KeyRelease event)', log, flags=re.M)
    seq = []
    for b in blocks:
        if not b.startswith("KeyPress event"):
            continue
        m = re.search(r"keysym (0x[0-9a-f]+)", b)
        if m:
            seq.append(int(m.group(1), 16))
    return seq

def run(label, extra, xevlog):
    subprocess.run(["pkill", "-f", "xev -name LUO_KEYTEST"], capture_output=True)
    if os.path.exists(xevlog): os.remove(xevlog)
    xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_KEYTEST"], env=ENV,
                           stdout=open(xevlog, "w"), stderr=subprocess.DEVNULL)
    wid = ""
    for _ in range(40):
        parts = subprocess.run(["xdotool", "search", "--name", "LUO_KEYTEST"],
                               capture_output=True, text=True, env=ENV).stdout.split()
        if parts: wid = parts[0]; break
        time.sleep(0.25)
    if not wid:
        xev.kill(); return None, "xev 没起来"
    subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=ENV, capture_output=True)
    time.sleep(0.5)
    cur = subprocess.run(["xdotool", "getwindowfocus"], capture_output=True, text=True,
                         env=ENV).stdout.strip()
    if cur != wid:
        xev.terminate(); return None, f"焦点没拿到({cur}!={wid})"

    proc = subprocess.Popen(args(extra), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    got = None
    try:
        s, fw, fh = handshake()
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, fw, fh))
        time.sleep(1.0)
        try:
            while True:
                s.settimeout(0.4); s.recv(262144)
        except socket.timeout:
            pass
        time.sleep(0.4)
        send_text(s, TEXT)
        time.sleep(2.5)
        log = open(xevlog, encoding="utf-8", errors="replace").read()
        seq = parse_presses(log)
        got = seq
        s.close()
    except Exception as e:
        print(f"    {label} 异常 {e!r}", flush=True)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        xev.terminate()
        try: xev.wait(timeout=3)
        except Exception: xev.kill()
    return got, None


XEVLOG = os.path.join(HERE, "artifacts", "xev_key.log")
TEXT = "abcdefghijklmnopqrstuvwxyz" * 12          # 312 字符, 全小写(不产生 Shift 噪声)
want = [ord(c) for c in TEXT]
MODS = {0xffe1, 0xffe2, 0xffe3, 0xffe4, 0xffe7, 0xffe8, 0xffe9, 0xffea, 0xffeb, 0xffec}
print(f"连发 {len(TEXT)} 个小写字符, 看实际到达几个 (期望 {len(TEXT)})\n", flush=True)

results = {}
for label, extra in [
    ("A 旧配置(无 -threads / 无 -allinput)", ["-wait", "5", "-defer", "5"]),
    ("B 当前(加了 -threads)", ["-wait", "5", "-defer", "5", "-threads"]),
    ("C 再加 -allinput", ["-wait", "5", "-defer", "5", "-threads", "-allinput"]),
]:
    seq, err = run(label, extra, XEVLOG)
    if err:
        print(f"  {label:34s} 跳过: {err}", flush=True)
        continue
    letters = [v for v in seq if v not in MODS]
    n = len(letters)
    # 按"子序列"比对: 判断到达的字符是否按原顺序出现(丢了哪些)
    it = iter(letters)
    ok = sum(1 for w in want if w in it)
    results[label[0]] = n
    print(f"  {label:34s} 到达 {n:3d}/{len(want)}  按序命中 {ok:3d}/{len(want)}",
          flush=True)

print()
if "A" in results and "C" in results:
    check("旧配置在长串连发下确实丢键", results["A"] < len(want),
          f"只到了 {results['A']}/{len(want)}")
    check("-allinput 后不再丢键", results["C"] >= len(want),
          f"到达 {results['C']}/{len(want)}")
if "B" in results:
    print(f"  参考: -threads 单独作用 -> {results['B']}/{len(want)}")

print("\n" + ("符合预期" if not fails else f"未达预期: {fails}"))
