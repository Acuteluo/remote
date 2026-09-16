#!/usr/bin/env python3
"""全量功能检查: 原有功能 + 后加的功能(告警/连接数/日志页/音视频)。

用法:
    DISPLAY=:0 python3 tests/all_check.py [PORT]

和 full_check.py 的分工: full_check 管"基础功能能不能用",
这个脚本管"所有页面/接口/新功能都在不在、行为对不对"。
"""
import base64
import http.client
import json
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _auth import login_body, wait_ready          # noqa: E402

HOST = os.environ.get("LUO_HOST", "127.0.0.1")
PORT = int(sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("LUO_PORT") or 8390))
CONSOLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def req(path, cookie=None, method="GET", body=None, hdr=None, timeout=20):
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    h = dict(hdr or {})
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body, h)
    r = c.getresponse()
    return r.status, r.read().decode("utf-8", "replace"), dict(r.getheaders())


def main():
    if not wait_ready(HOST, PORT, timeout=30):
        print(f"  ★ {HOST}:{PORT} 连不上")
        return 1

    st, _, h = req("/login", method="POST", body=login_body(),
                   hdr={"Content-Type": "application/x-www-form-urlencoded"})
    ck = (h.get("Set-Cookie") or "").split(";")[0]
    chk("登录成功", bool(ck))
    if not ck:
        return 1

    print()
    print("【1】六个页面渲染")
    pages = {"/login": None,
             "/home": ["alerts", "chips", "disks", "clock", "pw-btn"],
             "/vnc": ["vnc-ping", "vnc-audio", "set-panel", "set-vol", "vnc-shot",
                      "vnc-clients", "vnc-reset"],
             "/cam": ["cam-img", "cam-switch", "cam-audio", "cam-level"],
             "/term": ["terminal"],
             "/log": ["log-cats", "log-list", "log-refresh"]}
    for p, ids in pages.items():
        st, body, _ = req(p, ck if p != "/login" else None)
        ph = re.findall(r"\{\{[^}]+\}\}", body)
        chk(f"{p:8s} HTTP {st}", st == 200, f"{len(body)}B")
        chk(f"{p:8s} 无未替换占位符", not ph, str(ph[:2]) if ph else "")
        if ids:
            miss = [i for i in ids if f'id="{i}"' not in body]
            chk(f"{p:8s} 关键元素 {len(ids)} 个", not miss,
                ("缺: " + ",".join(miss)) if miss else "")

    print()
    print("【2】鉴权一致性")
    st, _, _ = req("/home")
    chk("未登录 GET 页面 → 302", st == 302, f"HTTP {st}")
    for p in ("/api/config", "/api/screen", "/api/dock", "/api/log", "/api/vnc/clients",
              "/api/volume"):
        st, _, _ = req(p)
        chk(f"未登录 GET {p} → 302", st == 302, f"HTTP {st}")
    st, _, _ = req("/api/type", method="POST", body="{}",
                   hdr={"Content-Type": "application/json"})
    chk("未登录 POST /api/type → 401", st == 401, f"HTTP {st}")
    st, _, _ = req("/api/password", method="POST", body="{}",
                   hdr={"Content-Type": "application/json"})
    chk("未登录 POST /api/password → 401", st == 401, f"HTTP {st}")

    print()
    print("【3】接口可用性（已登录）")
    for p in ("/api/health", "/api/config", "/api/dock", "/api/screen",
              "/api/clipboard", "/api/vnc/clients", "/api/log?n=50", "/api/audio/mics",
              "/api/volume"):
        st, body, _ = req(p, ck)
        good = False
        note = ""
        try:
            j = json.loads(body)
            good = j.get("ok") is True
            if not good:
                note = str(j.get("err"))[:40]
        except ValueError:
            pass
        if p == "/api/clipboard" and st == 200 and not good:
            # 剪贴板**没有属主**是完全正常的状态: 上次复制的程序一退出, X 选区就没了
            # (除非有剪贴板管理器)。这时接口如实回 {"ok":false,"err":"...选区无主"},
            # 不是故障 —— 别把它当失败(我一开始的断言写死了 ok=True, 会误报)。
            good = "无主" in note or "没有 xclip" in note
        if p == "/api/volume" and st == 200 and not good:
            # 音量接口在**没有 pulse/pactl 的环境**(隔离冒烟的沙箱实例)会如实回
            # {"ok":false,"vol":null,"err":"读不到电脑音量..."} —— 接口本身工作正常,
            # 不是故障。真实会话里则应回 ok=True + 实际音量。
            good = "读不到" in note
        chk(f"{p:22s} HTTP {st}", st == 200 and good, f"{len(body)}B" + (f" {note}" if note else ""))

    # 改密码: 原密码错误必须被拒(这条不改任何状态, 安全可跑)
    st, body, _ = req("/api/password", ck, method="POST",
                      body=json.dumps({"old": "wrong-old-password", "new": "whatever66"}),
                      hdr={"Content-Type": "application/json"})
    try:
        j = json.loads(body)
        chk("/api/password 原密码错误被拒", st == 200 and j.get("ok") is False
            and "不正确" in (j.get("err") or ""), (j.get("err") or "")[:30])
    except ValueError:
        chk("/api/password 原密码错误被拒", False, body[:60])

    print()
    print("【4】/ws/status 的 alerts + vnc_clients")
    snap = ws_snapshot(ck)
    chk("快照可解析", snap is not None)
    if snap:
        chk("含 vnc_clients", isinstance(snap.get("vnc_clients"), int),
            f"= {snap.get('vnc_clients')}")
        chk("含 alerts", isinstance(snap.get("alerts"), list),
            f"{len(snap.get('alerts') or [])} 条")
        chk("原有字段未丢", all(x in snap for x in (
            "cpu", "mem", "temps", "net", "disks", "battery", "gpu",
            "top_cpu", "top_mem", "windows", "peers", "load", "uptime",
            "host", "time")))

    print()
    print("【5】告警阈值逻辑（离线单测）")
    import importlib.util
    spec = importlib.util.spec_from_file_location("srv", os.path.join(CONSOLE, "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    col = m.Collector()
    col._port_alive = lambda p: True

    def mk(**kw):
        base = {"cpu": {"total": 20, "cores": [10] * 4},
                "mem": {"total": 100, "avail": 60, "swap_total": 0},
                "temps": [["coretemp/Package", 55]],
                "disks": [{"mp": "/", "total": 100, "free": 50}],
                "battery": None, "load": ["0.5"], "vnc_clients": 1}
        base.update(kw)
        return base

    def lv(s, key, level):
        return any(a["key"] == key and a["level"] == level for a in col.alerts(s))

    chk("全部正常 → 0 条告警", len(col.alerts(mk())) == 0)
    chk("内存 88% → warn", lv(mk(mem={"total": 100, "avail": 12, "swap_total": 0}), "mem", "warn"))
    chk("内存 96% → crit", lv(mk(mem={"total": 100, "avail": 4, "swap_total": 0}), "mem", "crit"))
    chk("温度 95 → crit", lv(mk(temps=[["coretemp/Package", 95]]), "temp", "crit"))
    chk("磁盘 95% → crit", lv(mk(disks=[{"mp": "/", "total": 100, "free": 5}]), "disk", "crit"))
    chk("负载 8.0/4核 → warn", lv(mk(load=["8.0"]), "load", "warn"))
    chk("VNC 5 连接 → crit", lv(mk(vnc_clients=5), "vnc", "crit"))
    chk("无效温度(0)被忽略", len(col.alerts(mk(temps=[["acpi", 0]]))) == 0)

    col2 = m.Collector()
    col2._port_alive = lambda p: True
    hot = {"cpu": {"total": 90, "cores": [90] * 4}}
    r1 = len(col2.alerts(mk(**hot)))
    r2 = len(col2.alerts(mk(**hot)))
    r3 = len(col2.alerts(mk(**hot)))
    chk("CPU 瞬时高不报(前 2 次)", r1 == 0 and r2 == 0, f"{r1},{r2}")
    chk("CPU 持续第 3 次才报", r3 == 1, f"{r3}")
    chk("CPU 回落立即清零", len(col2.alerts(mk())) == 0)

    print()
    print("【6】磁盘按设备去重")
    ds = m.Collector().disks()
    devs = {}
    try:
        with open("/proc/self/mounts") as f:
            for ln in f:
                p = ln.split()
                if p[0].startswith("/dev/"):
                    devs.setdefault(p[0], []).append(p[1])
    except OSError:
        pass
    multi = [d for d, mps in devs.items() if len(mps) > 1]
    chk("同一设备确实有多挂载点", bool(multi), f"{len(multi)} 个设备")
    chk("disks() 已去重", len(ds) <= len(devs), f"{len(ds)} 条 <= {len(devs)} 设备")

    print()
    print("【7】审计日志解析")
    st, body, _ = req("/api/log?n=100", ck)
    j = json.loads(body)
    rows = j.get("rows") or []
    chk("返回 rows", bool(rows), f"{len(rows)} 行")
    chk("每行含 d/t/c/m", all(all(x in r for x in ("d", "t", "c", "m")) for r in rows[:20]))
    chk("倒序（最新在前）", rows[0]["d"] + rows[0]["t"] >= rows[-1]["d"] + rows[-1]["t"])
    chk("含 counts 统计", isinstance(j.get("counts"), dict) and bool(j["counts"]))
    chk("含 cats 中文映射", isinstance(j.get("cats"), dict) and "login" in j["cats"])
    chk("时间格式 HH:MM:SS",
        all(re.fullmatch(r"\d{2}:\d{2}:\d{2}", r["t"]) for r in rows[:20]))
    chk("日期格式 YYYY-MM-DD",
        all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["d"]) for r in rows[:20]))

    print()
    print("【8】声音接口")
    st, body, _ = req("/api/audio/mics", ck)
    j = json.loads(body)
    chk("/api/audio/mics 结构", "usable" in j and "mics" in j,
        f"可用={j.get('usable') or '(无)'} 设备={len(j.get('mics') or [])}")
    chk("麦克风探测有缓存(页面加载不卡)",
        elapsed("/api/audio/mics", ck) < 1.0)

    print()
    print("【9】未登录不该泄漏数据")
    for p in ("/api/log", "/log"):
        st, _, _ = req(p)
        chk(f"未登录 {p} 被拦", st in (302, 401), f"HTTP {st}")

    print()
    print("=" * 46)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


