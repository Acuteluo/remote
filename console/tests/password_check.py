#!/usr/bin/env python3
"""修改密码接口自检: 错误原密码拒绝 / 太弱拒绝 / 改密→新密码可登录→改回→旧密码可登录。

用法: DISPLAY=:0 python3 tests/password_check.py

**安全设计**: 全程先拿当前凭据, 改成随机临时密码再**立刻改回**;
restore 放在 finally 里, 即使中途断言炸掉也会把密码还原, 不会把人锁在门外。
"""
import http.client
import json
import secrets
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds, login_body, wait_ready   # noqa: E402

HOST = os.environ.get("MEOW_HOST", "127.0.0.1")
PORT = int(sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("MEOW_PORT") or 8390))
OK, BAD = [], []


def chk(n, c, e=""):
    (OK if c else BAD).append(n)
    print(f"  {'✓' if c else '★'} {n}" + (f"   {e}" if e else ""))


def post(path, obj, ck=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    h = {"Content-Type": "application/json"}
    if ck:
        h["Cookie"] = ck
    c.request("POST", path, json.dumps(obj), h)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(body)
    except ValueError:
        return r.status, {"ok": False, "err": body[:60]}


def login_with(u, p):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("POST", "/login", f"user={u}&pass={p}",
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c.close()
    return ck


def main():
    if not wait_ready(HOST, PORT, timeout=30):
        print(f"  ★ {HOST}:{PORT} 连不上")
        return 1
    user, cur = creds()
    ck = login_with(user, cur)
    chk("当前凭据可登录", ck.startswith("meow_session="))

    tmp = "tmp_" + secrets.token_urlsafe(9)          # 随机临时密码, 测试完即弃
    restored = False
    try:
        st, j = post("/api/password", {"old": "definitely-wrong", "new": tmp}, ck)
        chk("原密码错误被拒", st == 200 and j.get("ok") is False
            and "不正确" in (j.get("err") or ""), j.get("err", "")[:30])

        st, j = post("/api/password", {"old": cur, "new": "123"}, ck)
        chk("新密码太短(<6位)被拒", st == 200 and j.get("ok") is False, j.get("err", "")[:30])

        st, j = post("/api/password", {"old": cur, "new": tmp}, ck)
        changed = st == 200 and j.get("ok") is True
        chk("改成临时密码成功", changed, j.get("err", "")[:40])

        if changed:
            ck2 = login_with(user, tmp)
            chk("新密码可登录", ck2.startswith("meow_session="))
            chk("旧密码已失效", not login_with(user, cur))

            st, j = post("/api/password", {"old": tmp, "new": cur}, ck2)
            restored = st == 200 and j.get("ok") is True
            chk("改回原密码成功", restored, j.get("err", "")[:40])

            if restored:
                chk("原密码可再登录", login_with(user, cur).startswith("meow_session="))
    finally:
        # 兜底: 只要曾经改成功过而没改回来, 用临时密码再试一次还原。
        if not restored and login_with(user, tmp):
            st, j = post("/api/password", {"old": tmp, "new": cur},
                         login_with(user, tmp))
            print(f"  (finally 兜底还原: ok={j.get('ok')})")
            if not j.get("ok"):
                print("  !! 密码未能自动还原, 请手动处理 console/auth.json")
    print(f"\n通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
