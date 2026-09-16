#!/usr/bin/env python3
"""量 /api/paste 各阶段耗时, 找"粘贴卡死"卡在哪一步。"""
# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import http.client, json, time

HOST, PORT = "127.0.0.1", 8390

c = http.client.HTTPConnection(HOST, PORT, timeout=10)
c.request("POST", "/login", login_body(),
          {"Content-Type": "application/x-www-form-urlencoded"})
r = c.getresponse(); r.read()
CK = (r.getheader("Set-Cookie") or "").split(";")[0]
print("登录 OK\n")


def call(path, obj, timeout=120):
    cc = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    t0 = time.time()
    try:
        cc.request("POST", path, json.dumps(obj),
                   {"Content-Type": "application/json", "Cookie": CK})
        rr = cc.getresponse()
        body = rr.read().decode("utf-8", "replace")
        return time.time() - t0, rr.status, body
    except Exception as e:
        return time.time() - t0, 0, f"异常: {type(e).__name__}: {e}"


print("=== /api/paste (设剪贴板 + Ctrl+V) ===")
for n in (10, 100, 2000, 8000):
    dt, st, body = call("/api/paste", {"text": "甲" * n})
    print(f"  {n:>5d} 字: {dt:6.2f}s  HTTP {st}  {body[:70]}")

print("\n=== /api/clipboard (只写剪贴板) ===")
for n in (10, 100, 2000, 8000):
    dt, st, body = call("/api/clipboard", {"text": "乙" * n})
    print(f"  {n:>5d} 字: {dt:6.2f}s  HTTP {st}  {body[:70]}")

print("\n=== /api/type ===")
for n in (10, 100, 500):
    dt, st, body = call("/api/type", {"text": "丙" * n})
    print(f"  {n:>5d} 字: {dt:6.2f}s  HTTP {st}  {body[:70]}")

print("\n=== 连续 5 次 paste(看会不会越点越慢/卡住) ===")
for i in range(5):
    dt, st, body = call("/api/paste", {"text": f"第{i}次"})
    print(f"  第{i+1}次: {dt:6.2f}s  HTTP {st}  {body[:50]}")
