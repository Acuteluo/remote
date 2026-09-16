#!/usr/bin/env python3
"""端口转发器(lan_forward)自检: 空闲连接不能被掐断。

用法: DISPLAY=:0 python3 tests/forward_check.py

背景(2026-09-16 实测): 手机连的不是 127.0.0.1, 而是 `lan_forward.py` 镜像出来的
`网卡IP:8390`。原来的实现里
    remote = socket.create_connection((BACKEND, port), timeout=5)
把这个 5 秒**连接**超时留在了 socket 上, 于是**每静默 5 秒** recv 就超时,
被 except OSError 吞掉 → finally 里 shutdown → 连接断掉。
远程桌面静止时正好没数据 → 手机端每 5 秒断一次("画面一直重连")。

本脚本自己起一个**只绑回环的转发实例**(不碰线上服务), 验证:
  1. 经转发的 HTTP 正常, 且响应头改写没坏(第三方库长缓存要保住)
  2. 经转发的桥接连接**空闲 25 秒不掉**
  3. 经转发的桥接还能正常出画面
"""
import http.client
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # 仓库根: lan_forward.py
import vnc_stability_check as V                    # noqa: E402
import lan_forward                                  # noqa: E402

IDLE_SECS = 25
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def start_forwarder():
    """在回环上起一个转发实例: 127.0.0.1:<临时端口> -> 127.0.0.1:8390。"""
    srv = lan_forward.Server(("127.0.0.1", 0), lan_forward.make_handler(8390))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def http_through(port, path, cookie=None):
    h = {"Cookie": cookie} if cookie else {}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, None, h)
    r = c.getresponse()
    body = r.read()
    return r.status, dict(r.getheaders()), body


def main():
    V.CK = V.cookie()                    # 直接用回环登录, cookie 不绑 host
    base0, _ = V.api_clients()           # 你自己可能也连着远程桌面, 所以看增量
    srv, fport = start_forwarder()
    print(f"  已起转发实例: 127.0.0.1:{fport} -> 127.0.0.1:8390")
    try:
        print("【1】经转发的 HTTP")
        st, hd, _ = http_through(fport, "/api/health")
        chk("HTTP 经转发可用", st == 200, f"HTTP {st}")
        st, hd, _ = http_through(fport, "/static/app.css", V.CK)
        chk("静态资源经转发可用", st == 200, f"HTTP {st}")
        st, hd, _ = http_through(fport, "/static/vendor/xterm/xterm.js", V.CK)
        cc = hd.get("Cache-Control", "")
        chk("第三方库的长缓存没被转发器抹掉", "max-age" in cc, cc or "(无)")
        st, hd, _ = http_through(fport, "/api/health")
        chk("本来没有 Cache-Control 的响应才补 no-cache",
            "no-cache" in hd.get("Cache-Control", ""),
            hd.get("Cache-Control", "(无)"))

        print(f"【2】经转发的桥接: 空闲 {IDLE_SECS} 秒不能掉")
        V.HOST, V.PORT = "127.0.0.1", fport
        b = V.Bridge()
        w, h = b.handshake()
        chk("经转发握手成功", w > 0 and h > 0, f"{w}x{h}")
        t0 = time.time()
        b.settimeout(5)
        dropped = None
        while time.time() - t0 < IDLE_SECS:
            try:
                b.recv_frame()
            except socket.timeout:
                continue
            except ConnectionError:
                dropped = time.time() - t0
                break
        held = time.time() - t0
        chk(f"空闲 {IDLE_SECS}s 内没有被掐断", dropped is None,
            f"坚持 {held:.1f}s" + (f", 第 {dropped:.1f}s 被断" if dropped else ""))
        if dropped is not None:
            chk("断链时机暴露了 5 秒超时(旧 bug)", abs(dropped - 5) > 1.5,
                f"{dropped:.1f}s ≈ create_connection 的 timeout=5")

        print("【3】空闲之后画面还能来")
        if dropped is None:
            req = struct.pack(">BBHHHH", 3, 0, 0, 0, 64, 64)
            b.send(req)
            got = False
            try:
                b.settimeout(5)
                op, pl = b.recv_frame()
                got = op == 0x2 and len(pl) > 0
            except (socket.timeout, ConnectionError):
                got = False
            chk("空闲后请求刷新仍有响应", got)
        b.close()
        time.sleep(0.5)
        c, br = V.api_clients()
        chk("经转发的连接关闭后也干净", (c or 0) <= base0,
            f"clients={c} (基线 {base0})")
    finally:
        srv.shutdown()
        srv.server_close()
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
