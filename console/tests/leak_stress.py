#!/usr/bin/env python3
"""僵尸连接压测: 反复 连上→要几帧→正常关闭, 看 x11vnc 上的客户端数会不会累积。

用法: DISPLAY=:0 python3 tests/leak_stress.py [轮数]   (默认 12 轮)

2026-09-16 实测(修复前): 5 轮就漏 2 条 —— 客户端**正常关闭**后, x11vnc 那边
仍然挂着连接(`ss -tnp` 看不到属主 fd, 但 `ss -e` 的 cgroup 写着 meow-console),
而控制台自己的登记表是空的。这些幽灵客户端在 `-shared` 模式下会拖慢
**所有人**的画面 —— 就是"越用越卡"的根源。

判据很简单: 每轮结束后 x11vnc 上的客户端数必须回到基线。
"""
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vnc_stability_check as V                    # noqa: E402

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
FRAMES = 6
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '★'} {name}" + (f"   {extra}" if extra else ""))


def main():
    V.CK = V.cookie()
    base_c, base_b = V.api_clients()
    if base_c is None:
        print("  ★ 拿不到客户端数")
        return 1
    print(f"  基线: x11vnc 上 {base_c} 个客户端 / 登记桥接 {base_b} 个")
    # 判据用**不变量**: x11vnc 上的连接数必须等于我们登记的桥接数 ——
    # 你自己手机连着时两边同时 +1, 所以这个判据不受影响(比"跟基线比"稳得多,
    # 之前就吃过"测试跑一半你连上来"导致误报的亏)。
    worst = 0
    for i in range(ROUNDS):
        b = V.Bridge()
        w, h = b.handshake()
        req = struct.pack(">BBHHHH", 3, 0, 0, 0, w, h)
        for _ in range(FRAMES):
            b.send(req)
            try:
                b.recv_frame()
            except Exception:
                pass
            time.sleep(0.05)
        b.close()
        time.sleep(0.6)
        c, br = V.api_clients()
        over = (c or 0) - (br or 0)
        if over > 0:
            print(f"  第 {i + 1:>2} 轮: clients={c} bridges={br}  ← {over} 个没人认领")
        worst = max(worst, over)
    print(f"  {ROUNDS} 轮之后: 没人认领的连接最多 {worst} 个")
    chk(f"{ROUNDS} 轮连接/关闭后不留僵尸", worst == 0, f"最多 {worst} 个")
    c, br = V.api_clients()
    chk("连接数 = 登记数(不变量成立)", (c or 0) == (br or 0),
        f"clients={c} bridges={br}")
    print("=" * 50)
    print(f"通过 {len(OK)} 项, 失败 {len(BAD)} 项")
    for n in BAD:
        print(f"  失败: {n}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
