#!/usr/bin/env bash
# MEOW Console —— 手机远程控制 Ubuntu 桌面的安装脚本(幂等, 可反复执行)
#
#   桌面+声音: x11vnc + noVNC 桥接(浏览器直连, 无需装客户端)
#   状态面板 : CPU/内存/温度/网络/磁盘/电池/进程/窗口/Tailscale 设备
#   终端     : 网页里开 bash, 支持中文输入
#   其它     : 剪贴板互通、摄像头、电源控制、电脑音量、审计日志
#
# 只做这一件事: 不装/不管别的服务。想让手机在外网访问就靠 lan_forward.py
# (默认只转发控制台这一个端口)。
#
# 用法:
#   ./install.sh                     # 默认端口 8390
#   ./install.sh --port 9000         # 换个端口
#   MEOW_PORT=9000 ./install.sh      # 等价写法
set -euo pipefail

info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 0. 参数 ----------
# 端口不写死: --port > 环境变量 MEOW_PORT > 8390。
# (服务端自己还支持 data/config.json 里的 port, 但那是装完之后改用的。)
PORT="${MEOW_PORT:-8390}"
VNC_PORT="${MEOW_VNC_PORT:-5900}"
while [ $# -gt 0 ]; do
  case "$1" in
    --port)     PORT="${2:-}"; shift 2 ;;
    --port=*)   PORT="${1#*=}"; shift ;;
    --vnc-port) VNC_PORT="${2:-}"; shift 2 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) warn "忽略未知参数: $1"; shift ;;
  esac
