// 远程桌面: noVNC <-> /ws/vnc (服务器侧桥接 x11vnc)
//
// ============================ 输入层设计说明 ============================
// 2026-09-14 重写。旧实现把触控板手势合成为 DOM MouseEvent 派发到 canvas, 复用
// noVNC 自己的鼠标管线。这条路径有两个致命问题:
//
// 1) noVNC 在 mousedown 时会调用 setCapture() (core/util/events.js), 往 body 里
//    插一张 position:fixed 覆盖全屏、z-index:10000 的透明层
//    (#noVNC_mouse_capture_elem)。它的"解除"只挂在 window 的 mouseup 上, 而画布
//    自己的 mouseup 处理器会先 stopPropagation() —— 于是"直接派发到画布"的合成
//    mouseup 永远到不了 window, 遮罩就永久留在页面上。表现为: 点一下触控板/按键
//    整个界面就卡死, 必须再点一次图传(那一下落在遮罩上, 冒泡到 window 才把遮罩
//    收掉)才能继续 —— 正是"点完都会卡住, 需要再点一次图传"。
// 2) 合成事件要经过 noVNC 的坐标换算 + 17ms 移动节流 + 50px 滚轮量化, 手感完全
//    不可控, 双指滚动基本等于没用。
//
// 现在改为**直接调用 RFB 协议层**写指针事件 (RFB.messages.pointerEvent), 全程不
// 产生任何 DOM 鼠标事件: 不触发 setCapture -> 不会有遮罩; 坐标/按键/滚轮完全自己
// 控制 -> 可以做动量滚动、灵敏度、方向切换。
// 另外保留一个"事后补救"看门狗 (releaseOverlay), 万一 noVNC 因为触摸事件又漏收了
// 遮罩, 也能在下一轮事件循环自动收掉。
// ======================================================================

import { RFB, releaseCapture } from '/static/vendor/novnc-bundle.js';

const $ = (id) => document.getElementById(id);
const statusEl = $('vnc-status');
const overlayMsg = $('vnc-overlay-msg');
const overlay = $('vnc-overlay');
const kb = $('vnc-kb');

// 顶栏状态文字统一从这里走。
// **连上以后不显示**"已连接": 那是默认状态, 顶栏位置紧张, 没必要一直占着
// (2026-09-16 用户要求)。只有"连接中/重连中/未连接/出错"才占位置 ——
// 那些才是需要你知道的信息。文字本身仍然写在 DOM 里(测试拿它当连接判据)。
function setStatus(text) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.classList.toggle('ok', text === '已连接');
}

// ================= 设置(存 localStorage) =================
// scrollDir: 1 = 双指上滑 -> 内容上移(继续往下读文档); -1 = 反之
const DEFAULTS = { quality: 6, padH: 195, hints: true, scrollDir: 1, scrollSpeed: 1.0,
                   edgeNudge: false, dockFixVer: 0, keybar: false, showPing: true,
                   // 输入: 投递方式 + 攒批时间(越大=每次请求带的字越多=越省流量, 但手感稍迟)
                   typeMode: 'auto', typeBatch: 80,
                   speed: 1,          // 触控板指针速度档位
                   keepAwake: true,   // 手机端屏幕常亮
                   bgKeep: false,     // 切后台时保持连接
                   fsWheel: false };  // 全屏右侧的半透明滚轮钮(下次进全屏自动恢复)
const S = Object.assign({}, DEFAULTS,
  JSON.parse(localStorage.getItem('meow_vnc') || '{}'));
// 兼容旧版本存的 natural(语义与 scrollDir 相反)
if (S.scrollDir === undefined && typeof S.natural === 'boolean') {
  S.scrollDir = S.natural ? 1 : -1;
}
delete S.natural;
const saveS = () => localStorage.setItem('meow_vnc', JSON.stringify(S));

const QUALITY_MAP = { 3: 5, 6: 2, 9: 1 };   // qualityLevel -> compressionLevel

// ================= 连接管理(自动重连) =================
let rfb = null;
let connected = false;
let wantConnected = true;
let reconnectDelay = 1000;
let connectedAt = 0;
let discoTimer = 0;        // 断线后延迟弹提示的计时器(抖动已恢复就不弹)
let everConnected = false; // 连上过一次之后, 重连过程就不再弹中央提示
let reconnectTimer = 0;
let hideTimer = 0;

// ---- 图传实际帧率/码率 ----
// 为什么要显示这个: "延迟 <50ms 但画面很卡"是很常见的情况 —— 延迟量的是
// HTTP 往返(几毫秒级), 而卡不卡取决于**每秒真正收到多少帧、多少数据**。
// 两者是完全不同的指标, 只显示延迟会让人误判。
let wsRxBytes = 0;          // 收到的总字节(累加)
let fbFrames = 0;           // FramebufferUpdate 次数(累加)
let rateAt = 0, rateBytes0 = 0, rateFrames0 = 0;

function connect() {
  clearTimeout(reconnectTimer);
  if (rfb) { try { rfb.disconnect(); } catch (e) {} rfb = null; }
  connected = false;
  if (!everConnected) {
    // 首次连接才在中央提示; 重连时不弹 —— 否则每次抖动都会闪一下中央文字
    overlay.style.display = 'flex';
    overlayMsg.className = '';
    overlayMsg.textContent = '正在连接远程桌面…';
  } else {
    overlay.style.display = 'none';
  }
  $('vnc-reconnect').style.display = 'none';
  setStatus(everConnected ? '重连中…' : '连接中…');
  releaseOverlay();

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  // 统计码率: 临时包一层 WebSocket 数收到的字节数 —— noVNC 在构造 RFB 时
  // 就会建连, 所以只在这几行里替换, 建完立刻还原, 不影响别的地方。
  const NativeWS = window.WebSocket;
  function CountingWS(...a) {
    const w = new NativeWS(...a);
    w.addEventListener('message', (ev) => {
      const d = ev.data;
      if (typeof d === 'string') wsRxBytes += d.length;
      else if (d && d.byteLength) wsRxBytes += d.byteLength;
    });
    return w;
  }
  CountingWS.prototype = NativeWS.prototype;
  CountingWS.CONNECTING = 0;
  CountingWS.OPEN = 1;
  CountingWS.CLOSING = 2;
  CountingWS.CLOSED = 3;
  let r;
  try {
    window.WebSocket = CountingWS;
    r = new RFB($('screen'), `${proto}://${location.host}/ws/vnc`, { shared: true });
  } finally {
    window.WebSocket = NativeWS;
  }
  rfb = r;
  // 数帧: noVNC 每处理一条 FramebufferUpdate 就调一次 _framebufferUpdate。
  // 属于内部方法, 版本换了可能失效 —— 所以套 try, 失效就只显示码率。
  try {
    const origFb = r._framebufferUpdate.bind(r);
    r._framebufferUpdate = function () { fbFrames++; return origFb(); };
  } catch (e) { /* 无所谓 */ }
  r.scaleViewport = true;
  r.background = '#000000';
  // 别让 noVNC 在点图传时把焦点抢到 canvas 上 —— 那会让隐藏输入框失焦、
  // 手机软键盘直接收起, 表现为"打着字点一下图传键盘就没了"。
  r.focusOnClick = false;
  applyQuality();

  r.addEventListener('connect', () => {
    if (rfb !== r) return;
    connected = true;
    connectedAt = Date.now();
    everConnected = true;
    clearTimeout(discoTimer);          // 抖动已恢复, 不用弹提示了
    overlay.style.display = 'none';
    $('vnc-reconnect').style.display = 'none';
    setStatus('已连接');
    virtInit();
    $('pad-cursor').style.display = 'block';
    startPing();                       // 右上角实时延迟
  });

  r.addEventListener('disconnect', (e) => {
    if (rfb !== r) return;
    connected = false;
    releaseHold();          // 断连时必须松开按住的 Alt, 否则远端修饰键卡死
    const clean = e.detail && e.detail.clean;
    // **先别弹中央提示**。手机锁屏/切网络/切后台常常只造成几秒抖动, 一断就在
    // 画面正中冒出"已断开"会一闪一闪的, 看着像故障(用户反馈)。
    // 只把小字状态改掉; 真的 5s 还没连上才在中央提示。
    clearTimeout(discoTimer);
    // 顺便把断开原因记在状态栏, 下次出问题能直接看出是哪一类
    const why = e.detail && e.detail.clean === false ? '异常断开' : '连接关闭';
    setStatus(`重连中…(${why})`);
    discoTimer = setTimeout(() => {
      if (connected) return;
      overlay.style.display = 'flex';
      overlayMsg.className = clean ? '' : 'err';
      overlayMsg.textContent = clean ? '已断开' : '连接断开(远程桌面服务可能未启动)';
      $('vnc-reconnect').style.display = 'inline-block';
      setStatus('未连接');
    }, 5000);
    $('pad-cursor').style.display = 'none';
    releaseOverlay();
    stopPing();
    renderAudio();
    if (wantConnected && !document.hidden) scheduleReconnect();
  });

  // 远端剪贴板变化 -> 同步到面板
  r.addEventListener('clipboard', (e) => {
    if (rfb !== r) return;
    onRemoteClipboard((e.detail && e.detail.text) || '');
  });
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  // 只有"连上并撑住 5 秒"才算成功, 才把间隔退回 1 秒。
  // 否则服务端一崩就是"连上->秒断->重置成1秒->再连"的死循环, 每秒轰炸一次
  // —— 手机看到的就是隔 1 秒闪一次重连, 桌面也被这些连接拖得更卡。
  if (connectedAt && Date.now() - connectedAt > 5000) reconnectDelay = 1000;
  connectedAt = 0;
  const d = reconnectDelay;
  reconnectDelay = Math.min(Math.round(reconnectDelay * 1.8), 15000);
  overlayMsg.className = '';
  overlayMsg.textContent = `连接断开, ${Math.round(d / 1000)} 秒后自动重连…`;
  reconnectTimer = setTimeout(connect, d);
}

$('vnc-reconnect').addEventListener('click', () => { wantConnected = true; connect(); });

// 切后台就主动断开: 手机锁屏/切走时 WebSocket 常常不干净地死掉, 会在 x11vnc 侧
// 留下僵尸客户端拖慢整个共享更新循环(这是"用一会儿就卡/连不上"的主因之一)。
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPing();
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (S.bgKeep) return;             // 设置里选了"切后台仍保持连接"
      wantConnected = false;
      clearTimeout(reconnectTimer);
      if (rfb) { try { rfb.disconnect(); } catch (e) {} }
    }, 4000);
  } else {
    clearTimeout(hideTimer);
    wantConnected = true;
    if (!connected) connect(); else startPing();
  }
});
window.addEventListener('pagehide', () => {
  wantConnected = false;
  clearTimeout(reconnectTimer);
  if (rfb) { try { rfb.disconnect(); } catch (e) {} }
});

// ================= 右上角实时延迟 =================
// 用 /api/health 做一次极小请求测往返(HTTP RTT)。
// 为什么不测 VNC 那条 WebSocket: 它是原始 RFB 字节流, 塞自定义消息会污染协议;
// 而延迟的大头本来就是网络往返, HTTP RTT 完全能代表, 顺带还验证了服务端活着。
// 5s 一次 + 指数平滑(避免数字乱跳); 点一下立刻重测。
let pingEma = 0, pingTimer = 0, pingBusy = false;

// 2026-09-22: 这一栏原来是"网络延迟(HTTP 往返)" —— 换成**音频端到端延时**。
// 理由: 网络往返只有几毫秒, 对听感/观感没有解释力; 真正决定体验的是"声音从电脑
// 到耳朵、画面从电脑到眼睛"各要多久。画面那栏保留(见 picLatency), 网络 RTT 退到
// 内部: 仍然每 5s 量一次, 只作为画面延时里"网络单程"那一项的输入, 不再单独显示。
//
// 音频延时的算法(客户端唯一能测到的口径):
//   手机播放器里积压的量 = buffered.end(最后一块已收到的数据) - currentTime(正在播的位置)
//   再加上电脑侧那一段 = 采集分片 85ms + 一帧 mp3 24ms + 转发(实测服务端不积压:
//   9.00 秒音频正好对应 9.0 秒墙钟, 零丢包), 合计按 0.25s 计。
// 所以这个数大 = 手机播放器为了不断流自己攒得多 —— 越稳越慢, 是个取舍, 不是故障。
const AUDIO_CHAIN_S = 0.25;
let audioEma = 0, audioShownS = 0, audioShownExact = false;

// ---- "实际延时"的测法: 把两边的时间轴对齐 --------------------------------
// 服务端知道自己这一路流已经发出多少秒音频(/api/audio/pos, 与墙钟严格 1:1),
// 手机知道自己正在播第几秒(audioEl.currentTime)。两者的差, 就是"现在耳朵里
// 听到的这段声音, 是多久以前从电脑发出的" —— 也就是**真实端到端延时**,
// 手机侧的缓冲、排队、网络全都在里面了。
// 难点只在"起点对齐": 手机是**中途**挂上这条流的(采集是共享的, 不会为谁重开),
// 所以元素时间轴的 0 点对应服务端流的第 aPos0 秒。aPos0 这样求: 第一次拿到
// 服务端位置 pos 时, 往回推"从开始播到现在经过的时间"即得。
let aPos0 = null, aT0 = 0, aGen = -1, aLastCur = 0, aPosTimer = 0, audioExact = false;

