#!/usr/bin/env python3
"""在独立子进程中获取控制终端，再替换为交互 shell。"""
import fcntl
import os
import sys
import termios

try:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe("bash", ["bash", "--login", "-i"], os.environ)
except OSError as e:
    print(f"无法启动终端: {e}", file=sys.stderr)
    sys.exit(1)
