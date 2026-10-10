"""Short-lived input receipts: duplicate requests never execute twice in one boot."""
import hashlib
import json
import math
import re
import threading
import time


class InputReceipts:
    def __init__(self, boot, clock=time.time, capacity=4096, max_pending=20):
        self.boot, self.clock = boot, clock
        self.capacity, self.max_pending = capacity, max_pending
        self.lock, self.records = threading.Lock(), {}

    def _error(self, message, code):
        return code, {"ok": False, "err": message, "boot": self.boot}

    def submit(self, user, data, payload, execute):
        if data.get('boot') != self.boot:
            return self._error('控制台已重启，旧输入不会自动重放；请检查远端窗口', 409)
        ident, issued = data.get('requestId'), data.get('issuedAt')
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            return self._error('输入请求ID格式错误', 400)
        try:
            valid_time = not isinstance(issued, bool) and isinstance(issued, (int, float)) and math.isfinite(issued)
        except (OverflowError, TypeError):
            valid_time = False
        if not valid_time:
            return self._error('输入请求时间格式错误', 400)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).digest()
        now, key = self.clock(), (user, ident)
        with self.lock:
            # Retain completed receipts longer than the accepted replay window.
            for old in list(self.records):
                record = self.records[old]
                if record['result'] is not None and now - record['created'] > 180:
                    del self.records[old]
            record = self.records.get(key)
            if record is not None:
                if record['digest'] != digest or record['issued'] != issued:
                    return self._error('同一输入请求ID不能用于不同内容', 409)
                return self._snapshot(record)
            if not -10 <= now - issued / 1000 <= 120:
                return self._error('输入请求已过期，未重新执行；请检查远端窗口', 409)
            if len(self.records) >= self.capacity or sum(r['result'] is None for r in self.records.values()) >= self.max_pending:
                return self._error('输入确认队列已满，此请求未执行，请稍后重试', 429)
            record = {'digest': digest, 'issued': issued, 'created': now, 'result': None}
            self.records[key] = record
            # Start under lock; another request cannot observe a missing execution.
            try:
                threading.Thread(target=self._execute, args=(record, execute), daemon=True,
                                 name='input-receipt').start()
            except Exception:
                del self.records[key]
                return self._error('输入任务启动失败，此请求未执行', 503)
            return self._snapshot(record)

    def _execute(self, record, execute):
        try:
            result = dict(execute())
        except Exception:
            result = {'ok': False, 'err': '输入执行异常，送达状态未知；请检查远端窗口'}
        with self.lock:
            record['result'] = result

    def _snapshot(self, record):
        if record['result'] is None:
            return 202, {'ok': False, 'state': 'pending', 'boot': self.boot}
        return 200, {**record['result'], 'state': 'done', 'boot': self.boot}

    def status(self, user, ident, boot):
        if boot != self.boot:
            return self._error('控制台已重启，旧输入送达状态未知；请检查远端窗口', 409)
        with self.lock:
            record = self.records.get((user, ident))
            if record is None:
                return self._error('没有该输入请求的确认记录', 404)
            return self._snapshot(record)
