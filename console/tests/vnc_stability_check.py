#!/usr/bin/env python3
"""连接稳定性 + 延迟专项自检（重点：会不会掉线、会不会越用越卡）。

用法: DISPLAY=:0 python3 tests/vnc_stability_check.py [--long]
      (默认长连接压 60 秒, --long 压 180 秒)

覆盖:
  A 桥接上限(定长队列): 第 3 条连接顶掉最旧的, 被顶掉的那条**确实收到关闭**
  B 正常关闭: 发 close 帧后 x11vnc 上的客户端数立刻回到 0
  C 半死连接: 连上后**不再回 pong**(模拟手机锁屏/切网) → 服务端必须 ~40s 内拆链
  D 长连接: 持续拉流, 数断连/客户端数漂移, 报 fps 与 KB/s
  E 延迟: HTTP RTT 与 /ws/vnc 小区域往返的分布
  F 判据: 用合成 ss 输出验证 vnc_client_sockets() / 遗留连接判定

注意: 会**占用桥接名额**(上限 2 个)。如果你手机正连着远程桌面, A 段会把它顶掉,
      手机端会自动重连 —— 不想被打断就先断开手机。
"""
import base64
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import login_body                      # noqa: E402

PORT = int(os.environ.get("LUO_PORT", "8390"))
HOST = "127.0.0.1"
LONG = "--long" in sys.argv
D_SECS = 180 if LONG else 60
OK, BAD = [], []
BASE_C = 0        # 起始客户端数(你自己可能也连着, 所有判定都相对它算)
BASE_B = 0        # 起始登记桥接数


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def pct(samples, p):
    if not samples:
        return None
    s = sorted(samples)
    i = min(len(s) - 1, max(0, int(round(p / 100 * len(s))) - 1))
    return s[i]


def cookie():
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    return (r.getheader("Set-Cookie") or "").split(";")[0]


CK = None


def api_clients():
    """→ (clients, bridges); 查不到给 (None, None)。"""
    try:
        c = http.client.HTTPConnection(HOST, PORT, timeout=10)
        c.request("GET", "/api/vnc/clients", None, {"Cookie": CK})
        j = json.loads(c.getresponse().read().decode())
        return j.get("clients"), j.get("bridges")
    except Exception:
        return None, None


def wait_balanced(secs=8):
    """等"x11vnc 上的连接数 == 我们登记的桥接数", 返回 (用时, clients, bridges)。

    **这才是真正的判据**(不变量): 每一条挂在 x11vnc 上的连接都该对应一条登记的
    桥接。你自己手机连着时两边同时 +1, 所以它不受影响 —— 比"跟起始基线比"稳得多
    (之前吃过"测试跑到一半你连上来"导致整段误报的亏)。
    """
    t0 = time.time()
    c = b = None
    while time.time() - t0 < secs:
        c, b = api_clients()
        if c is not None and c == b:
            return time.time() - t0, c, b
        time.sleep(0.4)
    return None, c, b


