#!/usr/bin/env python3
"""诊断: 全屏**放大**之后 fps 会不会掉到 0(用户报的现象)。

用法: DISPLAY=:0 python3 tests/zoom_fps_probe.py [每档观察秒数]

逐档读全屏信息栏(里面同时有 fps 和 KB/s):
  进入全屏 → 基线 → 放大 2 倍 → 放大 4 倍 → 拖动 → 复位 → 退出全屏
如果 fps 掉 0 而 KB/s 还在, 说明有数据但不是 FramebufferUpdate;
两个都掉 0, 说明服务端根本没再发画面。
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds                           # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

URL = "http://127.0.0.1:8390"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def parse(txt):
    m = re.search(r"([\d.]+)fps", txt) if txt else None
    k = re.search(r"([\d.]+)\s*(KB/s|MB/s)", txt) if txt else None
    fps = float(m.group(1)) if m else None
    kbs = float(k.group(1)) if k else None
    if kbs is not None and k and k.group(2) == "MB/s":
        kbs *= 1024
    return fps, kbs


def watch(pg, label, secs=SECS):
    got = []
    t0 = time.time()
    while time.time() - t0 < secs:
        pg.wait_for_timeout(1000)
        txt = pg.evaluate(
            "() => (document.getElementById('vnc-fs-info')||{}).textContent || ''")
        f, k = parse(txt)
        got.append((f, k))
    fs = [g[0] for g in got if g[0] is not None]
    ks = [g[1] for g in got if g[1] is not None]
    print(f"  {label:18s} fps={['%.1f' % x for x in fs]}  "
          f"KB/s={['%.0f' % x for x in ks]}")
    return fs, ks


def fire(pg, pts_start, pts_move):
    pg.evaluate("""([a, b]) => {
        const el = document.getElementById('vnc-fs-tap');
        const mk = (p, i) => (typeof Touch === 'function'
            ? new Touch({identifier: i + 1, target: el, clientX: p[0], clientY: p[1]})
            : {identifier: i + 1, target: el, clientX: p[0], clientY: p[1]});
        const fire = (type, pts) => {
            const ts = pts.map(mk);
            let ev;
            if (typeof TouchEvent === 'function') {
                ev = new TouchEvent(type, {touches: ts, targetTouches: ts,
                                           changedTouches: ts, bubbles: true,
                                           cancelable: true});
            } else {
                ev = new Event(type, {bubbles: true, cancelable: true});
                ev.touches = ts; ev.targetTouches = ts; ev.changedTouches = ts;
            }
            el.dispatchEvent(ev);
        };
        fire('touchstart', a); fire('touchmove', b);
    }""", [pts_start, pts_move])


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852}, has_touch=True)
        pg = ctx.new_page()
        pg.add_init_script(
            "HTMLMediaElement.prototype.play=function(){return Promise.resolve();};")
        pg.goto(URL + "/login", wait_until="domcontentloaded")
        pg.wait_for_selector('input[name="user"]')
        pg.fill('input[name="user"]', u)
        pg.fill('input[name="pass"]', p)
        pg.click('button[type="submit"]')
        pg.wait_for_url("**/home")
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')")
        pg.wait_for_timeout(3000)
        pg.click("#vnc-fs")
        pg.wait_for_timeout(1200)
        print("逐档观察(每 1 秒采一次):")
        watch(pg, "基线(无缩放)")
        fire(pg, [[500, 380], [700, 380]], [[400, 380], [800, 380]])   # 2x
        pg.wait_for_timeout(500)
        print("   transform:", pg.evaluate(
            "() => document.getElementById('screen').style.transform"))
        watch(pg, "放大 2 倍")
        fire(pg, [[400, 380], [800, 380]], [[100, 380], [1200, 380]])  # → 4x
        pg.wait_for_timeout(500)
        print("   transform:", pg.evaluate(
            "() => document.getElementById('screen').style.transform"))
        watch(pg, "放大 4 倍")
        pg.evaluate("""() => {
            const el = document.getElementById('vnc-fs-tap');
            const mk = (x, y) => (typeof Touch === 'function'
                ? new Touch({identifier: 9, target: el, clientX: x, clientY: y})
                : {identifier: 9, target: el, clientX: x, clientY: y});
            const fire = (type, pts) => {
                const ts = pts;
                let ev;
                if (typeof TouchEvent === 'function') {
                    ev = new TouchEvent(type, {touches: ts, targetTouches: ts,
                                               changedTouches: ts, bubbles: true,
                                               cancelable: true});
                } else {
                    ev = new Event(type, {bubbles: true, cancelable: true});
                    ev.touches = ts; ev.targetTouches = ts; ev.changedTouches = ts;
                }
                el.dispatchEvent(ev);
            };
            fire('touchend', []);
            fire('touchstart', [mk(600, 400)]);
            fire('touchmove', [mk(400, 400)]);
        }""")
        watch(pg, "放大后拖动")
        pg.evaluate("""() => {
            const el = document.getElementById('vnc-fs-tap');
            const mk = (x, y) => (typeof Touch === 'function'
                ? new Touch({identifier: 9, target: el, clientX: x, clientY: y})
                : {identifier: 9, target: el, clientX: x, clientY: y});
            const fire = (type, pts) => {
                let ev;
                if (typeof TouchEvent === 'function') {
                    ev = new TouchEvent(type, {touches: pts, targetTouches: pts,
                                               changedTouches: pts, bubbles: true,
                                               cancelable: true});
                } else {
                    ev = new Event(type, {bubbles: true, cancelable: true});
                    ev.touches = pts; ev.targetTouches = pts; ev.changedTouches = pts;
                }
                el.dispatchEvent(ev);
            };
            fire('touchend', []);
            fire('touchstart', [mk(600, 400)]);
            fire('touchend', []);
            fire('touchstart', [mk(600, 400)]);
            fire('touchend', []);
        }""")
        pg.wait_for_timeout(500)
        print("   transform:", pg.evaluate(
            "() => document.getElementById('screen').style.transform"))
        watch(pg, "复位后")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
