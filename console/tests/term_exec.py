#!/usr/bin/env python3
"""通过控制台的 /ws/term 在**真实用户会话**里执行只读命令并取回输出。

为什么需要它: 我的 shell 跑在隔离的挂载/PID 命名空间里, 看不到 /run/user、
/dev/snd, 也连不上 PulseAudio, 更看不到宿主机进程。而控制台服务是跑在真实
会话里的 —— 借它的终端就能拿到真实环境的信息。

用法:
    python3 tests/term_exec.py "pactl info" "ls /usr/share/alsa/ucm2"
    python3 tests/term_exec.py --file cmds.txt
只发只读命令, 不要用它改系统。
"""
import base64
import http.client
import os
import re
import socket
import struct
import sys
import time

HOST, PORT = "127.0.0.1", 8390
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import login_body  # noqa: E402


def ws_frames(sock, seconds):
    """收 WebSocket 帧, 返回拼起来的 payload(只取数据帧)。"""
    sock.settimeout(0.5)
    buf, out = b"", b""
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            d = sock.recv(65536)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        while len(buf) >= 2:
            op = buf[0] & 0x0F
            ln = buf[1] & 0x7F
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
            if op in (0x1, 0x2):
                out += payload
            elif op == 0x8:
                return out
    return out


def send_text(sock, s):
    """发一个客户端文本帧(带 mask)。

    长度字段必须按 WebSocket 规范分档: <126 直接放; 126 后跟 2 字节;
    127 后跟 8 字节。**只写 7 位会在命令超过 127 字节时直接报
    "bytes must be in range(0, 256)"**(踩过 —— 短命令能跑, 长命令就崩)。
    """
    pay = s.encode()
    m = os.urandom(4)
    n = len(pay)
    if n < 126:
        hdr = bytes([0x81, 0x80 | n])
    elif n < 65536:
        hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(hdr + m + bytes(b ^ m[i % 4] for i, b in enumerate(pay)))


def run(cmds, timeout=25, cols=200, rows=60):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    if not ck:
        raise SystemExit("登录失败")

    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET /ws/term HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\nCookie: {ck}\r\n"
        f"Origin: http://{HOST}:{PORT}\r\n\r\n").encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        raise SystemExit(f"WS 握手失败: {resp[:120]}")

    send_text(s, _json({"t": "r", "c": cols, "r": rows}))
    ws_frames(s, 1.0)                      # 丢掉提示符
    # 用一串唯一的标记把每条命令的输出夹起来。
    # 注意结尾必须是**真的换行符** —— 写成字面的 "\n"(反斜杠+n) bash 不会执行,
    # 只会把它当普通字符回显出来(踩过)。
    for i, cmd in enumerate(cmds):
        line = f"echo __B{i}__; {cmd}; echo __E{i}__\n"
        send_text(s, _json({"t": "i", "d": line}))
        time.sleep(0.4)
    raw = ws_frames(s, timeout)
    try:
        send_text(s, '{"t":"i","d":"exit\\n"}')
    except OSError:
        pass
    s.close()
    return raw


def _json(s):
    import json
    return json.dumps(s)


def clean(raw):
    """去掉 ANSI 转义和回显, 按标记切段。"""
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", t)
    t = re.sub(r"\x1b\][^\x07]*\x07", "", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t


if __name__ == "__main__":
    argv = sys.argv[1:]
    wait = 25.0
    if "--timeout" in argv:                 # 有些命令要跑十几秒, 得能调
        i = argv.index("--timeout")
        wait = float(argv[i + 1])
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    out = clean(run(args, timeout=wait))
    for i, cmd in enumerate(args):
        # **必须取最后一个匹配**: 终端会把输入回显出来, 回显行里也含 __B/__E 标记,
        # 第一个匹配拿到的是回显(踩过), 真正带输出的是后面那个。
        ms = re.findall(rf"__B{i}__(.*?)__E{i}__", out, re.S)
        body = (ms[-1] if ms else "").strip()
        lines = [ln for ln in body.split("\n")
                 if ln.strip() and ln.strip() != cmd.strip()
                 and f"__B{i}__" not in ln and f"__E{i}__" not in ln]
        print(f"\n$ {cmd}")
        print("\n".join(lines).strip() or "  (空)")
