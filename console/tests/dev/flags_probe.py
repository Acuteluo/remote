#!/usr/bin/env python3
"""在临时端口上验证新版 x11vnc 启动参数, 不碰用户正在跑的服务。

验证:
  1. 新参数(去掉 -noxdamage / 加 -threads)能否正常启动并完成 RFB 握手
  2. DAMAGE 变更检测是否仍然工作 —— 指针移动后客户端必须收到新的帧缓冲数据
     (这是"去掉 -noxdamage 会不会导致画面不刷新"的关键检查)
"""
import os
import socket
import struct
import subprocess
import sys
import time

PORT = 5901
# 沙箱与真实会话不完全同域(IPC 命名空间不同, MIT-SHM 挂不上; root 也锁不了
# Xauthority), 所以这里显式 -noshm, 且只在能读到 Xauthority 时才传 -auth。
# 这两点只影响本测试, 真实服务跑在用户会话里不受影响。
XAUTH_SRC = os.environ.get("XAUTHORITY", "/run/user/1000/gdm/Xauthority")
XAUTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts", "xauth_copy")
os.makedirs(os.path.dirname(XAUTH), exist_ok=True)
auth_args = []
try:
    with open(XAUTH_SRC, "rb") as f:
        data = f.read()
    with open(XAUTH, "wb") as f:
        f.write(data)
    os.chmod(XAUTH, 0o600)
    auth_args = ["-auth", XAUTH]
    print(f"使用 Xauthority: {XAUTH_SRC}")
except OSError as e:
    print(f"读不到 Xauthority({e}), 不带 -auth 继续")

NEW_ARGS = ["/usr/bin/x11vnc", "-display", ":0", *auth_args, "-noshm", "-forever",
            "-shared", "-threads", "-localhost", "-nopw", "-xkb", "-repeat",
            "-rfbport", str(PORT), "-wait", "5", "-defer", "5",
            "-scale", "1/2", "-quiet"]

fails = []
def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        fails.append(name)


def read_exact(s, n, deadline):
    buf = b""
    while len(buf) < n:
        s.settimeout(max(0.1, deadline - time.time()))
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


proc = subprocess.Popen(NEW_ARGS, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
time.sleep(2.5)
if proc.poll() is not None:
    err = proc.stderr.read().decode("utf-8", "replace")
    check("新参数 x11vnc 启动", False, err[:300])
    sys.exit(1)
check("新参数 x11vnc 启动", True, f"pid={proc.pid} port={PORT}")

try:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    dl = time.time() + 8
    ver = read_exact(s, 12, dl)
    check("RFB 版本", ver.startswith(b"RFB "), ver)
    s.sendall(b"RFB 003.008\n")
    n = read_exact(s, 1, dl)[0]
    types = read_exact(s, n, dl)
    check("安全类型(None)", 1 in types, list(types))
    s.sendall(bytes([1]))
    check("认证", struct.unpack(">I", read_exact(s, 4, dl))[0] == 0)
    s.sendall(bytes([1]))
    head = read_exact(s, 24, dl)
    fw, fh = struct.unpack(">HH", head[:4])
    nl = struct.unpack(">I", head[20:24])[0]
    read_exact(s, nl, dl)
    check("ServerInit 尺寸", fw > 0 and fh > 0, f"{fw}x{fh}")

    def fb_request(incremental):
        # FramebufferUpdateRequest: type 3, incremental, x, y, w, h
        s.sendall(struct.pack(">BBHHHH", 3, incremental, 0, 0, fw, fh))

    def drain(seconds):
        """返回(字节数, 帧数); 顺带应答 ping。"""
        total = 0
        frames = 0
        end = time.time() + seconds
        while time.time() < end:
            try:
                s.settimeout(max(0.05, end - time.time()))
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            total += len(d)
            frames += 1
        return total, frames

    # 注意: RFB 是"客户端先请求"的协议, 不先发 FramebufferUpdateRequest 服务端
    # 一个字节都不会发(这也是刚才 0B 的原因)。
    fb_request(0)
    first, _ = drain(3.0)
    check("收到初始全屏帧缓冲", first > 0, f"{first}B")

    fb_request(1)
    drain(1.0)
    before = drain(1.5)
    print(f"   静止画面 1.5s: {before[0]}B")

    # 让画面发生变化: 移动指针(带 -scale 时 x11vnc 会把指针画进缩放后的帧缓冲)
    subprocess.run(["xdotool", "mousemove", "600", "400"], capture_output=True)
    time.sleep(0.3)
    fb_request(1)
    after = drain(2.5)
    print(f"   指针移动后 2.5s: {after[0]}B")
    check("DAMAGE 能检测到画面变化(去掉 -noxdamage 后仍会推送)", after[0] > 0,
          f"{after[0]}B")

    # 再验证 -threads: 并发两个客户端都应能握手
    s2 = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    dl2 = time.time() + 8
    read_exact(s2, 12, dl2)
    s2.sendall(b"RFB 003.008\n")
    n2 = read_exact(s2, 1, dl2)[0]
    read_exact(s2, n2, dl2)
    s2.sendall(bytes([1]))
    ok2 = struct.unpack(">I", read_exact(s2, 4, dl2))[0] == 0
    s2.sendall(bytes([1]))
    h2 = read_exact(s2, 24, dl2)
    nl2 = struct.unpack(">I", h2[20:24])[0]
    read_exact(s2, nl2, dl2)
    check("-threads 下第二个客户端也能连", ok2, f"{struct.unpack('>HH', h2[:4])}")
    s2.close()
    s.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    subprocess.run(["xdotool", "mousemove", "2794", "848"], capture_output=True)
    print("临时 x11vnc 已退出, 指针已还原")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