function nowMs() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now();
}

function audioArm() {
  aT0 = nowMs(); aPos0 = null; aLastCur = 0;
  if (aPosTimer) { clearInterval(aPosTimer); aPosTimer = 0; }
  aPosTimer = setInterval(pollAudioPos, 3000);   // 3s 足够: 期间用本机时钟插值
  pollAudioPos();
}

function audioDisarm() {
  if (aPosTimer) { clearInterval(aPosTimer); aPosTimer = 0; }
  aPos0 = null; aLastCur = 0; aGen = -1;
}

function pollAudioPos() {
  fetch('/api/audio/pos?v=ad1', { cache: 'no-store' }).then((r) => r.json()).then((d) => {
    if (!d || !d.ok || !d.on) return;
    if (aGen !== d.gen) { aGen = d.gen; audioArm(); return; }   // 服务端换了采集, 重新对齐
    if (aPos0 == null) aPos0 = d.pos - (nowMs() - aT0) / 1000;
  }).catch(() => {});
}

function audioActualS() {
  if (aPos0 == null) return null;
  try {
    const cur = audioEl.currentTime || 0;
    if (cur + 0.5 < aLastCur) { audioArm(); return null; }   // 播放器时间轴被重置(重连)
    aLastCur = cur;
    const d = aPos0 + (nowMs() - aT0) / 1000 - cur + AUDIO_CHAIN_S;
    if (!(d > -0.5 && d < 120)) { audioArm(); return null; } // 明显不合理 -> 重新对齐
    return d;
  } catch (e) {
    return null;
  }
}

function audioNow() {
  if (!audioOn || !audioEl || typeof audioEl.buffered === 'undefined') return null;
  try {
    const actual = audioActualS();               // 优先: 真实端到端(含手机侧)
    if (actual != null) { audioExact = true; return actual; }
    audioExact = false;                          // 拿不到服务端位置 -> 退成"约"值
    // 兜底(刚开始那一两秒还没对齐上): 至少把播放器里积压的量报出来
    const b = audioEl.buffered;
    if (!b || !b.length) return null;
    const lead = b.end(b.length - 1) - (audioEl.currentTime || 0);
    if (!isFinite(lead) || lead < 0 || lead > 30) return null;
    return lead + AUDIO_CHAIN_S;
  } catch (e) {
    return null;
  }
}

function renderAudio() {
  const el = $('vnc-ping');
  if (!el) return;
  const raw = S.showPing ? audioNow() : null;
  if (raw == null) {
    audioEma = 0; audioShownS = 0;
    el.textContent = '音频 --';
    el.className = 'ping ' + (audioOn && S.showPing ? 'warn' : 'bad');
    el.title = !S.showPing ? '已在设置里关掉了"右上角显示延迟"'
      : audioOn ? '正在听电脑声音, 播放器还没攒够数据(等一两秒)'
                : '没在听电脑声音 —— 右下角 🔊 开启后才会有这个数';
    return;
  }
  audioEma = audioEma ? audioEma * 0.5 + raw * 0.5 : raw;   // 平滑, 免得数字乱跳
  audioShownS = audioEma; audioShownExact = audioExact;
  // 拿不到服务端流位置时(网络不通/刚开)只能给播放器积压量, 加 ~ 表示是"约"
  el.textContent = '音频 ' + (audioExact ? '' : '~') + audioEma.toFixed(1) + 's';
  el.className = 'ping ' + (audioEma < 1.5 ? 'ok' : audioEma < 5 ? 'warn' : 'bad');
  const _cur = audioEl ? (audioEl.currentTime || 0) : 0;
  el.title = (audioExact ? '' : '⚠ 暂时拿不到服务端流位置(网络不通?), 下面这个带 ~ 的'
    + '数只含手机播放器里积压的量, 不含链路上排队的部分。\n')
    + '音频**实际**端到端延时: 服务端已发出到第 '
    + (aPos0 == null ? '?' : (_cur + raw - AUDIO_CHAIN_S).toFixed(1))
    + 's, 手机正在播第 ' + _cur.toFixed(1) + 's —— 差值就是"现在听到的声音是多久前'
    + '从电脑出来的", 手机侧的缓冲/排队/网络都已算进去, 再加电脑侧采集编码 '
    + AUDIO_CHAIN_S.toFixed(2) + 's。\n'
    + '数大主要是手机播放器为自己留的抗抖动缓冲: 攒得越多越不容易断, 但延时越大。'
    + '\n点一下立刻重测网络(只影响旁边那栏画面的估算)。';
}

// ---- 图传帧率 / 码率 / 画面延时 ----
// 每秒刷新一次。**卡不卡看帧率, 不看延迟**。
let lastKbs = 0, lastFps = 0;

// 画面延时是**估算**, 不是实测 —— 只有网络那一段是真量出来的:
//   网络单程            = HTTP RTT/2                         实测
//   服务端变更检测      = x11vnc -wait 5 -defer 5, 最坏 10ms  由服务单元参数决定
//   编码 + 手机解码渲染 = 约 15ms                             经验值, 没有接口可测
// 常数 25ms = 10 + 15。它只保证"量级对"(几十毫秒), 不是标定过的真值:
// 换台手机、换个码率, 那 15ms 就会变。真值只能拿高速摄像机拍屏幕。
const PIC_SERVER_MS = 10;      // x11vnc -wait 5 -defer 5 的上界
const PIC_CLIENT_MS = 15;      // JPEG 编码 + 手机解码合成, 经验值
const PIC_FIXED_MS = PIC_SERVER_MS + PIC_CLIENT_MS;

function picLatency() {
  return pingEma ? Math.round(pingEma / 2 + PIC_FIXED_MS) : null;
}

function renderFsInfo() {
  const el = $('vnc-fs-info');
  if (!el) return;
  const parts = [];
  if (connected) parts.push(lastFps.toFixed(0) + 'fps');
  if (lastKbs) {
    parts.push(lastKbs >= 1024 ? (lastKbs / 1024).toFixed(1) + 'MB/s'
                               : lastKbs.toFixed(0) + 'KB/s');
  }
  const _ad = audioShownS || audioNow();
  parts.push(_ad == null ? '音频 --'
    : '音频 ' + (audioShownExact ? '' : '~') + _ad.toFixed(1) + 's');
  const pic = picLatency();
  if (pic != null) parts.push('画面 ' + pic + 'ms');
  el.textContent = parts.join(' · ');
}

function renderRate(fps, kbs) {
  const el = $('vnc-rate');
  lastFps = fps;
  lastKbs = kbs;
  if (el) {
    if (!connected) {
      el.textContent = '画面 --';
      el.className = 'ping bad';
    } else {
      const f = fps >= 10 ? 0 : (fps >= 5 ? 1 : 2);
      const pic = picLatency();
      // 顶栏只放"画面 xx ms"。手机顶栏只有 ~390px, 原来那句
      // "41fps · 画面 33ms" 会把"返回/远程桌面"挤成**竖排单字**。
      // 帧率/码率不丢, 挪到 tooltip 和全屏信息栏里(那边一整行, 够宽)。
      el.textContent = pic == null ? '画面 --' : `画面 ${pic}ms`;
      el.title = `图传帧率 ${fps.toFixed(1)} fps，码率 ${kbs >= 1024
        ? (kbs / 1024).toFixed(2) + ' MB/s' : kbs.toFixed(0) + ' KB/s'}\n`
        + `画面延时估算 ≈ 网络单程 + ${PIC_FIXED_MS}ms`
        + `（服务端变更检测≤${PIC_SERVER_MS}ms + 编码/手机解码约${PIC_CLIENT_MS}ms，`
        + '后两项是经验值，不是实测）';
      el.className = 'ping ' + (f === 0 ? 'ok' : f === 1 ? 'warn' : 'bad');
    }
  }
  renderAudio();
  renderFsInfo();
}

setInterval(() => {
  const now = Date.now();
  if (!rateAt) { rateAt = now; rateBytes0 = wsRxBytes; rateFrames0 = fbFrames; return; }
  const dt = (now - rateAt) / 1000;
  if (dt < 0.5) return;
  const fps = (fbFrames - rateFrames0) / dt;
  const kbs = (wsRxBytes - rateBytes0) / 1024 / dt;
  rateAt = now; rateBytes0 = wsRxBytes; rateFrames0 = fbFrames;
  renderRate(fps, kbs);
}, 1000);

async function pingOnce() {
  if (pingBusy) return;
  pingBusy = true;
  const t0 = performance.now();
  try {
    await fetch('/api/health?t=' + Date.now(), { cache: 'no-store' });
    const ms = performance.now() - t0;
    pingEma = pingEma ? pingEma * 0.6 + ms * 0.4 : ms;
    renderRate(lastFps, lastKbs);      // 网络值只喂给画面那栏的估算, 不再单独显示
  } catch (e) {
    pingEma = 0;
    renderRate(lastFps, lastKbs);
  } finally {
    pingBusy = false;
  }
}

function startPing() {
  stopPing();
  renderAudio();
  if (!S.showPing) return;             // 关掉就只显示 --, 也不再去请求
  pingOnce();
  pingTimer = setInterval(pingOnce, 5000);
}
function stopPing() {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = 0; }
}
$('vnc-ping').addEventListener('click', () => { pingEma = 0; pingOnce(); });

// ================= 键盘 =================
// 2026-09-14 修复的 5 个问题:
//   1. 没有修饰键: 手机上无法发 Ctrl+C/V/Z、Alt+Tab、Shift+方向键。现在有粘滞
//      修饰键(Ctrl/Alt/Shift/Super)+ 快捷键条, 软键盘输入也吃修饰键。
//   2. 没有 Tab/Esc/方向键: 手机软键盘根本没有这些键, 而 keydown 映射只有在
//      能按到物理键时才有效 —— 等于完全发不出去。现在快捷键条里有。
//   3. 输入法重复上屏: compositionend 里发了字符并把 composing 置回 false,
//      但浏览器紧接着还会再派发一次 input(此时 composing 已是 false), 于是同一批
//      字符被发两遍。改成按"文本 + 时间窗"去重, 与事件先后顺序无关。
//   4. enterkeyhint="done" 让软键盘回车键变成"完成", 通常只收起键盘而不产生回车。
//      已改为 enter。
//   5. 点图传会抢走焦点: noVNC 给 canvas 设了 tabindex=-1 并在 mousedown/touchstart
//      上 focus() 它, 于是隐藏输入框失焦、软键盘收起、再打字就没反应。
//      现在关闭 noVNC 的 focusOnClick, 焦点完全由"键盘"按钮掌控。
function canSend() { return rfb && connected; }

const MODS = [
  { key: 'ctrl', label: 'Ctrl', keysym: 0xffe3, code: 'ControlLeft' },
  { key: 'alt', label: 'Alt', keysym: 0xffe9, code: 'AltLeft' },
  { key: 'shift', label: 'Shift', keysym: 0xffe1, code: 'ShiftLeft' },
  { key: 'super', label: 'Super', keysym: 0xffeb, code: 'MetaLeft' },
];
const modsActive = { ctrl: false, alt: false, shift: false, super: false };

const SPECIAL = {
  esc: [0xff1b, 'Escape'], tab: [0xff09, 'Tab'], enter: [0xff0d, 'Enter'],
  backspace: [0xff08, 'BackSpace'], delete: [0xffff, 'Delete'],
  up: [0xff52, 'ArrowUp'], down: [0xff54, 'ArrowDown'],
  left: [0xff51, 'ArrowLeft'], right: [0xff53, 'ArrowRight'],
  home: [0xff50, 'Home'], end: [0xff57, 'End'],
  pgup: [0xff55, 'PageUp'], pgdn: [0xff56, 'PageDown'],
};

// 粘滞修饰键: 按一次点亮, 下一次按键带上它, 用完自动熄灭
function clearMods() {
  for (const m of MODS) {
    if (modsActive[m.key]) {
      modsActive[m.key] = false;
      const b = document.querySelector(`#keybar [data-mod="${m.key}"]`);
      if (b) b.classList.remove('on');
    }
  }
}

function withMods(fn) {
  if (!canSend()) return;
  const held = MODS.filter((m) => modsActive[m.key]);
  for (const m of held) rfb.sendKey(m.keysym, m.code, true);
  fn();
  for (const m of held.slice().reverse()) rfb.sendKey(m.keysym, m.code, false);
  clearMods();
}

function tapKey(keysym, code) {
  withMods(() => {
    rfb.sendKey(keysym, code, true);
    rfb.sendKey(keysym, code, false);
  });
}

function keysymFor(cp) {
  if (cp === 10 || cp === 13) return 0xff0d;  // Return
  if (cp >= 0x20 && cp <= 0xff) return cp;    // Latin-1 直接映射
  if (cp < 0x20) return 0;                    // 其它控制字符无法映射
  return 0x01000000 | cp;                     // Unicode keysym 区段
}

function cleanText(s) { return String(s).replace(/[\r\n]/g, ''); }

// 非 ASCII(中文/emoji 等)**不能**走 RFB 的 keysym 通道。
// 实测定位: 打 64 个中文, 前端确实发出了 128 条 keyEvent, 但 X(xev) 只收到 16 个,
// 而且 keysym 全乱。原因是 x11vnc 收到 Unicode keysym 时必须临时重映射一个 keycode
// 才能落到 X, 连发时它跟不上, 就丢/串了 —— 这正是"输入长度有限制 / 打一半就没了"。
// 纯 ASCII 不受影响(实测 144/144 全到)。
// 所以非 ASCII 改走服务端 xdotool type(它自己做重映射且带间隔), 实测 9/9 全到。
let typeBuf = [], typeTimer = 0;

