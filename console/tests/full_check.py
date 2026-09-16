#!/usr/bin/env python3
"""远程控制台全功能体检(只读 + 少量可逆操作)。

刻意**不测**: /api/power(锁屏/关机)、/api/kill(杀进程) —— 会打扰正在用电脑的人。
刻意**不测**: 超长 /api/type(会把一大段文本糊进焦点窗口)。
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import base64, http.client, json, os, socket, time

# 可用环境变量覆盖 —— 在隔离环境里跑"新机器模拟"时, 8390 已被本机真实服务占着,
# 必须能把实例起在别的端口上来体检。
HOST = os.environ.get("LUO_HOST") or "127.0.0.1"
PORT = int(os.environ.get("LUO_PORT") or 8390)
OK, BAD = [], []


def chk(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail else ""))


# ---------------- 登录 ----------------
# 先等端口真的 listen 起来。systemd 对 Type=simple 的服务是**一 exec 就返回**
# "启动完成", Python 这时还没 bind —— `systemctl restart` 之后紧跟一句测试必然
# ConnectionRefused。那不是服务挂了, 是竞态。
if not wait_ready(HOST, PORT, timeout=30):
    raise SystemExit(f"控制台 {HOST}:{PORT} 30s 内没起来, 后面不用跑了")

c = http.client.HTTPConnection(HOST, PORT, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]
chk("登录", bool(CK))
if not CK:
    raise SystemExit("登录失败, 后续免谈")


def get(path, auth=True, timeout=15):
    cc = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    cc.request("GET", path, None, {"Cookie": CK} if auth else {})
    rr = cc.getresponse()
    return rr.status, rr.read()


def getj(path, timeout=15):
    """GET 一个返回 JSON 的接口(之前用 POST 测, 全部 404, 误报过一次)。"""
    st, b = get(path, timeout=timeout)
    try:
        return st, json.loads(b.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return st, {}


def post(path, body, auth=True, timeout=30):
    cc = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    hdr = {"Content-Type": "application/json"}
    if auth:
        hdr["Cookie"] = CK
    cc.request("POST", path, json.dumps(body), hdr)
    rr = cc.getresponse()
    return rr.status, rr.read().decode("utf-8", "replace")


def jpost(path, body, **kw):
    st, b = post(path, body, **kw)
    try:
        return st, json.loads(b)
    except ValueError:
        return st, {"_raw": b[:120]}


print("\n【1】页面与鉴权")
st, _ = get("/login", auth=False)
chk("未登录可访问 /login", st == 200, f"HTTP {st}")
st, _ = get("/vnc", auth=False)
chk("未登录访问 /vnc 被拦", st in (401, 302, 303), f"HTTP {st}")
for p in ("/home", "/vnc", "/term"):
    st, b = get(p)
    chk(f"已登录可访问 {p}", st == 200 and len(b) > 200, f"HTTP {st} {len(b)}B")

print("\n【2】接口")
st, j = getj("/api/health")          # 注意: 这两个是 GET 接口, 不是 POST
chk("/api/health", st == 200 and j.get("ok") is True, f"HTTP {st} {j}")
st, j = getj("/api/screen")
ok = st == 200 and isinstance(j.get("w"), int) and j.get("w") > 0
chk("/api/screen 取到屏幕尺寸", ok, f"{j.get('w')}x{j.get('h')}")

st, b = post("/api/type", {"text": "x"}, auth=False)
chk("未登录 /api/type 返回 401", st == 401, f"HTTP {st}")

# GET 读状态; 再 POST 用**当前值**回写(幂等, 不会真的改用户设置), 校验读回一致。
# 之前这里发空载荷 {}, 服务端回 {"ok":false,"err":"没有要改的项"} 也算通过 —— 假绿。
st, j = getj("/api/dock", timeout=30)
chk("/api/dock 读 Dock 设置", st == 200 and j.get("ok") is True, f"{str(j)[:60]}")
if j.get("ok") and j.get("show_delay") is not None:
    st2, j2 = jpost("/api/dock", {"showDelay": j["show_delay"]}, timeout=30)
    ok2 = (st2 == 200 and j2.get("ok") is True
           and abs((j2.get("state") or {}).get("show_delay", -1)
                   - j["show_delay"]) < 0.011)
    chk("/api/dock 写入并读回一致", ok2, f"show-delay={j['show_delay']}")
else:
    chk("/api/dock 写入并读回一致", False, "读不到 show_delay, 跳过写校验")

print("\n【3】静态资源(断网可用要求)")
for f in ("/static/vnc.js", "/static/vendor/novnc/core/rfb.js",
          "/static/vendor/xterm/xterm.js", "/static/app.css"):
    st, b = get(f)
    chk(f, st == 200 and len(b) > 500, f"HTTP {st} {len(b)}B")

print("\n【4】WebSocket")
def ws(path, nbytes=200, send=None, timeout=6):
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    k = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
               f"Sec-WebSocket-Version: 13\r\nCookie: {CK}\r\n"
               f"Origin: http://{HOST}:{PORT}\r\n\r\n").encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        s.close(); return False, b""
    if send:
        # 客户端帧必须掩码
        pay = send.encode()
        h = bytearray([0x81, 0x80 | len(pay)])
        m = os.urandom(4)
        s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(pay)))
    s.settimeout(timeout)
    buf = b""
    try:
        while len(buf) < 4096:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    except OSError as e:
        # 服务端主动关 socket 后再发数据会收到 RST(ConnectionResetError)。
        # 只捕 socket.timeout 的话这条会冒出去, 把整个体检打断在半路 ——
        # 后面的项和汇总行全都不跑, 看着像"崩了"而不是"某几项失败"。
        # 这不是本项的失败, 已收到的数据照常返回。
        try:
            s.close()
        except OSError:
            pass
        return True, buf
    # 收尾: 先发一个 WebSocket close 帧, 再关 socket。
    # 只 close() 裸 socket 的话, 服务端要等读超时(90s)才发现人走了 ——
    # /ws/vnc 每条连接都会在 x11vnc 上占一个客户端, 而 x11vnc 是 -shared,
    # 僵尸客户端一多就会拖慢所有人的画面。反复跑体检 = 反复堆僵尸。
    try:
        s.sendall(b"\x88\x80" + os.urandom(4))     # FIN+close, 掩码长度 0
    except OSError:
        pass
    s.close()
    return True, buf

ok, buf = ws("/ws/status")
chk("/ws/status 推送系统状态", ok and b"cpu" in buf.lower(), f"{len(buf)}B")
ok, buf = ws("/ws/vnc")
chk("/ws/vnc 透传 RFB 版本", ok and b"RFB 003" in buf, f"{len(buf)}B")
# 终端要前端的真实格式: 先 resize {t:'r',c,r}, 再输入 {t:'i',d}
ok, buf = ws("/ws/term", timeout=8,
             send='{"t":"r","c":80,"r":24}')
if ok:
    ok2, buf2 = ws("/ws/term", timeout=8,
                   send='{"t":"i","d":"echo LUO_SELFTEST_OK\\n"}')
    buf = buf + buf2
    ok = ok2
chk("/ws/term 终端可回显", ok and b"LUO_SELFTEST_OK" in buf, f"{len(buf)}B")

print("\n【5】其它服务端口")
for p, name in ((5900, "x11vnc"), (8330, "RAKU Drive"), (3080, "dsh web")):
    s = socket.socket(); s.settimeout(2)
    try:
        s.connect((HOST, p)); chk(f"{name} {p}", True)
    except Exception as e:
        chk(f"{name} {p}", False, type(e).__name__)
    finally:
        s.close()

print("\n" + "=" * 46)
print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
if BAD:
    print("失败项:", ", ".join(BAD))
