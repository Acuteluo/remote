// MEOW控制台首页: /ws/status 实时渲染
'use strict';
const $ = (id) => document.getElementById(id);
const fmtB = (b) => b >= 1073741824 ? (b / 1073741824).toFixed(1) + ' GB'
  : b >= 1048576 ? (b / 1048576).toFixed(0) + ' MB' : (b / 1024).toFixed(0) + ' KB';
const fmtRate = (r) => r >= 1048576 ? (r / 1048576).toFixed(1) + ' MB/s'
  : r >= 1024 ? (r / 1024).toFixed(0) + ' KB/s' : r.toFixed(0) + ' B/s';
const fmtUp = (s) => {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + '天' : '') + (h ? h + '时' : '') + m + '分';
};

let ws = null, reconnectTimer = 0, hist = [];

// ---- 信息面板的折叠 ----
// 右上角一个倒三角(▼), 点一下收起、再点展开。状态**存服务端**
// (config.json 的 collapsed), 和主题色/背景一样属于"这台电脑的偏好" ——
// 换台设备打开也一致, 不会每次都重置。
const COLLAPSED = {};              // 卡片 id -> true(已折叠)
let collapsedLoaded = false;

function applyCollapsed() {
  document.querySelectorAll('.card[data-card]').forEach((card) => {
    card.classList.toggle('collapsed', !!COLLAPSED[card.dataset.card]);
  });
}
function saveCollapsed() {
  fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ collapsed: COLLAPSED }),
  }).catch(() => {});
}
function initFold() {
  document.querySelectorAll('.card[data-card]').forEach((card) => {
    const h3 = card.querySelector('h3');
    if (!h3 || h3.querySelector('.fold')) return;
    const b = document.createElement('button');
    b.className = 'fold';
    b.type = 'button';
    b.title = '折叠 / 展开';
    b.setAttribute('aria-label', '折叠或展开这个面板');
    // 用 SVG 画三角, 不用 '▼' 字符 —— 那个字形的墨迹几乎全在基线**上方**
    // (实测上伸 7px / 下伸 1px), 就算把盒子居中了, 看上去还是偏上。
    // SVG 的盒子边界就是墨迹边界, 居中 = 所见即所得。
    b.innerHTML = '<svg width="10" height="6" viewBox="0 0 10 6" aria-hidden="true">' +
      '<path d="M0 0h10L5 6z" fill="currentColor"/></svg>';
    const toggle = () => {
      const id = card.dataset.card;
      const now = !card.classList.contains('collapsed');
      card.classList.toggle('collapsed', now);
      if (now) COLLAPSED[id] = true; else delete COLLAPSED[id];
      saveCollapsed();
    };
    b.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
    // 整行都能点 —— 手机上 18px 的三角太小了。标题里没有别的可点元素, 不会误触。
    h3.addEventListener('click', (e) => {
      if (e.target.closest('a, button, input, select')) return;
      toggle();
    });
    h3.appendChild(b);
  });
}
initFold();
// 拉一次服务端记的折叠状态
fetch('/api/config', { cache: 'no-store' })
  .then((r) => r.json())
  .then((j) => {
    const c = j && j.config && j.config.collapsed;
    if (c && typeof c === 'object') {
      for (const k of Object.keys(c)) if (c[k]) COLLAPSED[k] = true;
    }
    collapsedLoaded = true;
    applyCollapsed();
  })
  .catch(() => { collapsedLoaded = true; });
// 进主页的加载进度: 页面 -> 状态通道 -> 首屏数据(见 loadtip.js)
if (window.LoadTip) LoadTip.start(['页面', '样式', '状态通道', '首屏数据', '曲线']);

