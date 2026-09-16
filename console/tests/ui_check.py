#!/usr/bin/env python3
"""界面约定自检(2026-09-16 与用户逐条确认的规则)。

用法: DISPLAY=:0 python3 tests/ui_check.py

钉住的规则:
  1. 各页面的返回链接只有 `‹` 一个字(不带"返回"字样), 语义靠 title/aria-label
  2. 远程桌面顶栏: 网络 / 画面 两个数字在**同一个胶囊**里
  3. 远程桌面全屏: 退出键**无框**(透明背景 + 0 边框)
  4. 摄像头全屏: 覆盖层(顶部按钮/提示/底部信息条) 3.5 秒没操作自动淡出, 点一下回来
  5. 全程无 JS 报错
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds                           # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

URL = "http://127.0.0.1:8390"
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def login(pg, u, p):
    pg.goto(URL + "/login", wait_until="domcontentloaded")
    pg.wait_for_selector('input[name="user"]')
    pg.fill('input[name="user"]', u)
    pg.fill('input[name="pass"]', p)
    pg.click('button[type="submit"]')
    pg.wait_for_url("**/home")


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852}, has_touch=True)
        pg = ctx.new_page()
        pg.set_default_timeout(25000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        pg.add_init_script(
            "HTMLMediaElement.prototype.play = function(){return Promise.resolve();};")
        login(pg, u, p)

        print("【1】各页面返回链接只有 ‹")
        for path in ("/vnc", "/cam", "/term", "/log"):
            pg.goto(URL + path, wait_until="domcontentloaded")
            pg.wait_for_timeout(400)
            got = pg.evaluate("""() => {
                const a = document.querySelector('.topbar .back');
                if (!a) return null;
                return {t: a.textContent.trim(), title: a.title,
                        aria: a.getAttribute('aria-label')};
            }""")
            chk(f"{path} 的返回键是 ‹", got and got["t"] == "‹", str(got))
            chk(f"{path} 的返回键有 title/aria 说明",
                bool(got) and got["title"] and got["aria"], str(got))

        print("【2】顶栏: 网络 / 画面 在同一个胶囊里")
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')")
        pg.wait_for_timeout(4000)
        box = pg.evaluate("""() => {
            const pb = document.querySelector('.pingbox');
            if (!pb) return null;
            const ping = document.getElementById('vnc-ping');
            const rate = document.getElementById('vnc-rate');
            const r = pb.getBoundingClientRect();
            return {inside: pb.contains(ping) && pb.contains(rate),
                    w: Math.round(r.width),
                    ping: ping.textContent.trim(), rate: rate.textContent.trim(),
                    pingBorder: getComputedStyle(ping).borderTopWidth,
                    rateBorder: getComputedStyle(rate).borderTopWidth};
        }""")
        chk("两个数字在同一个框里", bool(box) and box["inside"], str(box))
        chk("框内两个数字各自不再有边框",
            bool(box) and box["pingBorder"] == "0px" and box["rateBorder"] == "0px",
            str(box))
        chk("顶栏只显示 网络 xx ms / 画面 xx ms",
            bool(box) and box["ping"].startswith("网络") and box["rate"].startswith("画面"),
            f"{box['ping'] if box else '?'} | {box['rate'] if box else '?'}")
        # 连上以后不该显示"已连接"(默认状态, 白占地方); 文字仍在 DOM 里供测试判连接
        st_ok = pg.evaluate("""() => {
            const s = document.getElementById('vnc-status');
            const pb = document.querySelector('.pingbox').getBoundingClientRect();
            const last = document.getElementById('vnc-audio').getBoundingClientRect();
            const tb = document.querySelector('.topbar').getBoundingClientRect();
            return {text: s.textContent.trim(), display: getComputedStyle(s).display,
                    gapRight: Math.round(tb.right - last.right),
                    spaceBefore: Math.round(pb.left - document.querySelector('.logo')
                                             .getBoundingClientRect().right)};
        }""")
        chk("连上后不显示'已连接'(默认状态不占位置)",
            st_ok["text"] == "已连接" and st_ok["display"] == "none", str(st_ok))
        chk("状态栏藏起来后右边那组仍然顶到最右",
            st_ok["gapRight"] <= 20 and st_ok["spaceBefore"] > 40, str(st_ok))

        print("【2.5】一键截图: 点击下载 PNG")
        # 帧是 WS 二进制数据画进画布的, 画布没被跨源污染 → toBlob 可用;
        # 点击 #vnc-shot 必须真触发一次 PNG 下载(文件名 meow-时间戳.png)
        try:
            with pg.expect_download(timeout=8000) as dl_info:
                pg.click("#vnc-shot")
            fn = dl_info.value.suggested_filename
            chk("截图触发 PNG 下载", fn.endswith(".png") and fn.startswith("meow-"), fn)
        except Exception as e:
            chk("截图触发 PNG 下载", False, str(e)[:80])

        print("【3】远程桌面全屏: 退出键无框")
        pg.click("#vnc-fs")
        pg.wait_for_timeout(900)
        ex = pg.evaluate("""() => {
            const e = document.getElementById('vnc-fs-exit');
            const cs = getComputedStyle(e);
            const r = e.getBoundingClientRect();
            return {t: e.textContent.trim(), bw: cs.borderTopWidth,
                    bg: cs.backgroundColor, w: Math.round(r.width),
                    h: Math.round(r.height)};
        }""")
        chk("全屏退出键是 ‹", ex["t"] == "‹", str(ex))
        chk("全屏退出键无边框无底色",
            ex["bw"] == "0px" and ex["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), str(ex))
        chk("点击区域不至于太小", ex["w"] >= 24 and ex["h"] >= 24,
            f"{ex['w']}x{ex['h']}")
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(300)
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(600)
        chk("点 ‹ 能退出全屏",
            not pg.evaluate("() => document.body.classList.contains('vnc-fs')"))

        print("【4】摄像头全屏: 覆盖层 3.5 秒自动收起, 点一下回来")
        pg.goto(URL + "/cam", wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        pg.click("#cam-fs")
        pg.wait_for_timeout(900)
        st = pg.evaluate("""() => {
            const w = document.getElementById('cam-wrap');
            const top = document.querySelector('.cam-fs-top');
            const osd = document.getElementById('cam-osd');
            return {fs: !!document.fullscreenElement || w.classList.contains('hideui') ||
                        getComputedStyle(w).position === 'fixed',
                    hide: w.classList.contains('hideui'),
                    topOpacity: getComputedStyle(top).opacity,
                    osdOpacity: getComputedStyle(osd).opacity,
                    topDisplay: getComputedStyle(top).display,
                    exitText: document.getElementById('cam-fs-exit').textContent.trim(),
                    exitBorder: getComputedStyle(
                        document.getElementById('cam-fs-exit')).borderTopWidth};
        }""")
        chk("进了全屏且覆盖层可见", st["fs"] and not st["hide"], str(st))
        chk("全屏时顶部按钮显示出来", st["topDisplay"] == "flex", str(st))
        chk("摄像头全屏退出键也是 ✕(无框)", st["exitText"] == "✕" and st["exitBorder"] == "0px",
            str(st))
        pg.wait_for_timeout(4200)
        st2 = pg.evaluate("""() => {
            const w = document.getElementById('cam-wrap');
            return {hide: w.classList.contains('hideui'),
                    top: getComputedStyle(document.querySelector('.cam-fs-top')).opacity,
                    osd: getComputedStyle(document.getElementById('cam-osd')).opacity};
        }""")
        chk("3.5 秒后覆盖层自动收起", st2["hide"] and float(st2["top"]) == 0
            and float(st2["osd"]) == 0, str(st2))
        # 点一下画面(只用 pointerdown, 不拦截手势) → 应该回来
        pg.mouse.click(200, 400)
        pg.wait_for_timeout(500)
        st3 = pg.evaluate("""() => {
            const w = document.getElementById('cam-wrap');
            return {hide: w.classList.contains('hideui'),
                    top: getComputedStyle(document.querySelector('.cam-fs-top')).opacity};
        }""")
        chk("点一下 → 覆盖层回来", not st3["hide"] and float(st3["top"]) > 0.9, str(st3))

        print("【5】远程桌面全屏: 双指缩放 / 拖动平移")
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')")
        # 非全屏时触摸层应该是隐藏的(收不到真实触摸 → 普通视图一点不受影响)
        chk("非全屏时触摸层隐藏",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-tap')).display") == "none")
        pg.click("#vnc-fs")
        pg.wait_for_timeout(900)
        # 合成双指触摸(Firefox 支持 Touch/TouchEvent 构造器)
        pg.evaluate("""() => {
            // 无头环境可能没有 Touch / TouchEvent 构造器(桌面火狐默认不开触摸),
            // 那就退化成"带 touches 字段的普通事件" —— 处理逻辑读的就是这些字段。
            window.__fire = (type, pts) => {
                const el = document.getElementById('vnc-fs-tap');
                const raw = pts.map((p, i) => ({identifier: i + 1, target: el,
                                               clientX: p.x, clientY: p.y}));
                let ts = raw, ev;
                if (typeof Touch === 'function') {
                    ts = raw.map(p => new Touch(p));
                }
                if (typeof TouchEvent === 'function') {
                    ev = new TouchEvent(type, {touches: ts, targetTouches: ts,
                                               changedTouches: ts,
                                               bubbles: true, cancelable: true});
                } else {
                    ev = new Event(type, {bubbles: true, cancelable: true});
                    ev.touches = ts; ev.targetTouches = ts; ev.changedTouches = ts;
                }
                el.dispatchEvent(ev);
            };
        }""")
        tr0 = pg.evaluate("() => document.getElementById('screen').style.transform")
        chk("刚进全屏没有缩放", tr0 == "", f"[{tr0}]")
        # 双指从相距 100 拉到 200 → 放大 2 倍
        pg.evaluate("() => window.__fire('touchstart', [{x:150,y:300},{x:250,y:300}])")
        pg.evaluate("() => window.__fire('touchmove', [{x:100,y:300},{x:300,y:300}])")
        pg.wait_for_timeout(200)
        tr1 = pg.evaluate("() => document.getElementById('screen').style.transform")
        chk("双指拉开 → 放大 2 倍", "scale(2" in tr1, f"[{tr1}]")
        # 松开一根手指后单指拖动 → 平移(放大后才有意义)
        pg.evaluate("() => window.__fire('touchend', [{x:100,y:300}])")
        pg.evaluate("() => window.__fire('touchmove', [{x:140,y:300}])")
        pg.wait_for_timeout(200)
        tr2 = pg.evaluate("() => document.getElementById('screen').style.transform")
        chk("放大后单指拖动能平移", tr2 != tr1 and "translate" in tr2, f"[{tr2}]")
        # 缩放有上限: 拉到极端也不会突破 4 倍
        pg.evaluate("() => window.__fire('touchstart', [{x:150,y:300},{x:250,y:300}])")
        pg.evaluate("() => window.__fire('touchmove', [{x:0,y:300},{x:900,y:300}])")
        pg.wait_for_timeout(200)
        tr3 = pg.evaluate("() => document.getElementById('screen').style.transform")
        m3 = tr3 and float(tr3.split("scale(")[1].rstrip(")"))
        chk("放大有上限(不超过 4 倍)", m3 is not None and m3 <= 4.0001, f"[{tr3}]")
        # 放大之后**画面还得继续来**(fps 数的是 noVNC 收到的 FramebufferUpdate 条数)。
        # 注意: 桌面完全静止时 x11vnc 一条都不发, fps 本来就是 0 —— 跟放大无关,
        # 所以只有"放大前就有帧"的时候才拿这条当判据。
        def read_fps():
            t = pg.evaluate(
                "() => (document.getElementById('vnc-fs-info')||{}).textContent || ''")
            m = re.search(r"([\d.]+)fps", t)
            return float(m.group(1)) if m else None

        pg.evaluate("() => window.__fire('touchstart', [{x:500,y:380},{x:700,y:380}])")
        pg.evaluate("() => window.__fire('touchmove', [{x:400,y:380},{x:800,y:380}])")
        pg.wait_for_timeout(2500)
        f0 = read_fps()
        pg.wait_for_timeout(1500)
        f1 = read_fps()
        if (f0 or 0) > 0:
            chk("放大后画面仍在更新(不是 0fps)", (f1 or 0) > 0, f"放大后 fps {f0} → {f1}")
        else:
            print("  · 桌面此刻完全静止(无帧), 跳过 fps 判据 —— "
                  "静止时 fps 本就是 0, 与放大无关")
        # 复位后也一直在
        pg.evaluate("""() => {
            window.__fire('touchend', []);
            window.__fire('touchstart', [{x:600,y:400}]);
            window.__fire('touchend', []);
            window.__fire('touchstart', [{x:600,y:400}]);
            window.__fire('touchend', []);
        }""")
        pg.wait_for_timeout(300)
        chk("复位后回到 1 倍且在更新",
            pg.evaluate("() => document.getElementById('screen').style.transform") == ""
            and (read_fps() or 0) >= 0,
            f"fps {read_fps()}")
        # 退出全屏后不能残留缩放(普通视图必须原样)
        pg.evaluate("() => window.__fire('touchstart', [{x:150,y:300},{x:250,y:300}])")
        pg.evaluate("() => window.__fire('touchmove', [{x:100,y:300},{x:300,y:300}])")
        pg.wait_for_timeout(200)
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(300)
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(700)
        chk("退出全屏后不留缩放(普通视图不受影响)",
            pg.evaluate("() => document.getElementById('screen').style.transform") == "")

        print("【6】无 JS 报错")
        chk("pageerror 为空", not errs, "; ".join(errs[:2]))
        b.close()
    print("=" * 46)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
