# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""验证右上角实时延迟: 能测出来、颜色分级、位置在右上角、不挤乱顶栏。"""
import http.client, os, time
from playwright.sync_api import sync_playwright
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
c=http.client.HTTPConnection("127.0.0.1",8390,timeout=10)
c.request("POST","/login",login_body(),{"Content-Type":"application/x-www-form-urlencoded"})
r=c.getresponse(); r.read(); cookie=(r.getheader("Set-Cookie") or "").split(";")[0]
name,value=cookie.split("=",1)

with sync_playwright() as p:
    b=p.firefox.launch(headless=True)
    for label, vp, mobile in [("手机 412x900", {"width":412,"height":900}, True),
                              ("窄屏 360x780", {"width":360,"height":780}, True)]:
        ctx=b.new_context(viewport=vp, device_scale_factor=2.5, is_mobile=mobile, has_touch=True)
        ctx.add_cookies([{"name":name,"value":value,"domain":"127.0.0.1","path":"/"}])
        page=ctx.new_page(); page.on("pageerror", lambda e: print("  [pageerror]", e))
        page.goto("http://127.0.0.1:8390/vnc", wait_until="load")
        page.wait_for_timeout(3000)
        info=page.evaluate("""() => {
          const el=document.getElementById('vnc-ping');
          const r=el.getBoundingClientRect();
          const bar=document.querySelector('.topbar').getBoundingClientRect();
          return { text: el.textContent, cls: el.className,
                   right: Math.round(r.right), vw: window.innerWidth,
                   inBar: r.top >= bar.top-1 && r.bottom <= bar.bottom+1,
                   statusW: Math.round(document.getElementById('vnc-status').getBoundingClientRect().width) };
        }""")
        print(f"\n{label}:")
        print(f"  延迟文本 = {info['text']!r}  class={info['cls']!r}")
        print(f"  右边缘 {info['right']} / 视口宽 {info['vw']}  -> 贴右: {info['vw']-info['right'] <= 14}")
        print(f"  在顶栏内: {info['inBar']}   状态区宽度 {info['statusW']}px")
        page.screenshot(path=f"{ART}/ping_{vp['width']}.png")
        # 点一下立刻重测
        page.locator("#vnc-ping").click(); page.wait_for_timeout(800)
        again = page.evaluate("() => document.getElementById('vnc-ping').textContent")
        print(f"  点一下重测后 = {again!r}")
        ctx.close()
    b.close()
print("\n截图:", ART)