function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  clearTimeout(reconnectTimer);
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/status`);
  ws = socket;
  socket.onopen = () => { if (window.LoadTip) LoadTip.step('状态通道'); };
  socket.onmessage = (e) => {
    if (ws !== socket) return;
    try { render(JSON.parse(e.data)); } catch (err) {}
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    $('clock').textContent = '连接断开, 重连中…';
    if (!document.hidden) reconnectTimer = setTimeout(connect, 3000);
  };
}
connect();

// 切后台就断开: 这个流每 2s 要采一次全量状态(含 wmctrl/tailscale 子进程),
// 手机把标签页挂后台时还在跑纯属白烧 CPU, 会和远程桌面的图传抢资源。
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(reconnectTimer);
    if (ws) { const old = ws; ws = null; try { old.close(); } catch (e) {} }
  } else {
    connect();
  }
});

function barClass(p) { return p > 90 ? 'crit' : p > 70 ? 'hot' : ''; }

let firstPaint = false;
function render(s) {
  if (!firstPaint) { firstPaint = true; if (window.LoadTip) LoadTip.step('首屏数据'); }
  $('clock').textContent = `${s.host} · ${s.time} · 在线`;

  // 异常告警: 阈值判断在服务端(Collector.alerts), 这里只管显示。
  // 没告警时也渲染一条绿色的 —— 否则"一切正常"和"功能坏了"分不清。
  const alEl = $('alerts');
  const alerts = s.alerts || [];
  alEl.innerHTML = alerts.length
    ? alerts.map(a => `<div class="alert ${a.level}">` +
        `<span class="ai">${a.level === 'crit' ? '⛔' : '⚠️'}</span>` +
        `<span class="at">${esc(a.msg)}</span></div>`).join('')
    : '<div class="alerts-ok">✓ 运行正常，没有需要处理的问题</div>';

  const chips = [];
  const cpuT = s.cpu ? s.cpu.total : 0;
  chips.push(['CPU', cpuT.toFixed(0) + '%', cpuT > 85 ? 'bad' : '']);
  if (s.mem && s.mem.total) {
    const p = 100 * (s.mem.total - s.mem.avail) / s.mem.total;
    chips.push(['内存', p.toFixed(0) + '%', p > 90 ? 'bad' : '']);
  }
  const pkg = (s.temps || []).find(t => /coretemp\/Package/i.test(t[0]));
  if (pkg) chips.push(['CPU温度', pkg[1] + '°C', pkg[1] > 90 ? 'bad' : pkg[1] > 75 ? 'warn' : '']);
  if (s.battery) {
    const bi = battInfo(s.battery);
    chips.push(['电池', `${s.battery.cap}% ${bi.text}`, bi.level]);
  }
  const wlan = (s.net || []).find(n => /wl|tailscale/.test(n.if));
  if (wlan) chips.push([wlan.if, `↓${fmtRate(wlan.rx)} ↑${fmtRate(wlan.tx)}`, '']);
  // 当前 Wi-Fi: 和 CPU/温度那些一样, 给一眼能看懂的 SSID + 信号百分比
  if (s.wifi) {
    const wp = s.wifi.pct;
    chips.push(['WiFi', `${s.wifi.ssid || s.wifi.iface}${wp == null ? '' : ' (' + wp + '%)'}`,
      wp == null ? '' : wp < 30 ? 'bad' : wp < 55 ? 'warn' : 'ok']);
  }
  // 桌面连接数: x11vnc 是共享模式, 客户端越多共享更新循环越慢 ——
  // 放首页是为了"画面一卡就能一眼看到是不是有遗留连接"。
  if (typeof s.vnc_clients === 'number') {
    chips.push(['桌面连接', s.vnc_clients + ' 个',
      s.vnc_clients > 2 ? 'bad' : s.vnc_clients > 1 ? 'warn' : 'ok']);
  }
  chips.push(['在线时长', fmtUp(s.uptime || 0), '']);
  $('chips').innerHTML = chips.map(([k, v, c]) =>
    `<span class="chip ${c}">${k} <b>${v}</b></span>`).join('');

  // CPU
  if (s.cpu) {
    $('cpu-total').textContent = s.cpu.total.toFixed(1);
    $('cpu-bar').style.width = Math.min(100, s.cpu.total) + '%';
    $('cpu-bar').className = barClass(s.cpu.total);
    const cores = $('cpu-cores');
    const list = s.cpu.cores || [];
    const ci = s.cpu_info || {};
    // 大小核分组: 内核在 /sys/devices/cpu_core|cpu_atom 里给了划分(P 核在前)。
    // 分成两段画, 上面标一行小字, 一眼能看出"是哪个核在忙"。
    const pn = ci.p || 0, en = ci.e || 0;
    if (cores.childElementCount !== list.length + (pn && en ? 2 : 0)) {
      let html = '';
      if (pn && en) {
        html += '<div class="grp">P 核（性能核）× ' + pn + '</div>';
        html += list.slice(0, pn).map(() => '<div class="c"></div>').join('');
        html += '<div class="grp">E 核（能效核）× ' + en + '</div>';
        html += list.slice(pn).map(() => '<div class="c"></div>').join('');
      } else {
        html += list.map(() => '<div class="c"></div>').join('');
      }
      cores.innerHTML = html;
    }
    const cells = [...cores.querySelectorAll('.c')];
    cells.forEach((el, i) => {
      const v = Math.max(0, Math.min(100, list[i] || 0));
      el.textContent = v.toFixed(0);
      // 热力图: 越忙底色越亮, 80% 以上转黄/红
      el.style.background = v < 5 ? 'rgba(77,163,255,.07)'
        : v < 20 ? 'rgba(77,163,255,.22)'
        : v < 50 ? 'rgba(77,163,255,.40)'
        : v < 80 ? 'rgba(255,180,84,.48)' : 'rgba(255,107,107,.58)';
      el.className = 'c' + (v >= 50 ? ' hi' : '');
      el.title = 'cpu' + i + '  ' + v.toFixed(1) + '%';
    });
    hist.push(cpuT); if (hist.length > 120) hist.shift();
    drawHist();
  }
  if (s.load) $('load').textContent = s.load.join(' / ');
  // CPU 身份信息: 型号、线程数、大小核划分、当前频率、负载折算
  const ci = s.cpu_info || {};
  if (ci.model) $('cpu-model').textContent = ci.model.replace(/Intel\(R\) |Core\(TM\) |CPU /g, '');
  const topo = [];
  if (ci.threads) topo.push(ci.threads + ' 线程');
  if (ci.p && ci.e) topo.push(ci.p + 'P + ' + ci.e + 'E');
  $('cpu-topo').textContent = topo.join(' · ');
  if (s.load && s.load.length && ci.threads) {
    // 负载要除以核数才有意义: 18 核上 11 的负载其实才 6 成
    const l = s.load[0] / ci.threads * 100;
    $('load-pct').textContent = l.toFixed(0) + '%（按 ' + ci.threads + ' 线程折算）';
  }
  if (ci.mhz) {
    $('cpu-mhz').textContent = ci.mhz + ' MHz' + (ci.mhz_max ? ' / 最高 ' + ci.mhz_max : '');
  }

  // 内存
  if (s.mem && s.mem.total) {
    const used = s.mem.total - s.mem.avail;
    $('mem-used').textContent = fmtB(used);
    $('mem-total').textContent = fmtB(s.mem.total);
    const p = 100 * used / s.mem.total;
    const bar = $('mem-bar'); bar.style.width = p + '%'; bar.className = barClass(p);
    $('swap').textContent = s.mem.swap_total ?
      `${fmtB(s.mem.swap_total - s.mem.swap_free)} / ${fmtB(s.mem.swap_total)}` : '无';
  }
  $('uptime').textContent = fmtUp(s.uptime || 0);

  // 温度: 取前 8 个
  const temps = (s.temps || []).slice(0, 8);
  $('temps').innerHTML = temps.map(([n, v]) => {
    const c = v > 85 ? 'bad' : v > 70 ? 'warn' : '';
    return `<div class="kv"><span class="d">${n}</span><span style="color:var(--${c || 'fg'})">${v}°C</span></div>`;
  }).join('') || '<div class="kv d">无传感器数据</div>';

  // 网络: 当前 Wi-Fi 一行(SSID + 信号) + 各网卡速率前 6 个
  // WiFi 那行单独排版: 标签独占一行, 内容另起一行且允许换行。
  // 原来跟别的指标一样挤在 .kv 的"标签左 / 值右"一行里, 而
  // "SSID · 信号 84% (-58 dBm) · wlp0s20f3" 太长 —— 窄屏上标签被挤没,
  // 看着就像和 WiFi 名字重叠了。
  const wifiRow = s.wifi
    ? '<div class="kv" style="display:block">' +
      '<span class="d" style="display:block;margin-bottom:2px">WiFi</span>' +
      '<span style="display:block;line-height:1.55;word-break:break-word">' +
      esc(s.wifi.ssid || '(未命名)') +
      ` · 信号 ${s.wifi.pct == null ? '--' : s.wifi.pct + '%'}` +
      // dBm 是原始信号强度, 放括号里并弱化 —— 百分比才是给人看的那个数
      `${s.wifi.level == null ? '' : ' <span style="color:var(--dim)">(' +
        Number(s.wifi.level).toFixed(0) + ' dBm)</span>'}` +
      // 网卡名不在这里重复: 下一行速率那个标签就是它(原来两行都写, 显得重复)
      '</span></div>'
    : '';
  $('net').innerHTML = wifiRow + ((s.net || []).slice(0, 6).map(n =>
    `<div class="kv"><span class="d">${n.if}</span><span>↓${fmtRate(n.rx)} ↑${fmtRate(n.tx)}</span></div>`
  ).join('') || '<div class="kv d">无网卡数据</div>');

  // 磁盘
  $('disks').innerHTML = (s.disks || []).map(d => {
    const used = d.total - d.free, p = d.total ? 100 * used / d.total : 0;
    return `<div class="kv"><span class="d">${d.mp}</span><span>${fmtB(used)} / ${fmtB(d.total)} (${p.toFixed(0)}%)</span></div>
      <div class="bar"><i class="${barClass(p)}" style="width:${p}%"></i></div>`;
  }).join('') || '<div class="kv d">无挂载</div>';

  // 电池/GPU
  let bg = '';
  if (s.battery) {
    const bi2 = battInfo(s.battery);
    bg += `<div class="kv"><span class="d">电池</span><span title="内核原始状态: ${
      esc(s.battery.status)}${s.battery.ac == null ? '' : ' · 电源' + (s.battery.ac ? '已接' : '未接')}">` +
      `${s.battery.cap}% · ${bi2.text}` +
      (s.battery.watts != null ? ` · ${s.battery.watts.toFixed(1)}W` : '') + '</span></div>';
  }
  if (s.gpu) bg += `<div class="kv"><span class="d">核显频率</span><span>${s.gpu.cur} / ${s.gpu.max} MHz</span></div>`;
  $('batgpu').innerHTML = bg || '<div class="kv d">无数据</div>';

  // 进程表
  fillProc('tbl-cpu', s.top_cpu, 'cpu');
  fillProc('tbl-mem', s.top_mem, 'mem');

  // 窗口
  $('windows').innerHTML = (s.windows || []).map((w) => {
    const ic = w.icon
      ? `<img class="win-ic" src="/api/icon?n=${encodeURIComponent(w.icon)}" alt="" loading="lazy">`
      : `<span class="win-ic badge">${esc((w.app || w.cls || '?').trim().charAt(0).toUpperCase())}</span>`;
    const title = (w.title || '').trim();
    return `<div class="win-row">${ic}<span class="win-txt">` +
      `<span class="win-app">${esc(w.app || w.cls || '?')}</span>` +
      (title ? `<span class="win-title">${esc(title)}</span>` : '') +
      `</span><span class="win-id">${esc(w.id)}</span></div>`;
  }).join('') || '<div class="kv d">无可见窗口</div>';

  // 前台窗口 + 空闲时长 + 鼠标位置
  const ac = s.activity || {};
  const aw = ac.active;
  if (aw) {
    const ic = aw.icon
      ? `<img class="win-ic" src="/api/icon?n=${encodeURIComponent(aw.icon)}" alt="">`
      : `<span class="win-ic badge">${esc((aw.app || aw.cls || '?').trim().charAt(0).toUpperCase())}</span>`;
    $('active-win').innerHTML = `<div class="win-row">${ic}<span class="win-txt">` +
      `<span class="win-app">${esc(aw.app || aw.cls || '(未知应用)')}</span>` +
      (aw.title ? `<span class="win-title">${esc(aw.title)}</span>` : '') +
      `</span><span class="win-id">${esc(aw.id)}</span></div>`;
  } else {
    $('active-win').innerHTML = '<div class="kv d">拿不到前台窗口</div>';
  }
  $('idle').textContent = ac.idle_ms == null ? '--' : fmtIdle(ac.idle_ms);
  $('mouse-pos').textContent = ac.mouse ? (ac.mouse.x + ', ' + ac.mouse.y) : '--';

  // Tailscale
  $('peers').innerHTML = (s.peers || []).map(p =>
    `<div class="kv"><span><i class="dot ${p.online ? 'on' : 'off'}"></i>${esc(p.host)}${p.self ? ' (本机)' : ''}</span>
     <span class="d">${p.ip} · ${esc(p.os)}</span></div>`
  ).join('') || '<div class="kv d">tailscale 未运行</div>';
}