// 服务端键入失败时的兜底。
// **必须限长**: 这条路径是逐字经 RFB 发 keyEvent, 一次 8000 字就是 16000 条事件
// 灌进 x11vnc —— 服务端扛不住、客户端也会卡死(实测就是这么把机器拖死的)。
// 而且它对中文本来就有损(x11vnc 侧重映射跟不上), 长文本兜底毫无意义, 直接报错。
const FALLBACK_MAX = 40;

function keysymFallback(s) {
  // 纯 ASCII 经 RFB 发是"不可靠但不会污染", 且没有中文那种重映射问题,
  // 所以上限放宽些; 含非 ASCII 的一律严格限长, 发过去也只会变成乱码。
  const max = /[^\x20-\x7e]/.test(s) ? FALLBACK_MAX : 200;
  if (s.length > max) {
    if (typeof clipStatus === 'function') {
      clipStatus(`键入失败(${s.length} 字太长, 已放弃)`, true);
    }
    return;
  }
  for (const ch of s) {
    const ks = keysymFor(ch.codePointAt(0));
    if (ks) tapKey(ks, 'VoidSymbol');
  }
}

// 同一时刻只允许一个 /api/type 在飞。
// 服务端虽然已经把 xdotool 串行化了, 但前端若不自律, 后一批请求会在前一批
// 还没被处理时就入队 —— 队列里堆着的批次越多, 屏幕上出现得越晚, 手感就是
// "卡一下然后突出来一串"。这里卡住不发, 反而更快也更连贯。
let typeBusy = false;

// 我们自己改动过剪贴板后, x11vnc 会把变更广播回来。这段时间内一律忽略 ——
// 否则每敲一批字都会冒一条"剪贴板已变化", 纯噪音。
let selfClipUntil = 0;

// 键入失败一定要让用户看见。否则他以为自己没打上, 会反复重打, 越打越乱;
// 更糟的是旧代码会偷偷改走 RFB, 产出"上辈子打的字", 完全无法归因。
let typeErrTimer = 0;
function showTypeErr(msg) {
  const text = '键入失败: ' + msg;
  try { setStatus(text); } catch (e) { /* 页面还没初始化 */ }
  if (typeof clipStatus === 'function') clipStatus(text, true);
  clearTimeout(typeErrTimer);
  typeErrTimer = setTimeout(() => {
    try { setStatus(connected ? '已连接' : '未连接'); } catch (e) {}
  }, 4000);
}

function flushType() {
  clearTimeout(typeTimer);
  if (typeBusy) return;                  // 上一批还没落地; 回来时 .finally 会续上
  const s = typeBuf.join('');
  typeBuf = [];
  if (!s) return;
  typeBusy = true;
  const ac = new AbortController();
  // 15 秒足够本地 xdotool 敲完 normal 批次(5000 字约 10 秒)。别设太长 ——
  // 一次卡住期间 typeBuf 会一直攒, 攒够了再一次性发出去, 就是"上次的内容"。
  const to = setTimeout(() => ac.abort(), 15e3);
  fetch('/api/type', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: s, mode: S.typeMode || 'auto' }), signal: ac.signal,
  }).then((r) => r.json().then((j) => [r.status, j]).catch(() => [r.status, null]))
    .then(([st, j]) => {
      if (j && j.ok) return;
      // **绝不再静默回退到 RFB 直发。**
      // 那条路送出去的按键会被远端 fcitx 收进预编辑缓冲: 屏幕上什么都不显示,
      // 攒到某次提交时一次性冒出来 —— 用户看到的就是"打这个出来的是上次的内容"。
      // 中文回退过去更是直接变乱码。宁可明确报错, 也不要产出这种鬼影文字。
      const err = (j && (j.err || j.msg)) || `HTTP ${st}`;
      showTypeErr(err);
      // 唯一的例外: 完全没收到响应(网络断了, 服务端根本没处理), 且是纯 ASCII
      // 且很短 —— 这时 RFB 直发至少还有机会是对的。
      if (!j && !st && s.length <= 40 && !/[^\x20-\x7e]/.test(s)) keysymFallback(s);
    }).catch(() => {
      showTypeErr('网络异常, 未送达');
      if (s.length <= 40 && !/[^\x20-\x7e]/.test(s)) keysymFallback(s);
    }).finally(() => {
    clearTimeout(to);
    typeBusy = false;
    // /api/type 在 GUI 应用里是走剪贴板投递的, 等于我们改了剪贴板
    selfClipUntil = Date.now() + 2500;
    if (typeBuf.length) flushType();      // 排队期间又攒了新字, 接着发
  });
}

function queueType(s) {
  typeBuf.push(s);
  clearTimeout(typeTimer);
  // 攒一下, 一句话一次请求。间隔由设置面板的「输入响应」控制。
  typeTimer = setTimeout(flushType, Number(S.typeBatch) || 80);
}

// **所有文本一律走服务端 /api/type, 不再走 RFB 直发。**
// 原因: RFB 直发(tapKey/keysymFor)送出去的按键和真人敲键盘走同一条路, 会先被
// 远端 fcitx 截胡 —— 中文态下英文字母全进拼音候选框, 表现为"打错字/字被吞掉"。
// 服务端的 xdotool 通道会先用 fcitx-remote 把输入法捏成英文态再敲、打完还原,
// 是唯一可靠的路径。代价是每批多一次 HTTP 往返 + 80ms 攒批, 但正确 >> 快。
function sendText(s) {
  // **刻意不检查 canSend()**: 键入走的是 HTTP(/api/type -> 服务端 xdotool),
  // 本来就不需要 VNC 连着。而 VNC 一断就把输入静默丢掉, 是"打着打着没反应"的
  // 直接原因; 更糟的是重连后积攒的内容会一次性蹦出来, 看着像"打这个出上次的内容"。
  if (!s) return;
  queueType(s);
}

// ================= 隐藏输入框 -> 远端 (增量差分) =================
// 不再靠"哪个事件先来/内容一不一样"去猜, 而是**只认输入框里现在是什么**,
// 跟上次见到的做公共前缀差分: 被改掉的部分补退格, 新增的部分才发出去。
//
// 为什么必须这样: 手机输入法的事件顺序千变万化, 靠事件去重必漏。实测踩到的两次:
//   1) 打英文 recover -> compositionend 给 "recover"、紧接 input 给 "recover "
//      (多个尾随空格), 按整串比对就判成"没发过" -> recoverrecover
//   2) 打 refresh -> 字母先被逐条发出(r/e/f/l/e/r/e), 最后整个单词又被提交一次
// 差分天然幂等: 同样的内容再来一遍, 差出来是空的, 一个字都不会多发。
// 也不会吞重复字符(敲 `ll` / `-----` 每次都是纯新增, 照发)。
let composing = false;
let kbLast = '';                       // 上次同步时输入框的内容
let compGuardTimer = 0;

function kbSync() {
  const cur = cleanText(kb.value);
  if (cur === kbLast) return;          // 幂等: 内容没变就什么都不做
  const prev = kbLast;
  kbLast = cur;
  // 有些输入法提交完会把输入框清空。那不是"用户删光了"(真删除走 keydown 的
  // Backspace), 照差分算会补发一大串退格, 把刚上屏的字删掉 —— 直接当重置处理。
  if (!cur) { kbLast = ''; return; }
  let i = 0;
  const n = Math.min(prev.length, cur.length);
  while (i < n && prev[i] === cur[i]) i++;
  const removed = prev.length - i;
  const added = cur.slice(i);
  if (!removed && !added) return;
  // 退格要经 RFB, 断线时发不出去; 此时只补新增, 至少不把已上屏的内容弄乱
  if (canSend()) {
    for (let k = 0; k < removed; k++) sendSpecial('backspace');
  }
  if (added) sendText(added);
}

kb.addEventListener('compositionstart', () => {
  composing = true;
  // 兜底: 浏览器/输入法没给 compositionend 时 composing 会永久卡在 true,
  // 之后所有按键都被当"组合中间态"丢掉 —— 表现就是键盘突然没反应。
  clearTimeout(compGuardTimer);
  compGuardTimer = setTimeout(() => {
    if (!composing) return;
    composing = false;
    kbSync();
  }, 4000);
});

kb.addEventListener('compositionend', () => {
  clearTimeout(compGuardTimer);
  composing = false;
  // 不读 e.data: 它和输入框实际内容可能不一致(尤其自动更正时), 一律以 kb.value 为准
  kbSync();
});

kb.addEventListener('input', () => {
  if (composing) return;               // 组合中的中间态(拼音等)不上屏
  kbSync();                            // 回车交给 keydown 统一处理, 避免发两遍
});
kb.addEventListener('keydown', (e) => {
  if (composing || e.isComposing || e.keyCode === 229) return;
  const map = { Enter: 'enter', Backspace: 'backspace', Escape: 'esc', Tab: 'tab',
    ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
    Delete: 'delete', Home: 'home', End: 'end', PageUp: 'pgup', PageDown: 'pgdn' };
  const name = map[e.key];
  if (name) {
    e.preventDefault();                // 阻止软键盘再插入字符, 否则会发两遍
    sendSpecial(name);
  }
});

function sendSpecial(name) {
  const t = SPECIAL[name];
  if (!t) return;
  tapKey(t[0], t[1]);
}

// 快捷键条
document.querySelectorAll('#keybar [data-mod]').forEach((b) => {
  b.addEventListener('click', () => {
    const k = b.dataset.mod;
    modsActive[k] = !modsActive[k];
    b.classList.toggle('on', modsActive[k]);
  });
});
document.querySelectorAll('#keybar [data-key]').forEach((b) => {
  b.addEventListener('click', () => sendSpecial(b.dataset.key));
});
// 需要**按住**修饰键才成立的 combo, 值是"多久没动静就松手"(ms)。
// Alt+Tab 是典型: GNOME 的窗口切换器只在 Alt 保持按下期间存在, 松开 Alt 才提交。
// 旧实现 Alt↓ Tab↓ Tab↑ Alt↑ 一股脑连发, 切换器常常还没被抓住就结束了 ——
// 表现就是"点 Alt+Tab 没反应, 或者闪一下就没了"。
const HOLD_COMBO = { 'alt+tab': 900 };
const holdState = { combo: null, timer: 0 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function releaseHold() {
  const st = holdState;
  if (!st.combo) return;
  clearTimeout(st.timer);
  const [modk] = st.combo.split('+');
  const m = MODS.find((x) => x.key === modk);
  st.combo = null;
  if (m && canSend()) rfb.sendKey(m.keysym, m.code, false);   // 松开 = 提交切换
}

document.querySelectorAll('#keybar [data-combo]').forEach((b) => {
  b.addEventListener('click', async () => {
    if (!canSend()) return;
    const combo = b.dataset.combo;
    const [mod, key] = combo.split('+');
    const m = MODS.find((x) => x.key === mod);
    if (!m) return;
    let ks, code;
    if (SPECIAL[key]) { [ks, code] = SPECIAL[key]; }   // alt+tab / ctrl+esc 这类
    else { ks = keysymFor(key.codePointAt(0)); code = 'VoidSymbol'; }
    if (!ks) return;

    const hold = HOLD_COMBO[combo];
    if (!hold) {                                   // 普通 combo: 快按快放即可
      rfb.sendKey(m.keysym, m.code, true);
      rfb.sendKey(ks, code, true);
      rfb.sendKey(ks, code, false);
      rfb.sendKey(m.keysym, m.code, false);
      return;
    }

    clearMods();                                   // 别让粘滞修饰键混进来
    if (holdState.combo !== combo) {               // 首次按下: 按住修饰键
      if (holdState.combo) releaseHold();
      holdState.combo = combo;
      rfb.sendKey(m.keysym, m.code, true);
      await sleep(50);                             // 让 X 先认下 Alt
    }
    rfb.sendKey(ks, code, true);                   // 再点一次就再往后切一个
    await sleep(60);
    rfb.sendKey(ks, code, false);
    clearTimeout(holdState.timer);
    holdState.timer = setTimeout(releaseHold, hold);
  });
});

// 断连/切后台时必须把按住的键放掉, 否则远端 Alt 一直卡在按下状态
window.addEventListener('pagehide', releaseHold);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) releaseHold();
});

// Ctrl+Alt+Del
$('vnc-cad').addEventListener('click', () => { if (canSend()) rfb.sendCtrlAltDel(); });

// 所有按钮都不许抢焦点。
// 手机上一个 <button> 被点时, 默认行为是先把焦点从隐藏输入框挪到按钮上 ——
// 软键盘立刻收起。于是"点一下 Esc/Tab/Ctrl, 键盘就没了", 想连着输就得反复点开,
// 用起来就像"输入有长度上限"; 而且"键盘"按钮自己也会因为焦点已经不在输入框上,
// 而永远判断成"要打开", 点不掉。
// 在 mousedown 上 preventDefault 能阻止焦点转移, 且不影响 click 事件
// (在 pointerdown 上拦会把 click 一起吞掉, 这是踩过的坑)。
function noFocusSteal() {
  document.querySelectorAll('button').forEach((el) => {
    if (el.dataset.nofocus) return;
    el.dataset.nofocus = '1';
    el.addEventListener('mousedown', (e) => e.preventDefault());
  });
}

