#!/usr/bin/env python3
"""输入排队超时与重试语义回归检查；不调用 X11 或真实输入工具。"""
import importlib.util
import os
import sys
import threading
import time
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location('queue_check_server', os.path.join(ROOT, 'server.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

started, release, late = threading.Event(), threading.Event(), []
def slow():
    started.set()
    release.wait(2)
    return True, 'ok'

with patch.object(s, '_XIN_MAX_WAIT', 0.05):
    first = threading.Thread(target=lambda: s._xin_submit(slow, 1))
    first.start()
    assert started.wait(1)
    result = s._xin_submit(lambda: late.append('unexpected'), 1)
    assert not result[0] and '取消' in result[1], result
    release.set()
    first.join(1)
    done = s._xin_submit(lambda: (True, 'barrier'), 1)
    assert done[0] and not late
print('PASS expired pending input never executes later')

with patch.object(s, '_resolve_mode', return_value='paste'), patch.object(s, 'type_text', return_value=(True, 'ok')) as fallback:
    with patch.object(s, '_paste_raw', return_value=s.InputResult(False, '送达状态未知')):
        assert not s.deliver_text('test')[0]
        fallback.assert_not_called()
    with patch.object(s, '_paste_raw', return_value=s.InputResult(False, 'clipboard unavailable', retry_safe=True)):
        assert s.deliver_text('test')[0]
        fallback.assert_called_once_with('test')
print('PASS uncertain delivery is not duplicated; pre-delivery failure can fall back')

with patch.object(s, '_clip_original', ''), patch.object(s, 'clip_set', return_value=(True, 'ok')) as put:
    assert s._restore_clip_job()[0]
    put.assert_called_once_with('')
print('PASS empty clipboard is restored')
with patch.object(s, '_clip_generation', 2), patch.object(s, '_clip_original', 'original'), patch.object(s, 'clip_set') as put:
    assert s._restore_clip_job(1)[0]
    put.assert_not_called()
    assert s._clip_original == 'original'
print('PASS stale clipboard restore does not replace newer input')
with patch.object(s, '_clip_restore_timer') as timer, patch.object(s, 'clip_set', return_value=(True, 'ok')) as put:
    old_generation = s._clip_generation
    assert s._set_clip_persistent('chosen')[0]
    timer.cancel.assert_called_once()
    assert s._clip_generation == old_generation + 1
    put.assert_called_once_with('chosen')
print('PASS explicit clipboard write cancels pending automatic restore')
s._XIN_Q.put(None)