function fillProc(id, rows, key) {
  $(id).innerHTML = '<tr><th>PID</th><th>名称</th><th class="num">' +
    (key === 'cpu' ? 'CPU%' : '内存') + '</th><th></th></tr>' +
    (rows || []).map(r => `<tr><td>${r.pid}</td><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.name)}</td>` +
      `<td class="num">${key === 'cpu' ? r.cpu.toFixed(1) : fmtB(r.mem)}</td>` +
      `<td class="num"><button class="kbtn" data-pid="${r.pid}">结束</button></td></tr>`).join('');
}

// "上次键鼠操作是多久以前"。
//
// 判据是**键鼠输入**, 不是"页面/窗口有没有变" —— 服务端读的是 GNOME 的
// IdleMonitor(最后一次键鼠输入到现在)。所以后台挂着 AI 跑任务、放视频、
// 自己弹窗换前台, 都**不会**被算成"有人在用"(用户特别强调过这点)。
// 不写"刚刚": 直接给精确时长, 好判断到底多久没人碰了。
function fmtIdle(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
  const m = Math.floor(s % 3600 / 60), sec = s % 60;
  if (d) return d + ' 天 ' + h + ' 时 ' + m + ' 分前';
  if (h) return h + ' 时 ' + m + ' 分 ' + sec + ' 秒前';
  if (m) return m + ' 分 ' + sec + ' 秒前';
  return sec + ' 秒前';
}

