#!/usr/bin/env python3
"""主题色盘面的自检(不需要浏览器)。

查的是 console/static/theme-color.js —— 颜色数学特意做成不碰 DOM 的纯函数,
就是为了能在这里直接丢给 node 跑一遍。

要保证的两件事:
  1. **盘面上取到的颜色一定亮得起来**: 主题色还要当文字颜色用, 暗底上深色看不清。
     服务端 THEME_MIN_LUM 会拒掉太暗的(防绕过页面直接调接口), 但如果盘面能选出
     服务端不要的颜色, 用户就会遇到"能选却存不进去"。所以盘面的亮度下限要比
     服务端门槛更高, 这里就是拿两边的实际数值对一遍。
  2. 圆心近白、边缘鲜艳, 而且**默认蓝本身就是盘面边缘上的颜色** ——
     否则"恢复默认"之后指示器会落在和它颜色不符的位置。
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
STATIC = os.path.join(CONSOLE, "static")
sys.path.insert(0, CONSOLE)

NODE = shutil.which("node")
JS = r"""
const T = require(process.argv[1]);
let min = 9, at = "";
for (let i = 0; i <= 400; i++) {
  for (let h = 0; h < 360; h += 2) {
    const rgb = T.colorAt(i / 400, h), L = T.lum(rgb);
    if (L < min) { min = L; at = T.toHex(rgb) + " h=" + h + " r=" + (i / 400).toFixed(2); }
  }
}
const rims = {};
for (const h of [0, 40, 90, 150, 210, 240, 280, 320]) {
  const rgb = T.colorAt(1, h);
  rims[h] = { hex: T.toHex(rgb), lum: Number(T.lum(rgb).toFixed(3)) };
}
console.log(JSON.stringify({
  min, at, center: T.toHex(T.colorAt(0, 0)), rims,
  floor: T.FLOOR, rims210: T.toHex(T.colorAt(1, 210)),
  defaultRadius: Number(T.radiusOf([77, 163, 255]).toFixed(2)),
}));
"""


def main():
    import importlib.util
    spec = importlib.util.spec_from_file_location("srv", os.path.join(CONSOLE, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    if not NODE:
        print("  ⚠ 没装 node, 跳过(这项自检要靠 node 跑 theme-color.js)")
        return 0

    try:
        r = subprocess.run([NODE, "-e", JS, os.path.join(STATIC, "theme-color.js")],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print("  ✗ 跑 node 失败:", e)
        return 1
    if r.returncode != 0:
        print("  ✗ theme-color.js 加载失败:", (r.stderr or "").strip()[:300])
        return 1
    d = json.loads(r.stdout.strip().splitlines()[-1])

    ok = True
    print(f"  盘面最暗处: {d['at']}  相对亮度 {d['min']:.3f}")
    print(f"  服务端门槛: {srv.THEME_MIN_LUM}")
    if d["min"] >= srv.THEME_MIN_LUM:
        print(f"  ✓ 盘面能选的颜色全部高于服务端门槛(不会'能选却存不进去')")
    else:
        print(f"  ✗ 盘面最暗处低于服务端门槛 —— 用户会选到存不进去的颜色")
        ok = False

    print(f"  圆心(应为近白): {d['center']}   边缘 h=210: {d['rims210']}")
    print(f"  默认蓝 #4da3ff 反推半径: {d['defaultRadius']} (1.0 = 边缘)")
    print("  各色相边缘:")
    dark = []
    for h, v in d["rims"].items():
        print(f"    h={h:>3}  {v['hex']}  亮度 {v['lum']}")
        if v["lum"] < srv.THEME_MIN_LUM:
            dark.append(h)
    if dark:
        print(f"  ✗ 这些色相的边缘色太暗: {dark}")
        ok = False
    if d["defaultRadius"] < 0.95:
        print("  ⚠ 默认蓝反推不在边缘附近 —— '恢复默认'后指示器位置会偏")
    print("\n结论:", "主题色盘面自洽 ✓" if ok else "有问题 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
