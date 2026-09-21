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

let ws = null, hist = [];

function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/status`);
  ws.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (err) {} };
  ws.onclose = () => {
    ws = null;
    $('clock').textContent = '连接断开, 重连中…';
    if (!document.hidden) setTimeout(connect, 3000);
  };
}
connect();

// 切后台就断开: 这个流每 2s 要采一次全量状态(含 wmctrl/tailscale 子进程),
// 手机把标签页挂后台时还在跑纯属白烧 CPU, 会和远程桌面的图传抢资源。
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  } else {
    connect();
  }
});

function barClass(p) { return p > 90 ? 'crit' : p > 70 ? 'hot' : ''; }

function render(s) {
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
    chips.push(['电池', `${s.battery.cap}% ${s.battery.status}`,
      s.battery.cap < 15 && s.battery.status === 'Discharging' ? 'bad' : 'ok']);
  }
  const wlan = (s.net || []).find(n => /wl|tailscale/.test(n.if));
  if (wlan) chips.push([wlan.if, `↓${fmtRate(wlan.rx)} ↑${fmtRate(wlan.tx)}`, '']);
  // 当前 Wi-Fi: 和 CPU/温度那些一样, 给一眼能看懂的 SSID + 信号百分比
  if (s.wifi) {
    const wp = s.wifi.pct;
    chips.push(['WiFi', `${s.wifi.ssid || s.wifi.iface}${wp == null ? '' : ' ' + wp + '%'}`,
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
    if (cores.childElementCount !== (s.cpu.cores || []).length) {
      cores.innerHTML = (s.cpu.cores || []).map(() => '<div class="c"><i></i></div>').join('');
    }
    [...cores.children].forEach((el, i) => {
      el.firstChild.style.height = Math.min(100, s.cpu.cores[i] || 0) + '%';
    });
    hist.push(cpuT); if (hist.length > 120) hist.shift();
    drawHist();
  }
  if (s.load) $('load').textContent = s.load.join(' / ');

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
  // 网卡名(尤其虚拟网卡)太机器味, 面板上给人话标签; 原名塞进 title 悬停可见
  const ifLabel = (n) => /^wl/.test(n) ? 'WiFi 速率'
    : n === 'Meta' ? 'Clash 代理'
      : n === 'tailscale0' ? 'Tailscale'
        : /^(en|eth)/.test(n) ? '有线速率' : n;
  $('net').innerHTML = wifiRow + ((s.net || []).slice(0, 6).map(n =>
    `<div class="kv"><span class="d" title="${n.if}">${ifLabel(n.if)}</span>` +
    `<span>↓${fmtRate(n.rx)} ↑${fmtRate(n.tx)}</span></div>`
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
    bg += `<div class="kv"><span class="d">电池</span><span>${s.battery.cap}% · ${s.battery.status}` +
      (s.battery.watts != null ? ` · ${s.battery.watts.toFixed(1)}W` : '') + '</span></div>';
  }
  if (s.gpu) bg += `<div class="kv"><span class="d">核显频率</span><span>${s.gpu.cur} / ${s.gpu.max} MHz</span></div>`;
  $('batgpu').innerHTML = bg || '<div class="kv d">无数据</div>';

  // 进程表
  fillProc('tbl-cpu', s.top_cpu, 'cpu');
  fillProc('tbl-mem', s.top_mem, 'mem');

  // 窗口
  $('windows').innerHTML = (s.windows || []).map(w =>
    `<div class="kv"><span class="d">${w.id}</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:75%">${esc(w.title)}</span></div>`
  ).join('') || '<div class="kv d">无可见窗口</div>';

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

function esc(t) { const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

function drawHist() {
  const cv = $('cpu-hist'), ctx = cv.getContext('2d');
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