// 电池要**结合电源**看。
// 内核只报电池自己的状态, 而笔记本在满电附近充电电路会来回切
// (充满→停充→微放→再充), status 就在 Full / Not charging / Discharging 之间跳。
// 只看它就会出现"插着电却显示 Discharging"——用户反馈的"来回切"就是这个。
// 把 AC 的 online 一起看, 文案就稳定了。
function battInfo(b) {
  const cap = b.cap, ac = b.ac, st = String(b.status || '').toLowerCase();
  let text, level = 'ok';
  if (ac === true) {
    if (st === 'charging') text = '充电中';
    else if (cap >= 95) text = '已接电源 · 已充满';
    else text = '已接电源 · 未充电';
  } else if (ac === false) {
    if (st === 'charging') text = '充电中';
    else {
      text = '用电池';
      if (cap < 15) level = 'bad';
      else if (cap < 30) level = 'warn';
    }
  } else {
    text = b.status || '--';                 // 拿不到 AC 状态就照原样显示
    if (cap < 15 && st === 'discharging') level = 'bad';
  }
  return { text: text, level: level };
}

function esc(t) { const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

let histDrawn = false;
function drawHist() {
  const cv = $('cpu-hist'), ctx = cv.getContext('2d');
  if (!histDrawn) { histDrawn = true; if (window.LoadTip) LoadTip.step('曲线'); }
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  if (hist.length < 2) return;
  const span = hist.length - 1;
  // CPU 曲线的颜色也跟着主题色走(原来写死默认蓝)
  ctx.strokeStyle = (getComputedStyle(document.documentElement)
    .getPropertyValue('--acc').trim()) || '#4da3ff';
  ctx.lineWidth = 2; ctx.beginPath();
  hist.forEach((v, i) => {
    const x = i / span * W, y = H - Math.min(100, v) / 100 * (H - 4) - 2;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}

// 电源按钮
document.querySelectorAll('.power button').forEach(b => {
  b.addEventListener('click', () => {
    const act = b.dataset.act;
    const danger = ['suspend', 'reboot', 'poweroff'].includes(act);
    const names = { 'screen-off': '熄屏', 'screen-on': '亮屏', lock: '锁屏',
      suspend: '睡眠(断网, 需物理唤醒)', reboot: '重启', poweroff: '关机(需物理开机)' };
    if (danger && !confirm(`确认${names[act]}?`)) return;
    $('power-msg').textContent = '执行中…';
    fetch('/api/power', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: act, confirm: true }) })
      .then(r => r.json()).then(j => { $('power-msg').textContent = j.msg || j.err || '完成'; })
      .catch(() => { $('power-msg').textContent = '网络中断(若为睡眠/关机属正常)'; });
  });
});

