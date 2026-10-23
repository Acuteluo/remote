#!/usr/bin/env python3
"""X11 启动晚于控制台时，输入环境必须能自动恢复。"""
import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import patch

SERVER = os.path.join(os.path.dirname(__file__), "..", "server.py")
spec = importlib.util.spec_from_file_location("meow_console", SERVER)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

server._session_env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
with patch.object(server.subprocess, "run", return_value=SimpleNamespace(stdout="")), \
     patch.object(server.os.path, "isdir", return_value=True), \
     patch.object(server.glob, "glob", side_effect=[[], ["/tmp/.X11-unix/X0"]]):
    first = server.session_env()
    assert not first.get("DISPLAY")
    assert not server._session_env.get("DISPLAY")
    second = server.session_env()
    assert second["DISPLAY"] == ":0"
    assert server.x_env()["DISPLAY"] == ":0"

print("PASS: X11 就绪后重新探测 DISPLAY，输入环境自动恢复")
