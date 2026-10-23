#!/usr/bin/env python3
"""音频订阅与延迟停机的隔离回归测试，不启动采集或访问音频硬件。"""
import ast
import queue
import threading
from pathlib import Path
from unittest.mock import Mock

source = Path(__file__).resolve().parents[1].joinpath('server.py').read_text()
tree = ast.parse(source)
names = {'_hub_ensure', '_hub_detach', '_hub_stop_if_idle', '_hub_stop_locked'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
proc = Mock()
proc.poll.return_value = None
hub = {'key': None, 'proc': None, 'chunks': None, 'stop': None, 'subs': [],
       'first': b'', 'bytes': 0, 'gen': 0, 'idle_epoch': 0}
timers = []
class Timer:
    def __init__(self, delay, fn, args):
        self.fn, self.args = fn, args
        timers.append(self)
    def start(self): pass
ns = {'_hub': hub, '_hub_lock': threading.Lock(), '_audio_lock': threading.Lock(),
      'threading': Mock(Event=threading.Event, Timer=Timer), 'queue': queue,
      '_audio_start': Mock(return_value=(proc, queue.Queue(), b'first', '')),
      '_hub_fan': Mock(), '_cam_stop': Mock(), 'AUDIO_LINGER': 2.5}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'audio_hub_functions', 'exec'), ns)
first = queue.Queue(maxsize=64)
p, stop, err = ns['_hub_ensure']('source', 'pulse', first)
assert p is proc and stop is hub['stop'] and not err and first in hub['subs']
ns['_hub_detach'](first)
old_timer = timers[-1]
second = queue.Queue(maxsize=64)
p, stop, err = ns['_hub_ensure']('source', 'pulse', second)
old_timer.fn(*old_timer.args)
assert p is proc and not stop.is_set() and second in hub['subs']
ns['_hub_detach'](second)
old_timer.fn(*old_timer.args)
assert not stop.is_set()
timers[-1].fn(*timers[-1].args)
assert stop.is_set() and hub['proc'] is None
print('PASS subscription is atomic; old idle timer cannot stop a renewed stream')
