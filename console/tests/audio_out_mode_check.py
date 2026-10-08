#!/usr/bin/env python3
"""Isolated audio routing checks using mocked system and config operations."""

import ast
from pathlib import Path


def load_function():
    source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "set_audio_out_mode")
    return function


def run_case(set_result, readback, save_result=(True, "ok", {}),
             default_sink="speaker"):
    calls = []

    def set_volume(value):
        calls.append(("set_volume", value))
        return set_result

    def get_volume():
        calls.append(("get_volume",))
        return readback

    def save_config(patch):
        calls.append(("save_config", patch))
        return save_result

    namespace = {
        "SILENT_VOL": 1,
        "set_pc_volume": set_volume,
        "get_pc_volume": get_volume,
        "save_config": save_config,
        "audit": lambda *args: calls.append(("audit", *args)),
        "silent_sink_release": lambda: calls.append(("release",)),
        "_default_sink_name": lambda: default_sink,
        "NULL_SINK": "meow_silent",
    }
    exec(compile(ast.Module(body=[load_function()], type_ignores=[]),
                 "isolated_audio_out_mode", "exec"), namespace)
    return namespace["set_audio_out_mode"], calls


def main():
    set_mode, calls = run_case((False, "pactl failed"), None)
    ok, msg = set_mode("silent")
    assert not ok and msg == "pactl failed", (ok, msg)
    assert not any(call[0] == "save_config" for call in calls), calls

    set_mode, calls = run_case((True, "ok"), {"vol": 8})
    ok, msg = set_mode("silent")
    assert not ok and "未达到" in msg, (ok, msg)
    assert not any(call[0] == "save_config" for call in calls), calls

    set_mode, calls = run_case((True, "ok"), {"vol": 1})
    ok, mode = set_mode("silent")
    assert ok and mode == "silent", (ok, mode)
    assert ("save_config", {"audioOut": "silent"}) in calls, calls

    set_mode, calls = run_case((True, "ok"), {"vol": 1},
                               save_result=(False, "disk full", {}))
    ok, msg = set_mode("silent")
    assert not ok and "disk full" in msg, (ok, msg)

    set_mode, calls = run_case((False, "unused"), None,
                               default_sink="meow_silent")
    ok, msg = set_mode("pc")
    assert not ok and "默认输出设备未恢复" in msg, (ok, msg)
    assert not any(call[0] == "save_config" for call in calls), calls

    set_mode, calls = run_case((False, "unused"), None)
    ok, mode = set_mode("pc")
    assert ok and mode == "pc", (ok, mode)
    assert ("release",) in calls
    assert ("save_config", {"audioOut": "pc"}) in calls

    print("PASS: audio mode persists only after mocked output state is confirmed")


if __name__ == "__main__":
    main()
