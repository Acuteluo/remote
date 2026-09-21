#!/usr/bin/env python3
"""摄像头链路自检。

真实摄像头在受限环境里看不到(/dev/video* 不存在于容器/沙箱), 所以用 ffmpeg 的
lavfi 虚拟源(testsrc)代替, 把"采集 -> MJPEG 分帧 -> WebSocket 推送"整条链路跑通。
真实设备只需要设备节点存在, 走的完全是同一条代码路径。

重点验证三件事:
  1. 能出帧, 且每帧都是完整 JPEG
  2. 断开后 ffmpeg 真的死了(没人看就不该继续占着摄像头)
  3. **快速重连不会 busy** —— 刷新页面/切后台回来都走这条路径,
     摄像头是独占设备, 上一个没释放完就开新的必然失败。

用法(在隔离目录里另起实例, 不碰线上 8390):
    python3 tests/cam_check.py [port]
"""
import base64
import http.client
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18921
HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
# 测试实例单独写一份审计日志, 别往线上那份里掺
AUDIT_TMP = os.path.join(tempfile.gettempdir(), f"cam_check-{PORT}.audit")

WRAPPER = """
import importlib.util
spec = importlib.util.spec_from_file_location('srv', {server!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

# 0) 审计日志写别处(否则会污染线上 audit.log)
m.AUDIT_FILE = {audit!r}

# 1) 假装有一个设备节点
m.cam_devices = lambda *a, **k: ['/dev/video0']

# 2) 把 ffmpeg 的 v4l2 采集换成 lavfi 虚拟源(无摄像头环境下也能验证链路)。
#    注意 -re: 不加的话 ffmpeg 输出到管道会**全速**编码(实测 3 秒 8000+ 帧),
#    直接把 CPU 吃满, 连累同一台机器上正在跑的真实服务(终端握手都会超时)。
def fake_attempts(dev, size, fps):
    return [[
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-re',
        '-f', 'lavfi', '-i', f'testsrc=size={{size}}:rate={{fps}}',
        '-c:v', 'mjpeg', '-q:v', '5', '-f', 'mjpeg', '-',
    ]]
m._cam_attempts = fake_attempts

# 3) 声音: 用 lavfi 的 sine 当虚拟麦克风
#    注意要同时 mock 选源(audio_pick_source)和命令组装(_audio_cmd),
#    否则服务端会真的去试 pulse/alsa, 在容器里必然失败。
SINE = 'sine=frequency=440:sample_rate=24000'
m.audio_pick_source = lambda *a, **k: ('lavfi', SINE, [])
m._audio_cmd = lambda kind, src: [
    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-re',
    '-f', 'lavfi', '-i', src,
    '-ac', '1', '-ar', '24000', '-c:a', 'libmp3lame', '-b:a', '48k',
    '-f', 'mp3', '-',
]
m.PORT = {port}

# 4) 把"前端心跳超时"压到几秒, 自检里就能快速验完(生产是 20s)
m.CAM_PING_EVERY = 1.0
m.CAM_VIEWER_TIMEOUT = 4.0
m.main()
"""


