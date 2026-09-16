# MEOW Remote —— 手机远程控制 Linux 桌面

用手机（或另一台电脑）随时随地访问家里的 Ubuntu 桌面：**实时图传 + 完整操控 + 系统状态面板 + Web 终端**。
走 Tailscale 组网，跨网络、开流量也能连。

- **远程桌面**：noVNC 实图传真实 X11 桌面，可鼠标键盘操控；仿笔记本触控板（轻点左键 / 双指右键 / 双击拖拽 / 双指滚轮带惯性），全屏可双指缩放，右上角可开关**半透明滚轮按钮**（全屏翻文档用，只滚轮、不响应其它触控）
- **状态面板**：CPU（含每核）/ 内存 / 温度 / 电池功率 / 网速 / 磁盘 / 核显 / 进程 TOP / 桌面窗口 / Tailscale 设备，每秒推送
- **Web 终端**：xterm.js + pty bash，手机快捷键条（Esc / Tab / Ctrl-C / 方向键…），中文输入可用
- **其它**：剪贴板双向互通、电脑音量滑块、远程桌面一键截图存手机、摄像头、电源控制、审计日志

服务端是**纯 Python 标准库**实现（自实现 WebSocket，零第三方依赖），前端资源已本地化，断网也能用。

设计取向：**控制台只监听 `127.0.0.1`，对外一律经 `lan_forward.py` 把各网卡 IP 的端口镜像进来**（Tailscale 100.x 也走这条路）。VNC 端口永不直接对外。

---

## 快速开始（新机器）

```bash
git clone <你的仓库> ~/remote && cd ~/remote
./install.sh                    # 默认端口 8390
./install.sh --port 9000        # 想换端口
```

装完访问 `http://<本机IP>:8390`，**第一次打开会让你自己设置账号和密码**（存 `console/data/auth.json`，只存加盐哈希，不存明文）。设完直接用这套凭据登录。

想无人值守部署（不进设置页）：

```bash
MEOW_USER=你的账号 MEOW_PASSWORD=你的密码 ./install.sh
```

脚本流程：装依赖（x11vnc / xdotool / xclip / x11-utils）→ 写 systemd 用户单元 → 开 `loginctl enable-linger`（开机未登录桌面也能连）→ 启动服务 → 自检。已有单元文件会先备份再覆盖，**重复执行是安全的**。

### 前置条件

- Ubuntu 22.04（其它 systemd + X11 发行版理论上可以，未实测）
- **X11 图形会话**（Wayland 下 x11vnc 抓不到画面）
- Tailscale 已装并登录（跨网络用；同一局域网不需要）

---

## 架构

```
手机浏览器 ──▶ http://<网卡IP>:8390
                      │
              lan_forward.py（网卡 IP → 回环 镜像）
                      │
                      ▼
              127.0.0.1:8390   meow-console
                      │
    ┌─────────────────┼─────────────────┐
    ▼                 ▼                 ▼
/ws/status        /ws/term          /ws/vnc ──▶ 127.0.0.1:5900
状态面板(1s)      pty bash          远程桌面      x11vnc（仅回环，永不对外）
```

### 端口

| 端口 | 服务 | 说明 |
|---|---|---|
| 8390 | meow-console | 控制台，默认对外 |
| 5900 | x11vnc (meow-vnc) | **仅回环**，永不对外 |

两个端口都不写死，优先级：**命令行 `--port` > 环境变量 `MEOW_PORT` > `console/data/config.json` 的 `port`**（VNC 端口同理，用 `--vnc-port` / `MEOW_VNC_PORT` / `vncPort`）。

对外还想暴露别的端口？在仓库根放一个 `ports.local.txt`（一行一个端口，不进仓库）。

### 目录

```
.
├── install.sh            # 一键安装（幂等）
├── lan_forward.py        # 网卡 IP → 回环 的端口镜像
├── console/
│   ├── server.py         # 服务端（纯标准库）
│   ├── static/           # 前端；vendor/ 已本地化 → 断网可用
│   ├── templates/
│   ├── data/             # ★ 运行时产物全在这里（不进仓库）
│   │   ├── auth.json     # 凭据：随机盐 + sha256(盐+密码)
│   │   ├── .secret       # 会话签名密钥
│   │   ├── config.json   # 全部配置
│   │   └── audit.log     # 审计日志
│   └── tests/            # 自检脚本
└── docs/
    ├── 排查记录.md        # 踩坑实录（改代码前值得一看）
    └── 组件细节.md        # 各部件职责 / 端口 / 运行时文件
```

**备份** = 拷走 `console/data/`；**重置** = 删掉它（下次启动会重新引导设置账号密码）。

---

## 配置

全部配置在 `console/data/config.json`，改完重启服务即生效（也可以手改文件，服务端按 mtime 自动重读）：

| 键 | 默认 | 说明 |
|---|---|---|
| `port` | 8390 | 控制台端口 |
| `vncPort` | 5900 | x11vnc 的内部 RFB 端口 |
| `typeMode` | auto | 文本投递方式：auto / type / paste / term |
| `typeBatch` | 80 | 输入攒批间隔 |
| `typeDelayMs` | 20 | 逐字键入间隔（中文需要足够间隔，见代码注释） |
| `volume` | 100 | 电脑主音量（0~150，设置面板滑块会写它） |
| `modeByClass` | {} | 按窗口类名记住用户选过的投递方式 |

---

## 安全

- **首次使用由使用者自己设置账号密码**，不生成随机密码、不把密码明文写进任何文件。
- 服务只监听回环，外网只经 `lan_forward` 转发；**VNC 端口不对外**。
- WebSocket 握手校验 `Origin`：跨站网页想借浏览器 cookie 连你的远程桌面/终端（CSWSH）会被 403 拒绝。
- 未登录：页面 302 到登录页，接口 401/302；登录失败 8 次锁 5 分钟。
- 会话是 HMAC 签名的随机令牌，90 天滑动过期；危险操作（重启/关机/睡眠）保留二次确认。
- `.gitignore` 已排除 `console/data/`、`console/extras_local.py`、`ports.local.txt`。

> **推送到公开仓库前**：确认 `git status` 里没有 `console/data/` 下任何文件。

---

## 日常命令

```bash
systemctl --user status meow-console meow-vnc meow-forward
journalctl --user -u meow-console -f
tail -f console/data/audit.log

python3 console/tests/all_check.py        # 全量自检
python3 console/tests/offline_check.py    # 离线自检（不需要 X）
```

---

## 已知边界

- 需要 **X11 会话**（Wayland 下 x11vnc 抓不到画面）
- 画面按 1/2 缩放传输；贴边由前端「边缘吸附」补到真实最后一行像素（只吸附底边，用于 Dock 弹出）
- 终端并发上限 4
- `0fps` 通常不是故障：x11vnc 只在画面有变化时才发帧，桌面完全静止时就是 0

详细排查见 **[docs/排查记录.md](docs/排查记录.md)**。

## 许可证

MIT（见 [LICENSE](LICENSE)）。
