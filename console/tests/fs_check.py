#!/usr/bin/env python3
"""全屏(横屏)模式自检。

用法: DISPLAY=:0 python3 tests/fs_check.py [port]

检查: 元素就位 / 标签语义(网络延迟 vs 画面延时) / 进入全屏后布局正确且
**工具栏和触控板被隐藏** / 点屏幕先淡入再点退出 / ‹ 按钮退出 /
滚轮按钮: 开关出现与收回、只发滚轮不晃出控件、退出收回、重进自动恢复 / 无 JS 报错。
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
                  "vnc-fs-audio", "vnc-fs-info", "vnc-fs-wheel",
                  "vnc-fs-wheelbtns", "vnc-fs-wup", "vnc-fs-wdn"):
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

        print("【5】全屏滚轮按钮: 开关 -> 出现 -> 只滚轮不晃控件 -> 退出收回 -> 重进自动恢复")
        # 第 4 段结束时仍在全屏且控件已自动收起。滚轮钮默认关着(新会话 localStorage 是空的)。
        chk("开关默认不亮",
            pg.evaluate("() => !document.getElementById('vnc-fs-wheel').classList.contains('on')"))
        chk("滚轮钮默认隐藏",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display") == "none")
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(400)
        pg.click("#vnc-fs-wheel")
        pg.wait_for_timeout(300)
        chk("开关点亮",
            pg.evaluate("() => document.getElementById('vnc-fs-wheel').classList.contains('on')"))
        chk("滚轮钮出现",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display") == "flex")
        pos = pg.evaluate("""() => {
            const r = document.getElementById('vnc-fs-wheelbtns').getBoundingClientRect();
            return {right: window.innerWidth - r.right, cy: r.top + r.height / 2,
                    vh: window.innerHeight};
        }""")
        chk("滚轮钮贴右缘垂直居中",
            pos["right"] <= 30 and abs(pos["cy"] - pos["vh"] / 2) < pos["vh"] / 3,
            f"右边距={pos['right']:.0f}px 中点y={pos['cy']:.0f}/{pos['vh']}")
        bg = pg.eval_on_selector("#vnc-fs-wdn", "e => getComputedStyle(e).backgroundColor")
        chk("滚轮钮半透明", "rgba" in bg and not bg.endswith(", 1)"), bg)
        pg.wait_for_timeout(3700)             # 等顶部控件自己淡出
        chk("控件淡出后滚轮钮仍挂着",
            pg.evaluate("""() => !document.getElementById('vnc-fs-ui').classList.contains('show')
                                && getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display === 'flex'"""))
        pg.click("#vnc-fs-wdn")               # 点滚轮钮本身(能点到 = 压在触摸层上方)
        pg.wait_for_timeout(400)
        chk("点滚轮钮不退出全屏",
            pg.evaluate("() => document.body.classList.contains('vnc-fs')"))
        chk("点滚轮钮不晃出控件",
            pg.evaluate("() => !document.getElementById('vnc-fs-ui').classList.contains('show')"))
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(400)
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(500)
        chk("退出全屏滚轮钮收回",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display") == "none")
        pg.click("#vnc-fs")
        pg.wait_for_timeout(800)
        chk("重进全屏自动恢复(上次开着)",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display") == "flex")
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(400)
        pg.click("#vnc-fs-wheel")             # 关掉, 也验证关闭路径
        pg.wait_for_timeout(300)
        chk("再点开关滚轮钮收回",
            pg.evaluate("() => getComputedStyle(document.getElementById('vnc-fs-wheelbtns')).display") == "none")
        chk("开关已熄灭",
            pg.evaluate("() => !document.getElementById('vnc-fs-wheel').classList.contains('on')"))

        print("【6】‹ 退出 → 再进一次 → 再用 ‹ 退出")
        # 注意: 前面各段结束时**仍然在全屏**(点屏幕/滚轮钮都不会退出), 控件可能已
        # 自动收起 —— 所以先点一下屏幕让 ‹ 淡入, 才点得到它(控件层收起时是
        # pointer-events:none)。
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

        print("【7】无 JS 报错")
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
