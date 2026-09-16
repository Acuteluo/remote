#!/usr/bin/env python3
"""测试脚本共用的登录凭据 / 就绪等待。

**为什么要有这个文件**: 之前每个测试脚本里都写死了账号密码明文。
项目要开源, 密码不能进仓库; 而且新机器上 install.sh 是**随机生成**密码的,
写死必然登录失败。

凭据优先级:
  1. 环境变量 MEOW_USER / MEOW_PASSWORD      (CI / 临时指定, 最安全)
  2. console/.testpass                     (本机自己放, 一行 `user:pass` 或只写密码;
                                            已在 .gitignore 里, 不会进仓库)
  3. console/.initial-password             (install.sh 首次启动时生成的随机密码)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../console/tests
_CONSOLE = os.path.dirname(_HERE)                           # .../console
_DATA = os.path.join(_CONSOLE, "data")                      # 运行时目录


def _read(p):
    try:
        with open(p, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _kv(txt, user_keys, pass_keys):
    """从 `键: 值` 形式的文本里挑出 user / pass。"""
    u = p = None
    for line in txt.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if any(k.startswith(x) for x in user_keys) and v:
            u = v
        elif any(k.startswith(x) for x in pass_keys) and v:
            p = v
    return u, p


def creds():
    u, p = os.environ.get("MEOW_USER", ""), os.environ.get("MEOW_PASSWORD", "")
    if u and p:
        return u, p

    # .testpass: 一行 `user:pass`, 或只写密码。运行时目录优先(2026-09-16 起
    # 运行时文件统一放 console/data/), 老机器上可能还在 console/ 根下。
    t = (_read(os.path.join(_DATA, ".testpass")).strip()
         or _read(os.path.join(_CONSOLE, ".testpass")).strip())
    if t:
        if ":" in t:
            a, b = t.split(":", 1)
            return a.strip(), b.strip()
        return "tester", t

    # install.sh 首次启动生成的随机密码
    u2, p2 = _kv(_read(os.path.join(_CONSOLE, ".initial-password")),
                 ("user", "用户名", "账号"), ("pass", "密码"))
    if u2 and p2:
        return u2, p2
    return (u or "tester"), p


def login_body():
    u, p = creds()
    return f"user={u}&pass={p}"


def wait_ready(host="127.0.0.1", port=8390, timeout=30):
    """等服务把端口真正 listen 起来。

    **坑**: systemd 对 `Type=simple` 的服务是**一 exec 就返回**"启动完成",
    Python 这时还没 bind。所以 `systemctl restart` 之后立刻跑测试会
    ConnectionRefused —— 那是竞态, 不是服务挂了。
    """
    import socket
    import time
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect((host, port))
            return True
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
        finally:
            s.close()
        time.sleep(0.4)
    sys.stderr.write(
        f"[wait_ready] {host}:{port} {timeout}s 内没起来 ({last})\n"
        "  如果服务确实在跑, 检查是不是监听地址/端口不对\n")
    return False


def login(host="127.0.0.1", port=8390, wait=30):
    """登录并返回 Set-Cookie; 拿不到就明确报错退出。"""
    import http.client
    if not wait_ready(host, port, timeout=wait):
        raise SystemExit(
            f"控制台 {host}:{port} 未就绪。\n"
            "  先确认服务在跑: systemctl --user status meow-console\n"
            "  设凭据: export MEOW_PASSWORD=xxx  或写 console/.testpass")
    c = http.client.HTTPConnection(host, port, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    if not ck:
        raise SystemExit(
            "登录失败(没拿到 cookie)。请检查凭据:\n"
            "  export MEOW_PASSWORD=xxx          # 或\n"
            "  echo 'user:pass' > console/.testpass\n"
            "  (新机器的随机密码在 console/.initial-password)")
    return ck


if __name__ == "__main__":
    u, p = creds()
    print(f"user={u}  password={'*' * len(p) if p else '(空)'}")
    print("body:", login_body())
