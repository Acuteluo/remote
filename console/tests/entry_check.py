#!/usr/bin/env python3
"""进入速度自检：守住"进远程桌面要快"这条线。

用法: DISPLAY=:0 python3 tests/entry_check.py [port]

为什么要有这个: 2026-09-16 之前每个静态文件都是
"Cache-Control: no-cache + 不压缩 + 没有 ETag", 而 noVNC 有 50 个 ES 模块 ——
浏览器连 304 都做不了, **每次进页面都要重下 542 KB**, 在 54ms 往返的链路上 >1 秒。
改成 vendor 长缓存 + gzip + 单个 bundle 之后:
    请求 59 → 10 个, 传输 182 → 84 KB, DOMContentLoaded 2226 → 141 ms
这里把阈值固化下来, 免得哪天又退回去。
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


def chk(n, c, e=""):
    (OK if c else BAD).append(n)
    print(f"  {'✓' if c else '★'} {n}" + (f"   {e}" if e else ""))


SNAP = """() => {
    const rs = performance.getEntriesByType('resource');
    let total = 0, vendor = 0, vendorN = 0, biggest = 0, biggestName = '';
    for (const r of rs) {
        const t = r.transferSize || 0;
        total += t;
        if (r.name.includes('/vendor/')) { vendor += t; vendorN++; }
        if (t > biggest) { biggest = t; biggestName = r.name.split('/').pop(); }
    }
    return {n: rs.length, total: total, vendorN: vendorN, vendor: vendor,
            biggest: biggest, biggestName: biggestName,
            dom: Math.round(performance.getEntriesByType('navigation')[0]
                            .domContentLoadedEventEnd)};
}"""


def main():
    u, p = creds()
    with sync_playwright() as pw:
        b = pw.firefox.launch()
        ctx = b.new_context(viewport={"width": 393, "height": 852})
        pg = ctx.new_page()
        pg.set_default_timeout(25000)
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
        pg.wait_for_timeout(1200)
        s = pg.evaluate(SNAP)

        print("【冷缓存】")
        chk("请求数 ≤ 15 个", s["n"] <= 15, f"{s['n']} 个")
        chk("noVNC 只 1 个 bundle", s["vendorN"] <= 2, f"{s['vendorN']} 个")
        chk("传输总量 ≤ 150 KB", s["total"] <= 150 * 1024,
            f"{s['total'] / 1024:.0f} KB")
        chk("单个文件 ≤ 80 KB", s["biggest"] <= 80 * 1024,
            f"{s['biggestName']} {s['biggest'] / 1024:.0f} KB")
        chk("DOMContentLoaded ≤ 1200 ms", s["dom"] <= 1200, f"{s['dom']} ms")

        pg.goto(URL + "/vnc", wait_until="domcontentloaded")
        pg.wait_for_function(
            "() => document.getElementById('vnc-status').textContent.includes('已连接')",
            timeout=25000)
        pg.wait_for_timeout(1200)
        w = pg.evaluate(SNAP)
        print("【热缓存】")
        chk("DOMContentLoaded ≤ 600 ms", w["dom"] <= 600, f"{w['dom']} ms")
        chk("传输量不增加", w["total"] <= s["total"] * 1.2,
            f"{w['total'] / 1024:.0f} KB")

        print("【缓存头】")
        hdr = pg.evaluate("""async () => {
            const a = await fetch('/static/vendor/novnc-bundle.js', {method: 'GET'});
            const b = await fetch('/static/vendor/novnc/core/rfb.js', {method: 'GET'});
            return {bundle: a.headers.get('cache-control'),
                    vendor: b.headers.get('cache-control')};
        }""")
        chk("bundle 走 no-cache+ETag（可更新）",
            "no-cache" in (hdr["bundle"] or ""), str(hdr["bundle"]))
        chk("纯第三方库走长缓存",
            "immutable" in (hdr["vendor"] or ""), str(hdr["vendor"]))
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
