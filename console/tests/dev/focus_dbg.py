#!/usr/bin/env python3
"""查清 'xev 靶子拿不到焦点' 到底卡在哪一步。"""
import os, subprocess, time

ENV = {**os.environ, "DISPLAY": ":0"}


def xdo(*a):
    r = subprocess.run(["xdotool", *a], capture_output=True, text=True, env=ENV)
    return (r.stdout + r.stderr).strip()


print("初始: focus=%s active=%s" % (xdo("getwindowfocus"), xdo("getactivewindow")))

p = subprocess.Popen(["xev", "-name", "LUO_FD"], env=ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(2)
    ids = xdo("search", "--name", "LUO_FD").split()
    print("search LUO_FD ->", ids)
    if not ids:
        raise SystemExit("没找到窗口")
    wid = ids[0]
    print("geom:", xdo("getwindowgeometry", wid).replace("\n", " | "))
    print("mapped?", xdo("getwindowname", wid))
    for i in range(6):
        xdo("windowmap", "--sync", wid)
        xdo("windowraise", wid)
        xdo("windowactivate", "--sync", wid)
        xdo("windowfocus", "--sync", wid)
        time.sleep(0.6)
        f, a = xdo("getwindowfocus"), xdo("getactivewindow")
        print(f"  尝试{i+1}: focus={f} active={a} 期望={wid} "
              f"{'OK' if f == wid else '✗'}")
        if f == wid:
            break
    print("全部窗口数:", len(xdo("search", "--maxdepth", "1", ".*").split()))
finally:
    p.terminate()
    try:
        p.wait(timeout=4)
    except Exception:
        p.kill()
