#!/usr/bin/env python3
"""剪贴板面板应给普通窗口、终端和老 xterm 选择正确的粘贴键。"""
import importlib.util
import os
from unittest.mock import patch

SERVER = os.path.join(os.path.dirname(__file__), "..", "server.py")
spec = importlib.util.spec_from_file_location("meow_console", SERVER)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

with patch.object(server, "load_config", return_value={"modeByClass": {}}), \
     patch.object(server, "_focus_key", return_value="firefox"), \
     patch.object(server, "_focus_is_terminal", return_value=False):
    assert server._paste_combo(server._resolve_mode("auto")) == "ctrl+v"

with patch.object(server, "load_config", return_value={"modeByClass": {}}), \
     patch.object(server, "_focus_key", return_value="gnome-terminal"), \
     patch.object(server, "_focus_is_terminal", return_value=True):
    assert server._paste_combo(server._resolve_mode("auto")) == "ctrl+shift+v"

with patch.object(server, "load_config", return_value={"modeByClass": {"xterm": "term"}}), \
     patch.object(server, "_focus_key", return_value="xterm"):
    assert server._paste_combo(server._resolve_mode("auto")) == "shift+Insert"

print("PASS: 普通窗口、终端、老 xterm 的粘贴键选择正确")