def count_ffmpeg(pattern):
    """数一数真正在跑的 ffmpeg 进程。

    **不能简单用 pgrep -f**: 启动服务用的 `python3 -c '<wrapper>'` 命令行里
    就含这些参数, 会被一起匹配进来(实测误报成"ffmpeg 还在跑")。
    必须过滤掉 python/bash, 只认 ffmpeg。
    """
    out = subprocess.run(["pgrep", "-af", pattern],
                         capture_output=True, text=True).stdout
    n = 0
    for line in out.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        cmd = parts[1]
        if "ffmpeg" in cmd and "python" not in cmd and "bash" not in cmd:
            n += 1
    return n


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def connect(cookie, seconds=3.0, size="320x240", fps=10):
    """连一次 /ws/cam, 返回 (socket, 帧列表, 文本消息列表)。"""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET /ws/cam?size={size}&fps={fps} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\nCookie: {cookie}\r\n"
        f"Origin: http://127.0.0.1:{PORT}\r\n\r\n").encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        s.close()
        return None, [], ["握手失败"]

    s.settimeout(1.0)
    buf, jpgs, texts = b"", [], []
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            d = s.recv(65536)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        while len(buf) >= 2:
            op = buf[0] & 0x0F
            ln = buf[1] & 0x7F
            # 长度字段: <126 直接用; ==126 后跟 2 字节; ==127 后跟 8 字节
            hdr = 2 + (2 if ln == 126 else (8 if ln == 127 else 0))
            if len(buf) < hdr:
                break
            if ln == 126:
                ln = struct.unpack(">H", buf[2:4])[0]
            elif ln == 127:
                ln = struct.unpack(">Q", buf[2:10])[0]
            if len(buf) < hdr + ln:
                break
            payload = buf[hdr:hdr + ln]
            buf = buf[hdr + ln:]
            if op == 0x2:
                jpgs.append(payload)
            elif op == 0x1:
                texts.append(payload.decode("utf-8", "replace"))
    return s, jpgs, texts


