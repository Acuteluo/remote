# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""端到端 Dock 测试: 在真实浏览器里滑动触控板把指针推到屏幕底边, 截图看 Dock 有没有弹出来。"""
import http.client, os, subprocess, time
from playwright.sync_api import sync_playwright
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"); ENV={**os.environ,"DISPLAY":":0"}
def rp():
    o=subprocess.run(["xdotool","getmouselocation"],capture_output=True,text=True,env=ENV).stdout
    d=dict(p.split(":") for p in o.split() if ":" in p); return int(d["x"]),int(d["y"])
def shot(tag):
    subprocess.run(["import","-window","root","-crop","2880x230+0+1690","+repage",
                    f"{ART}/dock_{tag}.png"],capture_output=True,env=ENV)
def dm(a,b):
    r=subprocess.run(["convert",f"{ART}/dock_{a}.png",f"{ART}/dock_{b}.png","-compose",
                      "difference","-composite","-format","%[fx:mean]","info:"],
                     capture_output=True,text=True)
    return float(r.stdout.strip() or 0)

c=http.client.HTTPConnection("127.0.0.1",8390,timeout=10)
c.request("POST","/login",login_body(),{"Content-Type":"application/x-www-form-urlencoded"})
r=c.getresponse(); r.read(); cookie=(r.getheader("Set-Cookie") or "").split(";")[0]
name,value=cookie.split("=",1); orig=rp()
print("初始真实指针:", orig)

with sync_playwright() as p:
    b=p.firefox.launch(headless=True)
    ctx=b.new_context(viewport={"width":412,"height":900},device_scale_factor=2.5,
                      is_mobile=True,has_touch=True)
    ctx.add_cookies([{"name":name,"value":value,"domain":"127.0.0.1","path":"/"}])
    page=ctx.new_page(); page.on("pageerror",lambda e:print(" [pageerror]",e))
    page.goto("http://127.0.0.1:8390/vnc",wait_until="load")
    page.wait_for_timeout(5000)

    # 先把指针弄到屏幕中间偏上, 作为"远离底边"的基线
    box=page.locator("#pad-area").bounding_box()
    cx=box["x"]+box["width"]/2; cy=box["y"]+box["height"]/2
    page.evaluate("""([cx,cy])=>{window.__pe=(t,i,x,y)=>document.getElementById('pad-area')
      .dispatchEvent(new PointerEvent(t,{pointerId:i,clientX:x,clientY:y,bubbles:true,
      cancelable:true,pointerType:'touch',isPrimary:true,buttons:t==='pointerup'?0:1}));}""",[cx,cy])
    page.evaluate("""([cx,cy])=>{ window.__pe('pointerdown',1,cx,cy);
      for(let i=1;i<=60;i++) window.__pe('pointermove',1,cx,cy-i*2);
      window.__pe('pointerup',1,cx,cy-120); }""",[cx,cy])
    time.sleep(1.2); print("推到上方后指针:", rp()); shot("away"); time.sleep(1.5); shot("away")

    # 再往下推到屏幕底边(单指下滑, 与用户实际操作一致)
    page.evaluate("""([cx,cy])=>{ window.__pe('pointerdown',1,cx,cy);
      for(let i=1;i<=90;i++) window.__pe('pointermove',1,cx,cy+i*3);
      window.__pe('pointerup',1,cx,cy+270); }""",[cx,cy])
    time.sleep(2.2)
    pos=rp(); print("推到下方后指针:", pos, "(期望 y=1919)")
    shot("on")
    b.close()

print(f"\n底部区域与'指针远离底边'的平均像素差: {dm('away','on'):.4f}")
print("  (>0.1 基本就是 Dock 弹出来了; 接近 0 说明没弹)")
subprocess.run(["xdotool","mousemove",str(orig[0]),str(orig[1])],capture_output=True,env=ENV)
print("已还原指针")
