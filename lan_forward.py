#!/usr/bin/env python3
# 局域网镜像转发器
# 把发往本机"各局域网 IP":端口 的连接, 转发到 127.0.0.1 的同端口服务。
# 动机: 控制台出于安全**只监听回环**, 手机/外网访问一律经此镜像进来。
# 只绑定具体网卡 IP(不含回环), 因此与各服务自身的回环监听不冲突;
# 每 RECONCILE_SEC 秒对账一次, 自动适应 换Wi-Fi/热点/Tailscale晚启动。
#
# 默认**只转发控制台端口**(8390)。想顺带转发别的, 在脚本旁边放一个
# ports.local.txt(一行一个端口, 不进仓库), 例:
#   8330    # 文件管理
#   3080    # 自己的别的服务

import fcntl
import array
import json
import os
import re
import socket
import struct
import socketserver
import sys
import threading

BACKEND_HOST = "127.0.0.1"
DEFAULT_PORT = 8390
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_PORTS_FILE = os.path.join(HERE, "ports.local.txt")


def _console_port():
    """控制台的端口: 环境变量 > 服务端 config.json > 8390。

    不写死是为了和服务端保持一致 —— 那边改了端口, 这边自动跟上,
    不会出现"控制台在 9000、转发器还守着 8390"这种对不上的情况。
    """
    v = os.environ.get("MEOW_PORT", "").strip()
    if v.isdigit():
        return int(v)
    try:
        with open(os.path.join(HERE, "console", "data", "config.json")) as f:
            p = json.load(f).get("port")
        if isinstance(p, int):
            return p
    except (OSError, ValueError):
        pass
    return DEFAULT_PORT


def _local_ports():
    """本机私有端口(ports.local.txt, 不进仓库)。"""
    out = []
    try:
        with open(LOCAL_PORTS_FILE, errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line.isdigit():
                    out.append(int(line))
    except OSError:
        pass
    return out


# 命令行 > (控制台端口 + 本地私有端口); 普通用户无权绑 80 等特权端口
# (绑定失败会被跳过, 不致命)
PORTS = [int(p) for p in sys.argv[1:] if p.isdigit()] or \
    ([_console_port()] + [p for p in _local_ports() if p != _console_port()])


def list_ipv4_addresses():
    """枚举本机全部 IPv4 地址(含回环), 用 SIOCGIFCONF。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    buf = array.array("B", b"\0" * 8192)
    addr_len, _ = struct.unpack(
        "iL", fcntl.ioctl(
            s.fileno(), 0x8912,  # SIOCGIFCONF
            struct.pack("iL", buf.buffer_info()[1], buf.buffer_info()[0])))
    out = []
    for i in range(0, addr_len, 40):
        raw = bytes(buf[i:i + 40])
        ip = socket.inet_ntoa(raw[20:24])
        if ip:
            out.append(ip)
    return out


def make_handler(port):
    class ForwardHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                remote = socket.create_connection(
                    (BACKEND_HOST, port), timeout=5)
                # ★ 5 秒只是**连接**超时, 必须马上清掉(2026-09-16 修):
                # create_connection 会把这个值同时设成 socket 的读写超时,
                # 之后任何一次"超过 5 秒没有数据"都会被 recv 抛成 timeout,
                # 被 except OSError 吞掉后走 finally 的 shutdown —— 也就是
                # **每静默 5 秒就把连接掐断一次**。
                # 远程桌面静止(画面没变化)时正好没数据 → 手机端每 5 秒断一次,
                # 表现就是"延迟正常但画面一直重连"; 终端挂机、音频静音同理。
                remote.settimeout(None)
                # 终端按键/VNC 输入等小包禁用 Nagle, 避免延迟堆积
                for s in (self.request, remote):
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                return
            with remote:
                def pump(src, dst):
                    try:
                        header_done = False
                        pending = b""
                        while True:
                            data = src.recv(65536)
                            if not data:
                                break
                            if not header_done:
                                # 缓冲到响应头结束, 改写缓存策略:
                                # 皮肤/前端资源改为不缓存(手机 webview
                                # 否则会把 custom.css 抓住一天不更新)。
                                pending += data
                                if b"\r\n\r\n" in pending:
                                    head, _, rest = pending.partition(
                                        b"\r\n\r\n")
                                    # 只在**响应里没有** Cache-Control 时才补
                                    # no-cache。原来是一刀切(先删掉已有的再强加),
                                    # 结果把控制台给 /static/vendor/** 设的长缓存
                                    # 也一起抹掉了 —— 浏览器每次进远程桌面页都要
                                    # 重下 542 KB 的 noVNC 模块(50 个请求)。
                                    if not re.search(rb"(?im)^cache-control:",
                                                     head):
                                        if re.search(rb"(?im)^content-type:",
                                                     head):
                                            # rstrip 防止剥离行后 head 以 \r\n
                                            # 结尾, 拼接时产生空行把响应头挤进 body
                                            head = (head.rstrip(b"\r\n") +
                                                    b"\r\nCache-Control: "
                                                    b"no-cache")
                                    dst.sendall(head + b"\r\n\r\n" + rest)
                                    pending = b""
                                    header_done = True
                                    continue
                                if len(pending) > 262144:
                                    dst.sendall(pending)
                                    pending = b""
                                    header_done = True
                                continue
                            dst.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                t = threading.Thread(
                    target=pump, args=(self.request, remote), daemon=True)
                t.start()
                pump(remote, self.request)
                t.join(timeout=5)
    return ForwardHandler


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # 周期对账: 新地址自动补绑, 消失的自动关停 ——
    # 覆盖 换Wi-Fi/热点切换/Tailscale晚启动 等一切 IP 变化场景。
    RECONCILE_SEC = 20
    active = {}          # {(host, port): (Server, HandlerClass)}
    while True:
        want = set()
        hosts = [h for h in list_ipv4_addresses()
                 if not h.startswith("127.") and ":" not in h]
        if not hosts:
            print("[lan-forward] 未找到局域网 IPv4 地址", flush=True)
        else:
            for port in PORTS:
                for host in hosts:
                    want.add((host, port))
        for key in [k for k in active if k not in want]:
            srv, _ = active.pop(key)
            srv.shutdown()
            print(f"[lan-forward] 摘除 {key[0]}:{key[1]}", flush=True)
        for key in sorted(want - set(active)):
            host, port = key
            try:
                srv = Server(key, make_handler(port))
            except OSError as e:
                print(f"[lan-forward] 跳过 {key[0]}:{key[1]}: {e}",
                      flush=True)
                continue
            active[key] = (srv, None)
            threading.Thread(target=srv.serve_forever,
                             daemon=True).start()
            print(f"[lan-forward] {key[0]}:{key[1]} -> "
                  f"{BACKEND_HOST}:{port}", flush=True)
        threading.Event().wait(RECONCILE_SEC)


if __name__ == "__main__":
    main()
