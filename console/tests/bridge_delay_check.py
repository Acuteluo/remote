#!/usr/bin/env python3
"""独立回环桥接测试：600/2500ms 延迟、心跳、静默恢复；不接触运行中的 VNC。"""
import importlib.util, socket, threading, time, struct, os, sys
from unittest.mock import patch
CONSOLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CONSOLE)
spec=importlib.util.spec_from_file_location('check_server',os.path.join(CONSOLE, 'server.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
original_connect=socket.create_connection
backend=original_connect(('127.0.0.1',port));peer,_=listener.accept();listener.close()
a,b=socket.socketpair();b.settimeout(30);ws=m.WSConn(a);delay=[0.01]
def echo():
 try:
  while True:
   d=peer.recv(65536)
   if not d:break
   time.sleep(delay[0]);peer.sendall(d)
 finally:peer.close()
threading.Thread(target=echo,daemon=True).start()
def bridge():
 with patch.object(m.socket,'create_connection',return_value=backend),patch.object(m,'_vnc_register'),patch.object(m,'_vnc_unregister'),patch.object(m,'audit'):
  m.ws_vnc_bridge(ws, 'delay-check')
t=threading.Thread(target=bridge);t.start()
def send(op,data=b''):
 mask=os.urandom(4);n=len(data);h=bytes([0x80|op,0x80|n])
 b.sendall(h+mask+bytes(x^mask[i%4] for i,x in enumerate(data)))
def read(n):
 out=b''
 while len(out)<n:
  d=b.recv(n-len(out));assert d,'unexpected close';out+=d
 return out
def frame():
 h=read(2);n=h[1]&127
 if n==126:n=struct.unpack('>H',read(2))[0]
 if n==127:n=struct.unpack('>Q',read(8))[0]
 return h[0]&15,read(n)
for wait in [0.01,0.6,2.5]:
 delay[0]=wait;payload=b'delayed-rfb-byte-stream';start=time.monotonic();send(2,payload)
 while True:
  op,data=frame()
  if op==9:send(10,data);continue
  assert op==2 and data==payload;break
 print(f'PASS delayed delivery {1000*(time.monotonic()-start):.0f} ms',flush=True)
for lag in (0.6, 2.5):
 ws.ping();op,data=frame();assert op==9
 time.sleep(lag);send(10,data)
 end=time.monotonic()+1
 while time.monotonic()<end:
  stats=m.vnc_network_stats('delay-check')
  if stats['rttMs'] is not None and stats['rttMs'] >= lag*1000:break
  time.sleep(0.01)
 assert stats['active'] and lag*1000 <= stats['rttMs'] < lag*1000+250
 print(f"PASS channel RTT telemetry {stats['rttMs']:.0f} ms",flush=True)
ws.send_started=time.monotonic()-2
assert m.vnc_network_stats('delay-check')['sendMs'] >= 2000
ws.send_started=None;ws.last_send_ms=3000;ws.last_send_at=time.monotonic()-11
assert m.vnc_network_stats('delay-check')['sendMs']==0
print('PASS pending send and expired congestion telemetry',flush=True)
op,data=frame();assert op==9;send(10,data);time.sleep(5)
delay[0]=0.01;send(2,b'after-idle');op,data=frame();assert op==2 and data==b'after-idle'
print('PASS heartbeat and stream delivery after 25s idle',flush=True)
send(8);t.join(6);assert not t.is_alive();b.close()
assert m.vnc_network_stats('delay-check') == {'active': False}
print('PASS clean isolated bridge shutdown',flush=True)
