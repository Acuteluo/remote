// Web 终端: xterm.js <-> /ws/term
'use strict';
const statusEl = document.getElementById('term-status');
const FONT_KEY = 'meow_term_font';
const term = new Terminal({
  fontSize: Math.min(22, Math.max(10, Number(localStorage.getItem(FONT_KEY)) || 14)),
  fontFamily: '"Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace',
  cursorBlink: true,
  scrollback: 5000,
  theme: {
    background: '#0b0f14', foreground: '#dce6f2', cursor: '#4da3ff',
    selectionBackground: '#264f78',
    black: '#0b0f14', red: '#ff6b6b', green: '#3ecf8e', yellow: '#ffb454',
    blue: '#4da3ff', magenta: '#c792ea', cyan: '#56d4dd', white: '#dce6f2',
  },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById('terminal'));

let ws = null, manualClose = false;

function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function connect() {
  manualClose = false;
  statusEl.textContent = '连接中…';
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/term`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    // 不写死家目录: 换台机器/换个用户就错了。bash 起来后自己会打印提示符, 这里不用多说。
    statusEl.textContent = '已连接';
    doFit();
    term.focus();
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
    else term.write(e.data);
  };
  ws.onclose = () => {
    statusEl.textContent = '连接断开';
    if (!manualClose) setTimeout(connect, 2000);
  };
  ws.onerror = () => { statusEl.textContent = '连接出错'; };
}

function doFit() {
  try {
    fit.fit();
    wsSend({ t: 'r', c: term.cols, r: term.rows });
  } catch (e) {}
}

term.onData((d) => wsSend({ t: 'i', d }));
window.addEventListener('resize', () => { doFit(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) doFit(); });

const SEQ = {
  esc: '\x1b', tab: '\t', 'ctrl-c': '\x03', 'ctrl-d': '\x04', 'ctrl-u': '\x15',
  up: '\x1b[A', down: '\x1b[B', left: '\x1b[D', right: '\x1b[C',
  pipe: '|', slash: '/', dash: '--',
};
document.querySelectorAll('.qkbar button').forEach((b) => {
  b.addEventListener('click', (ev) => {
    ev.preventDefault();
    const s = b.dataset.seq;
    if (s === 'kb') { term.focus(); return; }
    if (SEQ[s] != null) { wsSend({ t: 'i', d: SEQ[s] }); term.focus(); }
  });
});
document.getElementById('term-reconnect').addEventListener('click', () => {
  if (ws) { manualClose = true; ws.close(); }
  term.reset();
  connect();
});

// 终端字号调节(持久化)
function changeFont(delta) {
  const n = Math.min(22, Math.max(10, term.options.fontSize + delta));
  term.options.fontSize = n;
  localStorage.setItem(FONT_KEY, String(n));
  doFit();
}
document.getElementById('qk-fontd').addEventListener('click', () => { changeFont(-1); term.focus(); });
document.getElementById('qk-fonti').addEventListener('click', () => { changeFont(1); term.focus(); });

connect();
