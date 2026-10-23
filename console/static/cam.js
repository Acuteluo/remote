/* 摄像头画面: 服务端 ffmpeg 无头采集 -> WebSocket 推 JPEG 帧 -> 这里显示。
   电脑端全程没有任何窗口; 连接一断服务端就停采集(见 server.py ws_cam_bridge)。

   2026-10-23 改版(用户要求):
     - 「摄像头画面」和「麦克风」各自一个开关, **进页面默认都是关的** ——
       不点开关, 一个字节都不采, 服务端也不会起 ffmpeg。
     - 关掉开关 = 立刻断开(服务端一收到 WS close / HTTP 流断就 kill ffmpeg);
       离开页面(pagehide / 切后台)同样立刻停; 下次进来又是默认关。
     - 收音电平旁边实时报 dB; 另有"画面 / 声音是多少秒以前的"两个实时读数。
*/
(() => {
  const $ = (id) => document.getElementById(id);
  const img = $('cam-img'), hint = $('cam-hint'), st = $('cam-status'), errEl = $('cam-err');
  const btnSwitch = $('cam-switch'), btnReconnect = $('cam-reconnect');
  const selSize = $('cam-size'), selFps = $('cam-fps');
  const osd = $('cam-osd');
  const swVideo = $('cam-video-sw'), swMic = $('cam-mic-sw'), swMicFs = $('cam-fs-mic');
  const videoBox = $('cam-video-box'), micBox = $('cam-mic-box');
  const lagV = $('cam-lag-v'), lagA = $('cam-lag-a'), lagFps = $('cam-lag-fps');
  const btnFs = $('cam-fs'), btnSnap = $('cam-snap'), btnFsSnap = $('cam-fs-snap');

  // 设备列表由服务端下发(已过滤掉不能采集的 metadata 节点); 默认就是第 0 个 ——
  // 笔记本自带摄像头通常编号最小, 也就是它。
  const PAIRS = (() => {
    try { return JSON.parse(($('cam-devs') || {}).dataset?.devs || '[]'); }
    catch (e) { return []; }
  })();
  const DEVS = PAIRS.map((p) => p[0]);
  const LABELS = PAIRS.map((p) => p[1]);
  let devIdx = 0;
  const devLabel = (d) => {
    const i = DEVS.indexOf(d);
    return i >= 0 ? (LABELS[i] || d) : d;
  };

  // ---- 状态 ----
  // videoOn / micOn 是**用户开关**; 两个都默认关, 而且**绝不写进 localStorage**
  // —— 用户明确要求"下次再点进来默认关闭"。
  let videoOn = false, micOn = false;
  let leaving = false;            // 页面正在走(pagehide/beforeunload)
  let ws = null, timer = 0, retry = 0, lastBusy = false;
  let prevUrl = null;             // 上一帧的 objectURL, 显示新帧后释放, 防内存泄漏
  let frames = 0, bytes = 0, tickAt = Date.now(), startedAt = 0;
  let curDev = '', curSize = '', curSetFps = '';

  // "画面是多少毫秒以前的": 服务端每帧带一个发送时刻(服务端时钟),
  // 这里用 clockOff 把它换算到本机时钟上(见 syncClock)。
  let frameSentAt = 0, clockOff = 0, clockOk = false;
  let clockRtt = 0;               // 最近一次时钟对齐的往返时延(ms), 用来估网络单向延时
  let curFrameAge = null;         // 最近一帧**到达时**的"年龄"(ms)
  let frameArrivedAt = 0;         // 最近一帧到达本机的时刻(算帧龄用, 见 frameAgeMs)
  let lastRealFps = null;         // 上一个 1 秒窗口实测到的帧率
  // "声音是多少秒以前的" = 浏览器里"已收到但还没播"的时长 + 服务端管道滞后
  let serverLag = 0;              // 秒; 由 /api/audio/hb 顺带捎回来
  // 这一路音频的流 id, 以及"第 0 个采样是什么时候采到的"(服务端 epoch ms)。
  // 服务端按 sid 记锚点, 前端拿它 + 时钟对齐算出"正在听的声音有多旧"(见 audioAgeMs)。
  let audioSid = '', audioSince = 0;

  const hms = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

  // 延时显示(约定): **<1s 用整数毫秒**(3ms / 999ms), **>=1s 用秒**
  // (1.005s / 10.12s / 100.1s —— 秒这一档小数位随量级自适应, 宽度基本恒定)。
  // 采集模块 / 远程桌面 / 一键自检三处同一套, 改的时候一起改。
  const fmtLag = (ms) => {
    if (ms == null || !isFinite(ms) || ms < 0) return '--';
    if (ms < 1000) return Math.round(ms) + 'ms';
    const sec = ms / 1000;
    return sec.toFixed(sec < 10 ? 3 : sec < 100 ? 2 : 1) + 's';
  };
  // 红黄绿(门槛是自己定的):
  //   画面延时  绿 <0.3s   黄 <1s    红 >=1s
  //   声音延时  绿 <2s     黄 <6s    红 >=6s  —— 声音天生被浏览器缓冲 ~5s,
  //                                            套画面的门槛会永远飘红, 没意义
  const lvLagVideo = (ms) => (ms == null ? '' : ms < 300 ? 'ok' : ms < 1000 ? 'warn' : 'bad');
  const lvLagAudio = (ms) => (ms == null ? '' : ms < 2000 ? 'ok' : ms < 8000 ? 'warn' : 'bad');
  // 帧率按**选定的目标帧率**打折判(设了 12fps 却只有 4fps 才是真有问题):
  //   绿 >=80%   黄 >=50%   红 <50%
  const lvFps = (fps, target) => {
    const t = Number(target);
    if (!fps || !isFinite(fps) || !t) return '';
    return fps >= t * 0.8 ? 'ok' : fps >= t * 0.5 ? 'warn' : 'bad';
  };
  const setLv = (el, lv) => { if (el) el.className = lv || ''; };

  // 帧龄 = 最近一帧**到达时**有多旧(不含解码/绘制那几毫秒)。
  // 到达时刻单独记着, 所以时钟还没对齐好也能事后补算。
  //
  // ★ 夹到 0 以上: 画面延时本来就只有几毫秒~几十毫秒, 而时钟对齐的误差是
  //   **±半个 RTT**(手机上走 Tailscale 就有几十毫秒) —— 两者同一个量级, 直接
  //   相减经常算出**负数**。负数不是"没数据", 是"低于测量分辨率", 显示成 `--`
  //   会让人以为坏了(实测用户手机上一直显示 `-- 前`)。夹到 0 就是 `0ms`, 诚实
  //   且不误导。
  const frameAgeMs = () => {
    if (!(videoOn && clockOk && frameSentAt && frameArrivedAt)) return null;
    return Math.max(0, frameArrivedAt + clockOff - frameSentAt);
  };

  function paintOsd(realFps, kbps) {
    if (!videoOn) { osd.textContent = '画面已关闭'; return; }
    const now = new Date();
    const clock = now.toTimeString().slice(0, 8);
    const dur = startedAt ? hms(Math.floor((Date.now() - startedAt) / 1000)) : '--:--';
    // 帧率旁边直接带上"这一帧是多久以前的"(用户要求: 图像统计里既要有帧率,
    // 也要有 xx 秒前)。两个数各按自己那套门槛上色。
    const lvF = lvFps(realFps, curSetFps);
    const lag = frameAgeMs();
    osd.innerHTML =
      `${clock}　·　<b class="${lvF}">${realFps}</b> fps` +
      `　·　<b class="${lvLagVideo(lag)}">${fmtLag(lag)} 前</b>` +
      `　·　<b>${kbps}</b> KB/s\n` +
      `${curSize || '--'} @ ${curSetFps || '--'}fps　·　已连 ${dur}` +
      (zScale > 1.01 ? `　·　<b>×${zScale.toFixed(1)}</b>` : '') +
      `　·　${curDev ? devLabel(curDev) : '--'}`;
  }

  // 记住上次的选择(只记**参数**, 不记开关状态); 上次那个摄像头拔掉了就回默认的
  try {
    const s = JSON.parse(localStorage.getItem('meow_cam') || '{}');
    if (s.dev) {
      const i = DEVS.indexOf(s.dev);
      if (i >= 0) devIdx = i;
    }
    if (s.size) selSize.value = s.size;
    if (s.fps) selFps.value = s.fps;
  } catch (e) { /* 无所谓 */ }

  const save = () => {
    try {
      localStorage.setItem('meow_cam', JSON.stringify(
        { dev: DEVS[devIdx] || '', size: selSize.value, fps: selFps.value,
          vol: Number(volEl.value) || 80 }));
    } catch (e) { /* 无所谓 */ }
  };

  function url() {
    const p = new URLSearchParams(
      { dev: DEVS[devIdx] || '/dev/video0',
        size: selSize.value, fps: selFps.value });
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/cam?${p}`;
  }

  function show(blob) {
    const u = URL.createObjectURL(blob);
    img.onload = () => { if (prevUrl) URL.revokeObjectURL(prevUrl); prevUrl = u; };
    img.src = u;
    hint.style.display = 'none';
    frames++;
    bytes += blob.size || 0;
    // 只记"到达时刻", 帧龄等显示时再算 —— /api/time 的时钟对齐是异步的,
    // 头几帧常常在它完成之前就到, 那时直接算会漏掉。
    frameArrivedAt = Date.now();
  }

  // 关掉画面时把画面和统计一起清掉, 别留一张"上一秒的图"骗人
  function clearFrame() {
    if (prevUrl) { URL.revokeObjectURL(prevUrl); prevUrl = null; }
    img.removeAttribute('src');
    curDev = ''; startedAt = 0; frames = 0; bytes = 0;
    frameSentAt = 0; curFrameAge = null;
  }

  function closeWs() {
    if (!ws) return;
    try { ws.onclose = null; ws.close(); } catch (e) { /* ignore */ }
    ws = null;
  }

  // 开关 + 页面状态: 现在到底该不该采集
  const wantVideo = () => videoOn && !leaving && !document.hidden;

  function schedule() {
    clearTimeout(timer);
    if (!wantVideo()) return;
    // 退避重连: 摄像头打不开时别每秒狂试。
    // 设备被占用的话退得更慢 —— 那种情况不是"再试一次就好", 狂试只会更糟。
    const base = lastBusy ? 5000 : 1000;
    const cap = lastBusy ? 20000 : 10000;
    const d = Math.min(Math.round(base * Math.pow(1.8, retry)), cap);
    retry++;
    st.textContent = `断开, ${Math.round(d / 1000)}s 后重试`;
    timer = setTimeout(connect, d);
  }

  function connect() {
    if (!wantVideo()) return;
    clearTimeout(timer);
    resetZoom();          // 换设备/换分辨率后画面尺寸变了, 旧的缩放比例没意义
    closeWs();
    errEl.textContent = '';
    st.textContent = '连接中…';
    hint.textContent = '正在连接摄像头…';
    hint.style.display = 'flex';
    try {
      ws = new WebSocket(url());
    } catch (e) {
      schedule();
      return;
    }
    ws.binaryType = 'blob';
    ws.onopen = () => { retry = 0; };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let j = {};
        try { j = JSON.parse(ev.data); } catch (e) { return; }
        if (j.t === 'f') {
          // 服务端"这一帧现在发出去"的时刻(见 server.py 的 pump)
          frameSentAt = j.s || 0;
        } else if (j.t === 'start') {
          curDev = j.dev; curSize = j.size; curSetFps = String(j.fps);
          startedAt = Date.now(); frames = 0; bytes = 0; tickAt = Date.now();
          st.textContent = `${j.dev} · ${j.size} @ ${j.fps}fps`;
          hb();          // 立刻报一次"有人在看", 别让服务端白等一个心跳周期
        } else if (j.t === 'err') {
          lastBusy = /占用|busy/i.test(j.d || '');
          errEl.textContent = '⚠ ' + j.d;
          hint.textContent = j.d;
          hint.style.display = 'flex';
          st.textContent = lastBusy ? '被占用' : '失败';
        }
        return;
      }
      show(ev.data);
    };
    ws.onclose = schedule;
    ws.onerror = () => { /* onclose 会接手 */ };
  }

  // 心跳: 告诉服务端"我还看着呢"。服务端**只认这个**, 不认 WebSocket 的 pong ——
  // 浏览器/系统的网络栈在页面切到后台、JS 已经被挂起时照样会自动回 pong, 拿 pong
  // 判活等于永远判不出"人已经走了"。
  // 切后台/锁屏后浏览器会把定时器节流到几乎不跑 -> 心跳停 -> 服务端 20s 内把摄像头
  // 关掉。这样"按返回退出"和"切后台一直不回"两条路都能自己停, 不用指望
  // visibilitychange 一定会触发(有的 webview 不触发)。
  function hb() {
    if (!wantVideo()) return;
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ t: 'hb' })); } catch (e) { /* ignore */ }
    }
  }
  setInterval(hb, 5000);

  // ---- 时钟对齐: 手机和服务器的 Date.now() 差多少 ----
  // 画面延迟必须知道这个差值才能算: 服务端给的是**它的**时刻, 我们要换算成本机的。
  // 打 3 次取 RTT 最小的那次(误差 ≈ RTT/2, 局域网里就几毫秒)。
  // 打 3 次取 RTT 最小的那次(误差 ≈ RTT/2)。**并发**发 —— 串行 await 会把
  // 3 个往返叠起来, 手机上白白多等两个 RTT; 而且请求数才是"进页面快不快"的关键
  // (见 tests/entry_check.py), 所以次数也压到 3 次。
  async function syncClock() {
    const c0 = Date.now();
    const rs = await Promise.all([0, 1, 2].map(() =>
      fetch('/api/time', { cache: 'no-store' })
        .then((r) => r.json()).then((j) => ({ j, t: Date.now() }))
        .catch(() => null)));
    let best = null;
    for (const x of rs) {
      if (!x || typeof x.j.s !== 'number') continue;
      const rtt = x.t - c0;
      if (!best || rtt < best.rtt) best = { rtt, off: x.j.s - (c0 + rtt / 2) };
    }
    if (best) { clockOff = best.off; clockOk = true; clockRtt = best.rtt; }
  }
  // 手机的网络会变(切 WiFi/4G/Tailscale 出口), 定期重新对一次;
  // 顺便刷新 clockRtt —— 声音延迟要用它估"网络那一段"。
  setInterval(syncClock, 30000);

  // 每 1s 刷新 OSD: 实测帧率 / 实测码率 / 已连时长 / 本地时间
  setInterval(() => {
    const now = Date.now();
    const dt = Math.max(0.001, (now - tickAt) / 1000);
    const f = startedAt ? Math.round(frames / dt) : '--';
    const kbps = startedAt ? Math.round(bytes / dt / 1024) : '--';
    frames = 0; bytes = 0; tickAt = now;
    lastRealFps = (typeof f === 'number') ? f : null;
    paintOsd(f, kbps);
  }, 1000);

  [selSize, selFps].forEach((s) => {
    s.addEventListener('change', () => { save(); retry = 0; connect(); });
  });

  // ---- 画面开关 ----
  function setVideo(on) {
    if (on === videoOn) { syncSwitches(); return; }
    videoOn = on;
    if (on) {
      retry = 0;
      errEl.textContent = '';
      connect();
    } else {
      clearTimeout(timer);
      closeWs();                 // 服务端收到 close 立刻 kill ffmpeg
      clearFrame();
      st.textContent = '画面已关闭';
      osd.textContent = '画面已关闭';
      hint.innerHTML = '画面已关闭<br>打开下面的「摄像头画面」开关开始采集';
      hint.style.display = 'flex';
    }
    syncSwitches();
  }

  // ---- 麦克风开关 ----
  const audioEl = $('cam-audio-el');
  const volEl = $('cam-vol'), audioState = $('cam-audio-state');
  const micInfoEl = $('cam-mic-info');
  const levelBox = $('cam-level'), levelBar = $('cam-level-bar'), levelPeak = $('cam-level-peak');
  const levelDb = $('cam-level-db');
  let audioOn = false;             // 实际在收(开关打开 + 页面在前台)
  let micInfo = null;              // null=还没检测 / false=没有可用麦 / {label,src}=可用
  let micProbe = null;             // 正在检测中的 Promise(避免重复探)
  let actx = null, analyser = null;

  // 音量: 记住上次调的; 全屏时也能用手机侧键调(系统媒体音量)
  try {
    const s0 = JSON.parse(localStorage.getItem('meow_cam') || '{}');
    if (typeof s0.vol === 'number') volEl.value = s0.vol;
  } catch (e) { /* 无所谓 */ }
  audioEl.volume = (Number(volEl.value) || 80) / 100;
  volEl.addEventListener('input', () => {
    audioEl.volume = (Number(volEl.value) || 0) / 100;
    save();
  });

  // 麦克风可用性: **要用了才检测**(?fresh=1 强制当场重探)。
  // 一进页面就探会白开一次麦克风设备, 和"默认不采集"矛盾;
  // 而设备存在 ≠ 能用 —— 这台机器的内置 DMIC 就曾经"设备在、一采就挂起",
  // 所以必须真试采一下才算数。
  //
  // ★★ 检测**绝不能挡在开流前面** ★★
  // `audioEl.play()` 必须在**用户手势里同步调用**。中间夹一个 await(哪怕只是
  // 一秒的探测), iPhone Safari 和严格的 Chrome 就会以 NotAllowedError 拒掉,
  // 而 `play()` 一被拒浏览器**根本不会去请求那个 src** —— 表现就是"点了开关
  // 没反应、服务端一个请求都收不到"(2026-10-23 踩过: 用户点了 9 次探测全成功,
  // 服务端却一个 /api/audio 都没收到)。
  // 所以顺序是: **先同步开流, 再后台补检测**; 检测结果只用来写信息行,
  // 以及流挂了之后给个说法。
  async function probeMic() {
    if (micProbe) return micProbe;
    micProbe = (async () => {
      try {
        const r = await fetch('/api/audio/mics?fresh=1', { cache: 'no-store' });
        const j = await r.json();
        const list = j.mics || [];
        if (j.usable) {
          micInfo = j.usable;
          micInfoEl.textContent = '麦克风可用: ' + (j.usable.label || j.usable.src);
          micInfoEl.className = 'mic-info ok';
          micInfoEl.title = j.usable.src;
        } else {
          micInfo = false;
          micInfoEl.textContent = list.length
            ? `没有可用的麦克风（检测到 ${list.length} 个设备，但都采不到声音）`
            : '没有检测到麦克风设备';
          micInfoEl.className = 'mic-info bad';
          micInfoEl.title = list.map((m) => `${m.src} → ${m.max_volume}dB`).join('\n');
        }
      } catch (e) {
        micInfo = false;
        micInfoEl.textContent = '麦克风检测失败';
        micInfoEl.className = 'mic-info bad';
      } finally {
        micProbe = null;
      }
      // 开关还开着、但流已经挂了 -> 这个检测结论就是原因, 补上说法并弹回开关。
      // (流要是还活着, 就一律以**实际在响**为准, 绝不动它。)
      if (micInfo === false && micOn && !audioOn) {
        micOn = false;
        syncSwitches();
        errEl.textContent = '⚠ ' + micInfoEl.textContent;
      }
      return micInfo;
    })();
    return micProbe;
  }

  // 告诉服务端"这个页面还开着"。麦克风是独立开关了, 收音不能再靠摄像头 WS 的
  // 心跳续命(只开麦、或开着收音把画面关掉, 都会被误判成"人走了"而掐掉)。
  // 顺带把服务端采集管道自身的滞后捎回来, 显示"声音是多少秒以前的"要用。
  let hbBusy = false;
  function hbMic() {
    if (hbBusy || leaving) return;
    hbBusy = true;
    fetch('/api/audio/hb?sid=' + encodeURIComponent(audioSid), { cache: 'no-store' })
      .then((r) => r.json())
      .then((j) => {
        if (typeof j.lag === 'number') serverLag = j.lag;
        if (typeof j.since === 'number' && j.since > 0) audioSince = j.since;
      })
      .catch(() => { /* 网络抖一下无所谓, 下一个周期再来 */ })
      .finally(() => { hbBusy = false; });
  }
  setInterval(() => { if (audioOn) hbMic(); }, 5000);

  function initAnalyser() {
    if (actx) { if (actx.state === 'suspended') actx.resume(); return; }
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      actx = new AC();
      const src = actx.createMediaElementSource(audioEl);   // 每个元素只能建一次
      analyser = actx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      analyser.connect(actx.destination);
    } catch (e) { analyser = null; }
  }

  function startAudio() {
    if (audioOn) return;
    initAnalyser();                 // 必须在用户手势里建 AudioContext
    hbMic();                        // 先报一次"页面还活着"
    audioOn = true;
    serverLag = 0;
    audioSince = 0;
    // 每开一次收音换一个 sid: 服务端按它记"这条流第 0 个采样是什么时候采到的"。
    audioSid = Math.random().toString(36).slice(2) + Date.now().toString(36);
    // 加时间戳: 别让浏览器/中间代理拿缓存的旧流。
    // src=mic 明确要**麦克风** —— 服务端会走已经预热好的麦克风列表, 秒开;
    // 用默认的 auto 会去做一次全候选试采, 点开要白等好几秒。
    audioEl.src = '/api/audio?src=mic&sid=' + encodeURIComponent(audioSid)
                + '&_=' + Date.now();
    const pr = audioEl.play();
    if (pr && pr.catch) {
      pr.catch((e) => {
        audioOn = false; micOn = false;
        syncSwitches();
        errEl.textContent = '⚠ 播放声音失败: ' + (e && e.message ? e.message : e);
      });
    }
    syncSwitches();
    // 服务端要等第一块音频写出去才会记下锚点, 所以起来后补两次心跳尽快拿到它
    setTimeout(hbMic, 700);
    setTimeout(hbMic, 2000);
  }

  function stopAudio() {
    if (audioOn) {
      audioOn = false;
      try { audioEl.pause(); } catch (e) { /* ignore */ }
      audioEl.removeAttribute('src');
      audioEl.load();          // 关掉连接 —— 服务端检测到就会停止采集
    }
    serverLag = 0;
    audioSid = ''; audioSince = 0;
    resetMeter();
  }

  // **同步函数**: 里面每一行都还在用户手势的那一个 task 里 —— 这是 play() 能被
  // 允许的前提, 千万别改成 async 或往中间塞 await(见上面 probeMic 的注释)。
  function setMic(on) {
    if (on) {
      if (micOn) return;
      micOn = true;
      syncSwitches();                        // 开关立刻给反馈
      micInfoEl.textContent = micInfo && micInfo !== false
        ? '麦克风可用: ' + (micInfo.label || micInfo.src) : '正在开启麦克风…';
      micInfoEl.className = 'mic-info';
      startAudio();                          // 同步: AudioContext + src + play() 全在手势里
      // 后台补一次真检测, 只为写信息行 / 失败时给说法; **不阻塞开流**。
      probeMic().then(() => { if (micOn) syncSwitches(); });
    } else {
      micOn = false;
      stopAudio();
      syncSwitches();
    }
  }

  audioEl.addEventListener('error', () => {
    if (!audioOn) return;      // 停止时会触发一次无害的 error, 忽略
    audioOn = false;
    micOn = false;
    syncSwitches();
    // 服务端拒绝时回的是 JSON(比如"没有可用的麦克风"), 媒体元素只会报错、
    // 读不到那段话; 所以这里先用已经探到的结论, 没有就说个通用的。
    errEl.textContent = '⚠ ' + (micInfo === false && micInfoEl.textContent
      ? micInfoEl.textContent : '声音中断(可能没有麦克风或采集失败)');
    probeMic();                // 顺带把真正的原因探出来, 它会补上信息行
  });

  // ---- 收音电平: 实时 dB + 峰值保持 ----
  // 用 WebAudio 量手机端**实际播出来**的声音, 所以它反映的是端到端的结果。
  // 刻度取 -60dB(空) ~ 0dB(满刻度): 说话大概落在 -30 ~ -12dB 这一段。
  let peakDb = -60, peakAt = 0;

  function resetMeter() {
    levelBar.style.width = '0%';
    levelPeak.style.left = '0%';
    levelBox.classList.remove('dead', 'warm', 'hot', 'live');
    levelDb.textContent = '--';
    peakDb = -60; peakAt = 0;
  }

  function tickMeter() {
    if (!audioOn || !analyser) { if (audioOn === false) resetMeter(); return; }
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const d = (buf[i] - 128) / 128;
      sum += d * d;
    }
    const rms = Math.sqrt(sum / buf.length);
    const db = rms > 1e-7 ? 20 * Math.log10(rms) : -100;
    const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    levelBar.style.width = pct.toFixed(1) + '%';
    // 峰值保持: 冲到新高就记住, 1.2 秒没再冲高就跟着当前值回落
    const now = Date.now();
    if (db > peakDb || now - peakAt > 1200) { peakDb = db; peakAt = now; }
    levelPeak.style.left =
      Math.max(0, Math.min(100, (peakDb + 60) / 60 * 100)).toFixed(1) + '%';
    levelBox.classList.add('live');
    levelBox.classList.toggle('dead', db < -55);
    levelBox.classList.toggle('warm', db >= -18 && db < -6);
    levelBox.classList.toggle('hot', db >= -6);
    levelDb.textContent = db <= -90 ? '静音' : db.toFixed(1) + ' dB';
    levelBox.title = '当前 ' + db.toFixed(1) + ' dBFS　峰值 ' + peakDb.toFixed(1) + ' dBFS';
  }

  // 正在听的那段声音是什么时候采到的。
  //
  // 服务端告诉我们: 这条流的**第 0 个采样**是在它的 audioSince 时刻采到的;
  // 播放头现在在 currentTime 秒处, 那一段就是 (audioSince + currentTime) 采的。
  // 把"现在"也换算到服务端时钟上(Date.now() + clockOff)一减, 就是延迟 ——
  // **网络延时天然包含在里面**, 不用另外估。
  //
  // 早先是拿浏览器的 `buffered.end - currentTime` 估的, 但 `buffered` 是
  // 粗粒度更新的(隔一段才刷), 算出来锯齿状乱跳(实测 0.1~3.6s), 不能当读数。
  // 服务端那条管道本身很平滑(实测交付间隔中位 91ms, 最大 102ms)。
  function audioAgeMs() {
    if (!audioOn || !clockOk || !audioSince) return null;
    const capturedAt = audioSince + audioEl.currentTime * 1000;
    const age = Date.now() + clockOff - capturedAt;
    return (age >= 0 && age < 600000) ? age : null;
  }

  // 电平 + 两个延迟读数一起刷(100ms: 电平要跟手, 延迟数字也不至于跳)
  setInterval(() => {
    tickMeter();
    const v = frameAgeMs();
    curFrameAge = v;
    lagV.textContent = v == null ? '--' : fmtLag(v) + ' 前';
    setLv(lagV, lvLagVideo(v));
    lagFps.textContent = videoOn && lastRealFps != null ? lastRealFps + ' fps' : '--';
    setLv(lagFps, videoOn ? lvFps(lastRealFps, curSetFps) : '');
    const a = audioAgeMs();
    lagA.textContent = a == null ? '--' : fmtLag(a) + ' 前';
    setLv(lagA, lvLagAudio(a));
  }, 100);

  // ---- 开关的 UI 同步 ----
  function syncSwitches() {
    swVideo.checked = videoOn;
    videoBox.classList.toggle('on', videoOn);
    swMic.checked = micOn;
    micBox.classList.toggle('on', micOn);
    if (swMicFs) swMicFs.checked = micOn;
    // 画面没开时, 这些按钮点了也没意义, 直接禁用比让人点了没反应强
    [btnReconnect, btnFs, btnSnap, btnFsSnap, selSize, selFps].forEach((b) => {
      if (b) b.disabled = !videoOn;
    });
    if (DEVS.length >= 2) btnSwitch.disabled = !videoOn;
    volEl.disabled = !audioOn;
    volEl.style.opacity = audioOn ? '1' : '.4';
    audioState.hidden = !audioOn;
  }

  swVideo.addEventListener('change', () => setVideo(swVideo.checked));
  swMic.addEventListener('change', () => setMic(swMic.checked));
  if (swMicFs) swMicFs.addEventListener('change', () => setMic(swMicFs.checked));

  // 切换摄像头: 只有一个时按钮是隐藏的(服务端按数量决定), 这里也兜一层
  function syncSwitch() {
    if (DEVS.length < 2) { btnSwitch.style.display = 'none'; return; }
    btnSwitch.textContent = `切换 · ${devIdx + 1}/${DEVS.length}`;
    btnSwitch.title = LABELS[devIdx] || DEVS[devIdx];
  }
  btnSwitch.addEventListener('click', () => {
    if (DEVS.length < 2) return;
    devIdx = (devIdx + 1) % DEVS.length;
    save();
    syncSwitch();
    retry = 0;
    connect();
  });
  syncSwitch();
  btnReconnect.addEventListener('click', () => { retry = 0; connect(); });

  // 切到后台就断开: 省电, 也别让摄像头/麦克风在没人看的时候还开着。
  // 开关状态**保持**, 切回来接着用; 但页面一旦真的走了(pagehide)就是彻底关掉。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer);
      closeWs();
      stopAudio();
      if (videoOn) st.textContent = '已暂停(切后台)';
    } else {
      if (videoOn) { retry = 0; connect(); }
      if (micOn) startAudio();
    }
  });

  // ---- 全屏 / 横屏观看 ----
  // 全屏目标是整个 .cam-wrap(画面 + 下方信息条), 这样全屏时信息条还在
  const wrap = $('cam-wrap');
  let fakeFs = false;        // 浏览器不支持真全屏(iPhone Safari)时的降级方案

  const inFs = () => !!(document.fullscreenElement ||
                        document.webkitFullscreenElement) || fakeFs;

  async function enterFs() {
    try {
      if (wrap.requestFullscreen) {
        await wrap.requestFullscreen({ navigationUI: 'hide' });
      } else if (wrap.webkitRequestFullscreen) {
        wrap.webkitRequestFullscreen();
      } else {
        throw new Error('no fullscreen api');
      }
    } catch (e) {
      // 降级: 用 fixed 铺满的"伪全屏", 效果一样, 只是地址栏还在
      fakeFs = true;
      document.body.classList.add('cam-fs');
    }
    // 尽量锁横屏; 不支持的浏览器会抛错, 静默放过(页面上有提示让用户自己转)
    try {
      if (screen.orientation && screen.orientation.lock) {
        await screen.orientation.lock('landscape');
      }
    } catch (e) { /* 忽略 */ }
    syncFs();
  }

  function exitFs() {
    try {
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      }
    } catch (e) { /* 忽略 */ }
    fakeFs = false;
    document.body.classList.remove('cam-fs');
    try {
      if (screen.orientation && screen.orientation.unlock) {
        screen.orientation.unlock();
      }
    } catch (e) { /* 忽略 */ }
    syncFs();
  }

  function syncFs() {
    btnFs.textContent = inFs() ? '退出全屏' : '全屏';
    syncFsUi();
    updateOrient();
  }

  // ---- 全屏里的覆盖层自动收起(和远程桌面全屏同一套逻辑) ----
  // 任何触摸都让它们淡入并重新计时, 3.5 秒没有操作就淡出。
  // 只**监听**不拦截(不 preventDefault / 不 stopPropagation), 所以不影响
  // stage 上的双指缩放和拖动平移 —— 这也是它对延迟零影响的原因(纯被动监听)。
  let fsHideTimer = 0;
  function showFsUi() {
    wrap.classList.remove('hideui');
    clearTimeout(fsHideTimer);
    fsHideTimer = setTimeout(() => wrap.classList.add('hideui'), 3500);
  }
  function syncFsUi() {
    clearTimeout(fsHideTimer);
    if (inFs()) showFsUi();
    else wrap.classList.remove('hideui');     // 退出全屏时清掉, 别带到普通视图里
  }
  document.addEventListener('pointerdown', () => { if (inFs()) showFsUi(); },
                            { passive: true });

  function updateOrient() {
    // 竖着拿时提示横过来(不强制, 用户自己舒服就行)
    wrap.classList.toggle('portrait', window.innerHeight > window.innerWidth);
  }

  document.addEventListener('fullscreenchange', syncFs);
  document.addEventListener('webkitfullscreenchange', syncFs);
  window.addEventListener('resize', updateOrient);
  window.addEventListener('orientationchange', () => setTimeout(updateOrient, 300));
  btnFs.addEventListener('click', () => { inFs() ? exitFs() : enterFs(); });
  $('cam-fs-exit').addEventListener('click', exitFs);

  // ---- 双指缩放 / 拖动平移(全屏时尤其需要) ----
  const stage = document.querySelector('.cam-stage');
  const ZMIN = 1, ZMAX = 8;
  let zScale = 1, zTx = 0, zTy = 0;
  let gesture = null;
  let lastTap = 0;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function applyZoom() {
    img.style.transform = `translate(${zTx}px, ${zTy}px) scale(${zScale})`;
  }

  function clampPan() {
    // 放大后允许平移到边缘, 但别把画面拖出可视区
    const r = stage.getBoundingClientRect();
    const mx = Math.max(0, (r.width * (zScale - 1)) / 2);
    const my = Math.max(0, (r.height * (zScale - 1)) / 2);
    zTx = clamp(zTx, -mx, mx);
    zTy = clamp(zTy, -my, my);
  }

  function resetZoom() {
    zScale = 1; zTx = 0; zTy = 0;
    applyZoom();
  }
  const onBtn = (t) => t && t.closest && t.closest('button');

  stage.addEventListener('touchstart', (e) => {
    if (onBtn(e.target)) return;          // 别把点按钮当成手势
    const r = stage.getBoundingClientRect();
    if (e.touches.length === 1) {
      gesture = { mode: 'pan', x: e.touches[0].clientX, y: e.touches[0].clientY,
                  tx: zTx, ty: zTy, moved: 0 };
    } else if (e.touches.length === 2) {
      const [a, b] = e.touches;
      const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
      gesture = {
        mode: 'pinch',
        dist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        scale: zScale, tx: zTx, ty: zTy,
        // 锚点: 双指初始中点(相对 stage 中心), 缩放时让它跟着手指走
        ax: cx - r.left - r.width / 2,
        ay: cy - r.top - r.height / 2,
      };
      e.preventDefault();                 // 拦住浏览器的页面缩放
    }
  }, { passive: false });

  stage.addEventListener('touchmove', (e) => {
    if (!gesture) return;
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    if (gesture.mode === 'pan' && e.touches.length === 1) {
      if (zScale <= 1) return;            // 没放大就没什么可拖的
      const dx = e.touches[0].clientX - gesture.x;
      const dy = e.touches[0].clientY - gesture.y;
      gesture.moved = Math.max(gesture.moved, Math.abs(dx) + Math.abs(dy));
      zTx = gesture.tx + dx;
      zTy = gesture.ty + dy;
      clampPan();
      applyZoom();
    } else if (gesture.mode === 'pinch' && e.touches.length === 2) {
      const [a, b] = e.touches;
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      const ns = clamp(gesture.scale * (d / gesture.dist), ZMIN, ZMAX);
      const k = ns / gesture.scale;
      const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
      const bx = cx - r.left - r.width / 2;
      const by = cy - r.top - r.height / 2;
      // 让"初始中点对应的那个图像点"跟着手指移动到当前中点
      zScale = ns;
      zTx = bx - (gesture.ax - gesture.tx) * k;
      zTy = by - (gesture.ay - gesture.ty) * k;
      clampPan();
      applyZoom();
    }
  }, { passive: false });

  stage.addEventListener('touchend', (e) => {
    if (e.touches.length === 0) {
      if (gesture && gesture.mode === 'pan' && gesture.moved < 10) {
        const now = Date.now();
        if (now - lastTap < 300) { resetZoom(); lastTap = 0; }   // 双击复位
        else lastTap = now;
      }
      gesture = null;
    } else if (e.touches.length === 1 && gesture && gesture.mode === 'pinch') {
      // 双指松掉一根 -> 无缝接回单指拖动
      gesture = { mode: 'pan', x: e.touches[0].clientX, y: e.touches[0].clientY,
                  tx: zTx, ty: zTy, moved: 99 };
    }
  });

  // 桌面/触控板: 滚轮缩放
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    const px = e.clientX - r.left - r.width / 2;
    const py = e.clientY - r.top - r.height / 2;
    const ns = clamp(zScale * (e.deltaY < 0 ? 1.15 : 1 / 1.15), ZMIN, ZMAX);
    const k = ns / zScale;
    zTx = px - (px - zTx) * k;
    zTy = py - (py - zTy) * k;
    zScale = ns;
    clampPan();
    applyZoom();
  }, { passive: false });

  stage.addEventListener('dblclick', resetZoom);

  const snap = () => {
    if (!img.src) return;
    const a = document.createElement('a');
    a.href = img.src;
    a.download = `cam-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
    a.click();
  };
  btnSnap.addEventListener('click', snap);
  btnFsSnap.addEventListener('click', snap);   // 全屏时左上角那个

  // 离开页面时**主动**断开, 画面和麦克风都停。
  // 不能指望浏览器一定发 WebSocket close 帧 —— 点"返回"/关标签/跳转时,
  // 有的浏览器只是把 TCP 放着, 服务端要等超时才反应过来, 摄像头会白开一段。
  // pagehide 是移动端最可靠的"页面要走"信号(beforeunload 在手机上常常不触发)。
  let byeSent = false;
  function sayBye() {
    if (byeSent) return;
    byeSent = true;
    leaving = true;
    clearTimeout(timer);
    stopAudio();
    closeWs();
  }
  window.addEventListener('pagehide', sayBye);
  window.addEventListener('beforeunload', sayBye);

  // ---- 启动 ----
  // 画面/麦克风**默认都是关的**: 这里什么都不采, 等用户点开关。
  syncSwitches();
  if (window.LoadTip) LoadTip.start(['页面', '样式', '时钟对齐']);
  syncClock().then(() => { if (window.LoadTip) LoadTip.step('时钟对齐'); });   // 只为"画面是多少毫秒以前的"做准备
  resetMeter();
})();
