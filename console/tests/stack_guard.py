#!/usr/bin/env python3
"""remote 全套服务的开机自检 / 定期巡检 / 一次自愈。

起因(2026-09-22 那次"手机进不来")
--------------------------------
三个毛病叠在一起: 转发器少了 `import os` 开机就崩溃循环(空转 255 次)、
Tailscale 掉线、控制台读错 token 文件。而 systemd 那侧看过去, 每个单元都是
`enabled`, 状态要么 active 要么在不停 auto-restart —— **没有任何一处告诉你
"整体是坏的"**。

enabled != 能用。所以这个守护不看"进程在不在", 只看**从外面连过去到底通不通**:

  1. 运行时文件能不能加载 —— 直接 import lan_forward, 缺 import 这类问题当场暴露
     (py_compile 查不出来, 因为它语法合法)
  2. 单元在不在跑(systemd 可用时)
  3. 回环端口在不在听
  4. **每个访问 IP 上的端口是不是真被镜像出来了** —— 真发一次 HTTP, 不是看进程
  5. 私有模块自己的检查: console/extras_local.py 里定义了 guard_check() 就会被调用
     (DSH 面板那条链的自检就挂在这个钩子上, 不进公开仓库)

自愈: 单元没起来就 restart 一次再复检; 仍然不行就记下来并退出非 0 ——
      systemd 会把这次运行标成 failed, 一眼能看见, 而不是静默空转。

状态: console/data/guard-status.json(最近一次全量结果) + guard.log(逐次一行)
用法: python3 console/tests/stack_guard.py [--no-heal] [--quiet]
退出码: 0 = 健康(含已自愈), 1 = 仍有问题
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
REPO = os.path.dirname(CONSOLE)
DATA = os.path.join(CONSOLE, "data")
STATUS_FILE = os.path.join(DATA, "guard-status.json")
LOG_FILE = os.path.join(DATA, "guard.log")

UNIT_DIR = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")

# 这套东西自己的单元; 文件存在就该是 active(不存在=还没装, 不算错)
EXPECTED_UNITS = ("meow-console", "meow-vnc", "meow-forward", "dsh-lan-forward")
# 新旧两个转发器单元同时存在会互相抢端口, 谁都不通
FORWARD_UNITS = ("meow-forward", "dsh-lan-forward")

DEFAULT_PORT = 8390
TIMEOUT = 2
# Clash 的 TUN(198.18.0.0/16) / link-local 不是访问路径, 别拿它们判故障
SKIP_IP_PREFIXES = ("198.18.", "169.254.")


# ---------------------------------------------------------------- 小工具
def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, "", ""


def systemctl_ok():
    """当前环境能不能用 systemctl --user(容器/受限 shell 里不行, 要优雅降级)。"""
    rc, out, _ = run(["systemctl", "--user", "is-system-running"])
    return rc == 0 or out in ("running", "degraded", "starting", "maintenance")


def unit_state(name):
    if not os.path.exists(os.path.join(UNIT_DIR, name + ".service")):
        return None                                  # 没装
    rc, out, _ = run(["systemctl", "--user", "is-active", name + ".service"])
    state = out or "unknown"
    if state == "unknown":                           # 有些版本不认 is-active
        rc, out, _ = run(["systemctl", "--user", "show", name + ".service",
                          "-p", "ActiveState", "--value"])
        state = out or "unknown"
    return state


def console_port():
    try:
        with open(os.path.join(DATA, "config.json"), errors="replace") as f:
            p = json.load(f).get("port")
        if isinstance(p, int) and 1024 <= p <= 65535:
            return p
    except (OSError, ValueError):
        pass
    return DEFAULT_PORT


def extra_ports():
    """ports.local.txt 里额外要暴露的端口(该文件不进仓库)。"""
    out = []
    try:
        with open(os.path.join(REPO, "ports.local.txt"), errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line.isdigit():
                    out.append(int(line))
    except OSError:
        pass
    return out


def extra_units():
    """units.local.txt 里本机私有的、也要一起盯着的单元(该文件不进仓库)。

    为什么不写死在代码里: 公开仓库不该出现某台机器私有的服务名。留一个
    本地清单文件, 私有服务(比如自己另外跑的 AI 面板)也能享受同样的
    "不开机自启就报错 + 自动拉起来" 待遇。
    """
    out = []
    try:
        with open(os.path.join(REPO, "units.local.txt"), errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line[:-len(".service")] if line.endswith(".service")
                               else line)
    except OSError:
        pass
    return out


def access_ips():
    """本机对外可访问的 IPv4(排除回环 / Clash TUN / link-local)。"""
    ips = []
    try:
        sys.path.insert(0, REPO)
        import lan_forward                            # 顺便验证运行时文件可加载
        ips = lan_forward.list_ipv4_addresses()
    except Exception:
        try:
            ips = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None,
                                                       socket.AF_INET)]
        except OSError:
            ips = []
    return sorted({i for i in ips
                   if not i.startswith("127.")
                   and not i.startswith(SKIP_IP_PREFIXES)})


def probe(host, port):
    """真连一次。返回 (ok, 说明)。HTTP 响应(含 401/404)算通; 退化成 TCP 也算通。"""
    try:
        c = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        c.request("GET", "/")
        r = c.getresponse()
        r.read(64)
        c.close()
        return True, "HTTP %d" % r.status
    except Exception as e_http:                       # noqa: BLE001
        try:
            s = socket.create_connection((host, port), timeout=TIMEOUT)
            s.close()
            return True, "TCP 通(非 HTTP 响应: %s)" % type(e_http).__name__
        except OSError as e:
            return False, "%s: %s" % (type(e).__name__, str(e)[:60])


# ---------------------------------------------------------------- 检查项
def check_runtime_importable(problems, notes):
    """运行时文件必须能加载 —— 这是 2026-09-22 事故的直接教训。"""
    sys.path.insert(0, REPO)
    try:
        import lan_forward                            # noqa: F401
        notes.append("lan_forward.py 可加载")
    except Exception as e:                            # noqa: BLE001
        problems.append("lan_forward.py 加载失败(%s: %s)—— 这个单元一起来就会崩溃循环, "
                        "先跑 console/tests/preflight.py 定位"
                        % (type(e).__name__, e))


def check_units(problems, notes, heal):
    if not systemctl_ok():
        notes.append("systemctl --user 不可用, 跳过单元检查(只做端口连通性)")
        return {}
    states = {}
    wanted = list(EXPECTED_UNITS) + extra_units()
    installed = [u for u in wanted
                 if os.path.exists(os.path.join(UNIT_DIR, u + ".service"))]
    if not installed:
        notes.append("没发现本项目的 user 单元(还没跑 install.sh?)")
        return states
    if all(os.path.exists(os.path.join(UNIT_DIR, u + ".service"))
           for u in FORWARD_UNITS):
        problems.append("转发器单元同时存在 %s —— 两个会抢同一批端口, "
                        "跑一次 ./install.sh 把旧的迁掉" % " 和 ".join(FORWARD_UNITS))

    for u in installed:
        st = unit_state(u)
        if st == "active":
            notes.append("%s active" % u)
            states[u] = st
            continue
        if heal:
            run(["systemctl", "--user", "reset-failed", u + ".service"])
            run(["systemctl", "--user", "restart", u + ".service"])
            time.sleep(2)
            st2 = unit_state(u)
            notes.append("%s 尝试自愈: %s -> %s" % (u, st, st2))
            states[u] = st2
            if st2 != "active":
                problems.append("%s 自愈后仍不是 active(当前 %s)" % (u, st2))
        else:
            states[u] = st
            problems.append("%s 不是 active(当前 %s)" % (u, st))
    return states


def check_ports(problems, notes):
    ports = [console_port()] + [p for p in extra_ports() if p != console_port()]
    ips = access_ips()
    if not ips:
        notes.append("没找到对外 IPv4, 只查回环")
    checked = 0
    for port in ports:
        ok, detail = probe("127.0.0.1", port)
        checked += 1
        if ok:
            notes.append("127.0.0.1:%d %s" % (port, detail))
        else:
            problems.append("服务本身没在听 127.0.0.1:%d(%s)" % (port, detail))
        for ip in ips:
            ok, detail = probe(ip, port)
            checked += 1
            if ok:
                notes.append("%s:%d %s" % (ip, port, detail))
            else:
                problems.append("转发没生效: %s:%d 连不上(%s)—— "
                                "检查转发器单元" % (ip, port, detail))
    return checked


def check_private_hooks(problems, notes):
    """私有模块的自检钩子(本机私有的 DSH 面板等), 不进公开仓库。"""
    sys.path.insert(0, CONSOLE)
    try:
        import extras_local
    except ImportError:
        notes.append("没有 console/extras_local.py, 跳过私有检查")
        return
    except Exception as e:                            # noqa: BLE001
        problems.append("extras_local.py 加载失败(%s: %s)" % (type(e).__name__, e))
        return
    fn = getattr(extras_local, "guard_check", None)
    if not callable(fn):
        return
    try:
        extra = fn() or []
    except Exception as e:                            # noqa: BLE001
        problems.append("extras_local.guard_check() 抛异常: %s: %s"
                        % (type(e).__name__, e))
        return
    for item in extra:
        problems.append(str(item))
    if not extra:
        notes.append("私有检查(extras_local.guard_check)通过")


# ---------------------------------------------------------------- 入口
def main(argv):
    heal = "--no-heal" not in argv
    quiet = "--quiet" in argv

    problems, notes = [], []
    check_runtime_importable(problems, notes)
    units = check_units(problems, notes, heal)
    checked = check_ports(problems, notes)
    check_private_hooks(problems, notes)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ok = not problems
    summary = "OK" if ok else "FAIL(%d)" % len(problems)

    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG_FILE, "a", errors="replace") as f:
            f.write("%s %s 单元 %d / 端口探测 %d\n"
                    % (stamp, summary, len(units), checked))
        with open(STATUS_FILE, "w", errors="replace") as f:
            json.dump({"time": stamp, "ok": ok, "problems": problems,
                       "notes": notes, "units": units}, f,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass

    if not quiet or not ok:
        print("==== remote 守护 %s ====" % stamp)
        for n in notes:
            print("  · %s" % n)
        for p in problems:
            print("  ✗ %s" % p)
        print("结果: %s" % ("健康 ✓" if ok else
                            "%d 个问题 —— 详见 %s" % (len(problems), STATUS_FILE)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
