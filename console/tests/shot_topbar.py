#!/usr/bin/env python3
"""手机宽度(393px)下的顶栏/声音条版式快照 —— 改完 CSS 自己先看一眼。

用法: DISPLAY=:0 python3 tests/shot_topbar.py [输出目录]
只截图, 不做断言; 断言在 fs_check / audio_mse_check / vnc_soak 里。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds                           # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/home/cly/.cache/luo-shots"
URL = "http://127.0.0.1:8390"


def main():
    os.makedirs(OUT, exist_ok=True)
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852})
        pg = ctx.new_page()
        # 无头浏览器没有音频设备, play() 会 reject → 声音条会自己收起来。存根掉。
        pg.add_init_script(
            "HTMLMediaElement.prototype.play = function(){return Promise.resolve();};")
        pg.goto(URL + "/login", wait_until="domcontentloaded")
        pg.wait_for_selector('input[name="user"]')
        pg.fill('input[name="user"]', u)
        pg.fill('input[name="pass"]', p)
        pg.click('button[type="submit"]')
        pg.wait_for_url("**/home")
        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')",
            timeout=30000)
        pg.wait_for_timeout(6000)          # 等延迟/帧率栏有数

        def snap(name, extra_js=None):
            if extra_js:
                pg.evaluate(extra_js)
                pg.wait_for_timeout(400)
            path = os.path.join(OUT, name)
            pg.screenshot(path=path)
            top = pg.evaluate("""() => {
                const g = id => (document.getElementById(id) || {}).textContent || '';
                const r = document.querySelector('.topbar').getBoundingClientRect();
                const back = document.querySelector('.back').getBoundingClientRect();
                const logo = document.querySelector('.logo').getBoundingClientRect();
                return {topbarH: Math.round(r.height), w: Math.round(r.width),
                        backH: Math.round(back.height), logoH: Math.round(logo.height),
                        status: g('vnc-status'), ping: g('vnc-ping'), rate: g('vnc-rate')};
            }""")
            print(f"  {name}: 顶栏 {top['w']}x{top['topbarH']}  "
                  f"返回高 {top['backH']}  标题高 {top['logoH']}")
            print(f"     状态[{top['status']}]  {top['ping']}  {top['rate']}")
            return top

        print("手机宽度 393px:")
        t1 = snap("topbar-normal.png")
        # 最长文案的极端情况: 状态栏会不会把别的挤坏
        t2 = snap("topbar-longstatus.png",
                  "() => {document.getElementById('vnc-status').textContent = "
                  "'重连中…(远程桌面服务已重启，正在自动恢复)';}")
        # 设置面板里的电脑音量滑块(替代原画面上的悬浮声音按钮) —— 设到 130% 截红色态
        pg.click("#vnc-setbtn")
        pg.wait_for_timeout(900)               # 等 refreshVolume 读回当前音量
        real = pg.evaluate("""() => {
            const v = document.getElementById('set-vol');
            return v ? Number(v.value) : null;
        }""")
        pg.evaluate("""() => {
            const v = document.getElementById('set-vol');
            if (v) { v.value = '130';
                     v.dispatchEvent(new Event('input', {bubbles: true})); }
        }""")
        pg.wait_for_timeout(400)
        snap("setpanel-volume.png")
        vol = pg.evaluate("""() => {
            const v = document.getElementById('set-vol');
            const lab = document.getElementById('set-vol-v');
            return {min: v && v.min, max: v && v.max,
                    label: lab ? lab.textContent.trim() : '',
                    red: lab ? lab.style.color !== '' : false};
        }""")
        print(f"  电脑音量滑块: 范围 {vol['min']}~{vol['max']}  "
              f"标签[{vol['label']}]  超100变红={vol['red']}")
        # 恢复真实音量再关面板
        if real is not None:
            pg.evaluate(f"""() => {{
                const v = document.getElementById('set-vol');
                if (v) {{ v.value = '{real}';
                         v.dispatchEvent(new Event('input', {{bubbles: true}})); }}
            }}""")
            pg.wait_for_timeout(700)
        pg.click("#vnc-setbtn")

        # 全屏(横屏): 退出键应该是无框的 ‹
        pg.click("#vnc-fs")
        pg.wait_for_timeout(1000)
        pg.screenshot(path=os.path.join(OUT, "vnc-fullscreen.png"))
        print("  vnc-fullscreen.png  (全屏: ‹ 无框)")
        # 双指放大(合成触摸) 后截一张, 确认画面真的被放大了
        pg.evaluate("""() => {
            const el = document.getElementById('vnc-fs-tap');
            const mk = (p, i) => (typeof Touch === 'function'
                ? new Touch({identifier: i + 1, target: el, clientX: p.x, clientY: p.y})
                : {identifier: i + 1, target: el, clientX: p.x, clientY: p.y});
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
            fire('touchstart', [{x: 500, y: 380}, {x: 700, y: 380}]);
            fire('touchmove', [{x: 400, y: 380}, {x: 800, y: 380}]);
        }""")
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(OUT, "vnc-fullscreen-zoom.png"))
        print("  vnc-fullscreen-zoom.png  (全屏: 双指放大后)")
        pg.click("#vnc-fs-tap", position={"x": 200, "y": 400})
        pg.wait_for_timeout(300)
        pg.click("#vnc-fs-exit")
        pg.wait_for_timeout(500)

        # 摄像头全屏: 覆盖层自动收起前后各一张
        pg.goto(URL + "/cam", wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        pg.click("#cam-fs")
        pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(OUT, "cam-fullscreen-ui.png"))
        print("  cam-fullscreen-ui.png  (全屏: 控件可见)")
        pg.wait_for_timeout(4200)
        pg.screenshot(path=os.path.join(OUT, "cam-fullscreen-clean.png"))
        print("  cam-fullscreen-clean.png  (全屏: 3.5s 后自动收起)")
        b.close()
    print(f"图已存到 {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
