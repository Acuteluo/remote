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

calls = []
def set_selection(text, selection="clipboard"):
    calls.append((text, selection))
    return True, "ok"

with patch.object(server, "clip_get", return_value="原选区"), \
     patch.object(server, "clip_set", side_effect=set_selection), \
     patch.object(server, "_combo_raw", return_value=(True, "ok")):
    assert server._paste_job("新内容", "shift+Insert")[0]
assert calls == [("新内容", "clipboard"), ("新内容", "primary"),
                 ("原选区", "primary")]

print("PASS: 普通窗口、终端、老 xterm 的粘贴键选择正确")
