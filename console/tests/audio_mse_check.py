#!/usr/bin/env python3
"""声音播放自检（MSE 低延迟路径 + 降级）。

用法: DISPLAY=:0 python3 tests/audio_mse_check.py

为什么不能在这里测出真实延迟: 无头浏览器没有音频输出设备, `currentTime` 不前进。
所以本脚本只验证三件事:
  1. 浏览器支持 audio/mpeg 的 MSE 时 → 走 MSE(blob: 源)
  2. 不支持 / MSE 抛错时 → **自动退回** 普通 <audio>, 不崩、不留未捕获异常
  3. 开启/关闭时服务端采集确实启停（查审计日志, 不靠 ps —— 沙箱看不到真实会话的进程）

真实的声音延迟要在手机上用耳朵判断(页面上那栏已经不显示了)。
"""
import http.client
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _auth import creds, login_body            # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

URL = "http://127.0.0.1:8390"
OK, BAD = [], []


def chk(n, c, e=""):
    (OK if c else BAD).append(n)
    print(f"  {'✓' if c else '★'} {n}" + (f"   {e}" if e else ""))


def audio_events(since=None):
    """审计日志里和声音有关的记录, 返回 [(时刻, 消息)]。

    **按时间戳判定, 不要数"最后 N 行里有几条"**: 日志窗口是滑动的, 新写进来
    一行就会把最老的一行挤出去, 条数可能原地不动 —— 实测出现过 "6 -> 6" 的
    假失败(服务端明明记了"开始采集")。since="HH:MM:SS" 只取该时刻之后的。
    """
    c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c2 = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c2.request("GET", "/api/log?n=400", None, {"Cookie": ck})
    j = json.loads(c2.getresponse().read().decode())
    out = [(x["t"], x["m"]) for x in (j.get("rows") or [])
           if ("声音" in x["m"] or "选源" in x["m"])]
    if since:
        out = [r for r in out if r[0] >= since]
    return out


