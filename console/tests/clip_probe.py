#!/usr/bin/env python3
"""端到端验证剪贴板修复(直接跑在真实 X 会话上)。

验证:
  1. 服务端 xclip 路径: 中文 + ASCII 写入/读回是否一字不差
  2. 对比旧路径: 经 noVNC 的 ClientCutText 发中文, 是否真的变成 '?'(复现原 bug)
  3. /api/paste: 设剪贴板 + Ctrl+V 是否真的发出了按键(用 xev 窗口收键事件)
  4. /api/type: xdotool 直接键入中文是否可用(终端场景)

安全措施: 输入靶子用 xev 窗口, 发按键前**必须**确认焦点已落在它上面, 拿不到焦点就
直接跳过输入类测试 —— 否则按键会打到用户正在用的窗口里。
结束后关闭 xev 并把焦点/剪贴板还原。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import base64
import http.client
import importlib.util
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import http.server
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
ART = os.path.join(HERE, "artifacts")
os.makedirs(ART, exist_ok=True)
sys.path.insert(0, CONSOLE)
ENV = {**os.environ, "DISPLAY": ":0"}

_s = socket.socket(); _s.bind(("127.0.0.1", 0)); PORT = _s.getsockname()[1]; _s.close()

spec = importlib.util.spec_from_file_location("meow_console", os.path.join(CONSOLE, "server.py"))
srv_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv_mod)
srv_mod.PORT = PORT

fails = []
def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        fails.append(name)

def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True,
                          env=ENV).stdout.strip()

def xclip_get():
    r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                       capture_output=True, env=ENV, timeout=5)
    return r.stdout.decode("utf-8", "replace")

httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), srv_mod.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=25)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, data, r.getheader("Set-Cookie"))
    c.close()
    return out


_, _, setc = req("POST", "/login", login_body(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
CK = {"Cookie": (setc or "").split(";")[0]}
JSON = {**CK, "Content-Type": "application/json"}
check("登录", CK["Cookie"].startswith("meow_session="))

saved_clip = xclip_get()
saved_win = xdo("getwindowfocus")
print(f"已保存: 剪贴板 {len(saved_clip)} 字, 焦点窗口 {saved_win} "
      f"({xdo('getwindowname', saved_win) if saved_win else ''})\n")

xev = None
xevlog = os.path.join(ART, "xev.log")
try:
    # ---- 1) 新路径: 服务端 xclip, 中文必须一字不差 ----
    print("== 1) 服务端 xclip 路径(新) ==")
    SAMPLE = "中文剪贴板测试 ABC123 !@# 换行\n第二行"
    st, body, _ = req("POST", "/api/clipboard", json.dumps({"text": SAMPLE}), JSON)
    check("POST /api/clipboard", st == 200 and json.loads(body).get("ok"), body[:100])
    st, body, _ = req("GET", "/api/clipboard", headers=CK)
    got = json.loads(body).get("text", "")
    check("GET /api/clipboard 中文往返一致", got == SAMPLE,
          f"发出 {len(SAMPLE)} 字 / 读回 {len(got)} 字")
    check("xclip 直接读取也一致", xclip_get() == SAMPLE, repr(xclip_get()[:30]))

    # ---- 2) 旧路径对比: noVNC ClientCutText 中文会变 '?' ----
    print("\n== 2) 旧路径对比(noVNC ClientCutText, 复现原 bug) ==")
    ws = socket.create_connection(("127.0.0.1", PORT), timeout=15)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    ws.sendall((f"GET /ws/vnc HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: binary\r\nCookie: {CK['Cookie']}\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += ws.recv(4096)
    head, _, buf = resp.partition(b"\r\n\r\n")
    assert b"101" in head.split(b"\r\n")[0], head

    def ws_frame(data):
        mask = secrets.token_bytes(4)
        n = len(data)
        hdr = bytearray([0x82])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        ws.sendall(bytes(hdr) + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))

    def read_n(n):
        global buf
        while len(buf) < n:
            d = ws.recv(65536)
            if not d: raise ConnectionError("closed")
            buf += d
        out, buf = buf[:n], buf[n:]
        return out

    def read_frame():
        h = read_n(2)
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126: n = struct.unpack(">H", read_n(2))[0]
        elif n == 127: n = struct.unpack(">Q", read_n(8))[0]
        return op, (read_n(n) if n else b"")

    def read_bin(n):
        global buf
        while len(buf) < n:
            op, d = read_frame()
            if op == 2: buf += d
            elif op == 9: ws.sendall(b"\x8a\x80" + secrets.token_bytes(4))
            elif op == 8: raise ConnectionError("ws closed")
        out, buf = buf[:n], buf[n:]
        return out

    read_bin(12)
    ws_frame(b"RFB 003.008\n")
    nn = read_bin(1)[0]; read_bin(nn)
    ws_frame(bytes([1]))
    read_bin(4)
    ws_frame(bytes([1]))
    h24 = read_bin(24); nl = struct.unpack(">I", h24[20:24])[0]; read_bin(nl)

    # 复刻 noVNC clipboardPasteFrom 的编码: 非 8859-1 一律替换成 '?'
    payload = bytes((ord(c) if ord(c) <= 0xff else 0x3f) for c in "中文ABC")
    ws_frame(struct.pack(">B3xI", 6, len(payload)) + payload)
    time.sleep(1.0)
    old = xclip_get()
    check("旧路径确实把中文变成 '?'", "中" not in old and "?" in old, repr(old[:30]))
    ws.close()

    # ---- 3/4) 用 xev 窗口当输入靶子 ----
    print("\n== 3/4) 按键类接口(靶子 = xev 窗口) ==")
    if os.path.exists(xevlog):
        os.remove(xevlog)
    xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_CLIPTEST"], env=ENV,
                           stdout=open(xevlog, "w"), stderr=subprocess.DEVNULL)
    wid = ""
    for _ in range(40):
        parts = xdo("search", "--name", "LUO_CLIPTEST").split()
        if parts:
            wid = parts[0]; break
        time.sleep(0.25)
    focused = False
    if wid:
        xdo("windowfocus", "--sync", wid)
        time.sleep(0.5)
        focused = (xdo("getwindowfocus") == wid)
    check("测试窗口拿到焦点(拿不到就不发按键, 避免误伤你在用的窗口)", focused, f"wid={wid}")

    if focused:
        PASTE = "粘贴中文XYZ"
        st, body, _ = req("POST", "/api/paste", json.dumps({"text": PASTE}), JSON)
        check("POST /api/paste", st == 200 and json.loads(body).get("ok"), body[:120])
        time.sleep(1.0)
        log = open(xevlog, encoding="utf-8", errors="replace").read()
        check("Ctrl+V 真的发出去了", "Control_L" in log and "keysym 0x76" in log,
              f"Control_L x{log.count('Control_L')}")

        mark = len(log)
        TYPE = "键入中文ABC"
        st, body, _ = req("POST", "/api/type", json.dumps({"text": TYPE}), JSON)
        check("POST /api/type", st == 200 and json.loads(body).get("ok"), body[:120])
        time.sleep(1.2)
        log2 = open(xevlog, encoding="utf-8", errors="replace").read()[mark:]
        want = {"0x100952e": "键", "0x1005165": "入", "0x1004e2d": "中",
                "0x1006587": "文", "0x41": "A", "0x42": "B", "0x43": "C"}
        miss = [f"{ch}({ks})" for ks, ch in want.items() if ks not in log2]
        check("直接键入中文逐字到达", not miss, f"缺失: {miss}" if miss else "7/7 字符命中")
        check("粘贴前剪贴板确实被设为待粘内容", PASTE in xclip_get(),
              repr(xclip_get()[:16]))

    st, body, _ = req("POST", "/api/type", json.dumps({"text": ""}), JSON)
    check("POST /api/type 空参数被拒", json.loads(body)["ok"] is False)

finally:
    if xev:
        xev.terminate()
        try: xev.wait(timeout=4)
        except subprocess.TimeoutExpired: xev.kill()
    if saved_clip:
        subprocess.run(["xclip", "-selection", "clipboard"],
                       input=saved_clip.encode(), env=ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if saved_win:
        xdo("windowfocus", "--sync", saved_win)
    httpd.shutdown()
    print("已还原剪贴板与焦点")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