ok = True
proc = subprocess.Popen(
    [sys.executable, "-c",
     WRAPPER.format(server=os.path.join(CONSOLE, "server.py"),
                    port=PORT, audit=AUDIT_TMP)],
    cwd=CONSOLE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    if not wait_port(PORT):
        print("  ✗ 实例没起来:", proc.stderr.read().decode("utf-8", "replace")[:400])
        sys.exit(1)
    print(f"  ✓ 实例已在 127.0.0.1:{PORT} 启动")

    sys.path.insert(0, HERE)
    from _auth import login_body
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    CK = (r.getheader("Set-Cookie") or "").split(";")[0]
    if not CK:
        print("  ✗ 登录失败")
        sys.exit(1)
    print("  ✓ 登录成功")

    # ---- 1) 第一次连接 ----
    sock1, jpgs1, texts1 = connect(CK)
    if sock1 is None:
        print("  ✗ WS 握手失败:", texts1)
        sys.exit(1)
    print(f"  ✓ 第一次连接: 收到 {len(jpgs1)} 帧")
    for t in texts1[:2]:
        print(f"      文本: {t[:80]}")

    bad = [i for i, j in enumerate(jpgs1)
           if not (j[:2] == b"\xff\xd8" and j[-2:] == b"\xff\xd9")]
    if jpgs1 and not bad:
        print(f"  ✓ 全部 {len(jpgs1)} 帧都是完整 JPEG(FFD8…FFD9), "
              f"平均 {sum(len(j) for j in jpgs1) // len(jpgs1)} 字节")
    else:
        print(f"  ✗ 帧不完整({len(bad)}/{len(jpgs1)}) 或一帧都没有")
        ok = False

    # ---- 2) 断开后 ffmpeg 应该死掉 ----
    ff_before = count_ffmpeg("testsrc=size=320x240")
    try:
        sock1.close()
    except OSError:
        pass
    time.sleep(1.5)
    ff_after = count_ffmpeg("testsrc=size=320x240")
    print(f"  断开前 ffmpeg: {ff_before} 个 -> 断开后: {ff_after} 个")
    if ff_before and not ff_after:
        print("  ✓ 连接断开后 ffmpeg 已停止(没人看就不会继续占着摄像头)")
    elif not ff_before:
        print("  ⚠ 采集期间没看到 ffmpeg 进程(pgrep 不可用?), 跳过该项")
    else:
        print("  ✗ ffmpeg 仍在跑 —— 摄像头会被一直占用")
        ok = False

    # ---- 3) 快速重连: 最容易 busy 的路径 ----
    time.sleep(0.3)                      # 故意只等很短, 模拟刷新页面
    sock2, jpgs2, texts2 = connect(CK, seconds=2.5)
    if sock2 is None:
        print("  ✗ 快速重连: 握手失败")
        ok = False
    else:
        errs = [t for t in texts2 if '"err"' in t]
        if jpgs2 and not errs:
            print(f"  ✓ 快速重连成功, 又收到 {len(jpgs2)} 帧(没有 busy)")
        else:
            print(f"  ✗ 快速重连失败: 帧={len(jpgs2)} err={errs[:1]}")
            ok = False
        try:
            sock2.close()
        except OSError:
            pass

    # ---- 3.5) 不发心跳、也不断开: 服务端必须自己把摄像头停掉 ----
    # 就是用户报的那个场景 —— 页面切到后台 / 手机锁屏 / 离开后 webview 没发 FIN,
    # 连接还在(甚至还在收画面), 但前端 JS 早被挂起、不再发心跳。
    # 2026-09-22 修之前这里**永远不会停**: 服务端拿 WebSocket 的 pong 当"还有人在看"
    # 的判据, 而浏览器/系统网络栈在 JS 挂起后照样会自动回 pong, 于是永远判不出
    # "人已经走了", 摄像头指示灯一直亮着, 和页面上"没人观看立即停止"的承诺不符。
    s3, jpgs3, _t3 = connect(CK, seconds=2.0)
    if s3 is None:
        print("  ✗ 心跳用例: 握手失败")
        ok = False
    else:
        ff_live = count_ffmpeg("testsrc=size=320x240")
        s3.settimeout(1.0)
        t0, dropped = time.time(), False
        while time.time() - t0 < 20:          # 服务端那个超时已压到 4s
            try:
                if not s3.recv(65536):        # 还在收画面 == TCP 活着且可写
                    dropped = True
                    break
            except socket.timeout:
                continue
            except OSError:
                dropped = True
                break
        waited = time.time() - t0
        ff_idle = count_ffmpeg("testsrc=size=320x240")
        print(f"  不发心跳也不断开: {waited:.1f}s 后 "
              f"{'服务端已断开' if dropped else '连接仍在'}, "
              f"ffmpeg {ff_live} -> {ff_idle}")
        if not ff_live:
            print("  ⚠ 采集期间没看到 ffmpeg 进程(pgrep 不可用?), 跳过该项")
        elif dropped and not ff_idle:
            print("  ✓ 前端心跳一停就自己停采(切后台/锁屏不会白开着摄像头)")
        else:
            print("  ✗ 没人看了还在采 —— 摄像头会一直亮着")
            ok = False
        try:
            s3.close()
        except OSError:
            pass
        time.sleep(1.0)

    # ---- 4) 声音: HTTP 流式 MP3 ----
    ac = http.client.HTTPConnection("127.0.0.1", PORT, timeout=12)
    ac.request("GET", "/api/audio", None, {"Cookie": CK})
    ar = ac.getresponse()
    ctype = ar.getheader("Content-Type") or ""
    print(f"  /api/audio HTTP {ar.status}  Content-Type={ctype}")
    if ar.status != 200:
        print("  ✗ 声音接口没起来:", ar.read()[:200])
        ok = False
    else:
        data = ar.read(6000)          # 读一段就够判断格式
        # MP3: ID3 头, 或 0xFF 开头的帧同步
        is_mp3 = data[:3] == b"ID3" or (
            len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
        print(f"  读到 {len(data)} 字节, MP3 格式: {'✓' if is_mp3 else '✗ ' + data[:8].hex()}")
        if not is_mp3:
            ok = False
        ac.close()                    # 断开 -> 服务端应该停采
        time.sleep(2.0)
        af = count_ffmpeg("sine=frequency=440")
        print(f"  断开后音频 ffmpeg: {af} 个 "
              f"{'✓ 已停止' if not af else '✗ 仍在跑'}")
        if af:
            ok = False

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n结论:", "摄像头链路可用 ✓" if ok else "有问题 ✗")
sys.exit(0 if ok else 1)