class Bridge:
    """最小的 /ws/vnc 客户端(含完整 RFB 握手)。"""

    def __init__(self, reply_ping=True, timeout=10):
        self.reply_ping = reply_ping
        self.pings = 0
        self.buf = b""
        self.s = socket.create_connection((HOST, PORT), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /ws/vnc HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                        f"Cookie: {CK}\r\nOrigin: http://{HOST}:{PORT}\r\n\r\n").encode())
        head = self.s.recv(4096)
        self.ok = b"101" in head.split(b"\r\n")[0]
        self.s.settimeout(timeout)

    def settimeout(self, t):
        self.s.settimeout(t)

    def send(self, payload, opcode=0x2):
        m = os.urandom(4)
        n = len(payload)
        h = bytearray([0x80 | opcode])
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127)
            h += struct.pack(">Q", n)
        self.s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload)))

    def recv_frame(self):
        """→ (opcode, payload)。连接被关掉抛 ConnectionError, 超时抛 socket.timeout。"""
        while True:
            if len(self.buf) >= 2:
                op = self.buf[0] & 0x0F
                ln = self.buf[1] & 0x7F
                off = None
                if ln < 126:
                    off = 2
                elif ln == 126 and len(self.buf) >= 4:
                    ln = struct.unpack(">H", self.buf[2:4])[0]
                    off = 4
                elif ln == 127 and len(self.buf) >= 10:
                    ln = struct.unpack(">Q", self.buf[2:10])[0]
                    off = 10
                if off is not None and len(self.buf) >= off + ln:
                    pl = self.buf[off:off + ln]
                    self.buf = self.buf[off + ln:]
                    if op == 0x9:                      # ping: 默认回 pong(不回想验半死)
                        self.pings += 1
                        if self.reply_ping:
                            self.send(pl, 0xA)
                        continue
                    if op == 0xA:
                        continue
                    return op, pl
            d = self.s.recv(262144)
            if not d:
                raise ConnectionError("closed")
            self.buf += d

    def handshake(self):
        """完整 RFB 握手 → (w, h); 失败抛异常。"""
        _, first = self.recv_frame()
        if not first.startswith(b"RFB 003."):
            raise RuntimeError(f"不是 RFB 版本: {first[:12]!r}")
        self.send(b"RFB 003.008\n")
        time.sleep(0.2)
        self.recv_frame()                     # 安全类型
        self.send(bytes([1]))
        time.sleep(0.2)
        self.recv_frame()                     # SecurityResult
        self.send(bytes([1]))                 # ClientInit(shared)
        time.sleep(0.3)
        _, si = self.recv_frame()             # ServerInit
        w, h = struct.unpack(">HH", si[:4])
        encs = [7, 16, 5, 1, 0, -223]
        self.send(struct.pack(">BBH", 2, 0, len(encs)) +
                  b"".join(struct.pack(">i", e) for e in encs))
        return w, h

    def close(self, graceful=True):
        try:
            if graceful:
                self.send(b"", 0x8)           # 正常关闭: 发 close 帧
            else:
                self.s.close()                # 模拟"被系统杀掉": 什么都不发
                return
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass


# ------------------------------------------------------------------ A/B 队列
def sec_queue():
    print("【A】桥接上限: 定长队列, 新的顶掉最旧的")
    b1 = Bridge()
    chk("第 1 条桥接连上", b1.ok)
    b1.handshake()
    b2 = Bridge()
    b2.handshake()
    time.sleep(0.8)
    c, br = api_clients()
    chk("两条桥接时: 连接数 = 登记数(没有多余)", c == br,
        f"clients={c} bridges={br}")

    b3 = Bridge()
    b3.handshake()
    time.sleep(1.2)
    c, br = api_clients()
    chk("第 3 条上来后: 连接数仍 = 登记数", c == br,
        f"clients={c} bridges={br}")
    chk("定长队列仍然只留 2 条", br == 2, f"bridges={br}")

    b1.settimeout(4)
    closed = False
    t0 = time.time()
    try:
        while time.time() - t0 < 4:
            op, _ = b1.recv_frame()
            if op == 0x8:
                closed = True
                break
    except ConnectionError:
        closed = True
    except socket.timeout:
        pass
    chk("被顶掉的那条**确实收到关闭**(不是干挂着)", closed,
        f"{time.time() - t0:.1f}s 内")

    print("【B】正常关闭: 立刻释放")
    b2.close()
    b3.close()
    dt, c, br = wait_balanced(secs=8)
    chk("发 close 帧后没有残留(连接数 = 登记数)", dt is not None,
        f"{dt:.1f}s" if dt is not None else f"clients={c} bridges={br}")
    chk("我们这几条已撤下登记", br is not None and br <= BASE_B,
        f"bridges={br} (起始 {BASE_B})")
    b1.close()


