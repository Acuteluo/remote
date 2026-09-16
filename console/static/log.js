// 审计日志页: 拉 /api/log 渲染, 支持按类别筛选。
// 只读, 没有 WebSocket —— 进来拉一次, 需要更新就点「刷新」。
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (t) => {
  const d = document.createElement('div');
  d.textContent = t == null ? '' : t;
  return d.innerHTML;
};

let ALL = [], CATS = {}, COUNTS = {}, FILTER = 'all';

// 内容里出现这些词就整行标红 —— 一眼挑出失败/异常, 不用逐行读
const BAD_RE = /失败|FAIL|错误|拒绝|超时|不可用|未生效|不通/;

async function load() {
  const st = $('log-status');
  st.textContent = '加载中…';
  try {
    const r = await fetch('/api/log?n=500', { cache: 'no-store' });
    const j = await r.json();
    if (!j.ok) throw new Error(j.err || '读取失败');
    ALL = j.rows || [];
    CATS = j.cats || {};
    COUNTS = j.counts || {};
    renderCats();
    render();
    st.textContent = `最近 ${ALL.length} 条`;
  } catch (e) {
    st.textContent = '加载失败';
    $('log-list').innerHTML = `<div class="card">读取失败：${esc(e.message)}</div>`;
  }
}

function renderCats() {
  const items = [['all', '全部', ALL.length]].concat(
    Object.entries(COUNTS).map(([k, v]) => [k, CATS[k] || k, v]));
  $('log-cats').innerHTML = items.map(([k, name, n]) =>
    `<button class="logcat${k === FILTER ? ' on' : ''}" data-c="${esc(k)}">` +
    `${esc(name)}<b>${n}</b></button>`).join('');
}

function render() {
  const rows = FILTER === 'all' ? ALL : ALL.filter((r) => r.c === FILTER);
  if (!rows.length) {
    $('log-list').innerHTML = '<div class="card">没有记录</div>';
    return;
  }
  let html = '', day = '';
  for (const r of rows) {
    if (r.d !== day) {                       // 跨天插一条日期分隔
      day = r.d;
      html += `<div class="logday">${esc(day)}</div>`;
    }
    html += `<div class="logrow${BAD_RE.test(r.m) ? ' bad' : ''}">` +
      `<span class="logt">${esc(r.t)}</span>` +
      `<span class="logc">${esc(CATS[r.c] || r.c)}</span>` +
      `<span class="logm">${esc(r.m)}</span></div>`;
  }
  $('log-list').innerHTML = html;
}

$('log-cats').addEventListener('click', (e) => {
  const b = e.target.closest('.logcat');
  if (!b) return;
  FILTER = b.dataset.c;
  renderCats();
  render();
});
$('log-refresh').addEventListener('click', load);
load();
