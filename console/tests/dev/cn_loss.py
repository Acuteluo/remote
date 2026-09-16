# 凭据统一从 _auth 取(不写死密码: 开源不能泄露, 新机器密码是随机的)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _auth import login_body, creds, wait_ready

import os
#!/usr/bin/env python3
"""定位中文输入丢字发生在哪一段: 浏览器发的 / 桥接转的 / x11vnc 落到 X 的。"""
import http.client, json, os, re, subprocess, time
from playwright.sync_api import sync_playwright
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"); ENV={**os.environ,"DISPLAY":":0"}
XEVLOG=f"{ART}/xev_cn.log"
def xdo(*a): return subprocess.run(["xdotool",*a],capture_output=True,text=True,env=ENV).stdout.strip()
def presses(log):
    out=[]
    for b in re.split(r'(?=^KeyPress event|^KeyRelease event)', log, flags=re.M):
        if not b.startswith("KeyPress event"): continue
        m=re.search(r"keysym (0x[0-9a-f]+)", b)
        if m: out.append(int(m.group(1),16))
    return out
c=http.client.HTTPConnection("127.0.0.1",8390,timeout=10)
c.request("POST","/login",login_body(),{"Content-Type":"application/x-www-form-urlencoded"})
r=c.getresponse(); r.read(); cookie=(r.getheader("Set-Cookie") or "").split(";")[0]
name,value=cookie.split("=",1); saved=xdo("getwindowfocus")
subprocess.run(["pkill","-f","xev -name LUO_CN"],capture_output=True)
if os.path.exists(XEVLOG): os.remove(XEVLOG)
xev=subprocess.Popen(["stdbuf","-o0","xev","-name","LUO_CN"],env=ENV,
                     stdout=open(XEVLOG,"w"),stderr=subprocess.DEVNULL)
wid=""
for _ in range(40):
    p=xdo("search","--name","LUO_CN").split()
    if p: wid=p[0]; break
    time.sleep(0.25)
xdo("windowfocus","--sync",wid); time.sleep(0.5)
if xdo("getwindowfocus")!=wid:
    print("焦点没拿到, 中止"); xev.terminate(); raise SystemExit(1)
print("靶子就绪")

INIT="""
window.__keys=[];
const _s=WebSocket.prototype.send;
WebSocket.prototype.send=function(d){
  try{ if(d instanceof ArrayBuffer||ArrayBuffer.isView(d)){
    const u=new Uint8Array(d instanceof ArrayBuffer?d:new Uint8Array(d.buffer,d.byteOffset,d.byteLength));
    if(u[0]===4) window.__keys.push(u[4]<<24|u[5]<<16|u[6]<<8|u[7]);
  }}catch(e){}
  return _s.apply(this,arguments);
};
window.__inputCount=0; window.__inputChars=0;
document.addEventListener('DOMContentLoaded',()=>{});
"""
try:
    with sync_playwright() as p:
        b=p.firefox.launch(headless=True)
        ctx=b.new_context(viewport={"width":412,"height":900},device_scale_factor=2.5,
                          is_mobile=True,has_touch=True)
        ctx.add_cookies([{"name":name,"value":value,"domain":"127.0.0.1","path":"/"}])
        ctx.add_init_script(INIT)
        page=ctx.new_page(); page.on("pageerror",lambda e:print(" [pageerror]",e))
        page.goto("http://127.0.0.1:8390/vnc",wait_until="load")
        page.wait_for_timeout(5000)
        page.evaluate("""() => {
          const kb=document.getElementById('vnc-kb');
          kb.addEventListener('input',()=>{window.__inputCount++;window.__inputChars+=kb.value.length;},true);
          window.__cs=0; window.__ce=0;
          kb.addEventListener('compositionstart',()=>window.__cs++,true);
          kb.addEventListener('compositionend',()=>window.__ce++,true);
        }""")
        page.evaluate("()=>document.getElementById('vnc-kbbtn').click()")
        page.wait_for_timeout(300)
        page.evaluate("()=>document.getElementById('vnc-kb').focus()")
        page.wait_for_timeout(200)

        CN="中文输入长度测试"*8
        page.evaluate("()=>{window.__keys=[];window.__inputCount=0;window.__inputChars=0;window.__cs=0;window.__ce=0;}")
        open(XEVLOG,"w").close()
        page.keyboard.type(CN, delay=3)
        time.sleep(2.5)
        st=page.evaluate("()=>({keys:window.__keys.length, uniq:[...new Set(window.__keys)].length,"
                         " input:window.__inputCount, chars:window.__inputChars,"
                         " cs:window.__cs, ce:window.__ce})")
        got=presses(open(XEVLOG,errors="replace").read())
        print(f"\n输入 {len(CN)} 个中文字符:")
        print(f"  浏览器 input 事件 {st['input']} 次, 累计字符 {st['chars']}, "
              f"compositionstart/end = {st['cs']}/{st['ce']}")
        print(f"  前端发出的 RFB keyEvent 消息: {st['keys']} 条, 其中不同 keysym {st['uniq']} 个")
        print(f"  X(xev) 实际收到 KeyPress: {len(got)} 个")
        want=[0x01000000|ord(ch) for ch in CN]
        it=iter(got); print(f"  按序命中期望字符: {sum(1 for w in want if w in it)}/{len(want)}")
        print(f"  X 收到的 keysym 前 12 个: {[hex(x) for x in got[:12]]}")
        b.close()
finally:
    xev.terminate()
    try: xev.wait(timeout=4)
    except Exception: xev.kill()
    if saved: xdo("windowfocus","--sync",saved)
    print("已清理")
