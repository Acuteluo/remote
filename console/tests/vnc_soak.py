#!/usr/bin/env python3
"""前端浸泡测试: 真浏览器挂着不刷新, 看会不会掉线 / 反复重连 / 报错。

用法: DISPLAY=:0 python3 tests/vnc_soak.py [秒数]   (默认 75 秒)

为什么要它: 其它测试都是"连一下、验一件事、断开"。真实使用是**长时间挂着** ——
掉线、重连风暴、内存里越堆越多这类问题只有挂着才看得出来。
顺带验: 页面显示的"画面 xx ms"确实等于 网络/2 + 25, 以及**关掉页面后
x11vnc 上的连接会不会干净地消失**(这条直接关系到"越用越卡")。
"""
import http.client
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds, login                   # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 75
URL = "http://127.0.0.1:8390"
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def clients(cookie):
    c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c.request("GET", "/api/vnc/clients", None, {"Cookie": cookie})
    j = json.loads(c.getresponse().read().decode())
    return j.get("clients"), j.get("bridges")


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852})
        pg = ctx.new_page()
        pg.set_default_timeout(30000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        pg.goto(URL + "/login", wait_until="domcontentloaded")
        pg.wait_for_selector('input[name="user"]')
        pg.fill('input[name="user"]', u)
        pg.fill('input[name="pass"]', p)
        pg.click('button[type="submit"]')
        pg.wait_for_url("**/home")

        ck = login()          # 给 /api/vnc/clients 用(和浏览器各自独立的一份会话)
        # 打开页面前先记基线: 你自己手机/电脑上可能也连着, 客户端数本来就不是 0
        base, _ = clients(ck)
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')",
            timeout=30000)
        print(f"  已连接, 开始浸泡 {SECS}s (每 3 秒采样一次)")
        c_on, br_on = clients(ck)
        chk("页面挂上后客户端数只多 1 个", c_on == (base or 0) + 1,
            f"clients={c_on} (你原有 {base} 个)")

        samples, reconnects, prev = [], 0, None
        t0 = time.time()
        while time.time() - t0 < SECS:
            pg.wait_for_timeout(3000)
            st = pg.evaluate("""() => {
                const g = id => (document.getElementById(id) || {}).textContent || '';
                const rate = document.getElementById('vnc-rate') || {};
                return {s: g('vnc-status'), r: g('vnc-rate'),
                        p: g('vnc-ping'), t: rate.title || ''};
            }""")
            samples.append((round(time.time() - t0), st["s"].strip(),
                            st["r"].strip(), st["p"].strip(), st["t"]))
            if prev is not None and st["s"].strip() != prev:
                reconnects += 1
                print(f"    [{samples[-1][0]:>3}s] 状态变化: {prev} → {st['s'].strip()}")
            prev = st["s"].strip()

        chk("全程没有掉线/重连", reconnects == 0, f"状态变化 {reconnects} 次")
        chk("全程无 JS 报错", not errs, "; ".join(errs[:2]))

        fps, pic, net = [], [], []
        for _, s, r, p, t in samples:
            # 帧率/码率在 tooltip 里 —— 顶栏只放"画面 xx ms"(手机顶栏太窄)
            m = re.search(r"([\d.]+) fps", t)
            if m:
                fps.append(float(m.group(1)))
            m = re.search(r"画面 (\d+)ms", r)
            if m:
                pic.append(int(m.group(1)))
            m = re.search(r"网络 (\d+)ms", p)
            if m:
                net.append(int(m.group(1)))
        chk("帧率栏一直在报数(不是 -- )", len(fps) >= len(samples) - 1,
            f"{len(fps)}/{len(samples)} 次有数字, 最低 {min(fps) if fps else '-'}fps")
        chk("有画面时确实在出帧", bool(fps) and max(fps) > 0,
            f"最高 {max(fps) if fps else '-'}fps (静止桌面出帧本来就少)")
        chk("网络延迟一直有数", len(net) >= len(samples) - 1,
            f"中位 {sorted(net)[len(net) // 2] if net else '-'}ms")
        # 画面延时 = 网络单程 + 25ms, 这是页面上那行字的定义, 必须对得上
        ok_pic = bool(pic) and bool(net) and all(
            abs(v - (net[min(i, len(net) - 1)] // 2 + 25)) <= 2
            for i, v in enumerate(pic))
        chk("画面延时 = 网络单边 + 25ms(标注与算法一致)", ok_pic,
            f"画面 {sorted(set(pic))[:4]} / 网络 {sorted(set(net))[:4]}")

        print("  最后 3 次采样: " + " | ".join(f"{t}s {s} {r}" for t, s, r, _, _ in samples[-3:]))

        # 关掉页面: x11vnc 上的连接必须干净消失(这条就是"越用越卡"的根)
        pg.close()
        ctx.close()
        b.close()
        gone, left = None, None
        t1 = time.time()
        while time.time() - t1 < 10:
            left, _ = clients(ck)
            if left == 0:
                gone = time.time() - t1
                break
            time.sleep(0.3)
        chk("关掉页面后 x11vnc 上的连接干净消失", gone is not None or (left or 0) <= (base or 0),
            f"{gone:.1f}s" if gone else f"10s 后还剩 {left} 个 (你原有 {base} 个)")
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
