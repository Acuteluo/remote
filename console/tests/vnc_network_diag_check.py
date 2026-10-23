#!/usr/bin/env python3
"""Isolated test for nonblocking, cached tailnet path diagnostics."""

import ast
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def main():
    source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    tree = ast.parse(source)
    selected = []
    ttl_value = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_refresh_tailscale_path", "_tailscale_path_summary",
                "vnc_network_stats"):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_TAILSCALE_PATH_TTL"
                for target in node.targets):
            selected.append(node)
            ttl_value = ast.literal_eval(node.value)
    assert ttl_value is not None and ttl_value >= 30, ttl_value

    status = {
        "Peer": {
            "direct-phone": {
                "Active": True,
                "HostName": "phone-direct",
                "OS": "android",
                "Relay": "hkg",
                "CurAddr": "203.0.113.8:41641",
                "TailscaleIPs": ["100.64.0.9"],
            },
            "relay-phone": {
                "Active": True,
                "HostName": "phone-relay",
                "OS": "android",
                "Relay": "hkg",
                "CurAddr": "",
                "TailscaleIPs": ["100.64.0.10"],
            },
        },
    }
    run_calls = []
    command_started = threading.Event()
    release_command = threading.Event()

    def mocked_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        command_started.set()
        release_command.wait(2)
        return SimpleNamespace(returncode=0, stdout=json.dumps(status))

    ws = SimpleNamespace(alive=True, send_started=None, last_send_ms=0,
                         last_send_at=0, ping_rtt_ms=34, last_pong=time.time())
    namespace = {
        "time": time,
        "threading": threading,
        "json": json,
        "subprocess": SimpleNamespace(run=mocked_run, SubprocessError=Exception),
        "_tailscale_path_lock": threading.Lock(),
        "_tailscale_path_cache": {"at": 0.0, "value": None, "refreshing": False},
        "_vnc_lock": threading.Lock(),
        "_vnc_network": {"viewer": ws},
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]),
                 "isolated_vnc_network_diag", "exec"), namespace)

    result_box = []
    request_done = threading.Event()

    def request_stats():
        result_box.append(namespace["vnc_network_stats"]("viewer"))
        request_done.set()

    request = threading.Thread(target=request_stats, daemon=True)
    request.start()
    assert request_done.wait(0.5), "metrics request waited on tailscale subprocess"
    assert command_started.wait(0.5), "background refresh was not scheduled"
    first = result_box[0]["tailnetPath"]
    assert first["state"] == "unknown", first

    # While the first refresh is blocked, more metrics requests return promptly
    # without starting another subprocess.
    second = namespace["vnc_network_stats"]("viewer")["tailnetPath"]
    assert second["state"] == "unknown", second
    assert len(run_calls) == 1, run_calls
    release_command.set()

    deadline = time.monotonic() + 1
    while namespace["_tailscale_path_cache"]["value"] is None and time.monotonic() < deadline:
        time.sleep(0.005)
    resolved = namespace["vnc_network_stats"]("viewer")["tailnetPath"]
    assert resolved["state"] == "ok", resolved
    paths = {p["device"]: p["path"] for p in resolved["peers"]}
    assert paths == {"phone-direct": "direct", "phone-relay": "DERP relay (hkg)"}, paths
    assert "not a confirmed route for this viewer" in resolved["scope"], resolved
    serialized = json.dumps(result_box + [resolved])
    assert "203.0.113.8" not in serialized and "100.64.0." not in serialized
    assert len(run_calls) == 1, run_calls
    assert run_calls[0][1]["timeout"] == 2, run_calls
    print("PASS: diagnostics return without waiting, cache one refresh, and prioritize CurAddr")

    # Let no background work escape the test even when an assertion fails later.
    release_command.set()


if __name__ == "__main__":
    main()
