#!/usr/bin/env python3
"""meow-console 离线自检(不需要真实 X 会话 / x11vnc)。

覆盖:
  1. 新接口 /api/screen /api/dock /api/edge 的健壮性(无 X 会话时必须优雅降级)
  2. /vnc 渲染出的 DOM id 是否覆盖 vnc.js 里所有 $('...') 查询
     —— 少一个 id 前端一加载就抛异常, 整个远程桌面页面直接废掉
  3. 静态资源能取到新版本
"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import importlib.util
import os
import re
import sys
import threading
import http.client
import http.server
import socket
import json

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
sys.path.insert(0, CONSOLE)

spec = importlib.util.spec_from_file_location("meow_console", os.path.join(CONSOLE, "server.py"))
srv_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv_mod)

fails = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        fails.append(name)


# ---- 1) 无 X 会话下的降级行为 -------------------------------------------
print("== 降级行为(本沙箱没有 X 会话, 这些调用必须不抛异常) ==")
try:
    w, h = srv_mod.screen_size()
    check("screen_size() 不抛异常", True, f"-> {w}x{h}")
except Exception as e:
    check("screen_size() 不抛异常", False, repr(e))

try:
    ok, msg = srv_mod.edge_nudge("bottom", 480, 269, 960, 540)
    # 有真实 X 会话时会成功钉指针; 没有时会优雅返回 False。两者都不能抛异常。
    check("edge_nudge() 返回结构正常", isinstance(ok, bool) and isinstance(msg, str),
          f"-> ok={ok} {msg}")
except Exception as e:
    check("edge_nudge() 返回结构正常", False, repr(e))

try:
    st = srv_mod.dock_state()
    check("dock_state() 返回结构", isinstance(st, dict) and "ok" in st, f"-> {st}")
except Exception as e:
    check("dock_state() 返回结构", False, repr(e))

# ---- 2) 起真实 HTTP 服务 -------------------------------------------------
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
PORT = sock.getsockname()[1]
sock.close()

httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), srv_mod.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


print("\n== HTTP 接口 ==")
st, _, body = req("GET", "/api/health")
check("GET /api/health", st == 200 and b"meow-console" in body)

st, hdrs, _ = req("GET", "/api/screen")
check("GET /api/screen 需登录", st in (301, 302), f"-> {st}")

# 登录拿 cookie
st, _, body = req("POST", "/login", login_body(),
                  {"Content-Type": "application/x-www-form-urlencoded"})
c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse()
r.read()
cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
c.close()
check("登录成功", cookie.startswith("meow_session="), cookie[:20] + "…")
CK = {"Cookie": cookie}

st, _, body = req("GET", "/api/dock", headers=CK)
j = json.loads(body)
check("GET /api/dock 结构", st == 200 and "ok" in j and "pressure" in j, body[:100])

st, _, body = req("GET", "/api/screen", headers=CK)
check("GET /api/screen(已登录)", st == 200 and "ok" in json.loads(body), body[:80])

st, _, body = req("POST", "/api/edge",
                  json.dumps({"edge": "bottom", "fx": 480, "fy": 539, "fbw": 960, "fbh": 540}),
                  {**CK, "Content-Type": "application/json"})
check("POST /api/edge 不 500", st == 200, body[:100])

st, _, body = req("POST", "/api/edge", json.dumps({"edge": "nonsense"}),
                  {**CK, "Content-Type": "application/json"})
check("POST /api/edge 参数校验", st == 200 and json.loads(body)["ok"] is False, body[:80])

# ---- 3) DOM id 一致性 ----------------------------------------------------
print("\n== vnc.js 查询的 DOM id vs 渲染出的 HTML ==")
st, _, html = req("GET", "/vnc", headers=CK)
check("GET /vnc", st == 200 and b"pad-area" in html)
html_s = html.decode()

js = open(os.path.join(CONSOLE, "static", "vnc.js")).read()
ids = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js))
ids |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", js))
# 这些是 noVNC 运行时动态创建的, 不在模板里
DYNAMIC = {"noVNC_mouse_capture_elem"}
ids -= DYNAMIC
missing = sorted(i for i in ids if f'id="{i}"' not in html_s)
check(f"vnc.js 用到的 {len(ids)} 个 id 全部存在", not missing, f"缺失: {missing}")

# vnc.js 里用到的选择器类名也要在
for sel in ["#set-panel .seg.q button", "#set-scrolldir button", "#pad-area .hint"]:
    cls = sel.split(".")[-1].split(" ")[0] if "." in sel else None
check("设置面板含画质 seg.q", 'class="seg q"' in html_s)
check("设置面板含滚动方向 seg", 'id="set-scrolldir"' in html_s)

# ---- 4) 静态资源是新版本 ------------------------------------------------
st, _, body = req("GET", "/static/vnc.js", headers=CK)
check("vnc.js 为新实现", b"RFB.messages.pointerEvent" in body and b"releaseOverlay" in body)
st, _, body = req("GET", "/static/app.css", headers=CK)
check("app.css 含 touch-action", b"touch-action: none" in body)

httpd.shutdown()
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