// 键盘按钮 = 显式开关软键盘。用显式状态而不是看 activeElement:
// 点按钮那一刻 activeElement 已经被浏览器挪走了, 靠它判断必然"关不掉"。
let kbOpen = false;
function syncKbBtn() { $('vnc-kbbtn').classList.toggle('on', kbOpen); }
function setKb(open) {
  kbOpen = open;
  if (open) kb.focus(); else kb.blur();
  syncKbBtn();
}
$('vnc-kbbtn').addEventListener('click', () => setKb(!kbOpen));
kb.addEventListener('focus', () => { kbOpen = true; syncKbBtn(); });
kb.addEventListener('blur', () => {
  kbOpen = false;
  syncKbBtn();
  clearMods();
  // 输入法组合中途失焦 => 不会再收到 compositionend, 必须在这里收尾。
  // 之后把输入框清空, kbLast 也要跟着归零 —— 否则下次聚焦时差分会把整段旧内容
  // 当成"被删掉", 补发一大串退格。
  clearTimeout(compGuardTimer);
  if (composing) {
    composing = false;
    kbSync();
  }
  kb.value = '';
  kbLast = '';
});

// 快捷键条显示/隐藏
$('vnc-keybtn').addEventListener('click', () => {
  S.keybar = !S.keybar;
  saveS();
  applyKeybar();
  window.dispatchEvent(new Event('resize'));
});

// ================= 指针注入原语 =================
// RFB 按钮位: bit0 左 / bit2 右 / bit3 滚轮上 / bit4 滚轮下 / bit5 滚轮左 / bit6 滚轮右
const BTN_LEFT = 0x1, BTN_RIGHT = 0x4;
const WHEEL_UP = 0x8, WHEEL_DOWN = 0x10, WHEEL_LEFT = 0x20, WHEEL_RIGHT = 0x40;

let mask = 0;                    // 当前按住的按钮(不含滚轮瞬时位)

const padPanel = $('pad-panel');
const padArea = $('pad-area');
const padCursor = $('pad-cursor');

function canvasEl() { return document.querySelector('#screen canvas'); }

// 画布矩形做缓存。
// 原来每次指针移动都要 getBoundingClientRect() 两三次, 而 placeCursor() 刚写过
// pad-cursor 的 style —— 读-写-读交替会强制同步重排(forced synchronous layout),
// 手机上每帧几十微秒到几百微秒, 是"手感发涩"的隐形来源。
// 现在只在真正会改变布局的时机失效重算, 并最多 400ms 兜底刷新一次
// (手机地址栏收放会改变视口高度, 光靠 resize 事件不一定抓得住)。
let cRect = null, cRectAt = 0;

function invalidateRect() { cRect = null; }

function canvasRect() {
  const now = performance.now();
  if (cRect && now - cRectAt < 400) return cRect;
  const c = canvasEl();
  if (!c) return null;
  cRect = c.getBoundingClientRect();
  cRectAt = now;
  return cRect;
}

function ready() {
  return rfb && connected && rfb._sock && rfb._display &&
         rfb._rfbConnectionState === 'connected';
}

// 虚拟光标(client CSS 坐标, 允许越出画布 12px 以便触到真实屏幕最边缘) -> 帧缓冲坐标
function toFb() {
  const d = rfb && rfb._display;
  const r = canvasRect();
  if (!d || !r) return null;
  const w = d.width, h = d.height;
  if (!w || !h) return null;
  let x = Math.round(d.absX(virt.x - r.left));
  let y = Math.round(d.absY(virt.y - r.top));
  // 关键: 上限放宽到 w/h(允许"越过帧缓冲一行/一列"), 而不是 w-1/h-1。
  // x11vnc 用 -scale 1/2 缩放时不会先夹客户端坐标, 而是按比例换算后交给 X 夹到
  // 屏幕范围内。实测: 客户端 y=960(帧缓冲高 960) -> 真实 y=1920 -> X 夹成 1919,
  // 正好是 Ubuntu Dock "驻留弹出"要求的最后一行像素; 而 y=959 只能到 1918, 差一像素
  // 就永远弹不出来。横向同理(x=1440 -> 2879)。
  // 所以贴边时多送一格, 远端就能真正触到屏幕最边缘 —— 不需要服务端帮忙。
  x = Math.max(0, Math.min(w, x));
  y = Math.max(0, Math.min(h, y));
  // 贴边锁(带滞回): 指针进入边缘 6px 就锁死到最边缘那一行/列, 退回 18px 才解锁。
  // 为什么需要锁: 远端 Dock 的弹出判定是"指针严格等于屏幕最后一行像素"
  // (docking.js: y == monitor.y + monitor.height - 1), 差一像素就永远不弹; 而且
  // "驻留"机制要求指针连续 250ms 不动, 期间任何位置变化都会取消(_cancelDockDwell)。
  // 手指停在边缘时不可避免会轻微抖动, 一抖就发出 1918 把刚建立的驻留打断 ——
  // 表现就是"得用力顶住才弹"。锁死之后边缘区内一律发最边缘坐标。
  // 贴边锁**只用于底边**, 顶/左/右边一律不吸附。
  // 它本来就是为 Ubuntu Dock 的驻留弹出做的(要求指针严格等于屏幕最后一行像素,
  // 手指抖动会打断驻留, 所以要把边缘区内的坐标锁死成那一行)。
  // 但推广到四条边会出大事: 最大化窗口的关闭按钮在**右上角**, 手指一进边缘区
  // 指针就被强行吸到 (x=w, y=0) 这个角, 而按钮实际在它下方几十像素 ——
  // 表现就是"窗口最大化后左键怎么点都关不掉"。顶/左/右边保持所见即所得的精度。
  if (virt.y < r.bottom - EDGE_EXIT) edgeLatch &= ~1;
  else if (virt.y >= r.bottom - EDGE_ENTER) edgeLatch |= 1;

  if (edgeLatch & 1) y = h;
  return { x, y };
}

function sendPointer(m) {
  if (!ready()) return;
  const p = toFb();
  if (!p) return;
  RFB.messages.pointerEvent(rfb._sock, p.x, p.y, m & 0xff);
}

// 移动节流: 一帧最多发一次, 触摸屏 pointermove 可达 120Hz, 全发会白占带宽
let moveQueued = false;
function queueMove() {
  if (moveQueued) return;
  moveQueued = true;
  requestAnimationFrame(() => { moveQueued = false; sendPointer(mask); });
}

function press(b) { mask |= b; sendPointer(mask); }
function release(b) { mask &= ~b; sendPointer(mask); }
function click(b) { press(b); release(b); }

function wheelStep(dirY, dirX) {
  if (!ready()) return;
  let bit = 0;
  if (dirY < 0) bit = WHEEL_UP; else if (dirY > 0) bit = WHEEL_DOWN;
  else if (dirX < 0) bit = WHEEL_LEFT; else if (dirX > 0) bit = WHEEL_RIGHT;
  if (!bit) return;
  sendPointer(mask | bit);
  sendPointer(mask);
}

// ================= noVNC 遮罩看门狗 =================
// 延迟到本轮事件派发结束之后才清理: 正常路径下 noVNC 自己已经收掉了(这里是空操作),
// 只有它漏收时才会兜底。用 setTimeout 而不是直接清理, 是为了不抢在 noVNC 的
// _captureProxy 之前把 window 监听摘掉 —— 那样反而会让按键抬起事件发不出去。
function releaseOverlay() {
  const el = document.getElementById('noVNC_mouse_capture_elem');
  if (document.captureElement) {
    try { releaseCapture(); } catch (e) {}
  }
  if (el && el.style.display !== 'none') el.style.display = 'none';
}
const overlayWatchdog = () => setTimeout(releaseOverlay, 0);
for (const t of ['pointerup', 'pointercancel', 'mouseup', 'touchend', 'touchcancel']) {
  window.addEventListener(t, overlayWatchdog, true);
}
window.addEventListener('blur', releaseOverlay);

// ================= 虚拟光标 =================
const SPEEDS = [1.0, 1.5, 2.1, 2.8];       // 低/中/高/极高(基准增益, 再叠加速度加速)
const SPEED_NAMES = ['低', '中', '高', '极高'];
let speedIdx = (Number.isInteger(S.speed) && S.speed >= 0 && S.speed < SPEEDS.length) ? S.speed : 1;   // 从设置恢复, 之前每次刷新都回到默认值
let lastMoveAt = 0;                        // 上一次单指移动的时间, 用来算滑动速度
const TAP_MAX_MOVE = 12;                   // 轻点判定: 累计位移(px)
const TAP_MAX_MS = 450;
const DBLTAP_MS = 300;                     // 双击间隔(第二击按住=拖拽)
const EDGE_M = 12;                         // 允许越出画布的距离, 保证能触到远端屏幕边缘
const EDGE_ENTER = 6;                      // 进入边缘区(px) -> 锁死到最边缘坐标
const EDGE_EXIT = 18;                      // 退出边缘区(px) -> 解锁(滞回, 防止抖动打断)
let edgeLatch = 0;                         // 边缘锁 位掩码: 1=下 2=上 4=左 8=右
const virt = { x: 0, y: 0, ok: false };
let dragLock = false;                      // 拖动锁(按钮切换, 常驻)
let tapDrag = false;                       // 双击后按住拖拽(手势, 自动释放)
let lastTapAt = 0;

function virtInit() {
  invalidateRect();
  const r = canvasRect();
  if (!r) return;
  virt.x = r.left + r.width / 2;
  virt.y = r.top + r.height / 2;
  virt.ok = true;
  placeCursor();
}

function virtClamp() {
  const r = canvasRect();
  if (!r) return;
  virt.x = Math.min(r.right + EDGE_M, Math.max(r.left - EDGE_M, virt.x));
  virt.y = Math.min(r.bottom + EDGE_M, Math.max(r.top - EDGE_M, virt.y));
}

function placeCursor() {
  if (!virt.ok) return;
  const r = canvasRect();
  if (!r) return;
  padCursor.style.left = Math.min(r.right, Math.max(r.left, virt.x)) + 'px';
  padCursor.style.top = Math.min(r.bottom, Math.max(r.top, virt.y)) + 'px';
}

window.addEventListener('resize', () => {
  invalidateRect(); virtClamp(); placeCursor(); edgeUpdate();
});
window.addEventListener('orientationchange', () => {
  invalidateRect(); setTimeout(() => { invalidateRect(); virtClamp(); placeCursor(); }, 300);
});

$('vnc-padbtn').addEventListener('click', () => {
  // 触控板常驻(默认开); 收起时图传占满, 通知 noVNC 重新适配尺寸
  const hide = padPanel.classList.toggle('hide');
  $('vnc-padbtn').classList.toggle('on', !hide);
  window.dispatchEvent(new Event('resize'));
  if (!hide) virtInit();
});

// ================= 滚动(带惯性) =================
// 远端只认"滚轮档位", 没有连续滚动量。所以自己做累加器 + 动量:
//   手指位移 -> 换算成档位 -> 抬手后按末速度继续衰减发射, 得到笔记本触控板的惯性手感。
const BASE_STEP = 20;                      // 1.0 倍速时: 20px 手指位移 = 1 档
const SCROLL = { acc: 0, accX: 0, vel: 0, velX: 0, lastT: 0, raf: 0 };

function scrollStepPx() { return BASE_STEP / Math.max(0.3, S.scrollSpeed); }

function drainScroll() {
  const step = scrollStepPx();
  // 每帧每个方向最多发 4 档(每档 2 条消息)。一帧灌几十条小消息会让手机端
  // WebSocket 发送队列反复堆积再清空, 表现就是滑动时一顿一顿。
  const CAP = 4;
  let n = 0;
  while (SCROLL.acc >= step && n < CAP) { wheelStep(1, 0); SCROLL.acc -= step; n++; }
  while (SCROLL.acc <= -step && n < CAP) { wheelStep(-1, 0); SCROLL.acc += step; n++; }
  n = 0;
  while (SCROLL.accX >= step && n < CAP) { wheelStep(0, 1); SCROLL.accX -= step; n++; }
  while (SCROLL.accX <= -step && n < CAP) { wheelStep(0, -1); SCROLL.accX += step; n++; }
  // 累加器封顶, 免得手指停下后还在继续滚(拖尾太长会像"失控")
  const lim = step * 6;
  SCROLL.acc = Math.max(-lim, Math.min(lim, SCROLL.acc));
  SCROLL.accX = Math.max(-lim, Math.min(lim, SCROLL.accX));
}

// 滚轮档位也按帧合并发送。
// 双指滑动时 pointermove 能到 120Hz, 每个事件又要发好几档(每档 2 条 RFB 消息),
// 直接就地 drain 会在极短时间内灌进几十条小消息 —— 手机端 WebSocket 发送队列
// 反复堆积再清空, 表现出来就是"双指滑动一顿一顿的"。合并到每帧一次就平顺了。
let drainQueued = false;
function scheduleDrain() {
  if (drainQueued) return;
  drainQueued = true;
  requestAnimationFrame(() => { drainQueued = false; drainScroll(); });
}

function cancelMomentum() {
  if (SCROLL.raf) { cancelAnimationFrame(SCROLL.raf); SCROLL.raf = 0; }
  SCROLL.vel = 0; SCROLL.velX = 0;
}

