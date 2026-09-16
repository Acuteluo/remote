#!/usr/bin/env python3
"""剪贴板读写往返测试。会短暂占用远端剪贴板, 测完**尽量还原**原内容。

只验证: 写入 -> 读回 是否完全一致(UTF-8 往返), 以及读取接口对当前内容的解码。
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
if not CK:
    raise SystemExit("登录失败")
print("登录 OK\n")


def call(method, path, body=None):
    cc = http.client.HTTPConnection(HOST, PORT, timeout=30)
    hdr = {"Cookie": CK}
    payload = None
    if body is None:
        pass
    else:
        hdr["Content-Type"] = "application/json"
        payload = json.dumps(body)
    cc.request(method, path, payload, hdr)
    rr = cc.getresponse()
    return json.loads(rr.read().decode("utf-8", "replace"))


# 1) 先看看现在剪贴板里是什么, 记下来好还原
cur = call("GET", "/api/clipboard")
orig = cur.get("text", "")
prev = orig[:40].replace("\n", "\\n")
print(f"1) 当前剪贴板内容: {cur.get('ok')}  {len(orig)} 字")
print(f"   前 40 字: 「{prev}」")

# 2) 写入测试文本(中文 + 英文 + 符号 + emoji)
TEST = "中文混排 ABCabc 123 ，。！？ 😀 emoji 结尾"
w = call("POST", "/api/clipboard", {"text": TEST})
print(f"\n2) 写入 {len(TEST)} 字 -> ok={w.get('ok')} msg={w.get('msg')}")
time.sleep(0.4)

# 3) 读回, 逐字节比对
back = call("GET", "/api/clipboard")
got = back.get("text", "")
ok = "✓ 完全一致" if got == TEST else "★ 不一致"
print(f"\n3) 读回 {len(got)} 字 -> {ok}")
if got != TEST:
    print(f"   期望: {TEST!r}")
    print(f"   实得: {got!r}")

# 4) 还原
if orig:
    time.sleep(0.2)
    rs = call("POST", "/api/clipboard", {"text": orig})
    print(f"\n4) 还原原内容 -> ok={rs.get('ok')} ({len(orig)} 字)")
else:
    print("\n4) 原剪贴板为空, 无需还原")

print("\n结论:", "剪贴板 UTF-8 往返正常" if got == TEST else "★ 往返有损, 需要回退")
