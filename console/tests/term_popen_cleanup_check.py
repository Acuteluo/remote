#!/usr/bin/env python3
"""Exercise ws_term_bridge's Popen failure cleanup without creating a real PTY."""

import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace


class FakeWS:
    def __init__(self):
        self.messages = []
        self.closed = False
        self.sock = SimpleNamespace(shutdown=lambda *_args: None)

    def send_text(self, message):
        self.messages.append(json.loads(message))

    def close(self):
        self.closed = True


def main():
    source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "ws_term_bridge")
    closed_fds = []
    fake_os = SimpleNamespace(
        openpty=lambda: (101, 102),
        close=closed_fds.append,
        path=SimpleNamespace(join=lambda *parts: "/".join(parts)),
    )

    def fail_popen(*_args, **_kwargs):
        raise OSError("mock exec failure")

    namespace = {
        "os": fake_os,
        "sys": SimpleNamespace(executable="/isolated/python"),
        "BASE_DIR": "/isolated",
        "socket": SimpleNamespace(SHUT_RDWR=2),
        "subprocess": SimpleNamespace(Popen=fail_popen),
        "threading": threading,
        "json": json,
        "_term_lock": threading.Lock(),
        "_term_count": 0,
        "MAX_TERMINALS": 4,
        "_term_env": lambda: {"HOME": "/isolated"},
        "session_env": lambda: None,
        "audit": lambda *_args: None,
        "fcntl": SimpleNamespace(),
        "WSClosed": type("WSClosed", (Exception,), {}),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 "isolated_ws_term_bridge", "exec"), namespace)

    ws = FakeWS()
    namespace["ws_term_bridge"](ws)
    assert sorted(closed_fds) == [101, 102], closed_fds
    assert ws.closed
    assert ws.messages and ws.messages[0]["t"] == "err", ws.messages
    assert namespace["_term_count"] == 0
    print("PASS: Popen failure reports an error and closes both PTY descriptors")


if __name__ == "__main__":
    main()