# -------------------------------------------------------------------- C 半死
def sec_halfdead():
    print("【C】半死连接(手机锁屏/切网): 不回 pong 必须被拆掉")
    b = Bridge(reply_ping=False)
    b.handshake()
    t0 = time.time()
    dropped = None
    b.settimeout(75)
    while time.time() - t0 < 75:
        try:
            op, _ = b.recv_frame()
            if op == 0x8:
                dropped = time.time() - t0
                break
        except ConnectionError:
            dropped = time.time() - t0
            break
        except socket.timeout:
            continue
    chk("服务端主动拆掉了半死连接", dropped is not None,
        f"{dropped:.0f}s" if dropped else "75s 内没拆")
    if dropped:
        # 心跳: 20s 一个 ping, 40s 收不到 pong 判死 → 应该在 40~50s 之间
        chk("拆链时机符合设计(40~55s)", 38 <= dropped <= 55, f"{dropped:.0f}s")
    chk("期间确实发过心跳 ping", b.pings >= 1, f"{b.pings} 次")
    dt, c, br = wait_balanced(secs=8)
    chk("拆链后没有残留(连接数 = 登记数)", dt is not None,
        f"{dt:.1f}s" if dt is not None else f"clients={c} bridges={br}")
    b.close(graceful=False)


# ------------------------------------------------------------------ D 长连接
def sec_soak():
    print(f"【D】长连接 {D_SECS}s: 会不会中途断、客户端数会不会漂")
    b = Bridge()
    w, h = b.handshake()
    print(f"  桌面 {w}x{h}, 每 40ms 要一次全屏刷新")
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)
    stop = threading.Event()
    sent = [0]

    def feeder():
        while not stop.is_set():
            try:
                b.send(req)
                sent[0] += 1
            except OSError:
                return
            stop.wait(0.04)

    threading.Thread(target=feeder, daemon=True).start()
    total, frames, drops = 0, 0, 0
    seen = []
    t0 = time.time()
    last_sample = t0
    b.settimeout(5)
    while time.time() - t0 < D_SECS:
        try:
            op, pl = b.recv_frame()
        except socket.timeout:
            continue
        except ConnectionError:
            drops += 1
            break
        if op == 0x2:
            total += len(pl)
            frames += 1
        if time.time() - last_sample >= 15:
            last_sample = time.time()
            seen.append(api_clients())        # (clients, bridges)
    dt = time.time() - t0
    stop.set()
    chk("长连接期间没有断", drops == 0, f"断开 {drops} 次")
    chk("全程 连接数 = 登记数", bool(seen) and all(a == b for a, b in seen),
        f"采样 {seen}")
    chk("一直在出画面", frames > 30, f"{frames} 帧 / {dt:.0f}s")
    print(f"  收到 {total / 1024:.0f} KB / {dt:.0f}s = {total / dt / 1024:.0f} KB/s, "
          f"{frames / dt:.1f} 帧/s, 发请求 {sent[0]} 次")
    b.close()


# -------------------------------------------------------------------- E 延迟
def sec_latency():
    print("【E】延迟分布")
    http_ms = []
    for _ in range(30):
        c = http.client.HTTPConnection(HOST, PORT, timeout=10)
        t0 = time.perf_counter()
        c.request("GET", "/api/health?t=%d" % time.time_ns(), None, {"Cookie": CK})
        c.getresponse().read()
        http_ms.append((time.perf_counter() - t0) * 1000)
        c.close()
        time.sleep(0.03)
    chk("HTTP RTT 中位 < 30ms", (pct(http_ms, 50) or 999) < 30,
        f"中位 {pct(http_ms, 50):.1f}ms  p95 {pct(http_ms, 95):.1f}ms  "
        f"最慢 {max(http_ms):.1f}ms")

    b = Bridge()
    w, h = b.handshake()
    req = struct.pack(">BBHHHH", 3, 0, 0, 0, 64, 64)      # 只问左上角 64x64
    br_ms = []
    b.settimeout(3)
    for _ in range(15):
        t0 = time.perf_counter()
        b.send(req)
        try:
            b.recv_frame()
        except (socket.timeout, ConnectionError):
            continue
        br_ms.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.08)
    chk("桥接往返(小区域)中位 < 100ms", (pct(br_ms, 50) or 999) < 100,
        f"中位 {pct(br_ms, 50):.1f}ms  最快 {min(br_ms):.1f}ms  "
        f"最慢 {max(br_ms):.1f}ms  n={len(br_ms)}")
    b.close()