def elapsed(path, ck):
    t0 = time.time()
    req(path, ck)
    return time.time() - t0


def ws_snapshot(ck):
    """连一次 /ws/status, 取最后一帧完整 JSON, 并**发 close 帧**后收尾。"""
    try:
        s = socket.create_connection((HOST, PORT), timeout=10)
    except OSError:
        return None
    k = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /ws/status HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
               f"Sec-WebSocket-Version: 13\r\nCookie: {ck}\r\n"
               f"Origin: http://{HOST}:{PORT}\r\n\r\n").encode())
    try:
        if b"101" not in s.recv(4096).split(b"\r\n")[0]:
            return None
    except OSError:
        return None
    s.settimeout(8)
    buf = b""
    try:
        while len(buf) < 300000:
            d = s.recv(65536)
            if not d:
                break
            buf += d
            if b'"alerts"' in buf and b'"vnc_clients"' in buf:
                break
    except socket.timeout:
        pass
    try:
        s.sendall(b"\x88\x80" + os.urandom(4))      # close 帧, 别在 x11vnc 上留僵尸
    except OSError:
        pass
    s.close()
    txt = buf.decode("utf-8", "replace")
    for st in reversed([m.start() for m in re.finditer(r'\{"time"', txt)]):
        try:
            snap, _ = json.JSONDecoder().raw_decode(txt[st:])
            return snap
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main())
