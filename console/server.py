#!/usr/bin/env python3
# meow-console: 手机远程控制台门户(状态监控 + Web终端 + 远程桌面 + 电源控制)
#
# 架构: 只监听 127.0.0.1, 由 lan_forward.py 把各网卡 IP(含 Tailscale 100.x)
# 的 8390 流量镜像进来(外网只暴露这一个端口)。远程桌面链路:
#   浏览器 noVNC --WebSocket(/ws/vnc)--> 本进程 --原始RFB--> x11vnc(仅回环:5900) --> X11 :0
# 依赖: 仅 Python3 标准库; 前端资源已本地化在 static/vendor/。

import atexit
import base64
import collections
import concurrent.futures
import fcntl
import glob
import gzip
import hashlib
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from email.utils import formatdate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# ---- 运行时目录(2026-09-16 整理) ----
# **一切运行产生的东西都落在这里**: 凭据/密钥、审计日志、配置、剪贴板临时文件。
# 好处: 备份就是拷一个目录、清空就是删一个目录、.gitignore 只要一条 data/。
# 代码本身(static/templates/server.py)保持只读, 干净可分。
DATA_DIR = os.path.join(BASE_DIR, "data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass          # 建不了也别崩, 后面的读写会如实报错

# ---- 端口 / 监听地址: 全部可配, 代码里不写死 ----
# 优先级: 命令行(--port/--bind) > 环境变量(MEOW_PORT/MEOW_BIND) >
#         data/config.json(port/vncPort/bind) > 默认值
# 换端口不用改代码: 写进 data/config.json 重启即可, 或 `--port 9000`。
DEFAULT_PORT = 8390
DEFAULT_VNC_PORT = 5900
DEFAULT_BIND = "127.0.0.1"      # 只听回环; 外网访问走 lan_forward.py 镜像

# ---- 静态资源传输优化（2026-09-16）----
# 背景: noVNC 要加载 50 个 ES 模块、源码共 542 KB。原来每个文件都是
# "Cache-Control: no-cache + 不压缩 + 没有 ETag" —— 浏览器连 304 都做不了,
# **每次进页面都要完整重下这 542 KB**; 在 54ms 往返的链路上光这一项就 >1 秒。
# 现在: vendor 目录长期缓存(第三方库内容永不变) + 全部文本资源 gzip + 其余走 ETag/304。
VENDOR_DIR = os.path.realpath(os.path.join(STATIC_DIR, "vendor")) + os.sep
# 我们自己用 esbuild 打出来的 noVNC 包(542KB 源码 → 180KB, 51 个请求 → 1 个)。
# 它**可能会重新生成**, 所以不能跟着 vendor 一起 immutable —— 否则手机上会一直
# 用旧的那份。走 no-cache + ETag: 一个 304 往返, 很便宜且不会出错。
BUILT_BUNDLE = os.path.realpath(
    os.path.join(STATIC_DIR, "vendor", "novnc-bundle.js"))
_GZIP_OK = ("text/", "application/json", "image/svg+xml")
_gzip_cache = {}                    # (path, mtime, size) -> gzip 后的字节
_gzip_lock = threading.Lock()
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SECRET_FILE = os.path.join(DATA_DIR, ".secret")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.log")

# ---------------------------------------------------------------- 全局配置
# 存在**电脑**上的 config.json, 和"只跟这台手机有关"的设置(画质/触控板/滚动,
# 那些留在浏览器 localStorage)区分开。
#   - typeMode   输入投递方式(跟电脑上的应用类型有关, 换手机也该一样)
#   - typeBatch  攒批间隔(服务端目前只作记录, 实际由前端执行)
#   - typeDelayMs 逐字键入时每字符间隔, 仅终端路径生效
#   - volume     电脑主音量(0~150, 百分比, 由设置面板滑块控制并持久化)
#   - brightness 电脑屏幕亮度(5~100, 百分比, 同上; xrandr 软件调光)
#   - themeAcc   主题色(#RRGGBB)。所有 var(--acc) 渲染的地方都跟着它走
# 启动时读一次; 之后文件 mtime 变了就重读 —— 手改文件也能即时生效, 不用重启。
# 注意: save_config 只认 DEFAULT_CONFIG 里有的键(防脏数据), 新设置**必须**加进来,
# 否则 POST 会"看着成功"但什么都没存(2026-09-22 加 brightness 时就踩了这个)。
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DEFAULT_CONFIG = {"typeMode": "auto", "typeBatch": 80, "typeDelayMs": 20,
                  "volume": 100, "brightness": 100, "themeAcc": "#4da3ff",
                  # 自定义背景: 面板透明度 / 底图暗化 / 模糊(见 render() 注入的 CSS)
                  "bgTrans": 35, "bgDim": 45, "bgBlur": 0,
                  # 声音去向: pc=电脑也响(默认) / silent=只发手机(电脑静音, 走虚拟输出)
                  "audioOut": "pc",
                  # 端口也归配置管(命令行 --port / 环境变量 MEOW_PORT 优先级更高):
                  # port = 控制台对外端口; vncPort = x11vnc 的内部 RFB 端口。
                  "port": DEFAULT_PORT, "vncPort": DEFAULT_VNC_PORT,
                  # 按窗口类名记住用户选过的投递方式。VSCode 这类"类名是 code、
                  # 但编辑器里 Ctrl+V 正常、集成终端里不行"的应用, 光看类名分不出来,
                  # 只能靠"用户手动选一次 -> 按类名记住"。
                  "modeByClass": {}}
_cfg_mtime = -1.0
_cfg_data = dict(DEFAULT_CONFIG)


def load_config():
    global _cfg_mtime, _cfg_data
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return dict(_cfg_data)
    if mt == _cfg_mtime:
        return dict(_cfg_data)
    try:
        with open(CONFIG_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            merged = dict(DEFAULT_CONFIG)
            for k in DEFAULT_CONFIG:
                if k in d and d[k] is not None:
                    merged[k] = d[k]
            _cfg_data = merged
    except (OSError, ValueError) as e:
        audit("cfg", f"读取 config.json 失败(沿用旧值): {e}")
    _cfg_mtime = mt
    return dict(_cfg_data)


def save_config(patch):
    d = load_config()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG and v is not None:
            d[k] = v
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, str(e), d
    # 两个都要声明: 漏了 _cfg_data 的话赋值只会写进函数局部变量, 缓存保持旧值,
    # 于是 POST 返回的是新配置、再 GET 又变回旧的(实际踩过)。
    global _cfg_mtime, _cfg_data
    try:
        _cfg_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        _cfg_mtime = -1.0      # 拿不到就下次强制重读
    _cfg_data = d
    return True, "ok", d


def _resolve_port(cli, env_key, cfg_key, default, lo=1024, hi=65535):
    """端口: 命令行 > 环境变量 > config.json > 默认值。值不合法就退回默认。"""
    for v in (cli, os.environ.get(env_key, "")):
        v = (v or "").strip()
        if v:
            try:
                n = int(v)
                if lo <= n <= hi:
                    return n
            except ValueError:
                pass
    try:
        n = int(load_config().get(cfg_key))
        if lo <= n <= hi:
            return n
    except (TypeError, ValueError):
        pass
    return default


# 命令行参数在 main() 里解析后回填, 所以这里读的是"运行时可覆盖的初值"
_CLI_ARGS = {}
PORT = _resolve_port(None, "MEOW_PORT", "port", DEFAULT_PORT)
VNC_PORT = _resolve_port(None, "MEOW_VNC_PORT", "vncPort", DEFAULT_VNC_PORT)
BIND = (os.environ.get("MEOW_BIND") or "").strip() or DEFAULT_BIND

SESSION_TTL = 90 * 86400          # 90 天滑动过期
COOKIE_NAME = "meow_session"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# 本进程的启动标识, 每次重启都会变。前端"清理并重启"靠它判断"服务真的换了进程",
# 而不是"探到 200 就算好了" —— 服务是延迟 ~1.2s 才重启的, 提前探到的 200 是
# **旧进程**在应答, 那时 reload 会正好撞进重启窗口, 页面直接打不开(用户报的 bug)。
BOOT_ID = secrets.token_hex(4)
CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

MAX_TERMINALS = 4

# ---------------------------------------------------------------- 基础设施

def _who(handler):
    """日志里想看到"谁连的": 客户端 IP + 截断的 User-Agent。

    只记 IP 的话, 同一台手机上分不出是哪个浏览器/是不是微信内置; 整串 UA 又太长,
    把日志刷得没法看 —— 所以头 44 个字符 + 省略号。
    """
    try:
        ip = handler.client_address[0]
    except (AttributeError, IndexError):
        ip = "?"
    ua = (handler.headers.get("User-Agent") or "").strip() if hasattr(handler, "headers") else ""
    if len(ua) > 46:
        ua = ua[:44] + "..."
    return "%s%s" % (ip, (" · " + ua) if ua else "")


def audit(action, detail=""):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {action} {detail}".rstrip()
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(f"[audit] {line}", flush=True)


def load_auth():
    """auth.json: {"user": "...", "salt": "...", "hash": "<sha256(salt+password)>"}"""
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def init_auth_and_secret():
    """准备好会话密钥; 凭据有没有, 决定要不要走"首次设置"。

    **不再自动生成随机密码**(2026-09-16 改): 新机器第一次打开网页时,
    由使用者自己在页面上设账号密码 —— 不需要先看日志找随机串, 也不会
    把密码明文写进任何文件。想无人值守部署时, 可以用环境变量/命令行:
      MEOW_USER / MEOW_PASSWORD 有值 -> 直接用它生成 auth.json(不进设置页)
    已经设过凭据的机器完全不受影响。
    """
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(SECRET_FILE, 0o600)
    if load_auth() is not None:
        return
    # 无人值守: 环境变量给了完整凭据就直接落盘, 跳过设置页
    user = (os.environ.get("MEOW_USER") or "").strip()
    pw = os.environ.get("MEOW_PASSWORD")
    if user and pw:
        set_credentials(user, pw)
        audit("init", "凭据已由环境变量生成(跳过首次设置页)")


def set_credentials(user, pw):
    """写入凭据: 随机盐 + sha256(盐+密码)。**不存明文密码**。"""
    salt = secrets.token_hex(8)
    auth = {"user": user, "salt": salt,
            "hash": hashlib.sha256((salt + pw).encode()).hexdigest()}
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f)
    os.chmod(AUTH_FILE, 0o600)
    global AUTH
    AUTH = auth
    return auth


def need_setup():
    """还没有凭据 -> 必须先走一次"首次设置"。"""
    return AUTH is None


def ensure_config():
    """第一次启动时落一份带默认值的 config.json。

    不写的话要等"用户改了某个设置"才出现 —— 新机器上想改端口/看有哪些配置
    就得先猜文件名。写一份出来, 打开就能看懂有什么可调(注释在 README 里)。
    """
    if os.path.exists(CONFIG_FILE):
        return
    save_config({})


init_auth_and_secret()
with open(SECRET_FILE) as f:
    SECRET = bytes.fromhex(f.read().strip())
AUTH = load_auth()
ensure_config()          # 第一次启动就落一份默认 config.json, 方便查看/手改

_login_fails = {}                  # ip -> [count, lock_until]
_login_lock = threading.Lock()


def check_login(user, pw):
    # 还没设凭据时(AUTH 为 None)一律拒绝 —— 设置页是唯一入口
    if not AUTH:
        return False
    ok = (user == AUTH.get("user") and isinstance(pw, str) and hmac.compare_digest(
        hashlib.sha256((AUTH.get("salt", "") + pw).encode()).hexdigest(),
        AUTH.get("hash", "")))
    return ok


def change_password(old_pw, new_pw):
    """校验旧密码后, 换新盐+哈希写回 auth.json。返回 (ok, msg)。"""
    global AUTH                    # 写回成功后要更新模块内缓存的 AUTH
    if not check_login(AUTH.get("user", ""), old_pw):
        return False, "原密码不正确"
    if not isinstance(new_pw, str) or len(new_pw) < 6:
        return False, "新密码至少 6 位"
    if len(new_pw) > 128:
        return False, "新密码太长(上限 128 位)"
    salt = secrets.token_hex(8)
    auth = {"user": AUTH.get("user", "user"), "salt": salt,
            "hash": hashlib.sha256((salt + new_pw).encode()).hexdigest()}
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(auth, f)
        os.chmod(AUTH_FILE, 0o600)
    except OSError as e:
        return False, f"写入 auth.json 失败: {e}"
    # 不踢已签发会话: token 是独立 HMAC+过期体系, 与密码无关;
    # 改密码的人就是持有者本人(要输原密码), 没必要让自己手机掉线。
    AUTH = auth
    return True, "ok"


def rate_limited(ip):
    with _login_lock:
        fails, lock_until = _login_fails.get(ip, [0, 0])
        return time.time() < lock_until


def record_login(ip, ok):
    with _login_lock:
        fails, lock_until = _login_fails.get(ip, [0, 0])
        if ok:
            _login_fails.pop(ip, None)
            return
        fails += 1
        if fails >= 8:
            lock_until = time.time() + 300   # 连错8次锁5分钟
            fails = 0
        _login_fails[ip] = [fails, lock_until]


def make_token():
    exp = int(time.time()) + SESSION_TTL
    msg = f"{AUTH['user']}|{exp}".encode()
    sig = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def parse_token(token):
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
        msg = f"{AUTH['user']}|{exp}".encode()
        good = hmac.compare_digest(sig, hmac.new(SECRET, msg, hashlib.sha256).hexdigest())
        if good and exp > time.time():
            return AUTH["user"]
    except (ValueError, AttributeError):
        pass
    return None


# ---------------------------------------------------------------- X11 会话环境

_session_env = None


def session_env():
    """探测图形会话的 DISPLAY/XAUTHORITY 等, 供 wmctrl / xset / 终端使用。"""
    global _session_env
    if _session_env is not None:
        return _session_env
    env = {}
    try:
        out = subprocess.run(["pgrep", "-x", "gnome-shell"], capture_output=True,
                             text=True, timeout=3).stdout.split()
        for pid in out[:1]:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for kv in f.read().split(b"\0"):
                    k, _, v = kv.partition(b"=")
                    k = k.decode()
                    if k in ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                             "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
                        env[k] = v.decode()
    except (OSError, subprocess.SubprocessError):
        pass
    if "DISPLAY" not in env and os.path.isdir("/tmp/.X11-unix"):
        xs = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if xs:
            env["DISPLAY"] = ":" + os.path.basename(xs[0])[1:]
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        # fcitx-remote / gsettings 都要走会话总线。gnome-shell 的 environ 里
        # 不一定带, 缺了就按 XDG_RUNTIME_DIR 拼出来 —— 拿不到总线地址的话
        # 输入法门禁会静默失效(探测当成"没在跑"), 输入就又错回去了。
        xrd = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if os.path.exists(f"{xrd}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={xrd}/bus"
    if "XAUTHORITY" not in env:
        cand = f"/run/user/{os.getuid()}/gdm/Xauthority"
        if os.path.exists(cand):
            env["XAUTHORITY"] = cand
        elif os.path.exists(os.path.expanduser("~/.Xauthority")):
            env["XAUTHORITY"] = os.path.expanduser("~/.Xauthority")
    if not env:
        return None
    _session_env = env
    return env


def x_env():
    """给 xdotool / xclip / gsettings / wmctrl 用的环境。

    必须保证是 UTF-8 locale: systemd 用户服务不一定继承到 LANG, 而 xdotool 在
    C locale 下遇到 UTF-8 会直接报 "Invalid multi-byte sequence encountered",
    一个字符都打不出去 —— 实测确认。所以只在当前 locale 不是 UTF-8 时强制覆盖,
    不干扰用户自己的设置。
    """
    env = {**os.environ, **(session_env() or {})}
    cur = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if "utf" not in cur.lower():
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
    return env


# ---- 真实屏幕几何(缓存 30s) ----
# 边缘助弹必须用真实像素坐标: 前端看到的帧缓冲被 x11vnc -scale 缩过,
# 反算回物理坐标总会差 1~2px, 而 Dock 的弹出判定是严格等于最后一行。
_screen_lock = threading.Lock()
_screen_cache = {"t": 0.0, "w": 0, "h": 0}


def screen_size():
    with _screen_lock:
        if _screen_cache["w"] and time.time() - _screen_cache["t"] < 30:
            return _screen_cache["w"], _screen_cache["h"]
    env = x_env()
    w = h = 0
    try:
        r = subprocess.run(["xdotool", "getdisplaygeometry"], env=env,
                           capture_output=True, text=True, timeout=5)
        parts = r.stdout.split()
        if len(parts) == 2:
            w, h = int(parts[0]), int(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if not w or not h:
        try:
            r = subprocess.run(["xdpyinfo"], env=env, capture_output=True,
                               text=True, timeout=5)
            m = re.search(r"dimensions:\s+(\d+)x(\d+)", r.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
        except (OSError, subprocess.SubprocessError):
            pass
    with _screen_lock:
        if w and h:
            _screen_cache.update({"t": time.time(), "w": w, "h": h})
        return _screen_cache["w"], _screen_cache["h"]


# ---- 边缘助弹 ----
_edge_lock = threading.Lock()
_edge_last = [0.0]


def edge_nudge(edge, fx, fy, fbw, fbh):
    """把真实指针钉到屏幕最边缘的精确像素行。

    为什么需要: Ubuntu Dock 的自动隐藏有两条路 ——
      a) require-pressure-to-show=true  -> 压力屏障, VNC/XTEST 的绝对跳转不触发;
      b) require-pressure-to-show=false -> docking.js 的驻留机制, 但判定是
         y == monitor.y + monitor.height - 1, 严格相等, 差一像素就不弹。
    而 x11vnc 用了 -scale 1/2, 客户端能寻址的最后一行换算回物理坐标总差 1~2px,
    所以这里按比例算出真实坐标, 用 xdotool 直接钉死。
    """
    w, h = screen_size()
    if not w or not h:
        return False, "无法获取屏幕尺寸(会话环境未探测到?)"
    with _edge_lock:
        now = time.time()
        if now - _edge_last[0] < 0.12:      # 限流, 别把 X 打满
            return True, "throttled"
        _edge_last[0] = now
    x = int(round(fx * (w / fbw))) if fbw else w // 2
    y = int(round(fy * (h / fbh))) if fbh else h // 2
    # 驻留判定要求 x 严格落在 workarea 内部(不含两端), 所以夹在 [1, w-2]
    x = max(1, min(w - 2, x))
    y = max(0, min(h - 1, y))
    if edge == "bottom":
        y = h - 1
    elif edge == "top":
        y = 0
    elif edge == "left":
        x = 0
    elif edge == "right":
        x = w - 1
    else:
        return False, "未知边缘"
    if not shutil.which("xdotool"):
        return False, "xdotool 未安装"
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], env=x_env(),
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return True, f"{x},{y}"


# ---- Dash to Dock 设置 ----
DOCK_SCHEMA = "org.gnome.shell.extensions.dash-to-dock"


def _gs_get(key):
    r = subprocess.run(["gsettings", "get", DOCK_SCHEMA, key], env=x_env(),
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def _gs_set(key, value):
    r = subprocess.run(["gsettings", "set", DOCK_SCHEMA, key, str(value)],
                       env=x_env(), capture_output=True, text=True, timeout=5)
    return r.returncode == 0


DOCK_DELAY_KEYS = (("show_delay", "show-delay"), ("hide_delay", "hide-delay"))


def dock_state():
    """读回真实值: 前端勾选框曾经出现过"点了但没生效"的假象, 必须能自证。"""
    try:
        pressure = _gs_get("require-pressure-to-show")
        out = {"ok": True, "pressure": pressure == "true"}
        for k, cast in (("autohide", lambda v: v == "true"),
                        ("dock-fixed", lambda v: v == "true")):
            try:
                out[k.replace("-", "_")] = cast(_gs_get(k))
            except (OSError, subprocess.SubprocessError):
                pass
        # 驻留(dwelling)模式下, 指针停在边缘多久才弹出 / 移开多久收起。
        # 远程指针是被"锁"在边缘的, 延迟长一点也没关系; 但**真实鼠标**快速划过
        # 底边时不会停那么久, show_delay 大了就"不出现或偶尔才出现"。
        for outk, key in DOCK_DELAY_KEYS:
            try:
                out[outk] = float(_gs_get(key))
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return out
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "err": str(e)}


# ---- 剪贴板 ----
# 为什么不走 noVNC 的 clipboardPasteFrom: 它的普通路径只支持 ISO 8859-1,
# core/rfb.js 里写死了 `if (code > 0xff) code = 0x3f; // '?'` —— 中文全变问号。
# (x11vnc 0.9.16 不支持扩展剪贴板协议, 所以走的必然是这条有损路径。)
# 这里直接用 xclip 设 X 选区, 与 VNC 协议无关, 任意 Unicode 都正确。
CLIP_TMP = os.path.join(DATA_DIR, ".clipboard.tmp")
_clip_procs = []


def _reap_clip():
    _clip_procs[:] = [p for p in _clip_procs if p.poll() is None]


def _decode_clip(raw):
    """猜剪贴板字节流的编码。

    远端程序往剪贴板里塞什么的都有: 现代 GTK/Qt 用 UTF8_STRING, 但老程序、
    终端里复制的、或者 Wine/Java 应用可能给的是 STRING(Latin-1) 甚至 GBK。
    一律按 UTF-8 解就会出乱码/替换符 —— 这就是"剪贴板偶尔乱码"。
    策略: UTF-8 优先(绝大多数), 失败再试中文环境最常见的遗留编码, 最后 latin-1
    兜底(至少不会整段变问号)。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            s = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in s:          # 解出来了但不能有替换符
            if enc != "utf-8":
                audit("clip", f"剪贴板按 {enc} 解码(非 UTF-8), {len(s)} 字")
            return s
    return raw.decode("latin-1")


def clip_get(timeout=3):
    """读 X CLIPBOARD; 失败返回 None。"""
    if not shutil.which("xclip"):
        return None
    env = x_env()
    ran_ok = False
    # 先明确要 UTF8_STRING; 选区持有者不认这个靶子时再退回默认(让 xclip 自己挑)
    for args in (["-target", "UTF8_STRING", "-o"], ["-o"]):
        try:
            r = subprocess.run(["xclip", "-selection", "clipboard", *args],
                               env=env, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        ran_ok = True
        # 空输出可能是"这个靶子不支持", 换下一个再试; 不空就用它
        if r.stdout:
            return _decode_clip(r.stdout)
    return "" if ran_ok else None      # 跑通了但没内容 = 剪贴板是空的


def clip_set(text):
    """把文本写进 X CLIPBOARD, 轮询确认真的生效。

    xclip 会 fork 一个子进程常驻持有选区 —— 必须把 stdout/stderr 指向 DEVNULL
    且不 wait, 否则管道不关, HTTP 请求会一直挂着。
    """
    if not shutil.which("xclip"):
        return False, "xclip 未安装"
    try:
        with open(CLIP_TMP, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return False, str(e)
    _reap_clip()
    try:
        p = subprocess.Popen(["xclip", "-selection", "clipboard", "-i", CLIP_TMP],
                             env=x_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError as e:
        return False, str(e)
    _clip_procs.append(p)
    # xclip 要跟当前选区持有者握手, 不是瞬时生效, 所以读回确认
    for _ in range(12):
        if clip_get(timeout=0.6) == text:
            return True, "ok"
        time.sleep(0.07)
    return False, "写入后读回不一致(远端可能有程序占着选区)"


# ---------------------------------------------------- X 输入注入串行化(重要)
# xdotool type 遇到键盘上没有直接对应按键的字符(中文/emoji)时, 会**临时改写
# X 键映射**再敲, 打完再改回去。实测两个 xdotool 同时干这件事会互相覆盖:
#   顺序发 120 字  -> X 收到 122 个按键, 按序命中 120/120
#   两路并发 120 字 -> X 收到 93 个按键,  按序命中  58/120
#   三路并发 120 字 -> X 收到 78 个按键
# 这正是"不停顿打字时像是有长度上限、后半句被吞掉"的根因(实测见
# tests/conc_type.py)。
#
# 所以所有往 X 注入输入的动作都必须**串行且保序**。这里用"专职线程 + FIFO
# 队列"而不是"每个请求抢一把锁": 锁只能保证互斥, 挡不住后到的请求插到前面,
# 而打字顺序错了比丢字更难发现。
_XIN_Q = queue.Queue()
_XIN_LOCK = threading.Lock()
_XIN_WORKER = None


def _xin_worker_loop():
    while True:
        job = _XIN_Q.get()
        if job is None:
            return
        fn, done, box = job
        try:
            box.append(fn())
        except Exception as e:                      # 绝不能让工作线程死掉
            box.append((False, f"输入线程异常: {e}"))
        finally:
            done.set()


def _xin_ensure_worker():
    global _XIN_WORKER
    with _XIN_LOCK:
        if _XIN_WORKER is None or not _XIN_WORKER.is_alive():
            _XIN_WORKER = threading.Thread(target=_xin_worker_loop,
                                           name="xinject", daemon=True)
            _XIN_WORKER.start()


_XIN_MAX_DEPTH = 20                 # 队列里最多压这么多活, 再多就拒绝
_XIN_MAX_WAIT = 120.0               # 单个任务的最长等待, 防止 HTTP 线程无限堆积
_last_ime_logged = "init"           # 输入法门禁结果只在变化时记一条


def _xin_submit(fn, budget):
    """把一个输入动作丢进串行队列并等它执行完。budget 是耐心上限(秒)。"""
    _xin_ensure_worker()
    depth = _XIN_Q.qsize()
    if depth >= _XIN_MAX_DEPTH:
        # 宁可明确报错, 也不能让 HTTP 线程无上限地堆下去 —— 那才是真的卡死
        return False, f"输入队列拥塞(积压 {depth} 个), 请稍后再试"
    done = threading.Event()
    box = []
    _XIN_Q.put((fn, done, box))
    wait = min(_XIN_MAX_WAIT, max(budget, 10.0 + depth * 2.0))
    if not done.wait(timeout=wait):
        return False, f"输入队列超时(等了 {wait:.0f}s, 前面还有 {depth} 个任务)"
    if not box:
        return False, "输入线程无返回"
    return box[0]


# ---------------------------------------------------------------- 输入法守卫
# 远端跑着 fcitx(XMODIFIERS=@im=fcitx / GTK_IM_MODULE=fcitx)。xdotool type 走
# XTEST, 和真人敲键盘是同一条路: 中文态下手机发来的英文会先被 fcitx 收进拼音
# 候选框, 一个字符都到不了目标程序; 中文又会被 fcitx 二次转换。这就是
# "电脑端输入法影响手机输入"。所以服务端键入前先把 fcitx 捏成英文态, 打完还原。
def _ime_guard_enter(env):
    """返回 True=我改了状态(打完要还原), False=没动, None=探测不了。

    **刻意不依赖 fcitx-remote 状态码的含义**: 不同版本/不同打包的 fcitx-remote
    返回码定义不一致(本机的 -h 就写着 "0 for close, 1 for inactive, 2 for
    active", 和常见的 "0=英文 1=中文 2=没跑" 正好相反)。所以这里只认一件事:
    `-c` 之后状态**有没有发生变化**。没变就当它没生效, 也不去做还原 ——
    否则会把输入法状态越改越乱。
    """
    if not shutil.which("fcitx-remote"):
        return None

    def state():
        try:
            return subprocess.run(["fcitx-remote"], env=env, capture_output=True,
                                  timeout=2).returncode
        except (OSError, subprocess.SubprocessError):
            return None

    before = state()
    if before is None:
        return None

    def try_switch(flag):
        try:
            subprocess.run(["fcitx-remote", flag], env=env, capture_output=True,
                           timeout=2)
        except (OSError, subprocess.SubprocessError):
            return before
        # 轮询等它生效(是 D-Bus 请求, 不是同步的), 最多 ~300ms
        deadline = time.time() + 0.30
        while time.time() < deadline:
            now = state()
            if now is None or now != before:
                return now
            time.sleep(0.03)
        return before

    # 先试标准的 -c(inactivate); 有些版本/输入法对它没反应, 再退一步试 -t(切换)
    after = try_switch("-c")
    if after == before:
        after = try_switch("-t")
    if after == before:
        return False                 # 都没起作用 -> 别乱还原, 也别假装成功
    time.sleep(0.02)
    return True


def _ime_guard_exit(env, changed):
    if not changed:
        return
    try:
        subprocess.run(["fcitx-remote", "-o"], env=env, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


def _type_delay_ms(text):
    """每个字符之间隔多少毫秒。

    **非 ASCII 必须留足时间**, 这是个反直觉但极关键的坑:
    xdotool 打中文/emoji 时, 键盘上根本没有对应的键, 它会临时用
    XChangeKeyboardMapping 把某个 keycode 改成目标字, 敲完再改下一个。
    但接收方应用(GTK/Qt)手里有一份 **keymap 缓存**, 要靠 X 发来的
    MappingNotify 才会更新 —— 间隔太短的话应用还在用上一版的映射,
    于是屏幕上出现同一个字反复刷("检检检检"), 或者整段乱掉。
    实测 6ms 扛不住(手机上连续打不同的汉字必现), 默认给到 20ms。

    值统一取自 data/config.json 的 typeDelayMs(历史上有过散装的
    .type_delay_ms 文件, 2026-09-16 整理时已并入 config, 不再读它)。
    """
    v = load_config().get("typeDelayMs")
    try:
        v = int(v)
        if 0 <= v <= 200:
            return str(v)
    except (TypeError, ValueError):
        pass
    if any(ord(c) > 0x7F for c in text):
        return "20"          # 需要重映射: 留足时间给应用更新 keymap
    return "6" if len(text) <= 60 else "2"


def _type_raw(text):
    """真正的键入。调用方必须保证同一时刻只有一个在执行。"""
    env = x_env()
    ime_changed = _ime_guard_enter(env)
    # 只在结果变化时记一条, 否则每个字都刷一行日志没法看。
    # "未生效"是关键信号: 说明 fcitx 仍是中文态, 键入会被它截胡。
    global _last_ime_logged
    if ime_changed is not _last_ime_logged:
        _last_ime_logged = ime_changed
        audit("ime", "输入法门禁=" + {True: "已切英文", False: "未生效(仍在中文态, 输入会被截胡)",
                                    None: "未探测到 fcitx-remote"}.get(ime_changed, str(ime_changed)))
    # 6ms/字敲 8000 字要 48 秒, 期间整条队列都堵着 —— 按长度自适应降延迟。
    # delay 只是字符间隔, 1ms 已足够 X 端按序收下; 0 = xdotool 能跑多快跑多快。
    n = len(text)
    delay = _type_delay_ms(text)
    try:
        for i in range(0, n, 800):          # 分片, 避免单次命令行过长
            part = text[i:i + 800]
            try:
                p = subprocess.run(["xdotool", "type", "--clearmodifiers",
                                    "--delay", delay, "--", part],
                                   env=env, capture_output=True,
                                   timeout=max(20, len(part) // 8 + 20))
            except (OSError, subprocess.SubprocessError) as e:
                return False, str(e)
            if p.returncode != 0:
                return False, p.stderr.decode("utf-8", "replace")[:200]
        return True, "ok"
    finally:
        _ime_guard_exit(env, ime_changed)   # 无论成败都要把输入法还回去


TYPE_MAX_LEN = 5000              # 单次投递上限(粘贴/键入都受它管)
TERMINAL_HINTS = ("xterm", "gnome-terminal", "konsole", "kitty", "alacritty",
                  "tilix", "terminator", "xfce4-terminal", "kgx", "ptyxis",
                  "guake", "xtermjs", "foot", "wezterm", "deepin-terminal",
                  "lxterminal", "mate-terminal", "sakura", "qterminal",
                  "yakuake", "tilda", "hyper", "tabby", "contour", "blackbox",
                  "terminal", "console")
# 类名判不出来时的兜底: 看窗口标题
TERMINAL_NAME_HINTS = ("terminal", "终端", "bash", "zsh", "fish", "tty", "sh -")


def _type_mode():
    """投递方式: auto / type / paste / term。

    唯一来源是 data/config.json 的 typeMode(历史上有过散装的 .type_mode 文件,
    2026-09-16 整理时已并入 config, 不再读它)。
    """
    v = load_config().get("typeMode")
    if v in ("auto", "type", "paste", "term"):
        return v
    return "auto"


def _focus_wid():
    try:
        r = subprocess.run(["xdotool", "getactivewindow"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _focus_class():
    """焦点窗口的类名(小写)。

    **不能用 `xdotool getwindowclassname`** —— 本机装的 xdotool 没有这个子命令,
    实测报 "Unknown command: getwindowclassname", 于是永远返回空字符串,
    终端识别因此**一直没生效过**。改用 xprop 读 WM_CLASS 的第二段(真正的 class)。
    """
    wid = _focus_wid()
    if not wid:
        return ""
    try:
        r = subprocess.run(["xprop", "-id", wid, "WM_CLASS"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'=\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', r.stdout or "")
        if m:
            return (m.group(2) or m.group(1) or "").strip().lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _focus_pid():
    try:
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowpid"],
                           env=x_env(), capture_output=True, text=True, timeout=5)
        return int((r.stdout or "").strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def _focus_comm():
    """焦点窗口所属进程的进程名。"""
    pid = _focus_pid()
    if not pid:
        return ""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip().lower()
    except OSError:
        return ""


def _focus_key():
    """焦点窗口的稳定标识: 优先类名, 拿不到就退回进程名。

    **关键**: WorkBuddy / 一部分 Electron 应用压根没设 WM_CLASS,
    `getwindowclassname` 返回空字符串 —— 只用类名的话"按窗口记忆投递方式"
    对它们完全失效。所以必须能退回进程名。
    """
    return _focus_class() or _focus_comm()


def _focus_is_terminal():
    """当前焦点窗口是不是终端。终端不响应 Ctrl+V, 得换粘贴键。

    先按标识(类名/进程名)判; 判不出来(有些终端打包后类名很怪)再看窗口标题兜底。
    """
    cls = _focus_key()
    if cls and any(h in cls for h in TERMINAL_HINTS):
        return True
    try:
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                           env=x_env(), capture_output=True, text=True, timeout=5)
        name = (r.stdout or "").strip().lower()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return bool(name) and any(h in name for h in TERMINAL_NAME_HINTS)


# 连续输入期间的原剪贴板暂存。
# 注意只记**这一整段输入的开头**那一次: 如果每批都记, 那"原内容"就是上一批刚粘进去
# 的文本, 还原它会把上一批又塞回剪贴板 —— 应用若延迟读选区, 就会再粘出一份,
# 表现正是"英文偶有重复"。
_clip_rlock = threading.Lock()
_clip_restore_timer = None
_clip_original = None


def _restore_clip_job():
    global _clip_restore_timer, _clip_original
    with _clip_rlock:
        orig, _clip_original, _clip_restore_timer = _clip_original, None, None
    if not orig:
        return True, "无需还原"
    return clip_set(orig)


def _combo_raw(combo):
    """按给定的组合键粘贴。combo 形如 "ctrl+v" / "ctrl+shift+v" / "shift+Insert"。"""
    try:
        p = subprocess.run(["xdotool", "key", "--clearmodifiers", combo],
                           env=x_env(), capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return p.returncode == 0, (f"已发送 {combo}" if p.returncode == 0
                               else p.stderr.decode("utf-8", "replace")[:200])


def _paste_job(text, combo="ctrl+v"):
    """设剪贴板 + 粘贴组合键, 必须作为**一个**队列任务执行。

    分成两个任务的话, 排在后面的"还原剪贴板"任务可能插在两者之间 —— 于是组合键
    按下去时选区已经被换回旧内容, 粘出来的就是上一批的字(重复)。
    """
    ok, msg = clip_set(text)
    if not ok:
        return False, f"剪贴板写入失败: {msg}"
    return _combo_raw(combo)


def _paste_raw(text, combo="ctrl+v"):
    """剪贴板 + 粘贴键。会把用户原有剪贴板暂存起来, 空闲一会儿再还原。

    这是往 Chromium/Electron/GTK 应用里送文本**最可靠**的方式: 完全不碰键映射,
    也就绕开了"应用 keymap 缓存没更新 -> 同一个字反复出现"那个坑。
    """
    global _clip_original, _clip_restore_timer
    with _clip_rlock:
        if _clip_restore_timer is None:      # 一段连续输入的开头才记录原内容
            _clip_original = clip_get(timeout=0.5)
        else:
            _clip_restore_timer.cancel()     # 取消上一个待还原, 下面重新排
    res = _xin_submit(lambda: _paste_job(text, combo), budget=20.0)

    with _clip_rlock:
        if _clip_restore_timer is not None:
            _clip_restore_timer.cancel()
        # 空闲 1.5 秒才还原: 连续打字时不断被推迟, 所以绝不会插进两次粘贴之间
        _clip_restore_timer = threading.Timer(
            1.5, lambda: _xin_submit(_restore_clip_job, budget=10.0))
        _clip_restore_timer.daemon = True
        _clip_restore_timer.start()
    return res


def _resolve_mode(mode):
    """把 'auto' 解析成具体方式。

    优先级: 当前窗口手动选过的 > 全局设置 > 终端识别 > 默认粘贴。

    **按窗口的记忆必须排在全局设置前面**。曾经反过来了, 结果全局设成 term 之后
    modeByClass 永远轮不上 —— 所有窗口(含浏览器/编辑器)都被拿去按 Ctrl+Shift+V,
    而这些应用根本不认这个键。越具体的设置越该优先。
    """
    saved = (load_config().get("modeByClass") or {}).get(_focus_key())
    if saved in ("type", "paste", "term"):
        return saved
    if mode in ("type", "paste", "term"):
        return mode
    return "term" if _focus_is_terminal() else "paste"


def _remember_mode_for_class(mode):
    """把用户这次的选择记到当前窗口类名上, 下次自动用。"""
    key = _focus_key()
    if not key:
        return
    cfg = load_config()
    mbc = dict(cfg.get("modeByClass") or {})
    if mode == "auto":
        mbc.pop(key, None)
    else:
        mbc[key] = mode
    save_config({"modeByClass": mbc})


def deliver_text(text, req_mode=None):
    """按目标窗口选择投递方式, 返回 (ok, msg, 方式)。

    req_mode 来自前端设置; 给 "auto" 或 None 时才看服务端的 .type_mode 文件,
    这样前端的设置面板能直接改, 不用去服务器上改文件。
    """
    mode = _resolve_mode(
        req_mode if req_mode in ("type", "paste", "term") else _type_mode())
    if mode == "type":
        ok, msg = type_text(text)
        return ok, msg, "键入"

    # 终端**不认 Ctrl+V**。VTE(gnome-terminal/kgx/tilix/terminator/xfce4-terminal)、
    # kitty、alacritty、konsole、foot 用 Ctrl+Shift+V; 老 xterm 只能 Shift+Insert。
    if mode == "term" or (mode == "auto" and _focus_is_terminal()):
        cls = _focus_key()
        combo = "shift+Insert" if "xterm" in cls and "gnome" not in cls \
            else "ctrl+shift+v"
        ok, msg = _paste_raw(text, combo)
        if ok:
            return True, msg, f"粘贴({combo})"
        ok2, msg2 = type_text(text)
        return ok2, (msg2 if ok2 else f"{msg} / {msg2}"), "键入(终端粘贴失败)"

    ok, msg = _paste_raw(text, "ctrl+v")
    if ok:
        return True, msg, "粘贴(Ctrl+V)"
    # 剪贴板这条路走不通(没装 xclip / 选区被占)就退回逐字键入
    ok2, msg2 = type_text(text)
    return ok2, (msg2 if ok2 else f"{msg} / {msg2}"), "键入(剪贴板不可用)"


def text_shape(s):
    """只统计字符类别, **不记录明文**(用户可能正在输密码)。

    用途是判断"字到服务端时就已经错了"还是"落地时被改坏了":
    手机上打了中文但这里是 ASCII:N -> 前端/手机输入法那头就错了;
    这里 CJK:N 但屏幕上显示不对 -> 是远端输入法/xdotool 的问题。
    """
    asc = cjk = other = 0
    for ch in s:
        cp = ord(ch)
        if cp < 0x80:
            asc += 1
        elif (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
              or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF):
            cjk += 1
        else:
            other += 1
    return f"ASCII:{asc} CJK:{cjk} 其它:{other}"


def type_text(text):
    """用 xdotool 直接键入 —— 终端类程序(xterm)不响应 Ctrl+V, 只能这样输。

    走串行队列, 并发调用也不会丢字/乱序。
    """
    if not shutil.which("xdotool"):
        return False, "xdotool 未安装"
    if not text:
        return True, "ok"
    # 上限保护(稳定优先): 键入是按 delay 逐字敲的, 5000 字约 10 秒。再长不仅
    # 自己慢, 密集按键还会让 x11vnc 疯狂推送整屏更新 —— 实测把桌面拖到卡死、
    # 手机端每秒重连就是这么来的。真有这个量请走剪贴板粘贴。
    if len(text) > TYPE_MAX_LEN:
        return False, f"单次键入上限 {TYPE_MAX_LEN} 字(收到 {len(text)}), 请改用剪贴板粘贴"
    return _xin_submit(lambda: _type_raw(text), budget=20.0 + len(text) * 0.02)


def _ctrl_v_raw():
    # 同步等它敲完再返回: 队列的意义就在于"前一个动作真的落地了才做下一个",
    # 这里若 fire-and-forget, 后面的键入就可能插到 Ctrl+V 之前。
    try:
        p = subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                           env=x_env(), capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return p.returncode == 0, ("已发送 Ctrl+V" if p.returncode == 0
                               else p.stderr.decode("utf-8", "replace")[:200])


def send_ctrl_v():
    """发 Ctrl+V。同样进队列: --clearmodifiers 会改修饰键状态, 不能和键入交错。"""
    if not shutil.which("xdotool"):
        return False, "xdotool 未安装"
    return _xin_submit(_ctrl_v_raw, budget=15.0)


# ---------------------------------------------------------------- WebSocket

class WSClosed(Exception):
    pass


class WSConn:
    """最小 RFC6455 服务端实现(阻塞式, 每连接独立线程使用)。"""

    def __init__(self, sock):
        self.sock = sock
        self._wlock = threading.Lock()
        self.alive = True
        # 最后一次收到 pong 的时间。用来判断"对端是不是真的还活着" ——
        # 只发 ping 不看 pong 是没用的: 手机锁屏/切网后连接会变成半死状态,
        # 数据发得出去(TCP 缓冲收着), 但对端早就不处理了。
        self.last_pong = time.time()

    # ---- 发送
    def send_frame(self, opcode, payload=b""):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self._wlock:
            self.sock.sendall(bytes(header) + payload)

    def send_text(self, s):
        self.send_frame(0x1, s.encode())

    def send_binary(self, b):
        self.send_frame(0x2, b)

    def ping(self):
        try:
            self.send_frame(0x9, b"hb")
        except OSError:
            self.alive = False

    # ---- 接收
    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise WSClosed()
            buf += chunk
        return buf

    def recv_frame(self):
        hdr = self._read_exact(2)
        fin = hdr[0] & 0x80
        opcode = hdr[0] & 0x0F
        masked = hdr[1] & 0x80
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        if n > 32 * 1024 * 1024:
            raise WSClosed()
        mask = self._read_exact(4) if masked else None
        data = self._read_exact(n) if n else b""
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return opcode, data, bool(fin)

    def recv_message(self):
        """返回 (opcode, payload); 自动处理 ping/pong/close 与分片。"""
        buf = b""
        msg_op = None
        while True:
            opcode, data, fin = self.recv_frame()
            if opcode == 0x8:
                raise WSClosed()
            if opcode == 0x9:
                self.send_frame(0xA, data)
                continue
            if opcode == 0xA:
                self.last_pong = time.time()
                continue
            if opcode in (0x1, 0x2):
                if msg_op is not None:
                    raise WSClosed()      # 分片中突然出现新数据帧
                msg_op, buf = opcode, data
            elif opcode == 0x0:
                if msg_op is None:
                    raise WSClosed()
                buf += data
            else:
                raise WSClosed()
            if fin and msg_op is not None:
                return msg_op, buf

    def close(self):
        self.alive = False
        try:
            self.send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 状态采集

class Collector:
    def __init__(self):
        self._cpu_prev = None          # (ts, {core: ticks})
        self._cpu_last = {"total": 0.0, "cores": []}
        self._net_prev = {}            # if -> (ts, rx, tx)
        self._proc_prev = None         # (ts, {pid: ticks})
        self._proc_last = ([], [])
        self._proc_lock = threading.Lock()
        self._cpu_hot = 0              # CPU 连续超标次数(告警要"持续"才算, 见 alerts)
        self.hostname = socket.gethostname()
        # 慢字段(wmctrl / tailscale 都要起子进程)做 TTL 缓存: 状态面板每 1s 推一次,
        # 不加缓存就等于每 2s 起两个进程, 和远程桌面的更新循环抢 CPU。
        self._cache = {}
        self._cache_lock = threading.Lock()

    def _cached(self, key, ttl, fn):
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        val = fn()
        with self._cache_lock:
            self._cache[key] = (now + ttl, val)
        return val

    # -- CPU
    def _cpu_sample(self):
        s = {}
        try:
            with open("/proc/stat") as f:
                for ln in f:
                    if not ln.startswith("cpu"):
                        continue
                    parts = ln.split()
                    vals = [int(x) for x in parts[1:9]]
                    # (总 tick, 空闲 tick=idle+iowait); 空闲也计入总增量,
                    # 占用率必须按 1 - Δidle/Δtotal 计算
                    s[parts[0]] = (sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0))
        except (OSError, ValueError, IndexError):
            pass
        return time.time(), s

    def cpu(self):
        now, cur = self._cpu_sample()
        if self._cpu_prev is None:
            self._cpu_prev = (now, cur)
            return self._cpu_last
        pts, prev = self._cpu_prev
        dt = now - pts
        if dt < 0.5:                    # 采样间隔太短(如预热后首帧)会虚高, 用上次结果
            return self._cpu_last
        self._cpu_prev = (now, cur)

        def busy(name):
            if name not in cur or name not in prev:
                return None
            dtotal = cur[name][0] - prev[name][0]
            didle = cur[name][1] - prev[name][1]
            if dtotal <= 0:
                return 0.0
            return min(100.0, max(0.0, 100.0 * (1.0 - didle / dtotal)))

        cores = []
        i = 0
        while f"cpu{i}" in cur:
            v = busy(f"cpu{i}")
            if v is not None:
                cores.append(round(v, 1))
            i += 1
        total = busy("cpu")
        self._cpu_last = {"total": round(total, 1) if total is not None else 0.0,
                          "cores": cores}
        return self._cpu_last

    # -- 内存
    def mem(self):
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for ln in f:
                    k, _, v = ln.partition(":")
                    info[k] = int(v.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {}
        return {
            "total": info.get("MemTotal", 0),
            "avail": info.get("MemAvailable", 0),
            "swap_total": info.get("SwapTotal", 0),
            "swap_free": info.get("SwapFree", 0),
        }

    def load_uptime(self):
        try:
            with open("/proc/loadavg") as f:
                load = f.read().split()[:3]
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            return {"load": [float(x) for x in load], "uptime": up}
        except (OSError, ValueError, IndexError):
            return {"load": [], "uptime": 0}

    # -- 网络
    def net(self):
        cur = {}
        try:
            with open("/proc/net/dev") as f:
                for ln in f.readlines()[2:]:
                    if ":" not in ln:
                        continue
                    ifname, data = ln.split(":", 1)
                    ifname = ifname.strip()
                    fld = data.split()
                    cur[ifname] = (int(fld[0]), int(fld[8]))
        except (OSError, ValueError, IndexError):
            return []
        now = time.time()
        out = []
        for ifname, (rx, tx) in cur.items():
            if ifname == "lo":
                continue
            prev = self._net_prev.get(ifname)
            self._net_prev[ifname] = (now, rx, tx)
            rxr = txr = 0
            if prev and now > prev[0]:
                dt = now - prev[0]
                rxr = max(rx - prev[1], 0) / dt
                txr = max(tx - prev[2], 0) / dt
            out.append({"if": ifname, "rx": round(rxr), "tx": round(txr),
                        "rx_total": rx, "tx_total": tx})
        # 清理已消失的网卡
        for gone in [k for k in self._net_prev if k not in cur]:
            del self._net_prev[gone]
        out.sort(key=lambda x: x["rx"] + x["tx"], reverse=True)
        return out

    # -- 温度
    def temps(self):
        out = []
        for hm in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                with open(os.path.join(hm, "name")) as f:
                    name = f.read().strip()
            except OSError:
                continue
            for tin in sorted(glob.glob(os.path.join(hm, "temp*_input"))):
                idx = re.search(r"temp(\d+)_input", tin).group(1)
                try:
                    with open(tin) as f:
                        val = int(f.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                label = f"temp{idx}"
                lab_file = os.path.join(hm, f"temp{idx}_label")
                if os.path.exists(lab_file):
                    try:
                        with open(lab_file) as f:
                            label = f.read().strip()
                    except OSError:
                        pass
                out.append([f"{name}/{label}", round(val, 1)])
        return out

    # -- 电池
    def battery(self):
        out = None
        for ps in glob.glob("/sys/class/power_supply/*"):
            try:
                with open(os.path.join(ps, "type")) as f:
                    t = f.read().strip()
                if t == "Battery":
                    cap = int(open(os.path.join(ps, "capacity")).read().strip())
                    st = open(os.path.join(ps, "status")).read().strip()
                    watts = None
                    pn = os.path.join(ps, "power_now")
                    if os.path.exists(pn):
                        watts = int(open(pn).read().strip()) / 1e6
                    else:
                        cn, vn = os.path.join(ps, "current_now"), os.path.join(ps, "voltage_now")
                        if os.path.exists(cn) and os.path.exists(vn):
                            watts = (int(open(cn).read().strip()) *
                                     int(open(vn).read().strip())) / 1e12
                    out = {"cap": cap, "status": st, "watts": watts}
            except (OSError, ValueError):
                continue
        return out

    # -- 磁盘
    def disks(self):
        out, seen_dev, seen_mp = [], set(), set()
        try:
            with open("/proc/self/mounts") as f:
                for ln in f:
                    dev, mp, fstype = ln.split()[:3]
                    if not dev.startswith("/dev/") or fstype == "swap":
                        continue
                    if fstype not in ("ext2", "ext3", "ext4", "btrfs", "xfs",
                                      "f2fs", "vfat", "exfat", "ntfs", "fuseblk"):
                        continue
                    # 按**设备**去重, 不能只按挂载点: 同一个分区经常被 bind mount
                    # 到多个路径(同一块磁盘可能挂到 /、/usr、/etc、/home/<用户>/... 多个挂载点
                    # /dev/nvme0n1p9), 不去重磁盘卡片会被同一块盘刷屏,
                    # 告警也会把同一条重复报十几遍。
                    if dev in seen_dev or mp in seen_mp:
                        continue
                    seen_dev.add(dev)
                    seen_mp.add(mp)
                    try:
                        st = os.statvfs(mp)
                        out.append({"mp": mp, "total": st.f_frsize * st.f_blocks,
                                    "free": st.f_frsize * st.f_bavail})
                    except OSError:
                        continue
        except OSError:
            pass
        out.sort(key=lambda d: 0 if d["mp"] == "/" else 1)
        return out

    # -- GPU(i915 核显频率)
    def gpu(self):
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            try:
                with open(os.path.join(card, "device", "vendor")) as f:
                    if f.read().strip() != "0x8086":
                        continue
                cur = mx = None
                f1 = os.path.join(card, "gt_cur_freq_mhz")
                f2 = os.path.join(card, "gt_max_freq_mhz")
                if os.path.exists(f1):
                    cur = int(open(f1).read().strip())
                if os.path.exists(f2):
                    mx = int(open(f2).read().strip())
                if cur is not None:
                    return {"cur": cur, "max": mx}
            except (OSError, ValueError):
                continue
        return None

    # -- 进程
    def _sample_procs(self):
        now = time.time()
        out = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            rp = raw.rfind(b")")
            comm = raw[raw.find(b"(") + 1:rp].decode("utf-8", "replace")
            fld = raw[rp + 2:].split()
            try:
                state = fld[0].decode()
                ppid = int(fld[1])
                ticks = int(fld[11]) + int(fld[12])
                threads = int(fld[17])
            except (IndexError, ValueError):
                continue
            rss = 0
            try:
                with open(f"/proc/{pid}/statm") as f:
                    rss = int(f.read().split()[1]) * PAGE_SIZE
            except (OSError, ValueError, IndexError):
                pass
            out[int(pid)] = (comm, state, ppid, ticks, rss, threads)
        return now, out

    def procs(self):
        with self._proc_lock:
            now, cur = self._sample_procs()
            if self._proc_prev is None:
                self._proc_prev = (now, cur)
                return [], []
            pts, prev = self._proc_prev
            dt = now - pts
            if dt < 0.5:               # 预热后立即采样会虚高, 沿用上次
                return self._proc_last
            self._proc_prev = (now, cur)
            rows = []
            for pid, (comm, state, ppid, ticks, rss, threads) in cur.items():
                p = prev.get(pid)
                cpu = (ticks - p[3]) / dt / CLK_TCK * 100 if p else 0.0
                if p and ticks < p[3]:
                    cpu = 0.0
                rows.append({"pid": pid, "name": comm, "cpu": round(cpu, 1),
                             "mem": rss, "state": state, "ppid": ppid,
                             "threads": threads})
            top_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:12]
            top_mem = sorted(rows, key=lambda r: r["mem"], reverse=True)[:12]
            self._proc_last = (top_cpu, top_mem)
            return top_cpu, top_mem

    # -- 窗口(慢: 起 wmctrl 子进程, 缓存 6s)
    def windows(self):
        return self._cached("windows", 6.0, self._windows_now)

    def _windows_now(self):
        env = session_env()
        if not env or not shutil.which("wmctrl"):
            return []
        try:
            # 用 -lx 带上 WM_CLASS: 光靠标题分不出"GNOME Shell 自己的隐形窗口"。
            r = subprocess.run(["wmctrl", "-lx"], env={**os.environ, **env},
                               capture_output=True, text=True, timeout=4)
            out = []
            for ln in r.stdout.splitlines():
                parts = ln.split(None, 4)          # ID 桌面 WM_CLASS 主机 标题
                if len(parts) < 5:
                    continue
                wid, desk, cls, _host, title = parts
                # 滤掉 GNOME Shell / 扩展的内部窗口: WM_CLASS 是 gjs.Gjs,
                # 或者标题是 GTK 的占位串(@!<x>,<y>;<随机>) —— 它们不可见,
                # 列在面板里就是纯噪音(用户反馈过 "0x04200003 @!0,0;BDHF 是什么")。
                if cls == "gjs.Gjs" or re.match(r"^@!\d+,\d+;", title):
                    continue
                out.append({"id": wid, "title": title, "cls": cls})
            return out
        except (OSError, subprocess.SubprocessError):
            return []

    # -- Tailscale 设备(慢: 起 tailscale 子进程, 缓存 15s)
    def tailscale(self):
        return self._cached("peers", 15.0, self._tailscale_now)

    def _tailscale_now(self):
        if not shutil.which("tailscale"):
            return []
        try:
            r = subprocess.run(["tailscale", "status"], capture_output=True,
                               text=True, timeout=5)
            out = []
            for ln in r.stdout.splitlines():
                fld = ln.split()
                if len(fld) < 5:
                    continue
                ip, host, _user, osname = fld[0], fld[1], fld[2], fld[3]
                status = " ".join(fld[4:])
                offline = "offline" in status or "logged out" in status
                out.append({"ip": ip, "host": host, "os": osname,
                            "online": not offline, "self": "self" in status})
            return out
        except (OSError, subprocess.SubprocessError):
            return []

    # ---- 异常告警 ----
    # 阈值判断集中在服务端, 前端只负责显示 —— 免得两边各写一套、改一处忘一处。
    # 关键在"持续": CPU 瞬时冲到 90% 很正常(编译/转码/解压), 连续几次超标才算
    # 真有事; 内存/磁盘/温度变化慢, 可以直接判。
    ALERT_SUSTAIN = 3                  # 连续 3 次快照(2s 一次 => 约 6 秒)

    def _port_alive(self, port):
        def probe():
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                return False
            finally:
                s.close()
        # 10s 缓存: 状态面板 1s 推一次, 不加缓存等于每 2s 连一次端口
        return self._cached(f"port{port}", 10, probe)

    def alerts(self, snap):
        out = []

        def add(level, key, msg):
            out.append({"level": level, "key": key, "msg": msg})

        cpu = (snap.get("cpu") or {}).get("total") or 0
        mem = snap.get("mem") or {}
        disks = snap.get("disks") or []
        temps = [t for t in (snap.get("temps") or []) if t[1] and 0 < t[1] < 150]
        cores = len((snap.get("cpu") or {}).get("cores") or []) or 1

        # CPU: 必须持续超标
        self._cpu_hot = self._cpu_hot + 1 if cpu > 85 else 0
        if self._cpu_hot >= self.ALERT_SUSTAIN:
            add("crit" if cpu > 95 else "warn", "cpu",
                f"CPU 持续 {cpu:.0f}%（已约 {self._cpu_hot * 2} 秒）")

        # 内存 / 交换
        if mem.get("total"):
            p = 100.0 * (mem["total"] - mem["avail"]) / mem["total"]
            if p > 93:
                add("crit", "mem", f"内存 {p:.0f}%（快耗尽，可能开始杀进程）")
            elif p > 85:
                add("warn", "mem", f"内存 {p:.0f}%（偏高）")
            sw = mem.get("swap_total") or 0
            if sw and mem.get("swap_free") is not None:
                sp = 100.0 * (sw - mem["swap_free"]) / sw
                if sp > 60:
                    add("warn", "swap", f"交换分区已用 {sp:.0f}%（内存不够用了）")

        # 温度: 只看最高的那个有效读数
        if temps:
            name, val = max(temps, key=lambda t: t[1])
            if val > 92:
                add("crit", "temp", f"{name} {val:.0f}°C（过热，会被降频）")
            elif val > 80:
                add("warn", "temp", f"{name} {val:.0f}°C（偏高）")

        # 磁盘
        for d in disks:
            if not d.get("total"):
                continue
            p = 100.0 * (d["total"] - d["free"]) / d["total"]
            if p > 93:
                add("crit", "disk", f"磁盘 {d['mp']} 已用 {p:.0f}%（快满）")
            elif p > 85:
                add("warn", "disk", f"磁盘 {d['mp']} 已用 {p:.0f}%")

        # 电池(只在放电时才有意义)
        bat = snap.get("battery")
        if bat and bat.get("status") == "Discharging":
            cap = bat.get("cap") or 100
            if cap < 10:
                add("crit", "bat", f"电池仅 {cap}%（马上关机，远程会断）")
            elif cap < 20:
                add("warn", "bat", f"电池 {cap}%（记得接电源）")

        # 负载: 要跟核数比, 绝对值没意义
        try:
            l1 = float((snap.get("load") or ["0"])[0])
        except (TypeError, ValueError):
            l1 = 0.0
        if l1 > cores * 3:
            add("crit", "load", f"系统负载 {l1:.1f}（{cores} 核，严重过载）")
        elif l1 > cores * 1.5:
            add("warn", "load", f"系统负载 {l1:.1f}（{cores} 核，偏高）")

        # 远程桌面连接数: x11vnc 是 -shared, 客户端越多共享更新循环越慢
        n = snap.get("vnc_clients")
        if isinstance(n, int):
            if n > 4:
                add("crit", "vnc", f"远程桌面有 {n} 个连接（遗留客户端在拖慢画面）")
            elif n > 2:
                add("warn", "vnc", f"远程桌面有 {n} 个连接（正常 1 个）")

        # 外部服务: x11vnc 挂了就没有画面了, 必须报; 别的看本地扩展里配了什么
        for name, port in [("x11vnc", VNC_PORT)] + (
                list(LOCAL_EXTRAS.extra_ports()) if LOCAL_EXTRAS else []):
            if not self._port_alive(port):
                add("crit", "svc", f"{name} 未响应（{port} 端口不通）")

        # 严重的排前面
        out.sort(key=lambda a: 0 if a["level"] == "crit" else 1)
        return out

    def wifi(self):
        """当前连着的 Wi-Fi: SSID + 信号强度(不是无线连接就返回 None)。

        信号从 /proc/net/wireless 读 —— 纯标准库, 不需要装 iw/nmcli;
        SSID 内核不给, 只能问外面: 依次试 iwgetid / nmcli, 都没有就退化成
        "接口名 + 信号"(至少还剩信号强弱这一半信息)。
        """
        iface, link, level = None, None, None
        try:
            with open("/proc/net/wireless") as f:
                for ln in f.readlines()[2:]:              # 前两行是表头
                    if ":" not in ln:
                        continue
                    name, rest = ln.split(":", 1)
                    fld = rest.split()
                    if len(fld) >= 3:
                        iface = name.strip()
                        link = float(fld[1].rstrip("."))  # 习惯刻度 0~70
                        level = float(fld[2].rstrip("."))  # dBm
                        break
        except (OSError, ValueError, IndexError):
            pass
        if not iface:
            return None

        ssid = ""
        for cmd in (["iwgetid", "-r", iface],
                    ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "dev", "show", iface]):
            try:
                r = subprocess.run(cmd, env=x_env(), capture_output=True,
                                   text=True, timeout=4)
                out = (r.stdout or "").strip().splitlines()
                if out:
                    v = out[0].split(":", 1)[-1].strip()
                    if v and v != "--":                   # nmcli 未连接时给 "--"
                        ssid = v
                        break
            except (OSError, subprocess.SubprocessError):
                continue

        # 百分比: 优先按 dBm 换算(和系统托盘/手机那套一致), 没有 dBm 就用 link/70
        pct = None
        if isinstance(level, float) and -120 < level <= 0:
            pct = int(max(0, min(100, 2 * (level + 100))))
        elif isinstance(link, float):
            pct = int(max(0, min(100, link / 70.0 * 100)))
        return {"iface": iface, "ssid": ssid, "pct": pct,
                "link": link, "level": level}

    def snapshot(self):
        top_cpu, top_mem = self.procs()
        lu = self.load_uptime()
        snap = {
            "time": time.strftime("%H:%M:%S"),
            "host": self.hostname,
            "uptime": lu["uptime"],
            "load": lu["load"],
            "cpu": self.cpu(),
            "mem": self.mem(),
            "temps": self.temps(),
            "battery": self.battery(),
            "net": self.net(),
            # SSID 要起子进程问 iwgetid/nmcli, 慢字段 —— 5s 一次就够(面板 1s 推一次)
            "wifi": self._cached("wifi", 5, self.wifi),
            "disks": self.disks(),
            "gpu": self.gpu(),
            "top_cpu": top_cpu,
            "top_mem": top_mem,
            "windows": self.windows(),
            "peers": self.tailscale(),
        }
        # 桌面连接数: 每 5s 才数一次(面板 1s 推一次, 没必要那么勤)
        snap["vnc_clients"] = self._cached("vncc", 5, vnc_client_count)
        snap["alerts"] = self.alerts(snap)
        return snap


COLLECTOR = Collector()
COLLECTOR.cpu()          # 预热采样, 让首屏就有 CPU 数据
COLLECTOR.procs()

# ---------------------------------------------------------------- 终端(pty)

_term_lock = threading.Lock()
_term_count = 0


def _term_env():
    """终端子进程的环境 —— **全部按当前机器推导, 不写死**。

    之前把 HOME/USER/PATH 硬编码成具体路径, 换台机器(或换个用户名)终端就会
    跑在错误的家目录和身份下。这里用 pwd 模块 + os 实际取值拼出来。
    """
    import pwd
    # $HOME 优先 —— 服务运行环境自己声明的家目录才是它该用的。
    # 但要校验属主: sudo/容器里 uid 和 $HOME 可能是两拨人的, 直接信 $HOME
    # 会让终端跑在别人的目录下。属主对不上就退回按 uid 查 passwd。
    home = user = None
    env_home = os.environ.get("HOME")
    if env_home and os.path.isdir(env_home):
        try:
            if os.stat(env_home).st_uid == os.getuid():
                home = env_home
        except OSError:
            pass
    try:
        pw = pwd.getpwuid(os.getuid())
        user = pw.pw_name
        if home is None:
            home = pw.pw_dir or os.path.expanduser("~")
    except (KeyError, ImportError):
        user = user or (os.environ.get("USER") or "user")
        if home is None:
            home = os.path.expanduser("~")
    home_bin = os.path.join(home, ".local/bin")
    local_bin = os.path.join(home, "bin")
    path = os.pathsep.join([home_bin, local_bin, "/snap/bin",
                            "/usr/local/sbin", "/usr/local/bin",
                            "/usr/sbin", "/usr/bin", "/sbin", "/bin"])
    # 语言: 系统设了就跟着系统, 没设才退回 zh_CN.UTF-8(中文环境优先)
    lang = (os.environ.get("LANG") or os.environ.get("LC_ALL") or "zh_CN.UTF-8")
    return {
        "PATH": path, "HOME": home, "USER": user, "LOGNAME": user,
        "SHELL": os.environ.get("SHELL") or "/bin/bash",
        "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": lang,
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
    }


def ws_term_bridge(ws):
    global _term_count
    with _term_lock:
        if _term_count >= MAX_TERMINALS:
            ws.send_text(json.dumps({"t": "err", "d": "终端数量已达上限"}))
            ws.close()
            return
        _term_count += 1
    pid = fd = None
    try:
        env = _term_env()
        senv = session_env()
        if senv:
            env.update({k: v for k, v in senv.items()
                        if k in ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")})

        # 用 Popen 而非 pty.fork: 多线程进程里 fork+exec 之间执行 Python 代码
        # (execvpe 找 PATH 等)可能死锁; Popen 的 exec 在 C 层完成, 无此风险。
        try:
            master, slave = os.openpty()
        except OSError as e:
            # 别静默吞掉: 容器/受限环境里没有 /dev/pts 会失败, 以前用户只会看到
            # 一片空白的终端, 没有任何提示, 还以为是自己网络的问题。
            audit("term", f"终端启动失败(无法分配 pty): {e}")
            try:
                ws.send_text(json.dumps(
                    {"t": "err", "d": f"无法创建终端(pty): {e}"}))
            except OSError:
                pass
            ws.close()
            return

        def _child_init():
            os.setsid()
            try:
                fcntl.ioctl(slave, 0x540E, 0)      # TIOCSCTTY: 设为控制终端
            except OSError:
                pass

        p = subprocess.Popen(
            ["bash", "--login", "-i"], stdin=slave, stdout=slave, stderr=slave,
            cwd=env["HOME"], env=env, preexec_fn=_child_init, close_fds=True)
        os.close(slave)
        pid, fd = p.pid, master

        def pty_reader():
            try:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    ws.send_binary(data)
            except OSError:
                pass
            finally:
                ws.alive = False
                try:
                    ws.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=pty_reader, daemon=True).start()
        threading.Thread(target=lambda: _hb(ws), daemon=True).start()

        while ws.alive:
            op, payload = ws.recv_message()
            if op != 0x1:
                continue
            try:
                msg = json.loads(payload.decode())
            except (ValueError, UnicodeDecodeError):
                continue
            t = msg.get("t")
            if t == "i" and isinstance(msg.get("d"), str):
                try:
                    os.write(fd, msg["d"].encode())
                except OSError:
                    break
            elif t == "r":
                try:
                    cols = max(2, min(int(msg.get("c", 80)), 500))
                    rows = max(2, min(int(msg.get("r", 24)), 200))
                    fcntl.ioctl(fd, 0x5414, struct.pack("HHHH", rows, cols, 0, 0))  # TIOCSWINSZ
                    os.kill(pid, signal.SIGWINCH)
                except OSError:
                    pass
    except (WSClosed, OSError):
        pass
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGHUP)
            except (ProcessLookupError, OSError):
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        with _term_lock:
            _term_count -= 1


def _hb(ws):
    while ws.alive:
        time.sleep(25)
        if not ws.alive:
            break
        ws.ping()


# ---------------------------------------------------------------- VNC 桥接

# x11vnc 是 -shared 的: 每个桥接都是一个客户端, 客户端越多, 共享更新循环越慢,
# 最后所有人都卡。僵尸客户端(手机锁屏/切网留下的)尤其致命。
# 这里登记所有活着的桥接, 超过上限就把最早的踢掉 —— 保证最多只有这么多个。
MAX_VNC_BRIDGES = 2
_vnc_lock = threading.Lock()
_vnc_bridges = []            # [[创建时间, teardown, 本地端口], ...] 按时间升序


def _vnc_register(teardown, port=None):
    """登记一个新桥接; 超了就把最早的踢掉, 返回被踢掉的数量。

    port = 我们连 x11vnc 时用的**本地端口**。记着它, 巡检时才能区分
    "这个连接是我们自己的桥接" 和 "这个连接是别人/历史遗留的"。
    """
    kicked = 0
    with _vnc_lock:
        _vnc_bridges.append([time.time(), teardown, port])
        while len(_vnc_bridges) > MAX_VNC_BRIDGES:
            old = _vnc_bridges.pop(0)
            try:
                old[1]()
                kicked += 1
            except (OSError, ValueError):
                pass
    if kicked:
        audit("vnc", f"桥接数超上限({MAX_VNC_BRIDGES}), 已踢掉 {kicked} 个最旧的")
    return kicked


def _vnc_unregister(teardown):
    with _vnc_lock:
        for i, item in enumerate(_vnc_bridges):
            if item[1] is teardown:
                _vnc_bridges.pop(i)
                break


def ws_vnc_bridge(ws):
    """浏览器 noVNC <-> 127.0.0.1:VNC_PORT x11vnc 双向搬运。

    2026-09-14 加固(针对"用一会儿就卡/要刷新"):
      - 去掉 vnc.settimeout(600): 画面静止时 x11vnc 可以几分钟不发一个字节,
        原来的 600s 读超时会把正常连接判死。
      - 浏览器侧加 90s 读写超时 + 每 20s 一个 WebSocket ping: 手机锁屏/切网后
        常常留下半死连接, 而 x11vnc 在 -shared 下这种僵尸客户端会拖慢所有客户端
        的共享更新循环。超时或心跳失败立刻拆链, 交给前端自动重连。
      - ping 同时保住 Tailscale/WireGuard 的 NAT 映射, 减少"过一会儿就连不上"。
    """
    try:
        vnc = socket.create_connection(("127.0.0.1", VNC_PORT), timeout=5)
    except OSError as e:
        audit("vnc", f"x11vnc 连接失败: {e}")
        try:
            ws.send_text(json.dumps({"t": "err", "d": f"无法连接远程桌面服务: {e}"}))
        except OSError:
            pass
        ws.close()
        return
    # 超时 10s(**不是 None**): 见 v2w 和收尾处的注释 —— 线程还堵在 vnc 的
    # recv/sendall 里时去 close(fd), 内核会替那个正在进行的系统调用继续持有
    # socket: fd 没了、TCP 还是 ESTAB, x11vnc 那边就永远多一个客户端。
    # 给个上限, 线程就一定退得出来, 关 fd 才是安全的。
    vnc.settimeout(10)
    try:
        vnc_port = vnc.getsockname()[1]   # 我们这条连接的本地端口, 巡检时要认领
    except OSError:
        vnc_port = None
    try:
        vnc.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        # 读超时从 90s 收到 45s: 半死连接(手机锁屏/切网)越快清掉越好 ——
        # 它们挂在 x11vnc 上会拖慢**所有**客户端的共享更新循环。
        # 正常连接不会触发它: 心跳每 20s 一个 ping, 浏览器会自动回 pong。
        ws.sock.settimeout(45)
    except OSError:
        pass
    stop = threading.Event()

    def teardown():
        stop.set()
        try:
            ws.alive = False
            ws.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            vnc.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    _vnc_register(teardown, vnc_port)

    def v2w():
        import select as _select
        try:
            while not stop.is_set():
                # 先 select 等数据(1s 超时)再 recv, 别直接 recv 永久阻塞:
                # 这样 stop 一置位本线程最多 1 秒就退出, 收尾处才敢等它 join 完
                # 再关 fd。直接 recv 的话, 关 fd 时它可能还堵在系统调用里。
                try:
                    rd, _, _ = _select.select([vnc], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if not rd:
                    continue
                try:
                    data = vnc.recv(65536)
                except socket.timeout:
                    continue          # select 说可读但抢输了, 下一轮再来
                if not data:
                    break
                # 把**已经到达**的数据一起收走, 合成一个 WebSocket 帧再发。
                # x11vnc 一次更新常拆成好几个 8KB 小包, 不合的话手机每秒要处理
                # 几百个 WS 消息 —— 纯属白烧手机 CPU, 还更容易被浏览器的调度抖动
                # 拖慢。这里只收"已就绪"的数据(select 超时 0, 不等待),
                # 所以**不增加延迟**, 只是把零碎的小帧并成大帧。
                if len(data) < 262144:
                    while len(data) < 262144:
                        try:
                            rd, _, _ = _select.select([vnc], [], [], 0)
                        except (OSError, ValueError):
                            break
                        if not rd:
                            break
                        try:
                            more = vnc.recv(65536)
                        except OSError:
                            break
                        if not more:
                            break
                        data += more
                ws.send_binary(data)
        except (OSError, WSClosed):
            pass
        finally:
            teardown()

    def w2v():
        try:
            while not stop.is_set():
                op, payload = ws.recv_message()
                if payload:
                    vnc.sendall(payload)
        except (OSError, WSClosed):
            pass
        finally:
            teardown()

    def heartbeat():
        # 每 5 秒检查一次, 但只在需要时才发 ping:
        #  - 长时间没发过 ping → 发一个(顺便保住 Tailscale/WireGuard 的 NAT 映射)
        #  - **超过 25 秒没收到 pong → 判定对端已死, 立刻拆链**
        # 为什么要看 pong: 手机锁屏/切网后连接会变成半死状态 —— 数据还发得出去
        # (只是堆在 TCP 缓冲里), 但对端早就不处理了。原来只发 ping 不看 pong,
        # 这种连接要等 90 秒读超时才消失; 而 x11vnc 是 -shared, 僵尸客户端会拖慢
        # **所有**客户端的共享更新循环。现在 25 秒就清掉。
        last_ping = time.time()
        while not stop.is_set():
            if stop.wait(5):
                return
            if not ws.alive:
                break
            if time.time() - ws.last_pong > 40:
                break
            if time.time() - last_ping >= 20:
                ws.ping()
                last_ping = time.time()
                if not ws.alive:
                    break
        teardown()

    threads = []
    for fn in (v2w, w2v, heartbeat):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        threads.append(t)
    stop.wait()
    # ★ 关 fd 之前**必须等搬运线程真的退出**(2026-09-16 修):
    # 线程还堵在 vnc.recv()/sendall() 里时调 vnc.close(), 内核会替那个正在
    # 进行的系统调用继续持有 socket —— fd 没了, TCP 却还是 ESTAB。表现就是
    # x11vnc 上挂着一个"没人认领"的客户端(`ss -tnp` 看不到属主 fd, 但
    # `ss -e` 的 cgroup 是我们的), 而我们的登记表是空的。
    # `-shared` 模式下客户端越多共享更新循环越慢 → **所有人一起卡**, 就是
    # "越用越卡"的根源。实测每 2~3 次连接就漏一个, 一晚上能攒到 7 个。
    for t in threads:
        t.join(timeout=3)
    stuck = [t.name for t in threads if t.is_alive()]
    if stuck:
        audit("vnc", f"桥接线程未及时退出: {','.join(stuck)} (可能有连接残留)")
    _vnc_unregister(teardown)           # 主动退出也要撤下登记, 别占着名额
    try:
        vnc.close()
    except OSError:
        pass
    ws.close()


# ---------------------------------------------------------------- 连接维护

def vnc_client_sockets():
    """x11vnc 上当前挂着的客户端连接: [{"port": 本地端口, "owner": 属主}]。

    数的是 **peer 为 :5900 的那一端**(= 连向 x11vnc 的客户端 socket, 也就是我们
    的桥接)。顺带把属主(`ss -tnp` 的 `users:(("python3",pid=…))`)一起带回来,
    是为了**能自证**: 多出来的连接究竟是谁的 —— 我们自己的旧进程? 测试脚本?
    还是别的什么东西。查不到属主就是空串(通常是别的用户的进程)。

    x11vnc 是 `-shared`: **客户端越多, 共享更新循环越慢**, 所有客户端的画面
    一起被拖慢 —— 表现就是"延迟正常但画面一直重连"。正常应该只有你自己 1 个。
    """
    try:
        out = subprocess.run(["ss", "-tnp"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not out.strip():
        return None
    seen, found = set(), []
    for ln in out.splitlines():
        f = ln.split()
        # State Recv-Q Send-Q Local Peer [users:(...)]
        if len(f) < 5 or f[0] != "ESTAB":
            continue
        if not f[4].endswith(f":{VNC_PORT}"):      # 只认"客户端 -> x11vnc"方向
            continue
        port = f[3].rsplit(":", 1)[-1]
        if port == str(VNC_PORT) or port in seen:  # 回环自连 / 重复行
            continue
        seen.add(port)
        owner = ""
        for tok in f[5:]:
            if tok.startswith("users:"):
                owner = tok
                break
        found.append({"port": port, "owner": owner})
    return found


def vnc_foreign_sockets(socks):
    """从 vnc_client_sockets() 的结果里挑出**不属于任何已登记桥接**的连接。

    这就是"遗留连接"的判据: 端口对不上, 说明它不是我们当前这个进程开的 ——
    可能是我们上一代进程/别的实例/测试脚本留下的。单独抽成函数是为了能被
    测试直接调用(巡检那是个 while True 的循环, 没法从外面验)。
    """
    if not socks:
        return []
    with _vnc_lock:
        ports = [it[2] for it in _vnc_bridges]
    # 只要有一条桥接没记下端口(getsockname 失败), 就认不出哪些是自己的 ——
    # 这时**宁可不下结论**: 巡检动手的代价是重启 x11vnc, 会掐掉你自己的会话,
    # 误判比漏判贵得多。
    if any(p is None for p in ports):
        return []
    ours = set(ports)
    return [s for s in socks if s["port"] not in ours]


def vnc_client_count():
    """x11vnc 上当前挂着几个客户端（None = 查不到）。"""
    s = vnc_client_sockets()
    return None if s is None else len(s)


def _vnc_janitor():
    """巡检 x11vnc 上的**遗留客户端**, 攒到一定量就重启 x11vnc 清掉。

    为什么必须有它: 手机锁屏 / 切网络 / 浏览器被系统杀掉时, 连接往往**不会正常
    关闭** —— 这些半死连接会一直挂在 x11vnc 上(实测见过一次 8 个, 属主进程早就
    没了但 socket 还在 ESTAB)。而 x11vnc 是 `-shared`: **客户端越多, 共享更新
    循环越慢, 所有客户端的画面一起被拖慢**, 表现就是"延迟正常但画面很卡"。

    判据: 正常情况下"我们登记的桥接数" ≥ "x11vnc 上的客户端数", 多出来的就是遗留。
    **连续 3 次(约 60 秒)都对不上才动手** —— 页面刷新时新旧连接会短暂重叠,
    一次性判断容易误伤。
    """
    stale = 0
    last_fix = 0.0
    while True:
        time.sleep(20)
        try:
            socks = vnc_client_sockets()
            if socks is None:
                continue
            with _vnc_lock:
                b = len(_vnc_bridges)
            n = len(socks)
            # 端口不在我们登记里的 = 遗留连接。带上属主信息是为了能自证:
            # 真出问题时日志里直接写着是谁(进程名+pid), 不用再猜。
            foreign = vnc_foreign_sockets(socks)
            over = n - max(1, b)          # 多出来的(不在我们登记里的)客户端数
            if over > 0:
                stale += 1
            else:
                stale = 0
            # 多 1 个: 连续 3 次(60s)才动手; 多 2 个以上: 2 次(40s)就动手
            need = 2 if over >= 2 else 3
            if stale >= need and time.time() - last_fix > 600:
                stale = 0
                last_fix = time.time()
                who = ", ".join(s["owner"] or ("本地端口 " + s["port"])
                                for s in foreign)[:220]
                audit("vnc", f"巡检: x11vnc 上有 {n} 个客户端但只登记了 {b} 个桥接"
                             f"(多 {over} 个), 判定为遗留连接 -> 重启 x11vnc 清理"
                             f"; 遗留属主: {who or '未知(可能属别的用户)'}")
                subprocess.run(["systemctl", "--user", "restart", "meow-vnc"],
                               env=x_env(), capture_output=True, timeout=25)
        except Exception:
            pass          # 巡检本身绝不能把服务搞挂


def _restart_soon(delay=1.2):
    """"一键清理并重启"的后半段: 稍后重启本服务。

    重启会把所有 VNC 桥接关掉, x11vnc 上的遗留客户端随之释放; 前端等
    /api/health 恢复后自动重连, 最终就只剩它自己一个。

    先 delay 一下是为了让 HTTP 响应能发回去, 否则前端只会看到连接被重置。
    """
    def run():
        time.sleep(delay)
        # 优先走 systemctl: 干净的 restart, 且不依赖单元的 Restart 策略。
        try:
            r = subprocess.run(["systemctl", "--user", "restart", "meow-console"],
                               env=x_env(), capture_output=True, timeout=25)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        # systemctl 不可用(没有 DBus 等)时的兜底: 带非零码退出,
        # 靠单元的 Restart=on-failure 拉起来。atexit 在 os._exit 下不跑,
        # 所以手动把 ffmpeg 子进程带走, 别留孤儿占着摄像头。
        _kill_children()
        os._exit(1)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- 审计日志

# 类别 -> 中文名。/log 页面按这个分组筛选。
AUDIT_CATS = {
    "login": "登录", "vnc": "远程桌面", "term": "终端", "cam": "摄像头",
    "audio": "声音", "clip": "剪贴板", "power": "电源", "cfg": "配置",
    "dock": "Dock", "kill": "进程", "dsh": "DSH", "boot": "启动",
    "init": "初始化", "ime": "输入法",
}


def read_audit(n=400):
    """读审计日志尾部 n 行, 返回**倒序**(最新在前)。

    用 deque(maxlen=n) 而不是 readlines()[-n:]: 后者会把整个文件读进内存,
    而审计日志是只追加的, 迟早会长到很大。
    """
    try:
        with open(AUDIT_FILE, "r", errors="replace") as f:
            lines = list(collections.deque(f, maxlen=n))
    except OSError:
        return []
    out = []
    for ln in lines:
        ln = ln.rstrip("\n")
        if not ln:
            continue
        # 格式: "2026-09-15 19:02:11 vnc 远程桌面连接"
        p = ln.split(" ", 3)
        if len(p) < 3:
            continue
        # 详细一丢丢: 只给**非今天**的行带上日期(月-日), 今天的仍只显示时分秒 ——
        # 翻旧记录时不用猜是哪天的, 又不至于每行都堆一串日期显得吵。
        t = p[1] if p[0] == time.strftime("%Y-%m-%d") else p[0][5:] + " " + p[1]
        out.append({"d": p[0], "t": t, "c": p[2],
                    "m": p[3] if len(p) > 3 else ""})
    out.reverse()
    return out


# ---------------------------------------------------------------- 状态推送

def ws_status_push(ws):
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                ws.recv_message()
        except (WSClosed, OSError):
            stop.set()
            try:
                ws.alive = False
                ws.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=reader, daemon=True).start()
    try:
        while ws.alive and not stop.is_set():
            snap = COLLECTOR.snapshot()
            ws.send_text(json.dumps(snap, ensure_ascii=False))
            stop.wait(1.0)
    except (WSClosed, OSError):
        pass
    finally:
        stop.set()
        ws.close()


# ---------------------------------------------------------------- 摄像头

# 设计要点(都是为了满足"电脑后台跑、不弹窗口、手机看"):
#   - 用 ffmpeg 直接读 v4l2 设备节点, **不经过任何 GUI**, 桌面上不会出现预览窗口。
#   - 只在有人连 /ws/cam 时才采集; 连接一断立刻 kill ffmpeg —— 既是隐私
#     (没人看就不该开着摄像头), 也省电省 CPU。
#   - 推的是 JPEG 一帧一帧(binary WS), 前端直接当图片显示, 不做任何录像/落盘。

CAM_DEFAULT_DEV = "/dev/video0"
CAM_DEFAULT_SIZE = "640x480"
CAM_DEFAULT_FPS = 12
CAM_MAX_FPS = 30
_CAM_SIZES = ("320x240", "640x480", "1280x720")
# 「还有人在看吗」的**唯一判据是前端 JS 主动发来的心跳**, 不是 WebSocket 的 pong:
# 浏览器/系统的网络栈会自动回 pong, 哪怕页面早就切到后台、JS 已经被挂起。
# 所以 JS 心跳一停(切后台/锁屏/离开), 就当没人看, 关掉摄像头。
CAM_IO_TIMEOUT = 10         # socket 读写超时: 卡死的客户端要在 10s 内被断掉
# 下面两个允许用环境变量压小 —— 好让 console/tests/cam_check.py 几秒内就能
# 把"心跳停了会不会自己停采"验完(生产环境保持默认)。
CAM_PING_EVERY = float(os.environ.get("MEOW_CAM_PING_EVERY") or 5)
CAM_VIEWER_TIMEOUT = float(os.environ.get("MEOW_CAM_VIEWER_TIMEOUT") or 20)


V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
_VIDIOC_QUERYCAP = 0x80685600          # _IOR('V', 0, struct v4l2_capability)


def _caps_is_capture(buf):
    """从 VIDIOC_QUERYCAP 的返回里判断能不能采画面(独立出来方便单测)。

    v4l2_capability: driver[16] card[32] bus_info[32] version[4] caps[4] device_caps[4]
                     -> caps 在 84, device_caps 在 88
    驱动若声明了 V4L2_CAP_DEVICE_CAPS, 要看 device_caps(整体 caps 是"整套设备"的)。
    """
    caps, dcaps = struct.unpack_from("<II", bytes(buf), 84)
    used = dcaps if caps & V4L2_CAP_DEVICE_CAPS else caps
    return bool(used & V4L2_CAP_VIDEO_CAPTURE)


def _v4l2_is_capture(dev):
    """这个 /dev/videoN 真的能采集画面吗?

    笔记本内置摄像头常常注册**两个**节点: video0 是真采集, video1 往往只是
    metadata(给 ISP/人脸算法用), 拿它去采会直接失败。问一句 QUERYCAP 就知道。
    """
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, buf, True)
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return _caps_is_capture(buf)


def cam_devices(capture_only=True):
    """列出本机视频设备节点。看不到不代表没有 —— 可能只是权限或命名空间。"""
    try:
        files = [p for p in glob.glob("/dev/video*") if re.search(r"\d+$", p)]
    except OSError:
        return []
    def key(p):
        m = re.search(r"(\d+)$", p)
        return int(m.group(1)) if m else 0
    files = sorted(files, key=key)
    if not capture_only or not files:
        return files
    cams = [p for p in files if _v4l2_is_capture(p)]
    # 一个都没判出来(老内核/没权限/容器)就别把能用的也弄没了 —— 宁可多列一个
    return cams or files


def cam_label(dev):
    """给设备一个人类可读的名字, 比如 'FHD Camera · /dev/video0'。

    从 sysfs 读, **不打开设备** —— 打开会让摄像头指示灯闪一下。
    """
    base = os.path.basename(dev)
    name = ""
    try:
        with open(f"/sys/class/video4linux/{base}/name", errors="replace") as f:
            name = f.read().strip()
    except OSError:
        pass
    # 内核给的常是 "厂商: 产品"(如 "FHD Camera: FHD Camera"), 冒号前那段没信息量
    if ": " in name:
        name = name.split(": ")[-1].strip() or name
    return f"{name} · {base}" if name else dev


def _jpeg_frames(stream):
    """从连续的 MJPEG 字节流里切出一张张完整 JPEG(FFD8…FFD9)。

    ffmpeg -f mjpeg 输出的就是首尾相接的 JPEG, 没有长度前缀, 必须自己按标记分帧。
    帧数据里不会出现裸的 FFD9(字节填充会插 00), 所以找第一个 FFD9 是安全的。
    """
    buf = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            i = buf.find(b"\xff\xd8")
            if i < 0:
                buf = b""
                break
            if i:
                buf = buf[i:]
            j = buf.find(b"\xff\xd9", 2)
            if j < 0:
                break
            frame = buf[:j + 2]
            buf = buf[j + 2:]
            yield frame


def _cam_attempts(dev, size, fps):
    """两组参数依次尝试: 先让摄像头直出 MJPEG(不重编码, 几乎不耗 CPU),
    失败(摄像头只支持 YUYV 等原始格式)再退回软件编码成 MJPEG。"""
    return [
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", size, "-framerate", str(fps), "-i", dev,
         "-c:v", "copy", "-f", "mjpeg", "-"],
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "v4l2", "-video_size", size, "-framerate", str(fps), "-i", dev,
         "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "-"],
    ]


def _cam_start(dev, size, fps, first_timeout=4.0):
    """起一个 ffmpeg 并等到拿到第一帧。返回 (proc, queue, err)。"""
    q = queue.Queue(maxsize=64)          # 有上限: 客户端卡住时别让帧堆爆内存
    last_err = ""
    for args in _cam_attempts(dev, size, fps):
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=x_env())
        except OSError as e:
            last_err = f"{e.__class__.__name__}: {e}"
            continue

        def reader(p=p, q=q):
            try:
                for f in _jpeg_frames(p.stdout):
                    q.put(f)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    q.put(None)
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()
        try:
            first = q.get(timeout=first_timeout)
        except queue.Empty:
            first = None
        if first is not None:
            q.put(first)                  # 放回去, 让推送循环统一发
            return p, q, ""
        # 这组参数没出帧 —— 收一下 ffmpeg 的报错再试下一组
        try:
            p.kill()
        except OSError:
            pass
        try:
            err = p.stderr.read()
        except (OSError, ValueError):
            err = b""
        msg = err.decode("utf-8", "replace").strip().splitlines()
        if msg:
            # 累积而不是覆盖: busy 这类关键信息常常只出现在第一组尝试里
            last_err = (last_err + " | " if last_err else "") + msg[-1][:200]
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return None, None, last_err or "摄像头没有输出画面"


_cam_lock = threading.Lock()
_cam_session = {"proc": None, "stop": None}      # 同一时刻只允许一个采集会话
# "摄像头页还有人在看"的最近时刻(前端 JS 心跳刷新)。给 /api/audio 复用:
# 麦克风只在摄像头页里开, 而那页走了之后 HTTP 流本身收不到任何信号 ——
# 半个死的连接连 _peer_gone 都探不出来(写还写得进去, 对端早不处理了)。
_viewer_seen = {"t": 0.0}


def _cam_release_current(timeout=3.0):
    """让上一个会话收工, 并等它**真的**把设备放开。

    摄像头是独占设备: 上一个 ffmpeg 还没退出就开新的, 必然 "Device or resource
    busy"。刷新页面/重连/从后台切回来都会触发, 这是 busy 最常见的原因。
    """
    stop = _cam_session.get("stop")
    proc = _cam_session.get("proc")
    if stop is not None:
        stop.set()
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        end = time.time() + timeout
        while time.time() < end and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is None:                  # 还不退就下狠手
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        # 记一笔: 否则"开始采集"次数会多于"停止采集", 看着像泄漏
        audit("cam", f"踢掉上一个采集会话 (pid={proc.pid})")
    _cam_session["proc"] = None
    _cam_session["stop"] = None


def _cam_hint(err):
    """把 ffmpeg 的英文报错翻成人能看懂的原因。"""
    e = (err or "").lower()
    if "busy" in e or "temporarily unavailable" in e:
        return ("摄像头被占用 —— 可能是别的程序在用(视频会议/浏览器/拍照软件), "
                "或上一次采集还没释放。关掉占用程序、稍等两秒再试")
    if "permission denied" in e:
        return "没有权限访问摄像头(执行 sudo usermod -aG video $USER 后重新登录)"
    if "no such device" in e or "no such file" in e:
        return "摄像头设备不存在(可能没接好或被拔掉了)"
    if "not a video capture device" in e or "inappropriate ioctl" in e:
        return "这个节点不是采集设备(内置摄像头常有个只给算法用的兄弟节点)"
    return err or "未知错误"


def _cam_stop(proc):
    """确保 ffmpeg 真的死掉 —— 漏一个就会在后台一直占着摄像头。"""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


def ws_cam_bridge(ws, dev=None, size=None, fps=None):
    """摄像头 -> 浏览器。单向推 MJPEG 帧。"""
    t0 = time.time()          # 记时长, 停止时写进日志
    dev = dev or CAM_DEFAULT_DEV
    size = size if size in _CAM_SIZES else CAM_DEFAULT_SIZE
    try:
        fps = max(1, min(int(fps or CAM_DEFAULT_FPS), CAM_MAX_FPS))
    except (TypeError, ValueError):
        fps = CAM_DEFAULT_FPS

    if not shutil.which("ffmpeg"):
        _cam_send_err(ws, "未安装 ffmpeg, 无法采集摄像头")
        return

    if not os.path.exists(dev):
        devs = cam_devices()
        if not devs:
            _cam_send_err(ws, "没有找到可用的摄像头设备")
            return
        dev = devs[0]                     # 指定的不存在就用第一个能采集的

    stop = threading.Event()
    with _cam_lock:
        # 摄像头是独占的: 先让上一个会话彻底放开设备再开新的, 否则必然 busy。
        # 刷新页面 / 自动重连 / 从后台切回来都会走到这里。
        _cam_release_current()
        proc, frames, err = _cam_start(dev, size, fps)
        if proc is None:
            _cam_send_err(ws, "打开摄像头失败: " + _cam_hint(err))
            return
        _cam_session["proc"] = proc
        _cam_session["stop"] = stop

    audit("cam", f"摄像头开始采集 {dev} {size}@{fps}fps (pid={proc.pid})")
    try:
        ws.send_text(json.dumps({"t": "start", "dev": dev,
                                 "size": size, "fps": fps}))
    except OSError:
        pass

    def pump():
        try:
            while not stop.is_set():
                try:
                    f = frames.get(timeout=1.0)
                except queue.Empty:
                    continue
                if f is None:
                    break
                ws.send_binary(f)
        except (OSError, WSClosed, ValueError):
            pass
        finally:
            stop.set()

    # 前端最后一次心跳的时间 —— 判"还有没有人在看"就看它
    last_viewer = time.time()
    _viewer_seen["t"] = last_viewer         # 供 /api/audio 复用(见 _serve_audio)

    def reader():
        """按 WS 协议读客户端消息, 只关心一件事: 前端 JS 还在不在发心跳。

        2026-09-22 修: 原来这里是 `ws.sock.recv()` 直接读原始字节, 后果有三 ——
          (a) 浏览器回的 pong 被当成普通数据, WSConn.last_pong 永远停在连上那一刻,
              于是"看 pong 判活"这套机制对摄像头完全失效;
          (b) 浏览器发来的 close 帧同样被当成"有数据", 服务端既不回应也不停采集;
          (c) 于是"按返回/切后台"之后, 只要 TCP 没有立刻 FIN(webview 在等服务端的
              close 回应、手机锁屏/切网造成的半死连接, 这些都很常见), 采集就永远
              不停, 摄像头指示灯一直亮 —— 和页面上"没人观看立即停止"的承诺不符。

        现在按协议读: close 帧会抛 WSClosed -> 立刻停; 同时只把前端 JS 的
        {"t":"hb"} 当作"有人在看"。切到后台时 JS 定时器被节流到几乎不跑, 心跳一停
        就停采集 —— 哪怕浏览器还在自动回 pong。
        """
        nonlocal last_viewer
        try:
            while not stop.is_set():
                op, data = ws.recv_message()
                if op == 0x1:                       # 前端心跳 {"t":"hb"}
                    try:
                        if json.loads(data.decode("utf-8", "replace")).get("t") == "hb":
                            last_viewer = time.time()
                            _viewer_seen["t"] = last_viewer   # 给 /api/audio 复用
                    except (ValueError, AttributeError, TypeError):
                        pass
        except socket.timeout:
            pass
        except (OSError, WSClosed):
            pass
        finally:
            stop.set()

    def watch():
        """看门狗: 定期 ping(NAT 保活), 并盯着"前端多久没来心跳了"。"""
        while not stop.is_set():
            if stop.wait(CAM_PING_EVERY):
                return
            if not ws.alive:
                break
            idle = time.time() - last_viewer
            if idle > CAM_VIEWER_TIMEOUT:
                audit("cam", "%.0fs 没收到前端心跳, 判定没人看, 停采集" % idle)
                break
        stop.set()

    try:
        # 10s 这个值两头都要顾: 客户端不再收数据时 sendall 要尽快超时(别白开着
        # 摄像头), 同时又不会把浏览器的控制帧读成半截(心跳/close 都是几字节, 一次到)。
        ws.sock.settimeout(CAM_IO_TIMEOUT)
    except OSError:
        pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t2 = threading.Thread(target=reader, daemon=True)
    t2.start()
    t3 = threading.Thread(target=watch, daemon=True)
    t3.start()
    try:
        stop.wait()                       # pump 或 watch 谁先发现问题都一样
    finally:
        stop.set()
        _cam_stop(proc)
        t.join(timeout=3)
        with _cam_lock:
            # 只清自己: 可能已经有新会话顶上来了, 别把它的记录抹掉
            if _cam_session.get("proc") is proc:
                _cam_session["proc"] = None
                _cam_session["stop"] = None
        try:
            ws.close()
        except OSError:
            pass
    audit("cam", "摄像头停止 %s (pid=%d, 共 %.0fs)" % (dev, proc.pid, time.time() - t0))


def _cam_send_err(ws, msg):
    audit("cam", "失败: " + msg)
    try:
        ws.send_text(json.dumps({"t": "err", "d": msg}))
    except OSError:
        pass
    try:
        ws.close()
    except OSError:
        pass


# ---------------------------------------------------------------- 声音

# 和摄像头同样的思路: ffmpeg 无头采集, 电脑端不弹任何东西。
# 但声音比画面更敏感, 所以:
#   - **默认绝不采集**: 只有用户点了「开启声音」才会请求 /api/audio
#   - 断开(关页面/停止)立刻杀 ffmpeg
#   - 走 HTTP 流式 MP3: 浏览器原生解码, 手机上省电, 也不用引入 JS 解码器
#     (代价是有 1~2 秒缓冲延迟; 监控场景完全够用)

AUDIO_RATE = 24000          # 语音够用, 省流量
AUDIO_BITRATE = "48k"
_audio_lock = threading.Lock()
_audio_session = {"proc": None, "stop": None}


def audio_sources():
    """列出 PulseAudio 的输入源。返回 [(源名, 是不是 monitor 回环)]。

    `.monitor` 结尾的是"扬声器回环"(能听到电脑播的声音, 但没播就是纯静音),
    不是麦克风。笔记本上两者常常都在, 选错就会出现"明明在采集却一点声都没有"。
    """
    try:
        r = subprocess.run(["pactl", "list", "sources", "short"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            out.append((parts[1], parts[1].endswith(".monitor")))
    return out


def audio_default_mic():
    """挑一个真正的麦克风(排除 .monitor 回环)。找不到就退回 default。"""
    for name, is_mon in audio_sources():
        if not is_mon:
            return name
    return "default"


def audio_unmute(src):
    """取消静音并把采集音量拉满。

    笔记本内置麦克风在 PulseAudio 里常常默认是 muted, 表现同样是"一点声都没有"。
    用户已经明确点了"开启声音", 帮他解除是合理的。
    """
    if not src or src == "default":
        return False
    ok = False
    for args in (["pactl", "set-source-mute", src, "0"],
                 ["pactl", "set-source-volume", src, "100%"]):
        try:
            subprocess.run(args, env=x_env(), capture_output=True, timeout=3)
            ok = True
        except (OSError, subprocess.SubprocessError):
            pass
    return ok


def audio_level(kind="pulse", src="default", seconds=1.5, wait=2.5):
    """采一小段看看电平 —— 用来判断"到底有没有采到声音"。

    安静环境下也应该有 -60 ~ -40 dB 的底噪; 如果是 -91 dB(数字静音),
    说明选错了源(扬声器回环)或麦克风被静音。

    wait: 在 seconds 之外额外容忍多少秒。**会挂起的设备**(这台机器的内置
    DMIC 就是: 能打开但一个字节都不出)只能靠超时跳过 —— 给太大就会白等。
    多个候选请用 audio_probe_all() 并行探, 别串行调这个。
    """
    cmd = ["ffmpeg", "-hide_banner", "-f", kind, "-i", src,
           "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, env=x_env(), capture_output=True,
                           text=True, timeout=seconds + wait + 6)   # 放宽: 这台机器光打开 pulse 源就要 2s+
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "err": str(e), "kind": kind, "src": src}
    err = p.stderr or ""
    def num(pat):
        m = re.search(pat, err)
        return float(m.group(1)) if m else None
    mx = num(r"max_volume:\s*([-\d.]+)\s*dB")
    return {
        "ok": p.returncode == 0 and mx is not None,
        "kind": kind, "src": src,
        "mean_volume": num(r"mean_volume:\s*([-\d.]+)\s*dB"),
        "max_volume": mx,
        "silent": (mx if mx is not None else -91.0) <= -90.0,
    }


def audio_probe_all(cands, seconds=1.2, wait=2.5, workers=8):
    """**并行**探测所有候选, 返回与 cands 同序的电平结果列表。

    为什么要并行: 串行探测的耗时是"所有候选之和", 而只要有**一个会挂起的
    设备**(这台机器的内置 DMIC 就是), 它就要吃掉一整个超时。实测这台机器
    mic 模式 5 个候选串行要 12 秒, 前端进摄像头页就一直转圈。
    并行之后总耗时 = 最慢的那一个(约 seconds+wait)。
    """
    if not cands:
        return []
    out = [None] * len(cands)

    def one(i):
        kind, src = cands[i]
        try:
            out[i] = audio_level(kind, src, seconds, wait)
        except Exception as e:                     # 兜底: 单个失败不影响整体
            out[i] = {"ok": False, "err": str(e), "kind": kind, "src": src}

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(cands))) as ex:
        list(ex.map(one, range(len(cands))))
    return out


def audio_probe_candidates(cands, seconds=0.7, wait=2.0):
    """探测全部候选, 返回与 cands **同序**的结果列表。

    分两路**同时**跑, 总耗时 = 两路里较慢的那一路, 而不是相加:

    - pulse 一路: 源是共享的(多个客户端可同时录音) → 候选之间并行
    - 非 pulse(alsa) 一路: 设备独占(`plughw:0,0` 和 `default` 可能指向
      同一块硬件, 并发打开会 EBUSY) → 候选之间串行

    这台机器 mic 模式有 5 个候选、其中内置 DMIC 会挂起吃满超时:
    串行 12 秒 → 现在约 3 秒。
    """
    out = [None] * len(cands)
    pulse = [(i, c) for i, c in enumerate(cands) if c[0] == "pulse"]
    other = [(i, c) for i, c in enumerate(cands) if c[0] != "pulse"]

    def do_pulse():
        if not pulse:
            return
        for (i, _), lv in zip(pulse,
                              audio_probe_all([c for _, c in pulse], seconds, wait)):
            out[i] = lv

    def do_other():
        for i, (kind, src) in other:          # 独占设备, 串行
            try:
                out[i] = audio_level(kind, src, seconds, wait)
            except Exception as e:
                out[i] = {"ok": False, "err": str(e), "kind": kind, "src": src}

    ths = [threading.Thread(target=f, daemon=True) for f in (do_pulse, do_other)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return [lv if lv is not None
            else {"ok": False, "err": "skipped", "kind": c[0], "src": c[1]}
            for lv, c in zip(out, cands)]


def _default_sink_name():
    try:
        r = subprocess.run(["pactl", "get-default-sink"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def get_pc_volume():
    """读默认输出设备的当前主音量。

    返回 {"vol": 0~150 整数(取声道最大值), "muted": bool, "sink": 名}；
    pactl 不可用 / 找不到默认设备时返回 None。
    """
    sink = _default_sink_name()
    if not sink:
        return None
    try:
        r = subprocess.run(["pactl", "get-sink-volume", sink],
                           env=x_env(), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    pcts = [int(x) for x in re.findall(r"(\d+)%", r.stdout or "")]
    if not pcts:
        return None
    vol = max(pcts)
    muted = False
    try:
        rm = subprocess.run(["pactl", "get-sink-mute", sink],
                            env=x_env(), capture_output=True, text=True, timeout=5)
        if rm.returncode == 0:
            muted = bool(re.search(r"mute:\s*yes", (rm.stdout or ""), re.I))
    except (OSError, subprocess.SubprocessError):
        pass
    return {"vol": vol, "muted": muted, "sink": sink}


def set_pc_volume(pct):
    """设默认输出设备主音量为 pct(0~150 整数)。返回 (ok, msg)。"""
    sink = _default_sink_name()
    if not sink:
        return False, "找不到默认输出设备(pactl get-default-sink 无输出)"
    try:
        pct = max(0, min(150, int(pct)))
    except (TypeError, ValueError):
        return False, "音量必须是 0~150 的整数"
    try:
        r = subprocess.run(["pactl", "set-sink-volume", sink, f"{pct}%"],
                           env=x_env(), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用 pactl 失败: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pactl 返回非零").strip()
    return True, "ok"


# ---------------------------------------------------------------- 屏幕亮度
# 走 xrandr 的软件调光(--brightness): 不需要任何权限, 外接屏也管用。
# 这台机器的 /sys/class/backlight/... 对普通用户不可写(实测), 所以不指望它。
def _xrandr_outputs():
    """已连接的显示输出名, 标了 primary 的排最前。"""
    try:
        r = subprocess.run(["xrandr", "--query"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    primary, others = [], []
    for ln in (r.stdout or "").splitlines():
        m = re.match(r"^(\S+)\s+connected\b(.*)$", ln)
        if not m:
            continue
        (primary if "primary" in m.group(2) else others).append(m.group(1))
    return primary + others


def get_pc_brightness():
    """当前屏幕亮度(5~100 的整数)。

    xrandr 的 --brightness 是"设了就完事", 回读不回来 —— 所以以 config.json 里
    存的那份为准(滑块写进去的), 没存过就当 100。
    """
    try:
        pct = int((load_config() or {}).get("brightness", 100))
    except (TypeError, ValueError):
        return 100
    return max(5, min(100, pct))


def set_pc_brightness(pct):
    """设屏幕亮度(5~100), 返回 (ok, msg)。改完写进 config.json 持久化。"""
    try:
        pct = max(5, min(100, int(pct)))
    except (TypeError, ValueError):
        return False, "亮度必须是 5~100 的整数"
    outs = _xrandr_outputs()
    if not outs:
        return False, "找不到可用的显示输出(xrandr 不可用, 或没接显示器)"
    val = "%.2f" % (pct / 100.0)
    for name in outs:
        try:
            r = subprocess.run(["xrandr", "--output", name, "--brightness", val],
                               env=x_env(), capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"调用 xrandr 失败: {e}"
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "xrandr 返回非零").strip()
    save_config({"brightness": pct})
    return True, "ok"


# ---------------------------------------------------------------- 主题色
# 影响所有用 var(--acc) 渲染的地方(文字/按钮底/选中态/边框...)。
# 页面侧不用改模板: 所有页面都过 render(), 在那里统一注入一个 :root{--acc:...},
# 排在 app.css 之后所以能盖住默认蓝。
THEME_DEFAULT = "#4da3ff"          # 就是原来那抹蓝
# 太暗的不给用: 控制台是暗底, 主题色还要当**文字颜色**用, 深色在上面看不清。
# 前端那个圆盘本身只画浅色范围, 这里再兜一道(接口也能被直接调用)。
THEME_MIN_LUM = 0.30


def _hex_rgb(s):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (s or "").strip())
    if not m:
        return None
    v = m.group(1)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def theme_luminance(rgb):
    """WCAG 相对亮度(0~1)。"""
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def get_theme_acc():
    v = (load_config() or {}).get("themeAcc") or THEME_DEFAULT
    return v if _hex_rgb(v) else THEME_DEFAULT


def set_theme_acc(hexv):
    """设主题色, 返回 (ok, msg)。太暗的直接拒绝。"""
    rgb = _hex_rgb(hexv)
    if rgb is None:
        return False, "颜色要写成 #RRGGBB"
    if theme_luminance(rgb) < THEME_MIN_LUM:
        return False, "这个颜色太暗了, 暗底上看不清 —— 挑个亮一点的"
    save_config({"themeAcc": "#%02x%02x%02x" % rgb})
    return True, "ok"


# ---------------------------------------------------------------- 自定义背景
# 图片存 data/bg.img(运行时目录, 不进仓库), 参数存 config.json。
# CSS 全在 render() 里注入 —— app.css 一行都不用改。
BG_FILE = os.path.join(DATA_DIR, "bg.img")
BG_MAX = 8 * 1024 * 1024            # 8 MB


def bg_state():
    """返回 (有图吗, 版本号, 透明, 暗化, 模糊)。版本号用 mtime, 兼做 cache buster。"""
    try:
        has = os.path.getsize(BG_FILE) > 0
        ver = int(os.path.getmtime(BG_FILE)) if has else 0
    except OSError:
        has, ver = False, 0
    c = load_config() or {}

    def gi(k, d, lo, hi):
        try:
            return max(lo, min(hi, int(c.get(k, d))))
        except (TypeError, ValueError):
            return d

    return (has, ver, gi("bgTrans", 35, 0, 90), gi("bgDim", 45, 0, 80),
            gi("bgBlur", 0, 0, 20))


def bg_css():
    """注入到 </head> 前的全局样式(主题色 + 背景)。"""
    has, ver, trans, dim, blur = bg_state()
    alpha = "%.2f" % (1 - trans / 100.0)
    out = [
        ":root{--acc:%s;--panel-a:%s}" % (get_theme_acc(), alpha),
        # 面板底色拆成"RGB + 单独的不透明度": 半透明就是手机 QQ 空间动态页那种
        # 卡片浮在图上; 透明度调到 0 时卡片完全不透明, 图片只从卡片缝隙里露出来,
        # 滑动页面看到的就是图片的不同部分。app.css 里 12 处 var(--card) 一起生效。
        ":root{--card:rgb(19 26 35 / var(--panel-a));"
        "--card2:rgb(15 21 29 / var(--panel-a))}",
        ".topbar{background:rgb(11 15 20 / calc(var(--panel-a) * .97))}",
    ]
    if has:
        # 底图层的高度用 100lvh(large viewport height)而不是 inset:0:
        # 手机上地址栏收起/展开会改变 100vh/100dvh, cover 跟着重算, 背景就会
        # 跟着"放大/缩小"(实测反馈)。lvh 是恒定值, 图片就不会再被缩放。
        # 另外: 用两个 position:fixed 的层(图 + 暗化)放在最底下, 而不是
        # background-attachment:fixed —— 后者在 iOS Safari 上很不可靠。
        out += [
            "html{background:#0b0f14}body{background:transparent}"
            # 远程桌面页 body 是 body.vnc-page{background:#000}(见 app.css),
            # 类选择器比光秃秃的 body 优先级高, 会把底图整个盖住 —— 显式盖回去。
            "body.vnc-page{background:transparent}",
            "body::before{content:'';position:fixed;left:0;right:0;top:0;height:100lvh;z-index:-1;"
            "background:#0b0f14 url('/bg/img?v=%d') center/cover no-repeat;"
            "pointer-events:none%s}"
            % (ver, (";filter:blur(%dpx)" % blur) if blur else ""),
            "body::after{content:'';position:fixed;left:0;right:0;top:0;height:100lvh;z-index:-1;"
            "background:rgba(11,15,20,%.2f);pointer-events:none}" % (dim / 100.0),
        ]
    return "<style>%s</style>\n" % "".join(out)


def audio_default_monitor():
    """默认输出设备对应的 monitor —— 采它就能拿到"电脑正在放的声音"。

    实测: 播放测试音时 monitor 能采到 -18 dB, 完全可用。
    """
    mons = [n for n, mon in audio_sources() if mon]
    try:
        r = subprocess.run(["pactl", "get-default-sink"], env=x_env(),
                           capture_output=True, text=True, timeout=5)
        sink = (r.stdout or "").strip()
        cand = sink + ".monitor"
        if sink and cand in mons:          # 必须真的存在于源列表里才认
            return cand
    except (OSError, subprocess.SubprocessError):
        pass
    return mons[0] if mons else None       # 退而求其次: 第一个回环


def audio_candidates(mode="auto"):
    """按优先级列出候选。mode: 'mic'=麦克风 / 'sys'=电脑声音 / 'auto'=自动"""
    srcs = audio_sources()
    mics = [n for n, mon in srcs if not mon]
    mons = [n for n, mon in srcs if mon]
    cands = []
    if mode == "sys":
        d = audio_default_monitor()
        if d:
            cands.append(("pulse", d))
        cands += [("pulse", m) for m in mons]
    elif mode == "mic":
        cands += [("pulse", m) for m in mics]
    else:
        cands += [("pulse", m) for m in mics]
        d = audio_default_monitor()
        if d:
            cands.append(("pulse", d))
        cands += [("pulse", m) for m in mons]
    # pactl 不可用(或只有回环)时, 直接走 ALSA 硬件
    cands.append(("alsa", "plughw:0,0"))
    cands.append(("alsa", "default"))
    cands.append(("pulse", "default"))    # 最后才用 default(很可能是空回环)
    return cands


# 选源探测很慢(要真去开设备试采), 而这台机器光开一个 pulse 源就要 2s+ ——
# 每次点 🔊 都等 2~3 秒才出声不值得, 而结果短时间内是稳定的。缓存它。
_audio_pick_cache = {}
_audio_pick_lock = threading.Lock()


def audio_pick_source_cached(mode="auto", ttl=120):
    """audio_pick_source 的缓存版(给正常播放路径用)。

    诊断接口 /api/audio/check 一律走**不缓存**的原函数 —— 排查时就是要看实时的。
    """
    now = time.time()
    with _audio_pick_lock:
        hit = _audio_pick_cache.get(mode)
        if hit and hit[0] > now:
            return hit[1]
    val = audio_pick_source(mode)
    with _audio_pick_lock:
        _audio_pick_cache[mode] = (now + ttl, val)
    return val


def audio_pick_source(mode="auto", seconds=0.9):
    """挑一个真的有信号的源, 返回 (kind, src, 尝试记录)。

    **mode='sys' 时不做静音检测**: 电脑没在放声音时 monitor 本来就是静音,
    检测会把它误判成"不可用"而跳过 —— 那正好是要采的那个。

    pulse 候选并行探(快); 命中就立刻返回, 不浪费时间去试 alsa。
    只有 pulse 全都不行时才串行试 alsa(独占设备, 不能并发)。
    """
    cands = audio_candidates(mode)
    if mode == "sys" and cands:
        kind, src = cands[0]
        return kind, src, [{"kind": kind, "src": src,
                            "ok": True, "silent": None}]
    tried = []
    pulse = [c for c in cands if c[0] == "pulse"]
    other = [c for c in cands if c[0] != "pulse"]
    if pulse:
        for (kind, src), info in zip(pulse,
                                     audio_probe_all(pulse, seconds, wait=2.0)):
            tried.append(info)
            if info.get("ok") and not info.get("silent"):
                return kind, src, tried
    for kind, src in other:
        info = audio_level(kind, src, seconds, wait=2.0)
        tried.append(info)
        if info.get("ok") and not info.get("silent"):
            return kind, src, tried
    return cands[0][0], cands[0][1], tried


def _audio_label(src):
    """给音频设备一个短一点的、能区分内置/外接的显示名。"""
    if not src:
        return ""
    if "usb-" in src.lower():
        where = "外接USB"
    elif "pci-" in src or "platform-" in src:
        where = "内置"
    else:
        where = ""
    tail = ".".join(src.split(".")[-3:])
    return f"{where} {tail}".strip()


_mics_cache = {"t": 0.0, "val": None, "busy": False}
_mics_lock = threading.Lock()


def _mics_refresh(ttl):
    """后台重新探测一遍并写入缓存(供 audio_list_mics_cached 用)。"""
    val = audio_list_mics()
    with _mics_lock:
        _mics_cache["t"] = time.time() + ttl
        _mics_cache["val"] = val
        _mics_cache["busy"] = False
    return val


def audio_list_mics_cached(ttl=90):
    """带缓存的麦克风探测。

    探测要**试采**(设备存在 ≠ 能用: 内置 DMIC 能打开但一个字节都不出,
    只能等超时)。已经改成并行探测(约 3 秒), 但前端每次进摄像头页都会调它,
    所以再加一层缓存:

    - 缓存新鲜: 立即返回
    - 缓存过期: **先把旧结果返回**(页面完全不卡), 同时后台刷新
    - 从没探过: 同步探一次(必须拿到正确结果, 否则会误报"没有麦克风")
    """
    now = time.time()
    with _mics_lock:
        val = _mics_cache["val"]
        if val is None:                       # 第一次: 同步探
            val = audio_list_mics()
            _mics_cache["t"] = time.time() + ttl
            _mics_cache["val"] = val
            return val
        if _mics_cache["t"] <= now and not _mics_cache["busy"]:
            _mics_cache["busy"] = True
            threading.Thread(target=_mics_refresh, args=(ttl,),
                             daemon=True).start()
        return val


def audio_list_mics(seconds=0.7):
    """列出麦克风, 并**实测**哪些真的能采到声音。

    设备存在 ≠ 能用: 这台机器的内置 DMIC 就是"设备在、采集一打开就挂起"。
    所以必须试采一下才算数 —— 插上耳机麦/U盘麦后, 它会自动变成可用。

    pulse 源并行探(共享设备, 并发安全); alsa 独占, 串行探。
    否则一个挂起的设备会把总耗时从 3 秒拖到十几秒。
    """
    cands, seen = [], set()
    for kind, src in audio_candidates("mic"):
        if kind == "pulse" and src.endswith(".monitor"):
            continue                       # 回环是"听电脑", 不是麦克风
        if src in seen:
            continue
        seen.add(src)
        cands.append((kind, src))
    levels = audio_probe_candidates(cands, seconds)
    return [{
        "kind": k, "src": s, "label": _audio_label(s),
        "usable": bool(lv.get("ok") and not lv.get("silent")),
        "max_volume": lv.get("max_volume"),
    } for (k, s), lv in zip(cands, levels)]


def audio_first_usable_mic():
    """返回第一个真的能用的麦克风; 没有就返回 None。走缓存, 不重复探测。"""
    for m in audio_list_mics_cached():
        if m["usable"]:
            return m
    return None


# ---- "本机不出声, 声音只发手机" 用的虚拟输出 ----
# 直接采硬件 sink 的 monitor 时, **本机一静音 monitor 就没声了**(用户实测确认),
# 所以想"电脑静音但手机能听"必须换条路: 建一个 null sink(数据没人播 -> 一点声音
# 都不出), 把正在播放的流挪过去, 再采它的 monitor。
NULL_SINK = "meow_silent"


def _pactl(*args, timeout=6):
    """跑一条 pactl, 返回 (ok, 输出)。"""
    try:
        r = subprocess.run(["pactl", *args], env=x_env(),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def _sink_idx(name):
    ok, out = _pactl("list", "short", "sinks")
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 2 and f[1] == name:
            return f[0]
    return None


def _null_sink_module():
    ok, out = _pactl("list", "short", "modules")
    for ln in out.splitlines():
        if NULL_SINK in ln:
            return ln.split()[0]
    return None


def audio_out_mode():
    """声音去向: pc(电脑也响) / silent(只发手机, 电脑静音)。"""
    v = (load_config() or {}).get("audioOut")
    return "silent" if v == "silent" else "pc"


def silent_sink_ensure():
    """确保虚拟输出存在, 并把正在播放的流都搬过去。返回 (ok, 说明)。"""
    if _sink_idx(NULL_SINK) is None:
        ok, out = _pactl("load-module", "module-null-sink",
                         "sink_name=" + NULL_SINK,
                         "sink_properties=device.description=MEOW-Silent")
        if not ok:
            return False, "建虚拟输出失败: " + out
    idx = _sink_idx(NULL_SINK)
    n = 0
    ok, out = _pactl("list", "short", "sink-inputs")
    for ln in out.splitlines():
        f = ln.split()
        if len(f) > 1 and f[1] != idx and _pactl("move-sink-input", f[0], NULL_SINK)[0]:
            n += 1
    return True, "搬到虚拟输出 %d 路声音(本机不再出声)" % n


def silent_sink_release():
    """把流搬回默认输出并卸掉虚拟输出 —— 不做的话电脑会一直没声音。"""
    idx = _sink_idx(NULL_SINK)
    if idx is None:
        return
    n = 0
    ok, out = _pactl("list", "short", "sink-inputs")
    for ln in out.splitlines():
        f = ln.split()
        if len(f) > 1 and f[1] == idx and _pactl("move-sink-input", f[0], "@DEFAULT_SINK@")[0]:
            n += 1
    mid = _null_sink_module()
    if mid:
        _pactl("unload-module", mid)
    audit("audio", "虚拟输出收工: 搬回 %d 路声音, 本机声音已恢复" % n)


def _audio_cmd(kind, src):
    """按 kind 组出 ffmpeg 采集命令(固定编码成 MP3 单声道)。"""
    # -fragment_size: pulse 采集每次读取的粒度。默认交给服务端决定, 在 PipeWire 上
    # 实测是"每 ~2 秒给一大块(12KB)"—— 音频就一段一段地到手机, 听着一直卡。
    # 压到 2048 字节(48k 单声道约 21ms)才能连续地取、连续地发。
    frag = ["-fragment_size", "2048"] if kind == "pulse" else []
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", kind, *frag, "-i", src,
            "-ac", "1", "-ar", str(AUDIO_RATE),
            "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
            "-flush_packets", "1",   # 每编完一包立刻吐出来(否则 ffmpeg 攒 ~2s/12KB 才发一次, 手机听感就是一段一段)
        "-f", "mp3", "-"]


def _audio_start(src, first_timeout=4.0, kind="pulse"):
    # 读 ffmpeg 的 stdout 一律用 read1() 而不是 read():
    # read(n) 会**阻塞到攒满 n 字节**才返回 —— 48kbps 下 8192 字节约 1.4 秒,
    # 于是音频变成"每 1~2 秒一整段"地推给手机, 听感就是一段一段的卡顿
    # (实测: 回环 3.5s 才一次性给 8390 字节, 转发器那边每 2 秒一批)。
    # read1(n) 是"有多少给多少", 立刻返回, 流才是连续的。
    """起 ffmpeg 采声音, 等到流出第一块数据。返回 (proc, queue, first, err)。"""
    last_err = ""
    for args in (_audio_cmd(kind, src),):
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=x_env())
        except OSError as e:
            last_err = f"{e.__class__.__name__}: {e}"
            continue
        q = queue.Queue(maxsize=64)

        def reader(p=p, q=q):
            try:
                while True:
                    d = p.stdout.read1(4096)
                    if not d:
                        break
                    q.put(d)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    q.put(None)
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()
        try:
            first = q.get(timeout=first_timeout)
        except queue.Empty:
            first = None
        if first is not None:
            return p, q, first, ""
        try:
            p.kill()
        except OSError:
            pass
        try:
            err = p.stderr.read()
        except (OSError, ValueError):
            err = b""
        msg = err.decode("utf-8", "replace").strip().splitlines()
        if msg:
            last_err = (last_err + " | " if last_err else "") + msg[-1][:200]
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return None, None, None, last_err or "没有采集到声音"


def _peer_gone(sock):
    """对端是不是已经关了? 用 select + MSG_PEEK, 不消耗数据也不阻塞。

    服务端是只写的(推流), 不读就不会发现对方已经走了 —— 内核缓冲区没满时
    write 一直成功, 于是麦克风/摄像头被白占。这里每轮瞄一眼。
    """
    try:
        import select
        rd, _, _ = select.select([sock], [], [], 0)
        if not rd:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False                 # 有数据但还没到 / 非阻塞, 算活着
    except OSError:
        return True


def _audio_hint(err):
    e = (err or "").lower()
    if "busy" in e or "temporarily unavailable" in e:
        return "录音设备被占用(别的程序在用麦克风), 关掉后重试"
    if "no such device" in e or "cannot find" in e or "unknown input" in e:
        return "找不到录音设备(没接麦克风, 或音频服务没起来)"
    if "permission denied" in e:
        return "没有录音权限"
    return err or "未知错误"


def _kill_children():
    """进程退出时把正在跑的 ffmpeg 一起带走。

    否则 `systemctl --user restart meow-console` 之后, 那些 ffmpeg 会变成
    **孤儿进程**继续占着摄像头/麦克风 —— 表现就是"明明退出了, 摄像头灯还亮着",
    而且下次再开还会因为设备被占而报 busy。
    """
    for sess in (_cam_session, _audio_session):
        p = sess.get("proc")
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except (OSError, subprocess.SubprocessError):
                pass


atexit.register(_kill_children)


def _audio_release_current(timeout=3.0):
    stop = _audio_session.get("stop")
    proc = _audio_session.get("proc")
    if stop is not None:
        stop.set()
    if proc is not None and proc.poll() is None:
        _cam_stop(proc)                  # 同样的"先礼后兵"收尾
    _audio_session["proc"] = None
    _audio_session["stop"] = None


# ---------------------------------------------------------------- 电源控制

def _run_detached(cmd, env=None):
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def power_action(action):
    env = {**os.environ, **(session_env() or {})}
    if action == "lock":
        _run_detached(["loginctl", "lock-session"])
        return True, "已发送锁屏"
    if action == "screen-off":
        _run_detached(["xset", "dpms", "force", "off"], env=env)
        return True, "已熄屏(动动鼠标/键盘即可唤醒)"
    if action == "screen-on":
        _run_detached(["xset", "dpms", "force", "on"], env=env)
        return True, "已亮屏"
    if action == "suspend":
        threading.Timer(0.8, _run_detached, args=(["sudo", "-n", "systemctl", "suspend"],)).start()
        return True, "即将睡眠(网络会断开, 唤醒需物理操作或WoL)"
    if action == "reboot":
        threading.Timer(0.8, _run_detached, args=(["sudo", "-n", "systemctl", "reboot"],)).start()
        return True, "即将重启"
    if action == "poweroff":
        threading.Timer(0.8, _run_detached, args=(["sudo", "-n", "systemctl", "poweroff"],)).start()
        return True, "即将关机"
    return False, "未知操作"


# ---------------------------------------------------------------- HTTP 服务

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".ico": "image/x-icon", ".woff": "font/woff",
    ".woff2": "font/woff2", ".map": "application/json", ".txt": "text/plain",
}


# ---------------------------------------------------------------- 本地可选扩展
# 这个仓库只发布「远程桌面控制台」本身。像文件管理(8330)、DSH 助手(3080) 这类
# **本机私有的东西一律不进仓库** —— 想要就在 console/ 下放一个 extras_local.py
# (.gitignore 已排除), 提供 quick_links() / extra_ports() / handle_route()。
# 没有这个文件就是纯远程桌面, 行为与现在完全一致。
def _load_local_extras():
    try:
        import extras_local
        return extras_local
    except ImportError:
        return None


LOCAL_EXTRAS = _load_local_extras()

_vnc_preload = {"t": 0.0, "html": ""}


def vnc_module_preloads():
    """提前把 noVNC 那个包拉起来, 别等 vnc.js 解析到 import 才开始。

    原来是 50 个 ES 模块(依赖深 4 层), 浏览器只能一层层发现依赖 ——
    在 54ms 往返的链路上光"发现"就要 ~270ms。现在 esbuild 打成了一个文件,
    这里只需要预载那一个。
    """
    if os.path.isfile(BUILT_BUNDLE):
        return ('<link rel="modulepreload" '
                'href="/static/vendor/novnc-bundle.js">')
    # 没打包时退回逐个模块预载(保证功能不受影响)
    now = time.time()
    if _vnc_preload["html"] and _vnc_preload["t"] > now:
        return _vnc_preload["html"]
    root = os.path.join(STATIC_DIR, "vendor", "novnc")
    seen, stack = set(), [os.path.join(root, "core", "rfb.js"),
                          os.path.join(root, "core", "util", "events.js")]
    while stack:
        p = os.path.realpath(stack.pop())
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for m in re.finditer(r"""from\s+['"]([^'"]+)['"]""", src):
            dep = m.group(1)
            if dep.startswith("."):
                stack.append(os.path.join(os.path.dirname(p), dep))
    urls = sorted("/static/vendor/novnc/" +
                  os.path.relpath(p, root).replace(os.sep, "/") for p in seen)
    _vnc_preload["html"] = "".join(
        f'<link rel="modulepreload" href="{u}">' for u in urls)
    _vnc_preload["t"] = now + 600
    return _vnc_preload["html"]


def render(name, **kw):
    with open(os.path.join(TEMPLATE_DIR, name)) as f:
        html = f.read()
    for k, v in kw.items():
        html = html.replace("{{" + k + "}}", str(v))
    # 主题色 + 自定义背景: 统一在这里注入。所有页面都过 render(), 所以一处管全部 ——
    # 不用挨个改模板, 将来新增模板也自动带上。
    # 放在 </head> 前 = 排在 app.css 之后; 同优先级后者生效, 正好盖住默认值。
    if "</head>" in html:
        html = html.replace("</head>", bg_css() + "</head>", 1)
    return html


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "meow-console"
    timeout = 120

    def access_host(self):
        """浏览器实际访问本控制台用的主机名/IP。

        不能直接用 socket.gethostname(): 本机短名**可能只解析到 IPv6**(实测过)
        (fd7a:... Tailscale / fe80:... 链路本地), 而 lan_forward 只转发 IPv4,
        于是面板里拼出的 http://<短名>:3080/... 在手机上连不通。
        用请求里的 Host 头 —— 手机是怎么连到控制台的, 就让它怎么去连别的端口。
        """
        h = (self.headers.get("Host") or "").strip()
        if not h:
            return COLLECTOR.hostname
        if h.startswith("["):                      # [IPv6]:port
            i = h.find("]")
            return h[:i + 1] if i > 0 else h
        return h.rsplit(":", 1)[0] if ":" in h else h

    def setup(self):
        # 交互式小包(点击/按键/状态帧)禁用 Nagle, 避免 40ms+ 延迟
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        super().setup()

    # 这些路径不打访问日志。前端每次加载页面要几十个静态资源请求, 远程桌面
    # 还每隔几秒用 /api/health 探一次延迟 —— 全打出来会把 syslog 刷爆
    # (实测远程桌面开着时 /api/health 一天几千行, 是 syslog 里 python 那部分的全部来源)。
    _QUIET_PATHS = ("/static/", "/favicon", "/api/health")

    def log_message(self, fmt, *args):
        try:
            path = str(args[0]).split(" ")[1]
        except (IndexError, AttributeError):
            path = ""
        # 出错还是要记 —— 别把问题一起静音了
        code = str(args[1]) if len(args) > 1 else ""
        if path.startswith(self._QUIET_PATHS) and not code.startswith(("4", "5")):
            return
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # ---- 工具
    def _user(self):
        c = self.headers.get("Cookie", "")
        m = re.search(COOKIE_NAME + r"=([0-9a-f.]+)", c)
        return parse_token(m.group(1)) if m else None

    def _login_page(self, err=""):
        """登录页。还没设凭据时显示的是**首次设置**表单。"""
        host = COLLECTOR.hostname
        if need_setup():
            form = (
                '<form method="post" action="/setup">'
                '<input type="text" name="user" placeholder="用户名(自己起一个)"'
                ' autocomplete="username" required>'
                '<input type="password" id="pw1" name="pass"'
                ' placeholder="密码(至少 6 位)" autocomplete="new-password"'
                ' required minlength="6">'
                '<input type="password" id="pw2" name="pw2" placeholder="再输一次密码"'
                ' autocomplete="new-password" required minlength="6">'
                '<button type="submit">设置并进入</button>'
                f'<p class="login-err">{err}</p>'
                "</form>"
            )
            self._html(200, render("login.html", title="首次设置", heading="MEOW控制台",
                                   sub=host, logo="⌘", form=form,
                                   tip="第一次使用: 设一个你自己的账号密码, 只存在这台电脑上"))
        else:
            form = (
                '<form method="post" action="/login">'
                '<input type="text" name="user" placeholder="用户名"'
                ' autocomplete="username" required>'
                '<input type="password" name="pass" placeholder="密码"'
                ' autocomplete="current-password" required>'
                '<button type="submit">进入控制台</button>'
                f'<p class="login-err">{err}</p>'
                "</form>"
            )
            self._html(200, render("login.html", title="登录", heading="MEOW控制台",
                                   sub=host, logo="⌘", form=form,
                                   tip="仅限 Tailscale / 局域网内访问"))

    def _reply(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None,
               cache="no-cache"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _write_chunk(self, data):
        """写一个 HTTP chunk。返回 False 表示客户端已经走了(该收工了)。"""
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except (OSError, ValueError):
            return False

    def _serve_audio(self, src="default"):
        """把麦克风的声音编码成 MP3, 边采边以 chunked 方式推给浏览器。"""
        if not shutil.which("ffmpeg"):
            self._json({"ok": False, "err": "未安装 ffmpeg, 无法采集声音"})
            return
        # 关键: 不能"能打开就用" —— PulseAudio 的 default 常是扬声器回环,
        # 没播声音时是纯静音(实测 max_volume = -91dB)。必须逐个试过去,
        # 挑第一个真的有信号的源。
        # 只用手机听: 走虚拟输出, 本机一点声音都不出
        # 走虚拟输出的两种情况: 显式请求(src=silent) 或 设置里选了"只发手机"
        use_null = (src in ("silent", "quiet", "onlyphone")
                    or audio_out_mode() == "silent")
        _null_forced = False
        if use_null:
            ok, msg = silent_sink_ensure()
            if not ok:
                self._json({"ok": False, "err": msg})
                return
            audit("audio", "只用手机听: " + msg)
            _null_forced = True

        mode = "auto"
        if src in ("sys", "system", "monitor", "sound"):
            mode = "sys"          # 电脑正在放的声音(扬声器回环)
        elif src in ("mic", "microphone"):
            mode = "mic"          # 麦克风(环境音)
        kind = "pulse"
        if mode == "mic":
            # 麦克风要先确认**真的能用** —— 设备存在不代表采得到声音。
            # 没有可用的就明确拒绝, 别让前端收到一段纯静音的流还以为成功了。
            # 用缓存版本, 别重复探两遍(每次探测都要真开设备试采)。
            mics = audio_list_mics_cached()
            mic = next((m for m in mics if m["usable"]), None)
            if mic is None:
                audit("audio", f"麦克风不可用(检测到 {len(mics)} 个设备, 都采不到声音)")
                self._json({"ok": False, "err":
                            "没有可用的麦克风(设备可能被占用/未初始化), 无法收音"})
                return
            kind, src = mic["kind"], mic["src"]
            audit("audio", f"选源(mic): {kind}:{src}")
        elif mode != "auto" or src in (None, "", "default"):
            if _null_forced:
                kind, src = "pulse", NULL_SINK + ".monitor"
            else:
                kind, src, _tried = audio_pick_source_cached(mode)
            audit("audio", f"选源({mode}): {kind}:{src} "
                           f"(试过 {len(_tried)} 个候选)")
        if kind == "pulse" and src != "default":
            audio_unmute(src)          # 内置麦克风常被默认静音
        stop = threading.Event()
        with _audio_lock:
            _audio_release_current()          # 同时只保留一路采集
            proc, chunks, first, err = _audio_start(src, kind=kind)
            if proc is None:
                self._json({"ok": False,
                            "err": "采集声音失败: " + _audio_hint(err)})
                return
            _audio_session["proc"] = proc
            _audio_session["stop"] = stop

        audit("audio", f"开始采集声音 {src} (pid={proc.pid})")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 写超时兜底: 客户端不读了(比如切后台)缓冲会满, 别让线程永久卡住
            try:
                self.connection.settimeout(15)
            except OSError:
                pass
            buf = first
            # 麦克风跟着"摄像头页还在不在"走 —— 但只在**这个流是从活着的摄像头页
            # 开起来的**时候才绑。否则像自检脚本、或者直接用 curl 拉流的场景,
            # 会被一个陈旧的全局时间戳误杀(踩过: cam_check 的音频用例就是这么挂的)。
            vt0 = _viewer_seen["t"]
            tied = bool(vt0) and (time.time() - vt0) <= CAM_VIEWER_TIMEOUT
            while not stop.is_set():
                # 主动探一下客户端还在不在。光靠"写失败"发现断开太慢 ——
                # 内核缓冲区没满时 write 照样成功, 麦克风会被白占好几秒。
                if _peer_gone(self.connection):
                    break
                # 半死连接上面那行探不出来: 手机锁屏/切网/webview 被挂起时写还写得
                # 进去, 对端早就不处理了。所以再挂一道"摄像头页还在不在"的判据 ——
                # 麦克风只在摄像头页里开, 那页的 JS 心跳停了就说明人走了。
                # (_viewer_seen 从没被写过 = 压根没人开过摄像头页, 不做这个判断)
                if tied and time.time() - _viewer_seen["t"] > CAM_VIEWER_TIMEOUT:
                    audit("audio", "摄像头页心跳停了 %.0fs, 判定没人听, 停止收音"
                                   % (time.time() - _viewer_seen["t"]))
                    break
                if buf:
                    if not self._write_chunk(buf):
                        break
                    buf = None
                    continue
                try:
                    buf = chunks.get(timeout=1.0)
                except queue.Empty:
                    continue
                if buf is None:
                    break
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stop.set()
            _cam_stop(proc)
            if use_null:
                silent_sink_release()      # 必须收工: 否则电脑一直没声音
            with _audio_lock:
                if _audio_session.get("proc") is proc:
                    _audio_session["proc"] = None
                    _audio_session["stop"] = None
            self.close_connection = True
        audit("audio", f"停止采集声音 {src} (pid={proc.pid})")

    def _html(self, code, html, extra=None):
        self._reply(code, html.encode(), "text/html; charset=utf-8", extra)

    def _json(self, obj, code=200, extra=None):
        self._reply(code, json.dumps(obj, ensure_ascii=False).encode(),
                    "application/json", extra)

    def _redirect(self, loc, extra=None):
        extra = dict(extra or {})
        self._reply(302, b"", extra={**extra, "Location": loc})

    def _static(self, relpath):
        path = os.path.realpath(os.path.join(STATIC_DIR, relpath))
        root = os.path.realpath(STATIC_DIR)
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            self._html(404, "<h1>404</h1>")
            return
        ext = os.path.splitext(path)[1]
        ctype = MIME.get(ext, "application/octet-stream")
        try:
            st = os.stat(path)
        except OSError:
            self._html(404, "<h1>404</h1>")
            return

        # ETag 用 修改时间+大小 拼, 不用读文件内容 —— 够用且零成本
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        # 第三方库(vendor)内容永不变 → 长期缓存, 重复进入**零请求**
        # 我们自己的文件必须能立刻生效 → no-cache + ETag(走 304, 只花一个往返)
        cache = ("public, max-age=31536000, immutable"
                 if (path.startswith(VENDOR_DIR) and path != BUILT_BUNDLE)
                 else "no-cache")

        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return

        with open(path, "rb") as f:
            body = f.read()
        extra = {"ETag": etag,
                 "Last-Modified": formatdate(st.st_mtime, usegmt=True)}

        compressible = ctype.startswith(_GZIP_OK)
        if compressible:
            # 告诉缓存/代理: 内容随 Accept-Encoding 变
            extra["Vary"] = "Accept-Encoding"
        if (compressible and len(body) > 512
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            key = (path, int(st.st_mtime), st.st_size)
            with _gzip_lock:
                gz = _gzip_cache.get(key)
            if gz is None:
                # 注意: 必须用 **gzip 格式**(zlib.compress 是 zlib 格式, 头字节
                # 是 0x78; 而 Content-Encoding: gzip 要求 0x1f 0x8b)。
                # 用错了浏览器解压失败, 脚本直接加载不出来(踩过一次)。
                gz = gzip.compress(body, 6)     # 6 是速度/压缩比的平衡点
                with _gzip_lock:
                    if len(_gzip_cache) > 600:  # 简单上限, 别无限涨
                        _gzip_cache.clear()
                    _gzip_cache[key] = gz
            if len(gz) < len(body):
                body = gz
                extra["Content-Encoding"] = "gzip"
        self._reply(200, body, ctype, extra, cache=cache)

    def _ws(self, protocols=None):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self._html(400, "need websocket upgrade")
            return None
        # CSWSH 防护(2026-09-16): 网页跨站发起的 WS 握手, 浏览器会强制带 Origin
        # (= 发起页的源)。它若和 Host 不同源, 就是恶意网页想借浏览器自动携带的
        # 登录 cookie 连我们的桥(/ws/vnc = 远程控制权, /ws/term = shell)—— 直接拒。
        # 非浏览器客户端(脚本/测试)不带 Origin, 放行: 它们不受网页 CSRF 影响。
        # (lan_forward 只改 Host 转发, 浏览器的 Origin 始终等于自己地址栏的源,
        #  经 Tailscale IP 访问时 Origin 与 Host 仍同源, 不受影响。)
        origin = self.headers.get("Origin", "")
        if origin:
            if urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
                audit("ws", f"拒绝跨源 WS 握手 Origin={origin!r} "
                            f"Host={self.headers.get('Host')!r} 路径={self.path}")
                self._html(403, "cross-origin websocket rejected")
                return None
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        lines = ["HTTP/1.1 101 Switching Protocols",
                 "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Accept: {accept}"]
        req = [p.strip() for p in
               self.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        for p in (protocols or []):
            if p in req:
                lines.append(f"Sec-WebSocket-Protocol: {p}")
                break
        try:
            self.connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        except OSError:
            return None
        self.close_connection = True
        ws = WSConn(self.connection)
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        return ws

    # ---- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            # boot: 本进程的启动标识, 前端靠它判断"服务是否真的换了进程"
            self._json({"ok": True, "app": "meow-console", "boot": BOOT_ID})
            return
        if path.startswith("/static/"):
            # 静态资源不含敏感信息, 放在鉴权前 —— 否则登录页 CSS 会被重定向
            self._static(path[len("/static/"):])
            return
        user = self._user()
        # 还没设过凭据 -> 一律先去"首次设置"页(连 /home 也进不去)
        if need_setup():
            if path not in ("/", "/login", "/setup", "/logout"):
                self._redirect("/login")
                return
        if path in ("/", "/login"):
            if user:
                self._redirect("/home")
            else:
                err = "用户名或密码错误" if "err=1" in self.path else ""
                self._login_page(err)
            return
        if path == "/logout":
            self._redirect("/login", extra={
                "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly"})
            return
        if not user:
            self._redirect("/login")
            return
        # 本地扩展的私有路由(比如 /dsh)先接; 没扩展时这里等于不存在。
        if LOCAL_EXTRAS and LOCAL_EXTRAS.handle_route(path, self):
            return
        if path == "/home":
            net = self.access_host()
            self._html(200, render("home.html", user=user, host=net,
                                   extras=(LOCAL_EXTRAS.quick_links(net)
                                           if LOCAL_EXTRAS else "")))
        elif path == "/term":
            self._html(200, render("term.html", user=user, host=COLLECTOR.hostname))
        elif path == "/vnc":
            self._html(200, render("vnc.html", user=user, host=COLLECTOR.hostname,
                                   preloads=vnc_module_preloads()))
        elif path == "/cam":
            # 模板引擎只做占位符替换(不支持 mustache 的 section), 数据自己拼好。
            # 只列**能采集**的节点: 内置摄像头常带一个纯 metadata 的兄弟节点, 选它必失败。
            devs = cam_devices() or [CAM_DEFAULT_DEV]
            # 默认用编号最小的那个 —— 笔记本自带摄像头通常就是它
            pairs = [[d, cam_label(d)] for d in devs]
            self._html(200, render(
                "cam.html", user=user, host=COLLECTOR.hostname,
                devs_json=json.dumps(pairs),
                # 只有一个摄像头就别显示切换按钮了, 没什么可切的
                sw_hidden="display:none" if len(devs) <= 1 else ""))
        elif path == "/theme":
            # 主题色: 圆盘取色页。颜色本身走 /api/theme, 这里只给页面和默认值。
            self._html(200, render("theme.html", user=user,
                                   host=COLLECTOR.hostname, default=THEME_DEFAULT))
        elif path == "/bg":
            # 自定义背景页(图片本体走 /bg/img)
            self._html(200, render("bg.html", user=user, host=COLLECTOR.hostname))
        elif path == "/log":
            self._html(200, render("log.html", user=user,
                                   host=COLLECTOR.hostname))
        elif path == "/api/dock":
            self._json(dock_state())
        elif path == "/api/config":
            self._json({"ok": True, "config": load_config()})
        elif path == "/api/volume":
            v = get_pc_volume()
            if v is None:
                self._json({"ok": False, "vol": None,
                            "err": "读不到电脑音量(没有 pulseaudio 或 pactl)"})
            else:
                self._json({"ok": True, **v})
        elif path == "/api/brightness":
            # 屏幕亮度: 值是滑块写的、存在 config.json(和音量一套做法)
            self._json({"ok": True, "pct": get_pc_brightness()})
        elif path == "/api/theme":
            self._json({"ok": True, "acc": get_theme_acc(),
                        "default": THEME_DEFAULT})
        elif path == "/api/audio/out":
            self._json({"ok": True, "mode": audio_out_mode()})
        elif path == "/api/bg":
            has, ver, trans, dim, blur = bg_state()
            try:
                n = os.path.getsize(BG_FILE) if has else 0
            except OSError:
                n = 0
            self._json({"ok": True, "has": has, "ver": ver, "bytes": n,
                        "trans": trans, "dim": dim, "blur": blur})
        elif path == "/bg/img":
            # 背景图本体(要登录才取得到; 没图就 404)
            try:
                with open(BG_FILE, "rb") as fh:
                    blob = fh.read()
            except OSError:
                self._html(404, "<h1>没有背景图</h1>")
            else:
                ct = ("image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n"
                      else "image/webp" if blob[8:12] == b"WEBP"
                      else "image/gif" if blob[:3] == b"GIF" else "image/jpeg")
                self._reply(200, blob, ct, {"Cache-Control": "no-cache"})
        elif path == "/api/screen":
            w, h = screen_size()
            self._json({"ok": bool(w), "w": w, "h": h})
        elif path == "/api/clipboard":
            t = clip_get()
            self._json({"ok": t is not None, "text": t or "",
                        "err": "" if t is not None else "读取失败(没有 xclip 或选区无主)"})
        elif path == "/ws/status":
            ws = self._ws()
            if ws:
                ws_status_push(ws)
        elif path == "/ws/term":
            ws = self._ws()
            if ws:
                audit("term", "终端会话打开 from " + _who(self))
                ws_term_bridge(ws)
        elif path == "/ws/vnc":
            ws = self._ws(protocols=["binary"])
            if ws:
                audit("vnc", "远程桌面连接 from " + _who(self))
                ws_vnc_bridge(ws)
        elif path == "/api/vnc/clients":
            # 诊断用: x11vnc 上当前几个客户端(正常 1)。数字偏大 = 有遗留连接。
            self._json({"ok": True, "clients": vnc_client_count(),
                        "bridges": len(_vnc_bridges)})
        elif path == "/api/log":
            # 审计日志尾部。默认 400 行, 最多 2000 —— 手机上一屏也就几十行。
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                n = min(2000, max(50, int((q.get("n") or ["400"])[0])))
            except ValueError:
                n = 400
            rows = read_audit(n)
            counts = collections.Counter(r["c"] for r in rows)
            self._json({"ok": True, "rows": rows,
                        "counts": dict(counts.most_common()),
                        "cats": AUDIT_CATS})
        elif path == "/api/audio/mics":
            # 给前端用: 有哪些麦克风、哪些真的能用(实测)。
            # 走缓存版 —— 每次真探要 20 秒上下, 而前端一进摄像头页就会调它。
            mics = audio_list_mics_cached()
            usable = next((m for m in mics if m["usable"]), None)
            self._json({"ok": True, "mics": mics, "usable": usable})
        elif path == "/api/audio/check":
            # 诊断用: 逐个候选试采, 返回每个的电平, 一眼看出哪个源真有声音
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            mode = (q.get("mode") or ["auto"])[0]
            kind, src, tried = audio_pick_source(mode)
            self._json({"ok": True, "mode": mode,
                        "picked": {"kind": kind, "src": src},
                        "default_sink": _default_sink_name(),
                        "sources": audio_sources(), "tried": tried})
        elif path == "/api/audio":
            # 声音**默认不采**: 只有用户主动点了「开启声音」才会来请求这里。
            # 断开(关页面/点停止)时 ffmpeg 立刻收工。
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._serve_audio(src=(q.get("src") or ["default"])[0])
        elif path == "/ws/cam":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            g = lambda k: (q.get(k) or [None])[0]
            ws = self._ws(protocols=["binary"])
            if ws:
                ws_cam_bridge(ws, dev=g("dev"), size=g("size"), fps=g("fps"))
        else:
            self._html(404, "<h1>404</h1>")

    # ---- POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # 表单类接口(/login /setup)共用一个 body 读取, 别在各分支里各读一遍
        body = ""
        if path in ("/login", "/setup"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode()
            except (OSError, ValueError, UnicodeDecodeError):
                body = ""
        if path == "/login":
            ip = self.client_address[0]
            form = urllib.parse.parse_qs(body)
            user = form.get("user", [""])[0]
            pw = form.get("pass", [""])[0]
            if need_setup():
                # 还没设凭据时不接受登录(也不该有人在登录), 直接回设置页
                self._login_page("请先设置账号和密码")
                return
            if rate_limited(ip):
                self._login_page("尝试过多, 请 5 分钟后再试")
                return
            ok = check_login(user, pw)
            record_login(ip, ok)
            if ok:
                audit("login", "登录成功 from " + _who(self))
                self._redirect("/home", extra={
                    "Set-Cookie": (f"{COOKIE_NAME}={make_token()}; Path=/; "
                                   f"HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")})
            else:
                audit("login", "登录失败 user=%r from %s" % (user, ip))
                time.sleep(0.6)
                self._login_page("用户名或密码错误")
            return
        if path == "/setup":
            # 首次设置: 只有**还没有凭据**时才允许, 设完这个入口立即失效 ——
            # 否则任何人都能重设密码, 等于没有鉴权。
            if not need_setup():
                self._html(403, "已经设置过账号了, 不能重设")
                return
            form = urllib.parse.parse_qs(body)
            user2 = (form.get("user") or [""])[0].strip()
            pw = (form.get("pass") or [""])[0]
            pw2 = (form.get("pw2") or [""])[0]
            if len(user2) < 1:
                self._login_page("用户名不能为空")
                return
            if len(pw) < 6:
                self._login_page("密码至少 6 位")
                return
            if pw != pw2:
                self._login_page("两次输入的密码不一致")
                return
            set_credentials(user2, pw)
            audit("init", f"首次设置完成 user={user2}")
            self._redirect("/home", extra={
                "Set-Cookie": (f"{COOKIE_NAME}={make_token()}; Path=/; "
                               f"HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")})
            return
        if not self._user():
            self._json({"ok": False, "err": "未登录"}, code=401)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode() or "{}")
        except (OSError, ValueError, UnicodeDecodeError):
            data = {}
        if path == "/api/power":
            action = data.get("action", "")
            if not data.get("confirm") and action in ("suspend", "reboot", "poweroff"):
                self._json({"ok": False, "err": "缺少确认"})
                return
            ok, msg = power_action(action)
            audit("power", f"{action} {'OK' if ok else 'FAIL'}")
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/dock":
            # pressure 变可选: 只想调延迟时不用连带改它
            changed, want_pressure = [], data.get("pressure")
            if want_pressure is not None:
                if not isinstance(want_pressure, bool):
                    self._json({"ok": False, "err": "参数错误"})
                    return
                if not _gs_set("require-pressure-to-show",
                               "true" if want_pressure else "false"):
                    self._json({"ok": False, "err": "gsettings 写入失败"})
                    return
                changed.append(f"pressure={want_pressure}")
            for in_k, key in (("showDelay", "show-delay"), ("hideDelay", "hide-delay")):
                v = data.get(in_k)
                if v is None:
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    self._json({"ok": False, "err": f"{in_k} 必须是数字"})
                    return
                if not 0.0 <= f <= 2.0:
                    self._json({"ok": False, "err": f"{in_k} 应在 0~2 秒"})
                    return
                if not _gs_set(key, repr(f)):
                    self._json({"ok": False, "err": "gsettings 写入失败"})
                    return
                changed.append(f"{key}={f}")
            if not changed:
                self._json({"ok": False, "err": "没有要改的项"})
                return
            # 写完立刻读回: gnome-shell 只认 dconf 的通知, 未生效要能暴露
            st = dock_state()
            audit("dock", "设置 " + " ".join(changed) + f" -> 读回 {st}")
            ok = bool(st.get("ok"))
            if isinstance(want_pressure, bool):
                ok = ok and st.get("pressure") == want_pressure
            if data.get("showDelay") is not None and st.get("show_delay") is not None:
                ok = ok and abs(st["show_delay"] - float(data["showDelay"])) < 0.011
            self._json({"ok": ok, "state": st})
        elif path == "/api/edge":
            edge = data.get("edge")
            if edge not in ("top", "bottom", "left", "right"):
                self._json({"ok": False, "err": "参数错误"})
                return
            try:
                fbw = int(data.get("fbw") or 0)
                fbh = int(data.get("fbh") or 0)
                fx = int(data.get("fx") or 0)
                fy = int(data.get("fy") or 0)
            except (TypeError, ValueError):
                self._json({"ok": False, "err": "参数错误"})
                return
            ok, msg = edge_nudge(edge, fx, fy, fbw, fbh)
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/config":
            # 'term' 只对终端有意义, 不该当全局设置:
            # 设成全局后浏览器/编辑器也会被拿去按 Ctrl+Shift+V, 而这些应用不认这个键。
            # 所以选「终端」时只记到当前窗口上, 全局退回 auto。
            tm = data.get("typeMode")
            if isinstance(tm, str) and tm == "term" and "typeMode" in data:
                data = dict(data, typeMode="auto")
            ok, msg, cfg = save_config(data)
            if ok and isinstance(tm, str):
                # 用户是在"当前聚焦的窗口"下做的选择, 记到这个窗口类名上,
                # 以后切回同类窗口自动沿用(VSCode 这类尤其需要)
                _remember_mode_for_class(tm)
            audit("cfg", ("保存配置 " if ok else "保存失败 ") +
                  json.dumps({k: data.get(k) for k in DEFAULT_CONFIG if k in data},
                             ensure_ascii=False) + (" " + msg if not ok else ""))
            self._json({"ok": ok, "msg": msg, "config": cfg})
        elif path == "/api/volume":
            # 电脑主音量: 滑块设的, 同时写进 config.json 持久化(像别的设置一样)。
            raw = data.get("vol")
            try:
                pct = int(raw)
            except (TypeError, ValueError):
                self._json({"ok": False, "err": "vol 必须是 0~150 的整数"})
                return
            old = (get_pc_volume() or {}).get("vol")
            ok, msg = set_pc_volume(pct)
            if ok:
                save_config({"volume": pct})
                v = get_pc_volume() or {"vol": pct, "muted": False, "sink": ""}
                audit("vol", "电脑音量 %s%% → %d%% (输出 %s)"
                      % (old if old is not None else "?", pct, v.get("sink") or "?"))
                self._json({"ok": True, **v})
            else:
                self._json({"ok": False, "err": msg})
        elif path == "/api/brightness":
            # 屏幕亮度: 和音量同一套 —— 滑块设, 写进 config.json 持久化。
            # 用的是 xrandr 软件调光, 不需要 sudo/udev 规则。
            raw = data.get("pct")
            try:
                pct = int(raw)
            except (TypeError, ValueError):
                self._json({"ok": False, "err": "pct 必须是 5~100 的整数"})
                return
            old = get_pc_brightness()
            ok, msg = set_pc_brightness(pct)
            if ok:
                audit("bri", "屏幕亮度 %d%% → %d%% (输出 %s)"
                      % (old, pct, ",".join(_xrandr_outputs()) or "?"))
                self._json({"ok": True, "pct": get_pc_brightness()})
            else:
                self._json({"ok": False, "err": msg})
        elif path == "/api/theme":
            # 主题色: 太暗的会被 set_theme_acc 拒掉(暗底上没法当文字色用)
            old = get_theme_acc()
            ok, msg = set_theme_acc(data.get("acc"))
            if ok:
                audit("theme", "主题色 %s → %s" % (old, get_theme_acc()))
                self._json({"ok": True, "acc": get_theme_acc()})
            else:
                self._json({"ok": False, "err": msg})
        elif path == "/api/audio/out":
            mode = (data.get("mode") or "").strip()
            if mode not in ("pc", "silent"):
                self._json({"ok": False, "err": "mode 只能是 pc 或 silent"})
                return
            save_config({"audioOut": mode})
            audit("audio", "声音去向 -> " + ("只发手机(电脑静音)" if mode == "silent" else "电脑也响"))
            self._json({"ok": True, "mode": mode})
        elif path == "/api/bg":
            # 两种 body: 图片二进制(Content-Type: image/*) / JSON(参数或清除)
            # 两种 body, 都是 JSON: 图片(base64, 键 img) / 参数或清除。
            # 不用二进制 body: do_POST 前面已经按 JSON 解过一遍 body 了, 分支里再
            # read(Content-Length) 会一直等下去(实测卡死过一次)。
            b64 = data.get("img")
            if b64:
                import base64 as _b64
                try:
                    blob = _b64.b64decode(b64, validate=False)
                except Exception:
                    self._json({"ok": False, "err": "图片解码失败"})
                    return
                if not blob or len(blob) > BG_MAX:
                    self._json({"ok": False, "err": "图片大小不对(最多 8 MB)"})
                    return
                try:
                    with open(BG_FILE, "wb") as fh:
                        fh.write(blob)
                except OSError as e:
                    self._json({"ok": False, "err": "存不下: %s" % e})
                    return
                audit("bg", "背景图已更新(%d KB)" % (len(blob) // 1024))
                self._json({"ok": True, "ver": bg_state()[1]})
                return
            if data.get("clear"):
                try:
                    os.remove(BG_FILE)
                except OSError:
                    pass
                if data.get("reset"):
                    save_config({"bgTrans": 35, "bgDim": 45, "bgBlur": 0})
                audit("bg", "背景图已清除" + ("并恢复默认参数" if data.get("reset") else ""))
                self._json({"ok": True})
                return
            params = data.get("params") or {}
            patch = {}
            # 前端发的是短键(trans/dim/blur), 存的是长键(bgTrans/...), 两个都认
            for sk, k, lo, hi in (("trans", "bgTrans", 0, 90),
                                  ("dim", "bgDim", 0, 80),
                                  ("blur", "bgBlur", 0, 20)):
                v = params.get(k, params.get(sk))
                if v is not None:
                    try:
                        patch[k] = max(lo, min(hi, int(v)))
                    except (TypeError, ValueError):
                        pass
            if patch:
                save_config(patch)
            audit("bg", "背景参数 " + (", ".join("%s=%s" % kv for kv in patch.items())
                                      or "无变化"))
            self._json({"ok": True})
        elif path == "/api/password":
            # 设置面板改密码: 必须输对原密码。用户名不可改(单机单用户)。
            old = data.get("old")
            new = data.get("new")
            if not isinstance(old, str) or not isinstance(new, str):
                self._json({"ok": False, "err": "参数错误"})
                return
            ok, msg = change_password(old, new)
            audit("pw", "修改密码 " + ("OK" if ok else f"拒绝({msg})"))
            self._json({"ok": ok, "msg": msg if ok else "", "err": "" if ok else msg})
        elif path == "/api/clipboard":
            text = data.get("text")
            if not isinstance(text, str):
                self._json({"ok": False, "err": "参数错误"})
                return
            ok, msg = clip_set(text)
            audit("clip", f"写入远程剪贴板 {len(text)} 字 {'OK' if ok else 'FAIL'}")
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/type":
            text = data.get("text")
            if not isinstance(text, str) or not text:
                self._json({"ok": False, "err": "参数错误"})
                return
            # 上限必须在**选投递方式之前**拦。type_text 里那道闸门只能管到逐字键入,
            # 而 deliver_text 会先试粘贴 —— 粘贴没有那道闸, 超长文本会整段糊进焦点
            # 窗口(而且会顶掉用户的剪贴板)。
            if len(text) > TYPE_MAX_LEN:
                audit("clip", f"拒绝超长键入 {len(text)} 字(上限 {TYPE_MAX_LEN})")
                self._json({"ok": False,
                            "err": f"单次上限 {TYPE_MAX_LEN} 字(收到 {len(text)}), "
                                   f"请改用剪贴板粘贴"})
                return
            mode = data.get("mode")
            ok, msg, how = deliver_text(text, mode if isinstance(mode, str) else None)
            # 只记首字的码点, 不记整句(可能在输密码)。用来判断"到服务端时字对不对":
            # 手机上打的是"你好", 首字应是 U+4F60; 若对不上就是前端/手机输入法那头错了。
            head = f" 首字U+{ord(text[0]):04X}" if text else ""
            audit("clip", f"{how} {len(text)} 字 [{text_shape(text)}]{head} "
                          f"{'OK' if ok else 'FAIL: ' + str(msg)[:60]}")
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/paste":
            # 关键: 设置剪贴板和发 Ctrl+V 必须在服务端一次做完。
            # 旧实现是前端先 rfb.clipboardPasteFrom() 再立刻 fetch /api/paste,
            # 两条路完全异步 —— Ctrl+V 常常在选区还没换过来时就按下去了,
            # 粘出来的是上一次的内容。
            text = data.get("text")
            if isinstance(text, str) and text:
                ok, msg = clip_set(text)
                if not ok:
                    # 设不上选区(远端有程序占着/没有 xclip)就只能退化成逐字键入。
                    # 但键入是每字 ~4ms, 8000 字要敲 40 秒 —— 期间界面一直停在
                    # "粘贴中…", 看着就跟卡死一样(实测到的"粘贴卡死"就是这个)。
                    # 长文本直接报错, 把选择权还给用户。
                    if len(text) > 300:
                        audit("clip", f"粘贴失败(剪贴板不可用且过长) {len(text)} 字")
                        self._json({"ok": False,
                                    "msg": f"剪贴板不可用({msg}); 内容 {len(text)} 字太长, "
                                           f"请改用『直接键入』或先写入剪贴板"})
                        return
                    ok2, msg2 = type_text(text)
                    audit("clip", f"粘贴退化直接键入 {len(text)} 字 {'OK' if ok2 else 'FAIL'}")
                    self._json({"ok": ok2, "msg": "剪贴板不可用, 已直接键入" if ok2
                                else f"失败: {msg} / {msg2}"})
                    return
            ok, msg = send_ctrl_v()
            audit("clip", f"粘贴 {len(text) if isinstance(text, str) else 0} 字")
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/vnc/reset":
            # 一键清理并重启: 先回响应, 再重启服务(见 _restart_soon)。
            # 响应里带上**当前**的 boot 标识 —— 前端拿它跟 /api/health 比对,
            # 变了才说明真的换进程了, 这时才刷新页面。
            n = vnc_client_count()
            audit("vnc", f"一键清理并重启(重启前 {n} 个客户端)")
            self._json({"ok": True, "clients": n, "boot": BOOT_ID,
                        "msg": "正在重启控制台, 稍候自动恢复…"})
            _restart_soon()
        elif path == "/api/kill":
            pid = data.get("pid")
            try:
                pid = int(pid)
                if pid in (1, os.getpid()):
                    raise ValueError
                os.kill(pid, signal.SIGTERM)
                audit("kill", f"SIGTERM -> {pid}")
                self._json({"ok": True, "msg": f"已向 {pid} 发送 SIGTERM"})
            except (ValueError, ProcessLookupError, PermissionError, OSError) as e:
                self._json({"ok": False, "err": str(e)})
        else:
            self._json({"ok": False, "err": "未知接口"}, code=404)


def _apply_cli():
    """命令行覆盖: --port N / --vnc-port N / --bind 地址(优先级最高)。"""
    global PORT, VNC_PORT, BIND
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if "=" in a:
            key, val = a.split("=", 1)
        else:
            key, val = a, None
        if key in ("--port", "--vnc-port", "--bind"):
            if val is None:
                i += 1
                val = args[i] if i < len(args) else ""
            if key == "--port":
                PORT = _resolve_port(val, "", "port", PORT)
            elif key == "--vnc-port":
                VNC_PORT = _resolve_port(val, "", "vncPort", VNC_PORT)
            elif val.strip():
                BIND = val.strip()
        i += 1


def main():
    _apply_cli()
    srv = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    # 启动标记: 审计日志里能看到"服务启动 vN", 用来确认改完的代码到底加载了没有
    # —— 改了 server.py 但没重启服务是排查时最常踩的坑。
    audit("boot", f"服务启动 pid={os.getpid()} 输入串行化=on 输入法门禁="
                  f"{'on' if shutil.which('fcitx-remote') else 'off(未装 fcitx-remote)'}")
    print(f"[meow-console] listening on {BIND}:{PORT}  (经 lan_forward 对外暴露)", flush=True)
    # 后台预热麦克风探测: 第一次探要真开设备试采(约 3 秒), 不等用户进摄像头页才做。
    # 失败无所谓(没麦克风本来就会返回空列表), 所以吞掉异常。
    def _warm():
        try:
            audio_list_mics_cached()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
    # 后台巡检 x11vnc 的遗留客户端(手机锁屏/切网留下的半死连接会拖慢所有人)。
    # **只在正式实例里跑**: 巡检读的是全局的 `ss -tn`(看到的是真实 x11vnc 的客户端),
    # 而隔离测试实例只登记自己的桥接 —— 两边一对比必然"多出来", 会误判成遗留连接
    # 并把真实的 x11vnc 重启掉。测试实例都改过 PORT, 用它区分最省事。
    if PORT == DEFAULT_PORT:
        threading.Thread(target=_vnc_janitor, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
