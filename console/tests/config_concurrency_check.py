#!/usr/bin/env python3
"""Isolated regression check for config save serialization and atomic replacement."""

import ast
import json
import os
import tempfile
import threading
import time
from pathlib import Path


def load_config_functions():
    source = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    tree = ast.parse(source)
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef)
                 and node.name in ("load_config", "save_config")]
    namespace = {
        "os": os,
        "json": json,
        "tempfile": tempfile,
        "threading": threading,
        "audit": lambda *_args: None,
        "DEFAULT_CONFIG": {"themeAcc": "#4da3ff", "volume": 100},
        "_cfg_lock": threading.RLock(),
        "_cfg_mtime": -1,
        "_cfg_data": {"themeAcc": "#4da3ff", "volume": 100},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 "server_config_functions", "exec"), namespace)
    return namespace


def main():
    with tempfile.TemporaryDirectory(prefix="meow-config-check-") as tmp:
        ns = load_config_functions()
        ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
        original_dump = json.dump

        def delayed_dump(*args, **kwargs):
            # Make concurrent callers overlap between snapshot and write if the
            # read-modify-write sequence is ever left unlocked.
            time.sleep(0.03)
            return original_dump(*args, **kwargs)

        json.dump = delayed_dump
        try:
            gate = threading.Barrier(3)
            results = []

            def save(patch):
                gate.wait()
                results.append(ns["save_config"](patch))

            threads = [
                threading.Thread(target=save, args=({"themeAcc": "#123456"},)),
                threading.Thread(target=save, args=({"volume": 73},)),
            ]
            for thread in threads:
                thread.start()
            gate.wait()

            # Read through the filesystem while replacements happen. Readers
            # must always observe a complete JSON document.
            parse_errors = []
            while any(thread.is_alive() for thread in threads):
                try:
                    with open(ns["CONFIG_FILE"]) as f:
                        json.load(f)
                except FileNotFoundError:
                    pass  # Before the first atomic install there is no config yet.
                except (json.JSONDecodeError, OSError) as exc:
                    parse_errors.append(exc)
                time.sleep(0.001)
            for thread in threads:
                thread.join()

            with open(ns["CONFIG_FILE"]) as f:
                final = json.load(f)
            assert all(result[0] for result in results), results
            assert final["themeAcc"] == "#123456", final
            assert final["volume"] == 73, final
            assert not parse_errors, parse_errors
            print("PASS: concurrent config patches are preserved; readers see valid JSON")
        finally:
            json.dump = original_dump


if __name__ == "__main__":
    main()