function startMomentum() {
  cancelMomentumKeepAcc();
  const v = SCROLL.vel, vx = SCROLL.velX;
  if (Math.abs(v) < 0.12 && Math.abs(vx) < 0.12) { SCROLL.acc = 0; SCROLL.accX = 0; return; }
  let last = performance.now();
  const tick = (now) => {
    const dt = Math.min(now - last, 40); last = now;
    SCROLL.acc += v * dt;
    SCROLL.accX += vx * dt;
    drainScroll();
    SCROLL.vel *= Math.pow(0.93, dt / 16.7);
    SCROLL.velX *= Math.pow(0.93, dt / 16.7);
    if (Math.abs(SCROLL.vel) >= 0.03 || Math.abs(SCROLL.velX) >= 0.03) {
      SCROLL.raf = requestAnimationFrame(tick);
    } else {
      SCROLL.raf = 0; SCROLL.vel = 0; SCROLL.velX = 0;
      SCROLL.acc = 0; SCROLL.accX = 0;
    }
  };
  SCROLL.raf = requestAnimationFrame(tick);
}
function cancelMomentumKeepAcc() {
  if (SCROLL.raf) { cancelAnimationFrame(SCROLL.raf); SCROLL.raf = 0; }
}

// ================= 触控板手势 =================
// 1 指: 移动 / 轻点=左键 / 双击后按住=拖拽
// 2 指: 上下滑=滚轮 / 左右滑=横向滚轮 / 双指点按=右键
const pts = new Map();            // pointerId -> {x, y}
let gesture = null;               // {maxPts, move, t0, scrolled}

padArea.addEventListener('pointerdown', (e) => {
  if (padPanel.classList.contains('hide')) return;
  try { padArea.setPointerCapture(e.pointerId); } catch (err) {}
  pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (!gesture) gesture = { maxPts: 1, move: 0, t0: Date.now(), scrolled: false };
  gesture.maxPts = Math.max(gesture.maxPts, pts.size);

  if (pts.size === 1) {
    cancelMomentum();
    SCROLL.acc = 0; SCROLL.accX = 0;
    SCROLL.vel = 0; SCROLL.velX = 0; SCROLL.lastT = performance.now();
    lastMoveAt = 0;
    // 笔记本触控板式"双击后按住=拖拽": 第二击落下时按住左键
    if (!dragLock && Date.now() - lastTapAt < DBLTAP_MS) {
      tapDrag = true;
      press(BTN_LEFT);
    }
  } else if (pts.size === 2) {
    // 第二根手指落下 => 这是滚动, 不是拖拽
    if (tapDrag) { tapDrag = false; release(BTN_LEFT); }
    gesture.move = 0;
    SCROLL.lastT = performance.now();
  }
  e.preventDefault();
});

padArea.addEventListener('pointermove', (e) => {
  const p = pts.get(e.pointerId);
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;
  if (!gesture) return;

  if (pts.size >= 2) {
    // 只认第一根手指的位移。
    // 双指时每根手指都会各自派发 pointermove, 两根一起累加 = 滚动量翻倍, 而且
    // 两根手指不可能完全同步, 逐帧差值一抖一抖 —— 表现就是"双指滑动一顿一顿的"。
    const firstId = pts.keys().next().value;
    if (e.pointerId !== firstId) return;
    // 双指滚动: 累积 + 估算速度供抬手后做惯性
    const now = performance.now();
    const dt = Math.max(1, now - SCROLL.lastT);
    SCROLL.lastT = now;
    const dir = S.scrollDir;
    const dAcc = (-dy * dir) * S.scrollSpeed;
    const dAccX = (-dx) * S.scrollSpeed;
    SCROLL.acc += dAcc;
    SCROLL.accX += dAccX;
    SCROLL.vel = SCROLL.vel * 0.6 + (dAcc / dt) * 0.4;
    SCROLL.velX = SCROLL.velX * 0.6 + (dAccX / dt) * 0.4;
    gesture.scrolled = true;
    gesture.move += Math.abs(dx) + Math.abs(dy);
    cancelMomentumKeepAcc();
    scheduleDrain();
  } else {
    gesture.move += Math.abs(dx) + Math.abs(dy);
    if (!virt.ok) virtInit();
    // 指针加速: 慢速微调用基准增益, 快速甩动按比例放大。
    // 这是笔记本触控板手感的关键 —— 没有它只能二选一: 要么增益低到够不到屏幕
    // 角落, 要么增益高到轻轻一碰指针就飞。
    const now = performance.now();
    const dt = Math.max(4, now - (lastMoveAt || now));
    lastMoveAt = now;
    const sp = Math.hypot(dx, dy) / dt;               // px/ms
    const acc = 1 + Math.min(1.9, sp * 0.55);         // 最快约 2.9 倍
    virt.x += dx * SPEEDS[speedIdx] * acc;
    virt.y += dy * SPEEDS[speedIdx] * acc;
    virtClamp();
    placeCursor();
    queueMove();
    edgeUpdate();
  }
});

function padUp(e) {
  if (!pts.has(e.pointerId)) return;
  pts.delete(e.pointerId);
  if (pts.size > 0) {
    // 抬起一根手指后重新基线, 避免剩下那根手指把残余位移当成移动
    if (gesture) { gesture.move = 0; gesture.t0 = Date.now(); }
    SCROLL.lastT = performance.now();
    return;
  }
  if (!gesture) return;
  const g = gesture;
  gesture = null;
  const quick = Date.now() - g.t0 < TAP_MAX_MS;

  if (tapDrag) {                          // 双击拖拽结束: 松开左键, 不产生点击
    tapDrag = false;
    release(BTN_LEFT);
  } else if (g.scrolled) {
    startMomentum();                      // 双指滚动抬手 -> 惯性继续
  } else if (g.maxPts === 1 && g.move < TAP_MAX_MOVE && quick && !dragLock) {
    click(BTN_LEFT);                      // 轻点=左键
    lastTapAt = Date.now();
  } else if (g.maxPts === 2 && g.move < TAP_MAX_MOVE && quick) {
    click(BTN_RIGHT);                     // 双指点按=右键
  } else if (g.maxPts === 1) {
    cancelMomentum(); SCROLL.acc = 0; SCROLL.accX = 0;
  }
}
padArea.addEventListener('pointerup', padUp);
padArea.addEventListener('pointercancel', padUp);

// ================= 触控板下方按键 =================
$('pad-l').addEventListener('pointerdown', (e) => { e.preventDefault(); click(BTN_LEFT); });
$('pad-r').addEventListener('pointerdown', (e) => { e.preventDefault(); click(BTN_RIGHT); });

// 滚轮键支持长按连发(翻长文档不用一直戳)
function holdRepeat(el, fn) {
  let t = 0, iv = 0;
  const stop = () => { clearTimeout(t); clearInterval(iv); t = 0; iv = 0; };
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    fn();
    t = setTimeout(() => { iv = setInterval(fn, 80); }, 400);
  });
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) {
    el.addEventListener(ev, stop);
  }
  window.addEventListener('blur', stop);
}
holdRepeat($('pad-wu'), () => wheelStep(-1, 0));
holdRepeat($('pad-wd'), () => wheelStep(1, 0));

$('pad-drag').addEventListener('click', () => {
  dragLock = !dragLock;
  $('pad-drag').classList.toggle('on', dragLock);
  if (dragLock) press(BTN_LEFT); else release(BTN_LEFT);
});
$('pad-speed').addEventListener('click', () => {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  S.speed = speedIdx; saveS();
  $('pad-speed').textContent = '速度 ' + SPEED_NAMES[speedIdx];
});

// ================= 边缘助弹(Dock / 热区) =================
// 为什么要服务端帮忙: x11vnc 用 -scale 1/2 缩放帧缓冲, 客户端能寻址的最后一行
// 反算回真实屏幕差 1~2px; 而 Ubuntu Dock 的"驻留弹出"判定是**严格等于**屏幕最后
// 一行 (docking.js: y == monitor.y + monitor.height - 1)。差一个像素就永远弹不出来。
// 所以指针贴边时让服务端用 xdotool 把真实指针钉到精确的最后一行像素。
let edgeTimer = 0, edgeSince = 0, edgeName = null;

function edgeUpdate() {
  if (!S.edgeNudge || !ready() || !virt.ok) return stopEdge();
  const r = canvasRect();
  if (!r) return stopEdge();
  let edge = null;
  if (virt.y >= r.bottom - 1.5) edge = 'bottom';
  else if (virt.y <= r.top + 1.5) edge = 'top';
  else if (virt.x <= r.left + 1.5) edge = 'left';
  else if (virt.x >= r.right - 1.5) edge = 'right';
  if (!edge) return stopEdge();

  if (edgeTimer && edgeName === edge) return;
  stopEdge();
  edgeName = edge;
  edgeSince = Date.now();
  const tick = () => {
    if (!ready()) return stopEdge();
    if (Date.now() - edgeSince > 5000) return stopEdge();   // 贴太久就别一直钉了
    if (dragLock || tapDrag || (mask & 0xff)) return;       // 正在拖拽/按住, 不抢指针
    const p = toFb();
    if (!p) return;
    fetch('/api/edge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edge, fx: p.x, fy: p.y, fbw: rfb._display.width,
                             fbh: rfb._display.height }),
    }).catch(() => {});
  };
  tick();
  edgeTimer = setInterval(tick, 300);
}

function stopEdge() {
  if (edgeTimer) { clearInterval(edgeTimer); edgeTimer = 0; }
  edgeName = null;
}

// ================= 剪贴板面板 =================
// 2026-09-14 修复:
//   1. 中文变问号 —— noVNC 的 clipboardPasteFrom 普通路径只支持 ISO 8859-1
//      (core/rfb.js 写死 `if (code > 0xff) code = 0x3f`), x11vnc 又不支持扩展
//      剪贴板协议, 所以走这条路中文必然全是 '?'。现在改为服务端用 xclip 直接设
//      X 选区, 与 VNC 协议无关, 任意 Unicode 都正确。
//   2. 粘贴粘出旧内容 —— 旧代码先 rfb.clipboardPasteFrom() 再立刻 fetch
//      /api/paste, 两条路异步, Ctrl+V 常在选区换过来之前就按下去了。
//      现在"设剪贴板 + 按 Ctrl+V"由服务端一次做完(读回确认后才按键)。
//   3. 只能写不能读 —— 新增"读取远程剪贴板", 并监听 noVNC 的 clipboard 事件自动同步。
//   4. 终端类程序(xterm)不响应 Ctrl+V, 新增"直接键入"(xdotool type)。
const clipText = $('clip-text');
const clipMsg = $('clip-msg');

function clipStatus(msg, bad) {
  clipMsg.textContent = msg;
  clipMsg.style.color = bad ? 'var(--bad)' : 'var(--dim)';
}

