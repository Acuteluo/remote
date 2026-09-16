#!/usr/bin/env python3
"""验证"连续打不同的汉字会串成同一个字"的假设。

xdotool 打中文靠临时改 keycode 映射, 接收方应用有 keymap 缓存, 间隔太短就会
一直用旧映射 -> 同一个字反复出现。之前只测过"同一个字重复 N 次"(映射只设一次),
从没测过**不同汉字连续打**, 这是测试盲区。

本脚本只打 12 个字, 不是压测。
"""
import os, re, subprocess, time

ENV = {**os.environ, "DISPLAY": ":0"}
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
LOG = f"{ART}/xev_remap.log"
TEXT = "检查一项试试吧"          # 7 个互不相同的汉字


def xdo(*a):
    return subprocess.run(["xdotool", *a], capture_output=True, text=True,
                          env=ENV).stdout.strip()


def grab(wid):
    xdo("windowmap", "--sync", wid); xdo("windowraise", wid)
    xdo("windowactivate", "--sync", wid); xdo("windowfocus", "--sync", wid)


if os.path.exists(LOG):
    os.remove(LOG)
xev = subprocess.Popen(["stdbuf", "-o0", "xev", "-name", "LUO_RM"], env=ENV,
                       stdout=open(LOG, "w"), stderr=subprocess.DEVNULL)
try:
    wid = ""
    for _ in range(40):
        p = xdo("search", "--name", "LUO_RM").split()
        if p:
            wid = p[0]
            break
        time.sleep(0.25)
    for _ in range(12):
        grab(wid); time.sleep(0.4)
        if xdo("getwindowfocus") == wid:
            break
    if xdo("getwindowfocus") != wid:
        raise SystemExit("!! 拿不到焦点, 中止(没做任何键入)")

    want = [0x01000000 | ord(c) for c in TEXT]
    for delay in ("6", "20", "40"):
        open(LOG, "w").close()
        subprocess.run(["xdotool", "type", "--clearmodifiers",
                        "--delay", delay, "--", TEXT], env=ENV,
                       capture_output=True, timeout=60)
        time.sleep(1.0)
        seq = []
        for b in re.split(r"(?=^KeyPress event)",
                          open(LOG, errors="replace").read(), flags=re.M):
            if b.startswith("KeyPress event"):
                m = re.search(r"keysym (0x[0-9a-f]+)", b)
                if m:
                    seq.append(int(m.group(1), 16))
        got = "".join(chr(k & 0xFFFFFF) for k in seq if k & 0xFF000000)
        ok = "✓ 完全正确" if got == TEXT else "★ 串字/丢字"
        print(f"  delay={delay:>3}ms -> 期望「{TEXT}」 实收「{got}」  {ok}")
finally:
    xev.terminate()
    try:
        xev.wait(timeout=4)
    except Exception:
        xev.kill()
print("\n已关闭靶子")