done
case "$PORT" in ''|*[!0-9]*) die "端口必须是数字: '$PORT'" ;; esac
[ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "端口超出范围(1024~65535): $PORT"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE_DIR="$REPO_DIR/console"
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(eval echo ~"$REAL_USER")"          # 不要用 /root: sudo 下 $HOME 会变
USER_UNIT_DIR="$REAL_HOME/.config/systemd/user"
DATA_DIR="$CONSOLE_DIR/data"

[ -f "$CONSOLE_DIR/server.py" ] || die "找不到 console/server.py, 请在项目根目录执行"

# ---------- 1. 依赖 ----------
info "检查依赖..."
MISSING=()
for c in x11vnc xdotool xclip xprop python3 systemctl; do
  command -v "$c" >/dev/null 2>&1 || MISSING+=("$c")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  info "缺少: ${MISSING[*]} —— 尝试 apt 安装(需要 sudo)"
  PKGS=()
  for m in "${MISSING[@]}"; do
    case "$m" in
      x11vnc)   PKGS+=(x11vnc) ;;
      xdotool)  PKGS+=(xdotool) ;;
      xclip)    PKGS+=(xclip) ;;
      xprop)    PKGS+=(x11-utils) ;;
      python3)  PKGS+=(python3) ;;
      systemctl) die "systemctl 都没有? 这不是 systemd 系统, 装不了" ;;
    esac
  done
  if [ ${#PKGS[@]} -gt 0 ]; then
    sudo apt-get update -qq
    sudo apt-get install -y "${PKGS[@]}"
  fi
else
  info "  依赖已齐全 ✓"
fi

# ---------- 2. 目录 ----------
# 运行时产物(凭据/审计/配置)统一放 console/data/, 备份和清空都只针对这一个目录
mkdir -p "$USER_UNIT_DIR" "$DATA_DIR" "$CONSOLE_DIR/tests/artifacts"

# ---------- 3. 写 systemd 单元 ----------
info "写入 systemd 用户单元..."

write_unit() {                       # write_unit <文件名> <内容>
  local f="$USER_UNIT_DIR/$1" content="$2"
  if [ -f "$f" ] && ! cmp -s <(printf '%s\n' "$content") "$f"; then
    local bak="$f.bak.$(date +%Y%m%d%H%M%S)"
    cp "$f" "$bak"
    warn "已存在的 $1 备份为 $(basename "$bak")"
  fi
  printf '%s\n' "$content" > "$f"
}

# 非默认端口时把 --port 写进 ExecStart, 免得还要额外维护一份环境变量
PORT_ARGS=""
[ "$PORT" != "8390" ] && PORT_ARGS=" --port $PORT"
VNC_ARGS=""
[ "$VNC_PORT" != "5900" ] && VNC_ARGS=" --vnc-port $VNC_PORT"

write_unit "meow-console.service" "[Unit]
Description=MEOW Console - 状态面板 / 终端 / 远程桌面网关
After=default.target

[Service]
ExecStart=/usr/bin/python3 $CONSOLE_DIR/server.py$PORT_ARGS$VNC_ARGS
WorkingDirectory=$REAL_HOME
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"

write_unit "meow-vnc.service" "[Unit]
Description=MEOW VNC - x11vnc 仅回环, 由 noVNC 桥接
After=default.target

[Service]
# -noxdamage 必须保留: 去掉它改 XDamage 后, 密集输入会触发全屏读取 + 缩放,
# x11vnc 扛不住 -> 客户端每秒重连、桌面卡死(实测过)。-noxdamage + -wait/-defer
# 是自限速的, 抗输入洪流。这里要的是稳定, 不是省 CPU。
ExecStart=/usr/bin/x11vnc -display :0 -auth guess -forever -shared \\
  -localhost -nopw -xkb -noxdamage -repeat -rfbport $VNC_PORT \\
  -wait 5 -defer 5 -scale 1/2 -quiet
Restart=on-failure
RestartSec=3
# 防紧密崩溃循环打满 CPU: 60 秒内最多重启 6 次, 超了先躺 60 秒再试
StartLimitIntervalSec=60
StartLimitBurst=6

[Install]
WantedBy=default.target
"

# 转发器: 控制台只监听回环, 手机要经 局域网IP / Tailscale 进来就得靠它镜像。
# 默认只转发控制台端口; 还有别的想一起暴露(比如自己的文件管理服务), 在仓库根
# 放一个 ports.local.txt 写上行号即可(该文件不进仓库)。
write_unit "meow-forward.service" "[Unit]
Description=MEOW forward - 把各网卡 IP 的控制台端口镜像到回环
After=default.target

[Service]
ExecStart=/usr/bin/python3 $REPO_DIR/lan_forward.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"

# 旧单元叫 dsh-lan-forward.service(名字里带别的项目)。改名后把旧的停掉删掉,
# 否则会和新转发器抢同一个端口 —— 两个都起来反而谁都不通。
if [ -f "$USER_UNIT_DIR/dsh-lan-forward.service" ]; then
  warn "发现旧转发器单元 dsh-lan-forward.service, 正在停掉并移除(新名字: meow-forward)"
  systemctl --user disable --now dsh-lan-forward.service >/dev/null 2>&1 || true
  rm -f "$USER_UNIT_DIR/dsh-lan-forward.service"
fi

# ---------- 4. linger(开机即连的关键) ----------
info "配置 linger(让服务在开机/未登录桌面时也能跑)..."
if command -v loginctl >/dev/null 2>&1; then
  if loginctl show-user "$REAL_USER" 2>/dev/null | grep -q 'Linger=yes'; then
    info "  linger 已开启 ✓"
  else
    sudo loginctl enable-linger "$REAL_USER" && info "  linger 已开启 ✓" \
      || warn "开 linger 失败(不影响登录后使用, 只是开机未登录时连不上)"
  fi
fi

# ---------- 5. 启动 ----------
info "重载 systemd 并启动服务..."
systemctl --user daemon-reload || warn "daemon-reload 失败(可能不在 systemd 用户会话里, 手动执行即可)"

SERVICES=(meow-vnc meow-console meow-forward)

# 记录启动是否真的成功 —— 自检要据此判断"能连上"到底是不是**我们装的**那个实例。
# 否则在 systemd 不可用的环境里(容器/CI/受限 shell), 端口可能本来就有别的服务在听,
# 自检会连上它并报告"核心功能就绪 ✓" —— 纯假绿。
STARTED=0
for s in "${SERVICES[@]}"; do
  systemctl --user enable "$s.service" >/dev/null 2>&1 || warn "enable $s 失败"
  if systemctl --user restart "$s.service" 2>/dev/null; then
    STARTED=$((STARTED + 1))
  else
    warn "启动 $s 失败"
  fi
done

sleep 2

# ---------- 6. 自检 ----------
info "自检..."
STARTED="$STARTED" PORT="$PORT" VNC_PORT="$VNC_PORT" python3 - <<'PY'
import http.client, os, socket, sys, time

# 服务到底有没有被 systemd 拉起来? 没起来的话下面"能连上"很可能是别人的端口,
# 不能算我们装好了。
started = int(os.environ.get('STARTED') or 0)
port = int(os.environ.get('PORT') or 8390)
vnc_port = int(os.environ.get('VNC_PORT') or 5900)

# systemd 对 Type=simple 的服务一 exec 就返回"启动完成", 这时 Python 还没 bind。
# 不等的话新机器第一次装完自检必然 ConnectionRefused(假失败)。
def wait(p, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket(); s.settimeout(1)
        try:
            s.connect(('127.0.0.1', p)); return True
        except OSError:
            time.sleep(0.4)
        finally:
            s.close()
    return False

ok = True
try:
    if not wait(port):
        raise OSError('30s 内端口没起来')
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.request('GET', '/api/health'); r = c.getresponse(); r.read()
    print(f"  控制台 {port}  HTTP {r.status} {'✓' if r.status == 200 else '✗'}")
    ok &= (r.status == 200)
except Exception as e:
    print(f"  控制台 {port}  ✗ {type(e).__name__}"); ok = False
for p, n in ((vnc_port, 'x11vnc'), (port, '控制台')):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect(('127.0.0.1', p)); print(f"  {n:10s} {p}  ✓ 在听")
    except Exception as e:
        print(f"  {n:10s} {p}  ✗ {type(e).__name__}"); ok = False
    finally:
        s.close()
if started == 0:
    # 一个服务都没起来 —— 下面端口"通"也不作数, 明确告诉用户别信这个自检。
    print("\n结果: ⚠ 服务未被 systemd 启动(本环境可能不支持 systemctl --user)。")
    print("      上面端口即使显示'在听', 也可能是别的服务占着, 不能算安装成功。")
    print("      请手动验证, 例如在真实桌面会话里执行:")
    print("        systemctl --user daemon-reload")
    print("        systemctl --user restart meow-console meow-vnc meow-forward")
    print("        python3 <项目>/console/tests/full_check.py")
    sys.exit(0)
print("\n结果:", "核心功能就绪 ✓" if ok else "有问题, 见上面 ✗")
PY

# ---------- 7. 收尾提示 ----------
cat <<EOF

$(printf '\033[36m==>\033[0m') 完成

  本机访问   : http://127.0.0.1:$PORT
  手机访问   : http://<本机IP>:$PORT   (Tailscale 用 100.x 那个地址)

  首次打开上面这个地址会让你**自己设置账号和密码**(存 console/data/auth.json,
  只存加盐哈希, 不存明文)。设置完就能用, 以后登录用这套凭据。
EOF
if [ -f "$DATA_DIR/auth.json" ]; then
  echo "  检测到已有 auth.json —— 沿用原来的账号密码, 不会被覆盖。"
fi
cat <<'EOF'

  常用命令:
    systemctl --user status meow-console meow-vnc meow-forward
    journalctl --user -u meow-console -f

  没起来先查:
    1) 有没有登录桌面(x11vnc 需要 X 会话: echo $DISPLAY)
    2) journalctl --user -u meow-vnc 看 x11vnc 报错
EOF