async function clipApi(path, body) {
  const opt = body === undefined
    ? { method: 'GET' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  // 必须带超时。fetch 默认永不超时: 手机锁屏/网络一断, 请求就一直挂着,
  // 界面永远停在"粘贴中…", 看着跟卡死一样 —— 实测到的"粘贴卡死"就是这个。
  const ac = new AbortController();
  const to = setTimeout(() => ac.abort(), 30e3);
  if (path === '/api/paste' || (path === '/api/clipboard' && body)) {
    selfClipUntil = Date.now() + 2500;      // 这次改动是我们引起的, 别再广播回来
  }
  try {
    opt.signal = ac.signal;
    const res = await fetch(path, opt);
    const txt = await res.text();
    try { return JSON.parse(txt); } catch (e) {
      return { ok: false, err: '服务返回异常(可能已登出)' };
    }
  } finally {
    clearTimeout(to);
  }
}

async function clipRun(label, path, body, done) {
  clipStatus(label + '…');
  try {
    const j = await clipApi(path, body);
    clipStatus(j.ok ? done : ('失败: ' + (j.err || j.msg || '未知错误')), !j.ok);
  } catch (e) {
    clipStatus('失败: 服务未响应', true);
  }
}

$('vnc-clipbtn').addEventListener('click', () => {
  const p = $('clip-panel');
  const show = p.style.display === 'none';
  p.style.display = show ? 'block' : 'none';
  // 面板开着就把按钮点亮(和键盘/触控板/设置按钮一致)
  $('vnc-clipbtn').classList.toggle('on', p.style.display === 'block');
  if (show) clipStatus('可读取/写入远端剪贴板');
});
$('clip-close').addEventListener('click', () => { $('clip-panel').style.display = 'none'; });

$('clip-get').addEventListener('click', async () => {
  clipStatus('读取中…');
  try {
    const j = await clipApi('/api/clipboard');
    if (!j.ok) { clipStatus('读取失败: ' + (j.err || ''), true); return; }
    clipText.value = j.text || '';
    if (j.text) { clipText.focus(); clipText.select(); }
    clipStatus(j.text ? `已读取 ${j.text.length} 字(长按可复制到手机)` : '远端剪贴板为空');
  } catch (e) { clipStatus('读取失败: 服务未响应', true); }
});

$('clip-set').addEventListener('click', () => {
  const text = clipText.value;
  if (!text) { clipStatus('内容为空', true); return; }
  clipRun('写入中', '/api/clipboard', { text }, '已写入远端剪贴板');
});

$('clip-paste').addEventListener('click', () => {
  const text = clipText.value;
  clipRun('粘贴中', '/api/paste', text ? { text } : {}, '已发送 Ctrl+V');
});

$('clip-type').addEventListener('click', () => {
  const text = clipText.value;
  if (!text) { clipStatus('内容为空', true); return; }
  clipRun('键入中', '/api/type', { text }, '已直接键入(终端推荐)');
});

// 远端剪贴板变化时自动同步(noVNC 的 clipboard 事件, 来自 ServerCutText)
// 远端剪贴板变化。**不能直接用 noVNC 带来的 text 填面板**: 它走 RFB ServerCutText,
// 而 noVNC 只支持 ISO 8859-1(非拉丁字符一律变 '?'), x11vnc 又不支持扩展剪贴板
// 协议 —— 直接拿来用就是"剪贴板乱码"的源头。
// 这里只把它当"变化通知"; 要内容就点『读取』, 那条路走服务端 xclip 直读选区,
// 是完整 UTF-8。不自动拉取是刻意的: 我们自己每敲一批字都会改一次剪贴板,
// 自动拉取会变成"自己触发自己"的高频请求。
function onRemoteClipboard(text) {
  if (!text) return;
  if (Date.now() < selfClipUntil) return;
  if (clipText.value && document.activeElement === clipText) return;  // 用户正编辑, 别打扰
  clipStatus('远端剪贴板已变化, 点『读取』获取完整内容');
}

// ================= 设置面板 =================
function applyQuality() {
  if (!rfb) return;
  rfb.qualityLevel = S.quality;
  rfb.compressionLevel = QUALITY_MAP[S.quality] ?? 2;
}
function applyPadH() {
  $('pad-area').style.height = S.padH + 'px';
  $('set-padh-v').textContent = S.padH + 'px';
}
function applyHints() {
  document.querySelector('#pad-area .hint').style.display = S.hints ? '' : 'none';
}
function applyKeybar() {
  $('keybar').classList.toggle('hide', !S.keybar);
  $('vnc-keybtn').classList.toggle('on', S.keybar);
}
function applyScrollUI() {
  $('set-scroll').value = Math.round(S.scrollSpeed * 10);
  $('set-scroll-v').textContent = S.scrollSpeed.toFixed(1) + 'x';
  document.querySelectorAll('#set-scrolldir button').forEach((b) =>
    b.classList.toggle('on', Number(b.dataset.d) === S.scrollDir));
}

applyPadH();
applyHints();
applyScrollUI();
applyKeybar();
$('set-padh').value = S.padH;
$('set-hints').checked = S.hints;
$('set-ping').checked = S.showPing;
$('set-edge').checked = S.edgeNudge;
$('set-awake').checked = S.keepAwake;
$('set-bgkeep').checked = S.bgKeep;
applyTypeUI();
applyAwake();                       // 异步, 不 await; 失败也不影响其它初始化
document.querySelectorAll('#set-panel .seg.q button').forEach((b) => {
  b.classList.toggle('on', Number(b.dataset.q) === S.quality);
});

$('vnc-setbtn').addEventListener('click', () => {
  const p = $('set-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  // 持久性的开关要能看出"现在是开着的": 和剪贴板/键盘那些一样点亮(主题色铺满)
  $('vnc-setbtn').classList.toggle('on', p.style.display === 'block');
  if (p.style.display === 'block') {
    refreshDockState(); refreshVncClients(); refreshVolume(); refreshBrightness();
  }
});

// ---- 连接维护: 看当前有几个客户端, 一键清理并重启 ----
// x11vnc 是 -shared, 客户端越多共享更新循环越慢 -> "延迟正常但画面一直重连"。
// 手机锁屏/切网留下的半死连接要等 90s 读超时才消失, 这期间就一直在拖慢画面。
const elClients = $('vnc-clients');
const elClientsNote = $('vnc-clients-note');
const elReset = $('vnc-reset');

async function refreshVncClients() {
  if (!elClients) return;
  let n = null;
  try {
    const r = await fetch('/api/vnc/clients', { cache: 'no-store' });
    const j = await r.json();
    if (j.ok) n = j.clients;
  } catch (e) { /* 服务没起来就算了 */ }
  if (n === null || typeof n !== 'number') {
    elClients.textContent = '查询失败';
    elClients.className = '';
    return;
  }
  elClients.textContent = n + ' 个';
  elClients.className = n > 2 ? 'bad' : n > 1 ? 'warn' : '';
  if (elClientsNote) {
    elClientsNote.textContent = n > 1
      ? `检测到 ${n} 个客户端（正常只有你自己 1 个）。多出来的会拖慢画面、让画面反复重连。`
        + `同一时刻最多保留 ${2} 个：新的顶上来、旧的自动踢掉；` 
        + `实在清不掉的遗留连接会在约 1 分钟内被自动清理。也可以点下面的「清理并重启」立刻处理。`
      : 'x11vnc 是共享模式，连它的客户端越多，画面越卡、越容易反复重连。'
        + '正常只有你自己 1 个；同一时刻最多保留 2 个（新的顶上、旧的踢掉），'
        + '多出来的遗留连接会自动清理。';
  }
}

if (elReset) {
  elReset.addEventListener('click', async () => {
    elReset.disabled = true;
    elReset.textContent = '重启中…';
    if (elClientsNote) elClientsNote.textContent = '正在清理并重启控制台…';
    // 关键: **不能"探到 200 就 reload"**。服务是延迟 ~1.2s 才重启的, 700ms 时
    // 探到的 200 是**旧进程**在应答, 这时 reload 正好撞进重启窗口 —— 页面直接
    // 打不开, 只能硬刷新(用户报的 bug)。
    // 正确做法: 拿响应里的 boot 标识, 等 /api/health 报出**另一个** boot,
    // 那才说明新进程起来了。同时兼容拿不到 boot 的情况(退化成"先下去再上来")。
    let oldBoot = null;
    try {
      const r = await fetch('/api/vnc/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const j = await r.json().catch(() => null);
      if (j && j.boot) oldBoot = j.boot;
    } catch (e) { /* 服务已经重启了, 请求被掐断属正常 */ }

    let wentDown = false;
    for (let i = 0; i < 100; i++) {              // 最多等 50 秒
      await new Promise((r) => setTimeout(r, 500));
      let j = null;
      try {
        const r = await fetch('/api/health', { cache: 'no-store' });
        if (r.ok) j = await r.json().catch(() => null);
      } catch (e) { /* 还没起来 */ }
      if (!j) { wentDown = true; continue; }     // 服务已经下去了
      if (oldBoot) {
        if (j.boot && j.boot !== oldBoot) { location.reload(); return; }
        continue;                                // 还是旧进程, 继续等
      }
      if (wentDown) { location.reload(); return; }   // 拿不到 boot 时的兜底
    }
    elReset.disabled = false;
    elReset.textContent = '重启超时, 点此重试';
  });
}
$('set-close').addEventListener('click', () => { $('set-panel').style.display = 'none'; });

document.querySelectorAll('#set-panel .seg.q button').forEach((b) => {
  b.addEventListener('click', () => {
    S.quality = Number(b.dataset.q);
    saveS(); applyQuality();
    document.querySelectorAll('#set-panel .seg.q button').forEach((x) =>
      x.classList.toggle('on', x === b));
  });
});
document.querySelectorAll('#set-scrolldir button').forEach((b) => {
  b.addEventListener('click', () => {
    S.scrollDir = Number(b.dataset.d);
    saveS(); applyScrollUI();
  });
});
$('set-padh').addEventListener('input', (e) => {
  S.padH = Number(e.target.value);
  $('set-padh-v').textContent = S.padH + 'px';
  applyPadH();
  window.dispatchEvent(new Event('resize'));
  saveS();
});
$('set-scroll').addEventListener('input', (e) => {
  S.scrollSpeed = Number(e.target.value) / 10;
  $('set-scroll-v').textContent = S.scrollSpeed.toFixed(1) + 'x';
  saveS();
});

// ---- 电脑音量滑块(替代原来画面上的静音圆按钮) ----
// 值直接读写**电脑**的默认输出音量(pactl), 并持久化到服务端 config.json ——
// 所以换手机打开还是同一个值, 跟"输入方式"一样是电脑级设置。
// 超过 100% 显示红色(accent-color 重染滑块轨道+thumb, 标签也变红)。
const elVol = $('set-vol');
const elVolV = $('set-vol-v');
let volTimer = null;

function volLabel(pct) {
  const over = pct > 100;
  elVolV.textContent = pct + '%';
  elVolV.style.color = over ? 'var(--bad)' : '';
  if (elVol) elVol.style.accentColor = over ? 'var(--bad)' : '';
}

async function refreshVolume() {
  if (!elVol) return;
  try {
    const r = await fetch('/api/volume', { cache: 'no-store' });
    const j = await r.json();
    if (j && j.ok && typeof j.vol === 'number') {
      elVol.value = String(Math.max(0, Math.min(150, j.vol)));
      volLabel(Number(elVol.value));
    }
  } catch (e) { /* 服务没起就不动滑块 */ }
}

if (elVol) {
  elVol.addEventListener('input', (e) => {
    const pct = Number(e.target.value);
    volLabel(pct);
    // 滑动中别每个 tick 都发包 —— 攒 250ms 再发(手感更跟手, 请求也更少)
    if (volTimer) clearTimeout(volTimer);
    volTimer = setTimeout(() => {
      fetch('/api/volume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vol: pct }),
      }).catch(() => {});
    }, 250);
  });
}
// ---- 屏幕亮度滑块(紧跟在电脑音量下面, 一套做法) ----
// 值经服务端 xrandr 软件调光落到**电脑那块屏幕**上: 不需要权限, 外接屏也管用。
// 和音量一样存在服务端 config.json —— 换手机打开还是同一个值。
const elBri = $('set-bri');
const elBriV = $('set-bri-v');
let briTimer = null;

function briLabel(pct) {
  if (elBriV) elBriV.textContent = pct + '%';
}

async function refreshBrightness() {
  if (!elBri) return;
  try {
    const r = await fetch('/api/brightness', { cache: 'no-store' });
    const j = await r.json();
    if (j && j.ok && typeof j.pct === 'number') {
      elBri.value = String(Math.max(5, Math.min(100, j.pct)));
      briLabel(Number(elBri.value));
    }
  } catch (e) { /* 服务没起就不动滑块 */ }
}

if (elBri) {
  elBri.addEventListener('input', (e) => {
    const pct = Number(e.target.value);
    briLabel(pct);
    // 和音量一样攒 250ms 再发: 拖动过程中别把请求打满(每次都要起一个 xrandr)
    if (briTimer) clearTimeout(briTimer);
    briTimer = setTimeout(() => {
      fetch('/api/brightness', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pct: pct }),
      }).catch(() => {});
    }, 250);
  });
}
$('set-hints').addEventListener('change', (e) => { S.hints = e.target.checked; applyHints(); saveS(); });
$('set-ping').addEventListener('change', (e) => {
  S.showPing = e.target.checked; saveS(); startPing();
});
$('set-edge').addEventListener('change', (e) => {
  S.edgeNudge = e.target.checked; saveS();
  if (!S.edgeNudge) stopEdge(); else edgeUpdate();
});

// ---------------- 输入方式 / 输入响应 ----------------
function applyTypeUI() {
  const m = S.typeMode || 'auto', b = Number(S.typeBatch) || 80;
  document.querySelectorAll('#set-typemode button').forEach((x) =>
    x.classList.toggle('on', x.dataset.m === m));
  document.querySelectorAll('#set-typebatch button').forEach((x) =>
    x.classList.toggle('on', Number(x.dataset.b) === b));
}
// 「输入方式 / 输入响应」是**跟电脑有关**的设置(切的是电脑上的投递方式),
// 换手机也该一样, 所以真正的家是电脑上的 config.json, localStorage 只当本地缓存。
// 开机先拉服务端的为准; 改的时候两边都写。
function saveCfg(patch) { clipApi('/api/config', patch).catch(() => {}); }

async function loadServerConfig() {
  try {
    const j = await clipApi('/api/config');
    if (j && j.ok && j.config) {
      if (j.config.typeMode) S.typeMode = j.config.typeMode;
      if (j.config.typeBatch) S.typeBatch = Number(j.config.typeBatch);
      saveS(); applyTypeUI();
    }
  } catch (e) { /* 读不到就用本地缓存的, 不耽误用 */ }
}
loadServerConfig();

document.querySelectorAll('#set-typemode button').forEach((b) => {
  b.addEventListener('click', () => {
    S.typeMode = b.dataset.m; saveS(); applyTypeUI(); saveCfg({ typeMode: S.typeMode });
  });
});
document.querySelectorAll('#set-typebatch button').forEach((b) => {
  b.addEventListener('click', () => {
    S.typeBatch = Number(b.dataset.b); saveS(); applyTypeUI();
    saveCfg({ typeBatch: S.typeBatch, typeDelayMs: 20 });
  });
});

