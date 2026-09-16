#!/usr/bin/env python3
"""全新机器自检: 第一次打开时能不能自设账号密码、设完能不能进、运行时文件是否都落在 data/。

**不碰正在跑的服务** —— 自己复制一份代码到临时目录、起在空闲端口、验完删掉。

用法: python3 tests/newmachine_check.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
SRC = os.path.dirname(CONSOLE)                     # 仓库根
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="meow-newmachine-")
    proc = None
    try:
        for d in ("static", "templates"):
            shutil.copytree(os.path.join(CONSOLE, d), os.path.join(tmp, d))
        shutil.copy(os.path.join(CONSOLE, "server.py"), tmp)
        env = dict(os.environ)
        env.pop("MEOW_USER", None)
        env.pop("MEOW_PASSWORD", None)              # 确保走"首次设置"路径
        proc = subprocess.Popen(
            [sys.executable, os.path.join(tmp, "server.py"), "--port", str(port)],
            cwd=tmp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等端口起来
        for _ in range(60):
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                s.close()
                break
            except OSError:
                s.close()
                time.sleep(0.25)
        else:
            chk("临时实例启动", False, f"端口 {port} 没起来")
            return 1

        def req(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
            c.request(method, path, body, headers or {})
            r = c.getresponse()
            d = r.read().decode("utf-8", "replace")
            h = dict(r.getheaders())
            c.close()
            return r.status, d, h

        print("【1】没有凭据时应该看到「首次设置」")
        st, body, _ = req("GET", "/login")
        chk("登录页可访问", st == 200, f"HTTP {st}")
        chk("显示首次设置表单", "首次设置" in body and 'action="/setup"' in body)
        chk("有两次密码确认", 'id="pw1"' in body and 'id="pw2"' in body)

        print("【2】提交设置")
        st, _, h = req("POST", "/setup", "user=tester&pass=hello123&pw2=hello123",
                       {"Content-Type": "application/x-www-form-urlencoded"})
        ck = (h.get("Set-Cookie") or "").split(";")[0]
        chk("设置成功并下发会话", st == 302 and ck.startswith("meow_session="), f"HTTP {st}")
        auth = os.path.join(tmp, "data", "auth.json")
        chk("凭据写入 data/auth.json", os.path.isfile(auth))
        if os.path.isfile(auth):
            j = json.load(open(auth))
            chk("存的是加盐哈希, 不是明文",
                set(j) == {"user", "salt", "hash"} and "hello123" not in json.dumps(j))

        print("【3】设完之后")
        st, body, _ = req("GET", "/home", headers={"Cookie": ck})
        chk("能进首页", st == 200 and "cpu-hist" in body, f"HTTP {st}")
        st, body, _ = req("GET", "/login")
        chk("登录页变回登录表单", 'action="/login"' in body and 'action="/setup"' not in body)
        st, _, _ = req("POST", "/setup", "user=x&pass=evil123&pw2=evil123",
                       {"Content-Type": "application/x-www-form-urlencoded"})
        chk("再调 /setup 被拒(不能重设密码)", st == 403, f"HTTP {st}")

        print("【4】运行时文件都在 data/ 下")
        for f in ("auth.json", ".secret", "audit.log", "config.json"):
            chk(f"data/{f} 存在", os.path.isfile(os.path.join(tmp, "data", f)))
        chk("代码目录没有散落的运行时文件",
            not os.path.exists(os.path.join(tmp, "auth.json"))
            and not os.path.exists(os.path.join(tmp, "audit.log")))
        cfg = json.load(open(os.path.join(tmp, "data", "config.json")))
        chk("config 含完整默认键",
            {"typeMode", "typeBatch", "typeDelayMs", "volume", "modeByClass",
             "port", "vncPort"} <= set(cfg))

        print("【5】用新设的账号登录")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        c.request("POST", "/login", "user=tester&pass=hello123",
                  {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        good = (r.getheader("Set-Cookie") or "")
        c.close()
        chk("新凭据可登录", good.startswith("meow_session="))
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        c.request("POST", "/login", "user=tester&pass=wrong",
                  {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        bad = (r.getheader("Set-Cookie") or "")
        c.close()
        chk("错密码不给会话", not bad)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