// 修改密码: 必须输对原密码; 两次新密码一致才提交
(function () {
  const btn = document.getElementById('pw-btn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const oldPw = document.getElementById('pw-old').value;
    const newPw = document.getElementById('pw-new').value;
    const newPw2 = document.getElementById('pw-new2').value;
    const msg = document.getElementById('pw-msg');
    if (!oldPw || !newPw) { msg.textContent = '原密码和新密码都要填'; return; }
    if (newPw !== newPw2) { msg.textContent = '两次输入的新密码不一致'; return; }
    msg.textContent = '提交中…';
    fetch('/api/password', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: oldPw, new: newPw }) })
      .then(r => r.json()).then(j => {
        msg.textContent = j.ok ? '已修改, 下次登录用新密码' : (j.err || '失败');
        if (j.ok) {
          document.getElementById('pw-old').value = '';
          document.getElementById('pw-new').value = '';
          document.getElementById('pw-new2').value = '';
        }
      })
      .catch(() => { msg.textContent = '网络中断, 请重试'; });
  });
})();

// 结束进程
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.kbtn');
  if (!btn) return;
  const pid = btn.dataset.pid;
  if (!confirm(`向进程 ${pid} 发送 SIGTERM?`)) return;
  fetch('/api/kill', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pid: Number(pid) }) })
    .then(r => r.json()).then(j => alert(j.msg || j.err || ''));
});