# ------------------------------------------------------------- F 判据自证
def sec_judgement():
    print("【F】遗留连接判据(用合成 ss 输出验, 不碰真实服务)")
    sys.path.insert(0, os.path.dirname(HERE))
    import server                                    # noqa: E402

    fake = (
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'ESTAB 0 0 127.0.0.1:41001 127.0.0.1:5900 users:(("python3",pid=111,fd=9))\n'
        'ESTAB 0 0 127.0.0.1:5900 127.0.0.1:41001 users:(("x11vnc",pid=222,fd=5))\n'
        'ESTAB 0 0 127.0.0.1:41002 127.0.0.1:5900 users:(("python3",pid=999,fd=3))\n'
        'ESTAB 0 0 127.0.0.1:41234 127.0.0.1:8390 users:(("firefox",pid=333,fd=8))\n'
        "ESTAB 0 0 127.0.0.1:5900 127.0.0.1:5900\n"
        'CLOSE-WAIT 0 0 127.0.0.1:41003 127.0.0.1:5900 users:(("python3",pid=444,fd=2))\n'
    )

    class R:
        stdout = fake

    real_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: R()
    try:
        socks = server.vnc_client_sockets()
        ports = sorted(s["port"] for s in socks)
        chk("只数'客户端 -> x11vnc'方向的 ESTAB", ports == ["41001", "41002"],
            f"{ports}")
        chk("x11vnc 自己那侧、别的端口、CLOSE-WAIT 都不算",
            all(p not in ports for p in ("5900", "41234", "41003")), f"{ports}")
        chk("属主(进程名+pid)被解析出来",
            any("pid=999" in s["owner"] for s in socks),
            str([s["owner"][:28] for s in socks]))

        real_bridges = server._vnc_bridges
        server._vnc_bridges = [[time.time(), lambda: None, "41001"]]
        foreign = server.vnc_foreign_sockets(socks)
        chk("登记过的端口不算遗留", [s["port"] for s in foreign] == ["41002"],
            f"遗留={[s['port'] for s in foreign]}")
        server._vnc_bridges = [[time.time(), lambda: None, None]]
        chk("端口没记下时不乱判(空)", server.vnc_foreign_sockets(socks) == [])
        server._vnc_bridges = real_bridges
    finally:
        server.subprocess.run = real_run


def main():
    global CK, BASE_C, BASE_B
    CK = cookie()
    if not CK:
        print("  ★ 登录失败")
        return 1
    BASE_C, BASE_B = api_clients()
    if BASE_C is None:
        print("  ★ 拿不到客户端数(ss 不可用?)")
        return 1
    print(f"起始状态: x11vnc 客户端 {BASE_C} 个 / 登记桥接 {BASE_B} 个")
    if BASE_C:
        print("  ⚠ 你自己正连着远程桌面 —— 下面的判定都以这个基线为准, "
              "A 段会把最旧的那条顶掉(手机端会自动重连)")
    print()
    sec_queue()
    print()
    sec_halfdead()
    print()
    sec_soak()
    print()
    sec_latency()
    print()
    sec_judgement()
    print()
    c1, b1 = api_clients()
    print(f"结束状态: x11vnc 客户端 {c1} 个 / 登记桥接 {b1} 个")
    chk("测试没留下残留连接(连接数 = 登记数)", (c1 or 0) == (b1 or 0),
        f"clients={c1} bridges={b1}")
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