def pc_volume_via_api():
    """经 /api/volume 读**服务端**视角的当前电脑音量(整数百分比)。

    浏览器/本脚本在沙箱里看不到 pulse socket, 但服务端跑在真实会话里能调 pactl,
    所以以服务端返回值为准。失败返回 None。
    """
    c = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c.request("POST", "/login", login_body(),
              {"Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c2 = http.client.HTTPConnection("127.0.0.1", 8390, timeout=10)
    c2.request("GET", "/api/volume", None, {"Cookie": ck})
    try:
        j = json.loads(c2.getresponse().read().decode())
    except ValueError:
        return None
    return j.get("vol") if j.get("ok") else None


def wait_audio_event(pg, since, word, tries=8):
    """等审计日志里出现含 word 的声音事件(每秒查一次, 最多 tries 秒)。"""
    ev = []
    for _ in range(tries):
        ev = audio_events(since)
        if any(word in m for _, m in ev):
            break
        pg.wait_for_timeout(1000)
    return ev


def run_case(b, u, p, force_mse):
    label = "强制 MSE 路径(验证降级)" if force_mse else "浏览器原生能力"
    print(f"【{label}】")
    ctx = b.new_context(viewport={"width": 393, "height": 852})
    pg = ctx.new_page()
    pg.set_default_timeout(25000)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:140]))
    if force_mse:
        # 让页面以为支持 MSE —— 用来验证 addSourceBuffer 抛错时能退回普通播放
        pg.add_init_script("""
            try { if (window.MediaSource) {
                MediaSource.isTypeSupported = function () { return true; };
            } } catch (e) {}
        """)
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

    sup = pg.evaluate("""() => {
        try { return {mp3: MediaSource.isTypeSupported('audio/mpeg')}; }
        catch (e) { return {mp3: false}; }
    }""")
    t_on = time.strftime("%H:%M:%S")
    pg.click("#vnc-audio")
    pg.wait_for_timeout(3000)
    # 服务端要先挑源(探测可能要 1~2 秒)才启动 ffmpeg, 所以这里轮询等, 别硬等固定时长
    ev = wait_audio_event(pg, t_on, "开始采集")
    st = pg.evaluate("""() => {
        const a = document.getElementById('vnc-audio-el');
        return {blob: (a.src || '').startsWith('blob:'),
                src: (a.src || '').slice(0, 30)};
    }""")
    # 断言的是**行为**而不是"一定用 MSE": 支持 MSE 且能起来就用 blob 源;
    # 报支持但 addSourceBuffer 失败(比如 Firefox 不支持 audio/mpeg 的 MSE)
    # 必须自动退回普通 <audio> —— 关键是**一定要有声音**, 不能什么都不播。
    used = "MSE(blob)" if st["blob"] else "普通 <audio>"
    chk(f"[{label}] 声音元素已挂上源", bool(st["blob"]) or st["src"].startswith("http"),
        f"MSE能力={sup['mp3']} → 实际走 {used}")
    chk(f"[{label}] 至少有一条播放路径生效",
        st["blob"] or st["src"].startswith("http"), str(st))
    chk(f"[{label}] 服务端开始采集",
        any("开始采集" in m for _, m in ev),
        f"{len(ev)} 条: {[m[:16] for _, m in ev][:2]}")
    chk(f"[{label}] 无未捕获异常", not errs, "; ".join(errs[:2]))

    # 电脑音量滑块(2026-09-16 替代画面上的悬浮静音按钮): 在设置面板里, 范围 0~150,
    # 超 100 变红, 改动经 /api/volume 落到电脑默认输出并持久化。画面上不再有任何声音控件。
    no_float = pg.evaluate("() => !document.getElementById('vnc-audiobar')")
    chk(f"[{label}] 画面上已无悬浮声音按钮", no_float)

    pg.click("#vnc-setbtn")                 # 打开设置面板 → 触发 refreshVolume
    pg.wait_for_timeout(900)                # 等 /api/volume 把当前音量读回滑块
    cur = pg.evaluate("""() => {
        const v = document.getElementById('set-vol');
        const lab = document.getElementById('set-vol-v');
        return {min: v.min, max: v.max, val: Number(v.value),
                label: lab.textContent.trim()};
    }""")
    chk(f"[{label}] 设置面板有电脑音量滑块(0~150)",
        cur["min"] == "0" and cur["max"] == "150" and 0 <= cur["val"] <= 150,
        str(cur))
    chk(f"[{label}] 滑块读数与服务端一致",
        pc_volume_via_api() == cur["val"], f"页面={cur['val']} 服务端={pc_volume_via_api()}")

    # 拖到 130%: 标签应变红, 并触发 /api/volume POST(防抖 250ms)
    pg.evaluate("""() => {
        const v = document.getElementById('set-vol');
        v.value = '130';
        v.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    pg.wait_for_timeout(600)                # 等防抖发包 + 服务端 pactl 生效
    over = pg.evaluate("""() => {
        const lab = document.getElementById('set-vol-v');
        return {text: lab.textContent.trim(), red: lab.style.color !== ''};
    }""")
    chk(f"[{label}] 130% 标签显示红色", over["text"] == "130%" and over["red"], str(over))
    applied = pc_volume_via_api()
    chk(f"[{label}] 服务端已把电脑音量设为 130%",
        applied == 130, f"/api/volume → {applied}")

    # 拖回原来的音量, 确认接口也跟着回来(测的是"能改也能还原")
    pg.evaluate(f"""() => {{
        const v = document.getElementById('set-vol');
        v.value = '{cur["val"]}';
        v.dispatchEvent(new Event('input', {{bubbles: true}}));
    }}""")
    pg.wait_for_timeout(600)
    restored = pc_volume_via_api()
    chk(f"[{label}] 恢复原音量成功", restored == cur["val"],
        f"/api/volume → {restored}")
    pg.click("#vnc-setbtn")                 # 关上设置面板

    # 再点顶部 🔊 即关闭转发 → 服务端停止采集(等价于原来"点浮层静音"的行为)
    t_off = time.strftime("%H:%M:%S")
    pg.click("#vnc-audio")
    ev2 = wait_audio_event(pg, t_off, "停止采集", tries=5)
    chk(f"[{label}] 关闭转发后服务端停止采集",
        any("停止采集" in m for _, m in ev2),
        str([m[:22] for _, m in ev2][:2])[:120])
    ctx.close()
    print()


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        run_case(b, u, p, False)
        run_case(b, u, p, True)
        b.close()
    print("=" * 46)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
