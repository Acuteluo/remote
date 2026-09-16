#!/usr/bin/env python3
"""全屏(横屏)模式自检。

用法: DISPLAY=:0 python3 tests/fs_check.py [port]

检查: 元素就位 / 标签语义(网络延迟 vs 画面延时) / 进入全屏后布局正确且
**工具栏和触控板被隐藏** / 点屏幕先淡入再点退出 / ‹ 按钮退出 / 无 JS 报错。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds                        # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

PORT = int(sys.argv[1] if len(sys.argv) > 1 else 8390)
URL = f"http://127.0.0.1:{PORT}"
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852})
        pg = ctx.new_page()
        pg.set_default_timeout(25000)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:150]))

        pg.goto(URL + "/login", wait_until="domcontentloaded")
        pg.wait_for_selector('input[name="user"]')
        pg.fill('input[name="user"]', u)
        pg.fill('input[name="pass"]', p)
        pg.click('button[type="submit"]')
        pg.wait_for_url("**/home")
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')",
            timeout=25000)

        print("【1】元素就位")
        for k in ("vnc-fs", "vnc-fs-tap", "vnc-fs-ui", "vnc-fs-exit",
                  "vnc-fs-audio", "vnc-fs-info"):
            chk(f"#{k}", pg.evaluate(f"() => !!document.getElementById('{k}')"))
        chk("全屏按钮文字", "全屏" in pg.eval_on_selector("#vnc-fs", "e=>e.textContent"))

        pg.wait_for_timeout(2500)                 # 等帧率那一栏刷一次
        print("【2】标签语义")
        ping = pg.eval_on_selector("#vnc-ping", "e=>e.textContent")
        chk("延迟栏标明是网络", ping.startswith("网络"), ping)
        rate = pg.eval_on_selector("#vnc-rate", "e=>e.textContent")
        chk("帧率栏带画面延时", "画面" in rate, rate)

        print("【3】进入全屏")
        pg.click("#vnc-fs")
        pg.wait_for_timeout(900)
        chk("body 有 vnc-fs", pg.evaluate("() => document.body.classList.contains('vnc-fs')"))
        chk("触摸层已显示",
            pg.evaluate("() => !document.getElementById('vnc-fs-tap').classList.contains('hide')"))
        chk("控件层已显示",
            pg.evaluate("() => !document.getElementById('vnc-fs-ui').classList.contains('hide')"))
        chk("控件正在淡入",
            pg.evaluate("() => document.getElementById('vnc-fs-ui').classList.contains('show')"))
        chk("工具栏已隐藏",
            pg.evaluate("() => getComputedStyle(document.querySelector('.vnc-toolbar')).display") == "none")
        chk("触控板已隐藏",
            pg.evaluate("() => getComputedStyle(document.getElementById('pad-panel')).display") == "none")
        box = pg.evaluate("""() => {
            const r = document.getElementById('screen-wrap').getBoundingClientRect();
            const c = document.querySelector('#screen canvas');
            return {w: Math.round(r.width), h: Math.round(r.height),
                    vw: window.innerWidth, vh: window.innerHeight,
                    real: !!document.fullscreenElement,
                    cw: c ? c.width : 0, ch: c ? c.height : 0};
        }""")
        chk("画面铺满可用区域", box["w"] == box["vw"] and box["h"] == box["vh"],
            f"{box['w']}x{box['h']} / 视口 {box['vw']}x{box['vh']} 真全屏={box['real']}")

        print("【4】点屏幕: 只淡入控件, 不退出全屏")
        pg.wait_for_timeout(3600)
        chk("3.5 秒后自动淡出",
            pg.evaluate("() => !document.getElementById('vnc-fs-ui').classList.contains('show')"))
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(400)
        chk("点一下 → 控件淡入",
            pg.evaluate("() => document.getElementById('vnc-fs-ui').classList.contains('show')"))
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(500)
        chk("控件已显示时再点 → 仍然在全屏(不会误退)",
            pg.evaluate("() => document.body.classList.contains('vnc-fs')"))
        chk("控件还在(点屏幕只是重新计时)",
            pg.evaluate("() => document.getElementById('vnc-fs-ui').classList.contains('show')"))
        pg.wait_for_timeout(3800)
        chk("之后仍然自动收起",
            pg.evaluate("() => !document.getElementById('vnc-fs-ui').classList.contains('show')"))

        print("【5】‹ 退出 → 再进一次 → 再用 ‹ 退出")
        # 注意: 第 4 段结束时**仍然在全屏**(点屏幕不再退出), 而且控件已经自动收起了
        # —— 所以要先点一下屏幕让 ‹ 淡入, 才点得到它(控件层收起时是 pointer-events:none)。
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(400)
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(500)
        chk("点 ‹ → 回竖屏", not pg.evaluate("() => document.body.classList.contains('vnc-fs')"))
        chk("工具栏恢复",
            pg.evaluate("() => getComputedStyle(document.querySelector('.vnc-toolbar')).display") != "none")
        pg.click("#vnc-fs")
        pg.wait_for_timeout(800)
        chk("工具栏恢复后还能再进全屏",
            pg.evaluate("() => document.body.classList.contains('vnc-fs')"))
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(500)
        chk("再点 ‹ 仍然能退出", not pg.evaluate("() => document.body.classList.contains('vnc-fs')"))

        print("【6】无 JS 报错")
        chk("pageerror 为空", not errs, "; ".join(errs[:2]))
        ctx.close()
        b.close()
    print()
    print("=" * 46)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
