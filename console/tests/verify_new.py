#!/usr/bin/env python3
"""验证上一轮新加的功能: 全局配置 config.json 接口 + 超长键入闸门。

超长键入那条会检查"被拒绝时剪贴板没有被改写"(即没有真的糊进焦点窗口)。
"""
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
print("登录:", "OK" if CK else "★失败")


def call(method, path, body=None):
    cc = http.client.HTTPConnection(HOST, PORT, timeout=30)
    hdr = {"Cookie": CK}
    payload = None
    if body is not None:
        hdr["Content-Type"] = "application/json"
        payload = json.dumps(body)
    cc.request(method, path, payload, hdr)
    rr = cc.getresponse()
    raw = rr.read().decode("utf-8", "replace")
    try:
        return rr.status, json.loads(raw)
    except ValueError:
        return rr.status, {"_raw": raw[:150]}


print("\n【1】全局配置 config.json")
st, j = call("GET", "/api/config")
print(f"  GET  -> HTTP {st}  {j.get('config')}")
cfg0 = j.get("config") or {}

# 改成 paste 再读回, 确认真的落到电脑上
st, j = call("POST", "/api/config", {"typeMode": "paste"})
print(f"  POST typeMode=paste -> HTTP {st} ok={j.get('ok')}  {j.get('config')}")
st, j2 = call("GET", "/api/config")
got = (j2.get("config") or {}).get("typeMode")
print(f"  再 GET -> typeMode={got}   {'✓ 已持久化到电脑' if got == 'paste' else '★ 没存住'}")

# 改回 auto
call("POST", "/api/config", {"typeMode": cfg0.get("typeMode", "auto")})
st, j3 = call("GET", "/api/config")
print(f"  还原为 {(j3.get('config') or {}).get('typeMode')}")

print("\n【2】超长键入闸门(5000 字)")
before = call("GET", "/api/clipboard")[1].get("text", "")
st, j = call("POST", "/api/type", {"text": "甲" * 6000})
ok = st == 200 and j.get("ok") is False
print(f"  6000 字 -> HTTP {st} ok={j.get('ok')} err={j.get('err') or j.get('msg')}")
print(f"  {'✓ 已拒绝' if ok else '★ 没拦住(危险!)'}")
time.sleep(0.6)
after = call("GET", "/api/clipboard")[1].get("text", "")
print(f"  剪贴板是否被改写: {'✗ 被改了(说明真糊进去了)' if after != before else '✓ 没动(安全)'}")

print("\n【3】正常长度仍可用")
st, j = call("POST", "/api/type", {"text": "ok"})
print(f"  2 字 -> HTTP {st} ok={j.get('ok')} msg={j.get('msg')}")