// ---- 一键自检(主页多功能面板最下面那个按钮) -------------------------------
// 分两段:
//   ① 服务端 /api/selfcheck —— 接口 / ffmpeg / 设备 / 服务 / 系统告警。
//      它只**试采一次麦克风**(约 0.9 秒), 不碰摄像头。
//   ② 客户端这半边 —— 网络 RTT、时钟对齐、本地播放能力、**画面延时**、**声音延时**。
//      这两条延时必须真的采一小段才量得出来(摄像头会亮一下指示灯), 所以单独说明。
//      声音是**静音播的**(volume=0), 自检不会出声。
// 计时口径见 cam.js: 画面 = 服务端发帧时刻换算到本机; 声音 = 服务端给的第 0 个
// 采样时刻 + currentTime。两个都是**在手机上**算的, 网络那一段天然包含在内。
(function () {
  const btn = $('sc-open'), mask = $('sc-mask'), body = $('sc-body'),
        title = $('sc-title'), note = $('sc-note'), again = $('sc-again');
  if (!btn || !mask) return;

  const ICON = { ok: '✓', warn: '!', bad: '✗', run: '…' };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // 延时显示(约定): **<1s 用整数毫秒**(3ms / 999ms), **>=1s 用秒**
  // (1.005s / 10.12s / 100.1s —— 秒这一档小数位随量级自适应, 宽度基本恒定)。
  // 采集模块 / 远程桌面 / 一键自检三处同一套, 改的时候一起改。
  const fmtLag = (ms) => {
    if (ms == null || !isFinite(ms) || ms < 0) return '--';
    if (ms < 1000) return Math.round(ms) + 'ms';
    const sec = ms / 1000;
    return sec.toFixed(sec < 10 ? 3 : sec < 100 ? 2 : 1) + 's';
  };
  const fmtMs = fmtLag;   // 自检里凡是时间都走这一套
  const lvLagVideo = (ms) => (ms == null ? '' : ms < 300 ? 'ok' : ms < 1000 ? 'warn' : 'bad');
  const lvLagAudio = (ms) => (ms == null ? '' : ms < 2000 ? 'ok' : ms < 8000 ? 'warn' : 'bad');
  let running = false, audio = null, vws = null;

  function grp(name) {
    const h = document.createElement('div');
    h.className = 'sc-grp';
    h.textContent = name;
    body.appendChild(h);
    return h;
  }
  // 插一条结果行; 返回句柄, 可以改它的 level/detail(拿来做进度), 也可以 remove()
  function item(name, level, detail) {
    const d = document.createElement('div');
    d.className = 'sc-it ' + level;
    d.innerHTML = '<span class="lv"></span><span class="nm"></span><span class="dt"></span>';
    d.querySelector('.lv').textContent = ICON[level] || '·';
    d.querySelector('.nm').textContent = name;
    d.querySelector('.dt').textContent = detail == null ? '' : detail;
    body.appendChild(d);
    return {
      set(lv, dt) {
        d.className = 'sc-it ' + lv;
        d.querySelector('.lv').textContent = ICON[lv] || '·';
        if (dt !== undefined) d.querySelector('.dt').textContent = dt;
      },
      remove() { d.remove(); },
    };
  }
  const newSid = () => 'sc' + Math.random().toString(36).slice(2) + Date.now().toString(36);

  // ---- 画面延时: 连一次 /ws/cam, 收几帧就算 (帧时刻由服务端给) ----
  function videoLatency(clockOff, clockOk) {
    return new Promise((resolve) => {
      let done = false, sentAt = 0;
      const ages = [];
      const fin = (r) => {
        if (done) return;
        done = true;
        try { if (vws) { vws.onclose = null; vws.close(); } } catch (e) { /* ignore */ }
        vws = null;
        resolve(r);
      };
      const to = setTimeout(() => fin({ level: 'warn', detail: '8 秒内没收到画面' }), 8000);
      try {
        vws = new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') +
                            '://' + location.host + '/ws/cam?size=640x480&fps=12');
      } catch (e) { clearTimeout(to); return resolve({ level: 'bad', detail: '建不了 WebSocket' }); }
      vws.binaryType = 'blob';
      vws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          let j = {};
          try { j = JSON.parse(ev.data); } catch (e) { return; }
          if (j.t === 'f') sentAt = j.s || 0;
          else if (j.t === 'err') { clearTimeout(to); fin({ level: 'warn', detail: j.d || '摄像头打不开' }); }
          return;
        }
        if (sentAt && clockOk) ages.push(Date.now() + clockOff - sentAt);
        if (ages.length >= 4) {
          clearTimeout(to);
          ages.sort((a, b) => a - b);
          const med = ages[ages.length >> 1];
          fin({ level: lvLagVideo(med) || 'ok',
                detail: fmtLag(med) + '（' + ages.length + ' 帧，最小 ' + fmtLag(ages[0]) + '）' });
        }
      };
      vws.onerror = () => { clearTimeout(to); fin({ level: 'bad', detail: 'WebSocket 出错' }); };
      vws.onclose = () => { clearTimeout(to); fin({ level: 'warn', detail: '连接被服务端关闭' }); };
    });
  }

  async function run() {
    if (running) return;
    running = true;
    btn.disabled = true;
    body.innerHTML = '';
    title.textContent = '一键自检';
    mask.hidden = false;
    note.textContent = '正在检查…';
    const tot = { ok: 0, warn: 0, bad: 0 };

    // ★ 声音探针必须**在点击的同一个 task 里起播** —— 中间夹 await 之后
    //   play() 会被浏览器的自动播放策略拒掉(踩过, 见 cam.js)。所以先起它。
    let sid = '';
    try {
      sid = newSid();
      audio = document.createElement('audio');
      audio.volume = 0;                 // 静音: 自检不该出声
      audio.preload = 'none';
      audio.src = '/api/audio?src=mic&sid=' + encodeURIComponent(sid) + '&_=' + Date.now();
      document.body.appendChild(audio);
      const pr = audio.play();
      if (pr && pr.catch) pr.catch(() => {});
    } catch (e) { audio = null; }

    // ---- ① 服务端 ----
    const hSrv = grp('服务端 / 设备');
    const itSrv = item('服务端检查', 'run', '…');
    let srv = null;
    try {
      const r = await fetch('/api/selfcheck', { cache: 'no-store' });
      srv = await r.json();
    } catch (e) { /* 下面统一报 */ }
    itSrv.remove();
    hSrv.remove();
    if (srv && srv.groups) {
      for (const g of srv.groups) {
        grp(g.name);
        for (const it of g.items) {
          item(it.name, it.level, it.detail);
          tot[it.level] = (tot[it.level] || 0) + 1;
        }
      }
    } else {
      grp('服务端 / 设备');
      item('自检接口', 'bad', '拿不到 /api/selfcheck 的结果');
      tot.bad++;
    }

    // ---- ② 网络 + 时钟对齐 ----
    grp('网络 / 时钟');
    const hRtt = item('网络往返', 'run', '测 5 次…');
    const rtts = [];
    let clockOff = 0, clockOk = false, bestRtt = Infinity;
    for (let i = 0; i < 5; i++) {
      const c0 = Date.now();
      try {
        const j = await (await fetch('/api/time', { cache: 'no-store' })).json();
        const c1 = Date.now();
        if (typeof j.s === 'number') {
          const rtt = c1 - c0;
          rtts.push(rtt);
          if (rtt < bestRtt) { bestRtt = rtt; clockOff = j.s - (c0 + rtt / 2); clockOk = true; }
        }
      } catch (e) { /* 单次失败无所谓 */ }
    }
    rtts.sort((a, b) => a - b);
    if (rtts.length) {
      const med = rtts[rtts.length >> 1];
      hRtt.set(med < 400 ? 'ok' : 'warn',
               '中位 ' + fmtMs(med) + '，最小 ' + fmtMs(rtts[0]) +
               '（' + rtts.length + ' 次）');
      tot[med < 400 ? 'ok' : 'warn']++;
    } else {
      hRtt.set('bad', '5 次都没打通 /api/time');
      tot.bad++;
    }
    const hClk = item('时钟对齐', clockOk ? 'ok' : 'bad',
                      clockOk ? '偏差 ' + fmtMs(clockOff) + '，误差约 ±' + fmtMs(bestRtt / 2) : '没对上');
    tot[clockOk ? 'ok' : 'bad']++;

    // ---- ③ 本地播放能力 ----
    grp('本机(手机)');
    let canPlay = '';
    try { canPlay = new Audio().canPlayType('audio/mpeg') || ''; } catch (e) { /* ignore */ }
    const hasAC = !!(window.AudioContext || window.webkitAudioContext);
    item('音频解码', canPlay ? 'ok' : 'warn', canPlay ? '支持 MP3（' + canPlay + '）' : '浏览器报不支持 MP3');
    tot[canPlay ? 'ok' : 'warn']++;
    item('WebAudio（电平表）', hasAC ? 'ok' : 'warn', hasAC ? '可用' : '没有 AudioContext');
    tot[hasAC ? 'ok' : 'warn']++;

    // ---- ④ 两条延时(要真采一小段) ----
    grp('延时实测（会短暂采集）');
    const hVid = item('画面延时', 'run', '正在取帧…');
    const v = await videoLatency(clockOff, clockOk);
    hVid.set(v.level, v.detail);
    tot[v.level] = (tot[v.level] || 0) + 1;

    const hAud = item('声音延时', 'run', '正在等麦克风流…');
    let since = 0;
    if (!audio) {
      hAud.set('bad', '起不了播放器');
      tot.bad++;
    } else {
      for (let i = 0; i < 14 && !since; i++) {
        await sleep(500);
        try {
          const j = await (await fetch('/api/audio/hb?sid=' + encodeURIComponent(sid),
                                       { cache: 'no-store' })).json();
          if (typeof j.since === 'number' && j.since > 0) since = j.since;
        } catch (e) { /* 继续等 */ }
      }
      if (audio.error) {
        hAud.set('bad', '麦克风流起不来（可能没有可用麦克风）');
        tot.bad++;
      } else if (!since) {
        hAud.set('bad', '服务端没给出音频锚点');
        tot.bad++;
      } else {
        await sleep(4000);                    // 让它播一会儿, 缓冲稳定
        const ages = [];
        for (let i = 0; i < 6; i++) {
          ages.push((Date.now() + clockOff) - (since + audio.currentTime * 1000));
          await sleep(300);
        }
        ages.sort((a, b) => a - b);
        const med = ages[ages.length >> 1];
        const lv = lvLagAudio(med) || 'ok';
        hAud.set(lv, fmtLag(med) + '（' + ages.length + ' 次采样，波动 ' +
                      fmtLag(ages[ages.length - 1] - ages[0]) + '）');
        tot[lv]++;
      }
    }

    // ---- 汇总 ----
    const sum = document.createElement('div');
    sum.className = 'sc-sum';
    sum.innerHTML = '共 <b>' + (tot.ok + tot.warn + tot.bad) + '</b> 项：' +
      '<b class="ok">' + tot.ok + ' 正常</b> · ' +
      '<b class="warn">' + tot.warn + ' 注意</b> · ' +
      '<b class="bad">' + tot.bad + ' 异常</b>';
    body.appendChild(sum);
    title.textContent = tot.bad ? '一键自检 · 有 ' + tot.bad + ' 项异常'
                                : (tot.warn ? '一键自检 · 有 ' + tot.warn + ' 项要注意' : '一键自检 · 全部正常');
    note.textContent = '服务端只试采了一次麦克风；画面/声音延时那两项会真的采一小段'
                     + '（摄像头指示灯会亮一下）。声音是静音播的，不会出声。';
    if (srv && srv.ms) note.textContent = '服务端耗时 ' + srv.ms + ' ms。' + note.textContent;

    // 收尾: 停掉这一路探针
    try { if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); audio.remove(); } } catch (e) { /* ignore */ }
    audio = null;
    running = false;
    btn.disabled = false;
    again.disabled = false;
  }

  btn.addEventListener('click', run);
  again.addEventListener('click', run);
  $('sc-close').addEventListener('click', () => { mask.hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') mask.hidden = true; });
})();