// ---------------- 手机屏幕常亮 ----------------
// 用手机控电脑时屏幕动不动就熄, 非常烦。Wake Lock API 只在 HTTPS / localhost 下
// 可用, 且切后台会被系统自动释放 —— 所以每次回到前台都要重新申请。
let wakeLock = null;
async function applyAwake() {
  if (!('wakeLock' in navigator)) return;
  try {
    if (S.keepAwake && document.visibilityState === 'visible') {
      if (!wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } else if (wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch (e) { /* 不支持或用户拒绝, 默默跳过 */ }
}
$('set-awake').addEventListener('change', (e) => {
  S.keepAwake = e.target.checked; saveS(); applyAwake();
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') applyAwake();
});

// ---------------- 切后台是否保持连接 ----------------
$('set-bgkeep').addEventListener('change', (e) => { S.bgKeep = e.target.checked; saveS(); });

// Dock 弹出: Ubuntu Dock 有两套机制, 由 "require-pressure-to-show" 决定走哪套。
//   =true (Ubuntu 默认) -> Mutter 压力屏障: 要求指针"顶住"屏幕边缘产生压力事件。
//     实测(console/tests/dock_ab2.py): xdotool 与 VNC(RFB) 注入的指针**都触发不了**,
//     而它同时会关掉下面的驻留机制 —— 于是远程桌面下 Dock 完全不弹。
//   =false -> 驻留(dwelling): 指针停在最后一行像素上并保持一小会儿就弹出。
//     合成指针可以正常触发, 这是远程桌面下唯一可行的路径。
// 所以这里把它关掉, 再配合 toFb() 的贴边锁(保证落在最后一行且不被手指抖动打断)。
async function refreshDockState() {
  const el = $('dock-state');
  try {
    const j = await (await fetch('/api/dock')).json();
    $('set-dock').checked = !j.pressure;
    // 标出当前延迟档位(系统里可能是别的值, 那就都不高亮, 不撒谎)
    document.querySelectorAll('#set-dockdelay button').forEach((b) => {
      b.classList.toggle('on', j.show_delay != null
        && Math.abs(Number(b.dataset.sd) - j.show_delay) < 0.011);
    });
    el.textContent = j.ok
      ? `系统当前: Ubuntu"需顶住边缘"=${j.pressure ? '开 → 远程弹不出' : '关 → 正常'}` +
        ` · 自动隐藏=${j.autohide} · 弹出延迟=${j.show_delay}s · 收起延迟=${j.hide_delay}s`
      : '读取失败: ' + (j.err || '');
  } catch (e) {
    $('set-dock').disabled = true;
    el.textContent = '读取失败(服务未响应)';
  }
}

$('set-dock').addEventListener('change', async (e) => {
  const pressure = !e.target.checked;      // 勾选 = 关掉 Ubuntu 的"需顶住边缘"
  e.target.disabled = true;
  try {
    const j = await (await fetch('/api/dock', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pressure }),
    })).json();
    if (!j.ok) e.target.checked = !e.target.checked;
  } catch (err) { e.target.checked = !e.target.checked; }
  e.target.disabled = false;
  refreshDockState();
});

// Dock 弹出延迟。远程指针被"锁"在边缘, 延迟大点更稳; 但电脑上真实鼠标是**划过去**,
// 停不了那么久 —— 延迟一大就表现为"拉到底边不弹, 偶尔才弹"。
document.querySelectorAll('#set-dockdelay button').forEach((b) => {
  b.addEventListener('click', async () => {
    const v = Number(b.dataset.sd);
    document.querySelectorAll('#set-dockdelay button').forEach((x) =>
      x.classList.toggle('on', x === b));
    try {
      const j = await (await fetch('/api/dock', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ showDelay: v }),
      })).json();
      if (!j.ok) $('dock-state').textContent = '设置失败: ' + (j.err || '未知');
    } catch (err) {
      $('dock-state').textContent = '设置失败(服务未响应)';
    }
    refreshDockState();
  });
});

// 一次性校正(版本 3): 确保 Ubuntu 的"需顶住边缘才弹出"处于**关闭**状态。
// 开着它 = 走压力屏障, 而远程注入的指针永远触发不了(dock_ab2.py 实测), 等于把
// Dock 彻底关死。只在没校正过时执行一次, 之后完全听用户/系统的。
(async function fixDockSetting() {
  if ((S.dockFixVer || 0) >= 3) return;
  try {
    const j = await (await fetch('/api/dock')).json();
    if (j.ok && j.pressure) {
      await fetch('/api/dock', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pressure: false }),
      });
    }
    if (j.ok) { S.dockFixVer = 3; saveS(); }
  } catch (e) {}
  refreshDockState();
})();

noFocusSteal();   // 所有按钮都不抢焦点(否则点一下键, 软键盘就收起)
startPing();      // 右上角延迟: 不依赖 VNC 是否连上, 开机就测

// 服务端版本自检: static/ 与 templates/ 是每次请求都从磁盘读的, 所以前端改动
// 刷新即生效; 但 server.py 改了必须重启服务。新接口返回 404 就说明跑的还是旧进程,
// 直接告诉用户该敲什么命令 —— 不然只会静默失败, 让人以为代码没起作用。
(async function checkServerVersion() {
  try {
    const r = await fetch('/api/screen');
    if (r.status !== 404) return;
    const el = $('vnc-banner');
    el.classList.add('show');
    el.innerHTML = '服务端仍是旧版本: <b>剪贴板 / 直接键入</b> 还没生效。'
      + '请在电脑上执行 <b>systemctl --user restart meow-console meow-vnc</b> 再刷新本页。'
      + '(触控板、键盘、Dock 贴边弹出属于前端改动, 已经生效)';
  } catch (e) {}
})();

connect();        // 设置就绪后再连接(画质等偏好需先加载)

// ================= 电脑声音转发 =================
// 采集的是**扬声器回环(monitor)**, 所以手机听到的是"电脑正在放的声音"
// (视频/音乐/系统提示音), 不是麦克风的环境音。
// 默认关闭, 点了才采; 关掉/切后台立刻停(见 server.py _serve_audio)。
const audioEl = $('vnc-audio-el');
const audioBtn = $('vnc-audio');
let audioOn = false;

// 播放音量恒为 100%, 响度交给**手机自己的音量键**; 电平条已去掉(2026-09-16)。
// 电脑本机音量改由设置面板滑块(#set-vol)控制, 画面上不再浮任何声音控件。
// 顺带的好处: 不再需要 AudioContext + analyser —— 声音少走一道音频图,
// 也少了一个每 0.5 秒跑一次的定时器(手机上白烧 CPU)。

function syncAudioUI() {
  if (!audioBtn) return;
  audioBtn.textContent = audioOn ? '🔊' : '🔕';
  audioBtn.classList.toggle('on', audioOn);
  audioBtn.title = audioOn ? '关闭电脑声音转发' : '把电脑正在放的声音转发到手机';
  const fsBtn = $('vnc-fs-audio');           // 全屏里那个声音按钮跟着同步
  if (fsBtn) {
    fsBtn.textContent = audioOn ? '🔊' : '🔕';
    fsBtn.title = audioOn ? '关闭声音' : '开启声音';
  }
}

// ---- 低延迟声音播放(MSE) ----
// 直接用 <audio src=...> 时, 浏览器为了"不断流"会自己先攒 1~3 秒缓冲再开始播,
// 这段延迟服务端控制不了(实测页面上"声音延迟"一直是 2 秒上下)。
// 改用 MediaSource 自己喂数据, 就能把缓冲压在 0.35 秒左右。
// Android Chrome 支持 audio/mpeg 的 MSE; **iOS Safari 不支持** → 自动退回 <audio>。
const MSE_TARGET = 0.35;        // 目标缓冲(秒)。太小会卡顿, 太大延迟高。
let mse = null;

function mseSupported() {
  // ★ 2026-09-22 停用 MSE, 走原生 <audio> 流(src=/api/audio)。
  // 原因: MSE 那条路的 appendBuffer 一旦抛异常就置 pumping=false 静默停摆,
  // 只能靠前端重连恢复 —— 表现就是"播几秒后没声音, 要反复开关"。
  // 而服务端把采集粒度压到 21ms/包(fragment_size, 修卡顿用的)之后, 碎包更频繁地
  // 命中它的缓冲/裁剪逻辑, 这个停摆就变成必然。原生流由浏览器自己缓冲, 稳得多
  // (代价是 1~3 秒延迟, iOS 一直这么用)。要低延迟再单独优化 MSE, 别拿稳定性换。
  return false;

}

function stopMse() {
  if (!mse) return;
  const cur = mse;
  mse = null;
  cur.stopped = true;
  try { if (cur.reader) cur.reader.cancel(); } catch (e) { /* ignore */ }
}

async function startMse() {
  const ms = new MediaSource();
  const st = {ms: ms, sb: null, reader: null, stopped: false};
  mse = st;
  audioEl.src = URL.createObjectURL(ms);
  await new Promise((res, rej) => {
    ms.addEventListener('sourceopen', res, {once: true});
    ms.addEventListener('error', rej, {once: true});
  });
  const sb = ms.addSourceBuffer('audio/mpeg');
  st.sb = sb;

  const res = await fetch('/api/audio?src=sys&_=' + Date.now());
  if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
  const reader = res.body.getReader();
  st.reader = reader;

  const q = [];
  let pumping = false;

  const ahead = () => {
    const b = sb.buffered;
    return b.length ? b.end(b.length - 1) - (audioEl.currentTime || 0) : 0;
  };
  const trim = () => {
    // 丢掉已经播过的部分, 别让内存一直涨(留 5 秒余量再删, 免得反复触发)
    const b = sb.buffered;
    if (b.length > 1 && b.start(1) > 5) {
      try { sb.remove(b.start(0), b.start(1)); } catch (e) { /* ignore */ }
    }
  };
  const pump = () => {
    if (!mse || st.stopped || pumping || !q.length || sb.updating) return;
    if (ahead() > MSE_TARGET) return;          // 缓冲够了就先不喂, 延迟才压得住
    pumping = true;
    try { sb.appendBuffer(q.shift()); } catch (e) { pumping = false; }
  };

  sb.addEventListener('updateend', () => {
    pumping = false;
    trim();
    // 第一次拿到数据时跳到"直播边缘", 别从头慢慢放
    if (!audioEl.currentTime && sb.buffered.length) {
      try {
        audioEl.currentTime = Math.max(0, sb.buffered.end(0) - 0.12);
      } catch (e) { /* ignore */ }
    }
    pump();
  });

  const pr = audioEl.play();
  if (pr && pr.catch) pr.catch(() => { /* 交给外面的兜底 */ });

  (async () => {
    try {
      while (mse === st && !st.stopped) {
        const {value, done} = await reader.read();
        if (done) break;
        if (value && value.length) q.push(value);
        pump();
        // 缓冲堆太多就等一等, 让网络和播放都喘口气(也是压延迟的关键)
        let guard = 0;
        while (mse === st && !st.stopped && ahead() > MSE_TARGET * 2 && guard++ < 200) {
          await new Promise((r) => setTimeout(r, 80));
          pump();
        }
      }
    } catch (e) { /* 用户关掉/切页面了 */ }
  })();
}

function startPlainAudio() {
  audioEl.removeAttribute('src');
  audioEl.src = '/api/audio?src=sys&_=' + Date.now();   // src=sys -> 扬声器回环
  audioArm();           // 元素时间轴的 0 点 = 现在, 用来和服务端流位置对齐
  const pr = audioEl.play();
  if (pr && pr.catch) {
    pr.catch((e) => {
      audioOn = false;
      syncAudioUI();
      if (statusEl) {
        setStatus('声音播放失败: ' + (e && e.message ? e.message : e));
      }
    });
  }
}

function setAudio(on) {
  audioOn = on;
  if (on) {
    audioEl.volume = 1;                 // 音量交给手机音量键(见文件上方说明)
    audioEl.pause();
    if (mseSupported()) {
      startMse().catch(() => {
        // MSE 万一不被支持/报错, 退回普通播放, 保证一定有声音
        stopMse();
        startPlainAudio();
      });
    } else {
      startPlainAudio();
    }
  } else {
    stopMse();
    audioEl.pause();
    audioEl.removeAttribute('src');
    audioEl.load();          // 断开连接 -> 服务端检测到就停止采集
    audioDisarm();
  }
  syncAudioUI();
}

if (audioBtn) audioBtn.addEventListener('click', () => setAudio(!audioOn));

// ---- 一键截图: 把 noVNC 画布(原始帧缓冲分辨率, 不受屏幕缩放影响)存成 PNG 下载 ----
// 帧是 WS 二进制数据画进画布的, 画布**没被跨源污染**, toBlob 可用。
// 文件命名带时间戳, 免得连截几张互相覆盖。
const shotBtn = $('vnc-shot');
if (shotBtn) {
  shotBtn.addEventListener('click', () => {
    const cv = document.querySelector('#screen canvas');
    if (!cv || !cv.width) { shotBtn.textContent = '无画面'; setTimeout(() => { shotBtn.textContent = '截图'; }, 1200); return; }
    const ts = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const name = `meow-${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}-`
               + `${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}.png`;
    cv.toBlob((b) => {
      if (!b) return;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      shotBtn.textContent = '已保存';
      setTimeout(() => { shotBtn.textContent = '截图'; }, 1200);
    }, 'image/png');
  });
}

syncAudioUI();
fsWheelApply();   // 初始化「滚轮」开关的点亮态(容器本身要进全屏才会显示)

// 离开页面时主动断开声音流 —— 别指望浏览器一定发 close 帧,
// 否则服务端要等超时才发现, 回环/麦克风会白开一段。
let byeSent = false;
function sayBye() {
  if (byeSent) return;
  byeSent = true;
  if (audioOn) setAudio(false);
  try { if (rfb) rfb.disconnect(); } catch (e) { /* ignore */ }
}
window.addEventListener('pagehide', sayBye);
window.addEventListener('beforeunload', sayBye);

