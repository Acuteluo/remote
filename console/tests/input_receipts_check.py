"""Isolated behavioral checks for the pure input receipt module."""
import threading
import time
import unittest
from unittest.mock import patch

from console.input_receipts import InputReceipts


class FakeClock:
    def __init__(self, value=1_800_000_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def request(clock, ident):
    return {
        'boot': 'boot-A',
        'requestId': ident,
        'issuedAt': int(clock() * 1000),
    }


class InputReceiptsCheck(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_invalid_timestamp_does_not_execute(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        calls = []
        for issued in (True, None, float('nan'), 10 ** 400):
            data = request(self.clock, 'f' * 32)
            data['issuedAt'] = issued
            self.assertEqual(receipts.submit('user-A', data, {'text': 'x'},
                                             lambda: calls.append(1))[0], 400)
        self.assertEqual(calls, [])

    def wait_done(self, receipts, user, ident):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = receipts.status(user, ident, 'boot-A')
            if result[0] == 200:
                return result
            time.sleep(0.005)
        self.fail('receipt did not reach done state')

    def test_concurrent_duplicate_executes_once_and_rejects_changed_content(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        ident = 'a' * 32
        entered, release = threading.Event(), threading.Event()
        calls = []

        def execute():
            calls.append(1)
            entered.set()
            release.wait(2)
            return {'ok': True, 'accepted': 1}

        barrier = threading.Barrier(3)
        replies = []

        def submit():
            barrier.wait()
            replies.append(receipts.submit('user-A', request(self.clock, ident), {'text': 'x'}, execute))

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(2)
        self.assertTrue(entered.wait(2))
        self.assertEqual(len(replies), 2)
        self.assertEqual([reply[0] for reply in replies], [202, 202])
        self.assertEqual(receipts.submit('user-A', request(self.clock, ident), {'text': 'changed'}, execute)[0], 409)
        release.set()
        self.assertEqual(self.wait_done(receipts, 'user-A', ident)[0], 200)
        self.assertEqual(len(calls), 1)

    def test_pending_to_done_and_user_isolation(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        ident = 'b' * 32
        entered, release = threading.Event(), threading.Event()

        def execute():
            entered.set()
            release.wait(2)
            return {'ok': True, 'value': 'done'}

        code, pending = receipts.submit('user-A', request(self.clock, ident), {'v': 1}, execute)
        self.assertEqual((code, pending['state']), (202, 'pending'))
        self.assertTrue(entered.wait(2))
        self.assertEqual(receipts.status('user-B', ident, 'boot-A')[0], 404)
        release.set()
        code, done = self.wait_done(receipts, 'user-A', ident)
        self.assertEqual((code, done['state'], done['value']), (200, 'done', 'done'))

    def test_old_boot_is_rejected_without_execution(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        calls = []
        data = request(self.clock, 'c' * 32)
        data['boot'] = 'old-boot'
        self.assertEqual(receipts.submit('user-A', data, {}, lambda: calls.append(1))[0], 409)
        self.assertEqual(calls, [])

    def test_pruned_expired_id_never_executes_again(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        ident = 'd' * 32
        calls = []
        data = request(self.clock, ident)
        self.assertEqual(receipts.submit('user-A', data, {}, lambda: (calls.append(1) or {'ok': True}))[0], 202)
        self.wait_done(receipts, 'user-A', ident)
        self.clock.advance(181)
        self.assertEqual(receipts.submit('user-A', data, {}, lambda: (calls.append(2) or {'ok': True}))[0], 409)
        self.assertEqual(calls, [1])

    def test_thread_start_failure_leaves_no_placeholder(self):
        receipts = InputReceipts('boot-A', clock=self.clock)
        ident = 'e' * 32
        data = request(self.clock, ident)

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError('simulated start failure')

        with patch('console.input_receipts.threading.Thread', FailingThread):
            code, result = receipts.submit('user-A', data, {}, lambda: {'ok': True})
        self.assertEqual((code, result['ok']), (503, False))
        self.assertEqual(receipts.status('user-A', ident, 'boot-A')[0], 404)

        code, result = receipts.submit('user-A', data, {}, lambda: {'ok': True})
        self.assertEqual((code, result['state']), (202, 'pending'))
        self.assertEqual(self.wait_done(receipts, 'user-A', ident)[0], 200)

    def test_capacity_rejection_does_not_execute_second_request(self):
        receipts = InputReceipts('boot-A', clock=self.clock, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def hold():
            calls.append('first')
            entered.set()
            release.wait(2)
            return {'ok': True}

        self.assertEqual(receipts.submit('user-A', request(self.clock, 'f' * 32), {}, hold)[0], 202)
        self.assertTrue(entered.wait(2))
        code, result = receipts.submit('user-A', request(self.clock, '0' * 32), {}, lambda: calls.append('second'))
        self.assertEqual((code, result['ok']), (429, False))
        self.assertEqual(calls, ['first'])
        release.set()
        self.wait_done(receipts, 'user-A', 'f' * 32)


if __name__ == '__main__':
    unittest.main()