// ================= 全屏(横屏)模式 =================
// 像视频播放器: 只展示画面 + 声音, **完全不响应远程触控**(透明层接住所有触摸,
// noVNC 的 canvas 收不到事件)。
// 点屏幕 → 控件(‹ / 声音 / 信息)淡入, 3.5 秒无操作自动收起; **再点不会退出**,
// 退出全屏只有 ‹ 一个入口。
// 摄像头页已经踩过"iPhone Safari 不支持任意元素全屏"的坑, 这里复用同一套降级。
let fsFake = false;        // 浏览器不支持元素全屏时的伪全屏降级
let fsHideTimer = 0;

function fsOn() { return document.body.classList.contains('vnc-fs'); }

function fsShowUi() {
  const ui = $('vnc-fs-ui');
  if (!ui) return;
  ui.classList.add('show');
  clearTimeout(fsHideTimer);
  fsHideTimer = setTimeout(() => ui.classList.remove('show'), 3500);
}

// 布局变了要让 noVNC 重新按容器算画布尺寸。
// noVNC 用的是 ResizeObserver, 但容器是 fixed 定位切换的, 补一次 resize 事件最稳。
function fsResize() {
  try { window.dispatchEvent(new Event('resize')); } catch (e) { /* ignore */ }
  // 画布重新适配容器之后, 放大状态的平移范围也要跟着更新(转屏 / 进出全屏)
  zClamp(); zApply();
}

async function fsEnter() {
  const wrap = $('screen-wrap');
  fsFake = false;
  try {
    if (wrap.requestFullscreen) {
      await wrap.requestFullscreen({ navigationUI: 'hide' });
    } else if (wrap.webkitRequestFullscreen) {
      wrap.webkitRequestFullscreen();
    } else {
      throw new Error('no fullscreen api');
    }
  } catch (e) {
    fsFake = true;                  // iPhone Safari 之类: 退化成 CSS 伪全屏
  }
  document.body.classList.add('vnc-fs');
  $('vnc-fs-tap').classList.remove('hide');
  $('vnc-fs-ui').classList.remove('hide');
  fsWheelApply();                   // 上次开着滚轮钮的话, 进全屏就自动挂上
  try {                             // 只有真全屏下才允许锁方向
    if (screen.orientation && screen.orientation.lock) {
      await screen.orientation.lock('landscape');
    }
  } catch (e) { /* 不支持就算了, 用户自己把手机横过来 */ }
  setTimeout(() => { fsResize(); fsShowUi(); }, 120);
  auditFs('进入全屏');
}

function fsExit() {
  document.body.classList.remove('vnc-fs');
  $('vnc-fs-tap').classList.add('hide');
  $('vnc-fs-ui').classList.remove('show');
  $('vnc-fs-ui').classList.add('hide');
  fsWheelApply();                   // 滚轮钮只在全屏挂载, 退出即收回(开关状态仍记住)
  clearTimeout(fsHideTimer);
  zReset();                       // 放大状态不带出去(普通视图不能有变化)
  try {
    if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
  } catch (e) { /* ignore */ }
  try {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
  } catch (e) { /* ignore */ }
  setTimeout(fsResize, 120);
}

function auditFs(what) { /* 只在前端记录, 不打扰服务端 */ }

if ($('vnc-fs')) {
  $('vnc-fs').addEventListener('click', () => { if (!fsOn()) fsEnter(); });
}
if ($('vnc-fs-exit')) {
  $('vnc-fs-exit').addEventListener('click', fsExit);
}
// 点屏幕**只让控件淡入**(并重新计时 3.5 秒自动收起), **不退出全屏**。
// 退出全屏只有 ‹ 一个入口 —— 之前写成"控件已经出来了再点一下 = 退出",
// 结果手指想收起控件/想继续看画面时反而退出了全屏(用户明确否掉了这个行为)。
if ($('vnc-fs-tap')) {
  $('vnc-fs-tap').addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    fsShowUi();
  });
}
// 全屏里也能开关声音(音量拉条复用下面那条浮层, 开启后它自己会显示)
if ($('vnc-fs-audio')) {
  $('vnc-fs-audio').addEventListener('click', (e) => {
    e.stopPropagation();
    setAudio(!audioOn);
    fsShowUi();
  });
}
// ---- 全屏滚轮按钮(2026-09-16 加) ----
// 全屏是"完全只看"模式, 想翻文档就得退出全屏去触控板划两下。这两个半透明圆钮
// 只发滚轮档位(复用触控板的 wheelStep, 支持长按连发), 不带指针移动/点击等
// 任何其它输入; 开关和声音按钮并排放在全屏右上角, 状态记进 localStorage,
// 下次进全屏自动恢复。圆钮压在 #vnc-fs-tap 上方: 点它只滚轮, 不会晃出顶部控件。
function fsWheelApply() {
  const box = $('vnc-fs-wheelbtns');
  if (!box) return;
  // body.vnc-fs 不在时 CSS 也不会显示它, 这里同步收掉 .show 保持状态一致
  box.classList.toggle('show', !!S.fsWheel && fsOn());
  const btn = $('vnc-fs-wheel');
  if (btn) {
    btn.classList.toggle('on', !!S.fsWheel);
    btn.title = S.fsWheel ? '收起右侧的滚轮按钮' : '在画面右侧显示滚轮按钮(翻文档用)';
  }
}
if ($('vnc-fs-wheel')) {
  $('vnc-fs-wheel').addEventListener('click', (e) => {
    e.stopPropagation();
    S.fsWheel = !S.fsWheel;
    saveS();
    fsWheelApply();
    fsShowUi();                       // 重新计时, 让用户看清开关结果再淡出
  });
}
holdRepeat($('vnc-fs-wup'), () => wheelStep(-1, 0));
holdRepeat($('vnc-fs-wdn'), () => wheelStep(1, 0));
// ---- 全屏里的双指缩放 / 拖动平移(2026-09-16 加) ----
// 和摄像头页**同一套手感**(那套你已经验收过): 双指捏合缩放、放大后单指拖动平移、
// 双击复位。只给 `#screen` 加 CSS transform ——
//   * 不碰 noVNC 的输入通道, 所以"全屏不响应远程触控"这个行为**一点没变**;
//   * 不触发画布重画(纯合成), 对延迟和帧率没有影响;
//   * 放大本质是"放大已有像素": 手机竖屏看 1440 宽的桌面, 放到 3~4 倍刚好接近
//     原始分辨率, 再往上就只是把像素拉大了(那属于"让服务端按新分辨率重画", 是另一回事)。
const ZMIN = 1, ZMAX = 4;
let zScale = 1, zTx = 0, zTy = 0, zGesture = null, zLastTap = 0;

function zApply() {
  const el = $('screen');
  if (!el) return;
  el.style.transform = (zScale <= 1.0001) ? ''
    : `translate(${zTx}px, ${zTy}px) scale(${zScale})`;
}

function zClamp() {
  const el = $('screen');
  if (!el) return;
  const w = el.offsetWidth || 1, h = el.offsetHeight || 1;
  const mx = Math.max(0, (w * (zScale - 1)) / 2);
  const my = Math.max(0, (h * (zScale - 1)) / 2);
  zTx = Math.max(-mx, Math.min(mx, zTx));
  zTy = Math.max(-my, Math.min(my, zTy));
}

// 退出全屏一定要复位, 否则放大的状态会带到普通视图里(普通视图不该有任何变化)
function zReset() { zScale = 1; zTx = 0; zTy = 0; zApply(); }

if ($('vnc-fs-tap')) {
  const tapEl = $('vnc-fs-tap');
  tapEl.addEventListener('touchstart', (e) => {
    const r = tapEl.getBoundingClientRect();
    if (e.touches.length === 1) {
      zGesture = {mode: 'pan', x: e.touches[0].clientX, y: e.touches[0].clientY,
                  tx: zTx, ty: zTy, moved: 0};
    } else if (e.touches.length === 2) {
      const [a, b] = e.touches;
      const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
      zGesture = {
        mode: 'pinch',
        dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        scale: zScale, tx: zTx, ty: zTy,
        // 锚点: 双指初始中点(相对容器中心), 缩放时让它跟着手指走
        ax: cx - r.left - r.width / 2, ay: cy - r.top - r.height / 2,
      };
      e.preventDefault();              // 拦住浏览器自己的页面缩放
    }
  }, {passive: false});

  tapEl.addEventListener('touchmove', (e) => {
    if (!zGesture) return;
    e.preventDefault();
    const r = tapEl.getBoundingClientRect();
    if (zGesture.mode === 'pan' && e.touches.length === 1) {
      if (zScale <= 1) return;         // 没放大就没什么可拖的
      const dx = e.touches[0].clientX - zGesture.x;
      const dy = e.touches[0].clientY - zGesture.y;
      zGesture.moved = Math.max(zGesture.moved, Math.abs(dx) + Math.abs(dy));
      zTx = zGesture.tx + dx;
      zTy = zGesture.ty + dy;
      zClamp(); zApply();
    } else if (zGesture.mode === 'pinch' && e.touches.length === 2) {
      const [a, b] = e.touches;
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      const ns = Math.max(ZMIN, Math.min(ZMAX, zGesture.scale * (d / zGesture.dist)));
      const k = ns / zGesture.scale;
      const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
      // 让"初始中点对应的那个画面点"跟着手指移动到当前中点
      zScale = ns;
      zTx = (cx - r.left - r.width / 2) - (zGesture.ax - zGesture.tx) * k;
      zTy = (cy - r.top - r.height / 2) - (zGesture.ay - zGesture.ty) * k;
      zClamp(); zApply();
    }
  }, {passive: false});

  tapEl.addEventListener('touchend', (e) => {
    if (e.touches.length === 0) {
      // 双击复位(没放大时它是空操作, 不影响原来的"点一下淡入控件")
      if (zGesture && zGesture.mode === 'pan' && zGesture.moved < 10) {
        const now = Date.now();
        if (now - zLastTap < 300) { zReset(); zLastTap = 0; }
        else zLastTap = now;
      }
      zGesture = null;
    } else if (e.touches.length === 1 && zGesture && zGesture.mode === 'pinch') {
      // 双指松掉一根 → 无缝接回单指拖动
      zGesture = {mode: 'pan', x: e.touches[0].clientX, y: e.touches[0].clientY,
                  tx: zTx, ty: zTy, moved: 99};
    }
  });
}

// 系统层面退出全屏(按 ESC / 手势返回)时把样式一起收掉
function fsSync() {
  if (!document.fullscreenElement && !document.webkitFullscreenElement && !fsFake) {
    if (fsOn()) fsExit();
  }
}
document.addEventListener('fullscreenchange', fsSync);
document.addEventListener('webkitfullscreenchange', fsSync);

// ---- 声音去向: 电脑也响 / 只发手机(虚拟输出, 电脑静音) ----
// 放在设置面板的音量下面, 和「画质」那些用同一套 .seg 分段按钮。
// 这是**电脑级设置**(存服务端 config.json), 两个页面都照它走, 不用各自加按钮。
(function bindAudioOut() {
  const seg = document.querySelector('.seg.ao');
  if (!seg) return;
  const btns = Array.prototype.slice.call(seg.querySelectorAll('button'));
  // 标签跟着模式走: 模拟输出时它调的是"发给手机的音量"(二级控制), 不再是电脑音量
  function labels(mode) {
    const silent = mode === 'silent';
    const l = document.getElementById('vol-label');
    const n = document.getElementById('vol-note');
    if (l) l.textContent = silent ? '输出音量' : '电脑音量';
    if (n) n.textContent = silent
      ? '模拟输出: 电脑音量被钉在 1%, 听不见但转发仍是满幅信号; 锁定不可调以免拖到 0。手机上用你自己的音量键调, 切回电脑输出即可解锁。'
      : '调的是电脑本机的扬声器音量(不限于这次远程会话), 换手机打开也是同一个值; 超过 100% 会变红(软件放大, 可能失真), 拖到 0 即静音。';
  }
  function mark(mode) {
    btns.forEach((b) => b.classList.toggle('on', b.dataset.ao === mode));
    labels(mode);
  }
  fetch('/api/audio/out', { cache: 'no-store' })
    .then((r) => r.json())
    .then((j) => { if (j && j.ok) mark(j.mode); })
    .catch(() => {});
  btns.forEach((b) => b.addEventListener('click', () => {
    mark(b.dataset.ao);
    fetch('/api/audio/out', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: b.dataset.ao }),
    }).catch(() => {});
    if (typeof refreshVolume === 'function') setTimeout(refreshVolume, 400);
  }));
})();

// ---- 分辨率: 手机上画面卡时最有效的一档 ----
(function bindScreenMode() {
  const sel = document.getElementById('set-mode');
  if (!sel) return;
  fetch('/api/screen/modes', { cache: 'no-store' }).then((r) => r.json()).then((j) => {
    if (!j || !j.ok || !j.modes) return;
    sel.innerHTML = j.modes.map((m) =>
      '<option value="' + m.name + '"' + (m.name === j.current ? ' selected' : '') + '>'
      + m.name + ' @' + m.hz + 'Hz</option>').join('');
  }).catch(() => {});
  sel.addEventListener('change', () => {
    fetch('/api/screen/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: sel.value }),
    }).then((r) => r.json()).then((j) => {
      if (j && !j.ok) alert('切换失败: ' + (j.err || ''));
    }).catch(() => {});
  });
})();
