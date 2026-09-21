/* 摄像头画面: 服务端 ffmpeg 无头采集 -> WebSocket 推 JPEG 帧 -> 这里显示。
   电脑端全程没有任何窗口; 连接一断服务端就停采集(见 server.py ws_cam_bridge)。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const img = $('cam-img'), hint = $('cam-hint'), st = $('cam-status'), errEl = $('cam-err');
  const btnSwitch = $('cam-switch');
  const selSize = $('cam-size'), selFps = $('cam-fps');
  const osd = $('cam-osd');

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

  let ws = null, timer = 0, retry = 0, stopped = false, lastBusy = false;
  let prevUrl = null;            // 上一帧的 objectURL, 显示新帧后释放, 防内存泄漏
  // OSD 统计: 实测帧率 / 码率 / 连接时长
  let frames = 0, bytes = 0, tickAt = Date.now(), startedAt = 0;
  let curDev = '', curSize = '', curSetFps = '';

  const hms = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

  function paintOsd(realFps, kbps) {
    const now = new Date();
    const clock = now.toTimeString().slice(0, 8);
    const dur = startedAt ? hms(Math.floor((Date.now() - startedAt) / 1000)) : '--:--';
    osd.innerHTML =
      `${clock}　·　<b>${realFps}</b> fps　·　<b>${kbps}</b> KB/s\n` +
      `${curSize || '--'} @ ${curSetFps || '--'}fps　·　已连 ${dur}` +
      (zScale > 1.01 ? `　·　<b>×${zScale.toFixed(1)}</b>` : '') +
      `　·　${curDev ? devLabel(curDev) : '--'}`;
  }

  // 记住上次的选择; 上次那个摄像头拔掉了就自动回到默认的(内置那个)
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
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped) return;
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
    if (stopped) return;
    clearTimeout(timer);
    resetZoom();          // 换设备/换分辨率后画面尺寸变了, 旧的缩放比例没意义
    if (ws) { try { ws.onclose = null; ws.close(); } catch (e) { /* ignore */ } }
    errEl.textContent = '';
    st.textContent = '连接中…';
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
        if (j.t === 'start') {
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
    if (stopped || document.hidden) return;
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ t: 'hb' })); } catch (e) { /* ignore */ }
    }
  }
  setInterval(hb, 5000);

  // 每 1s 刷新 OSD: 实测帧率 / 实测码率 / 已连时长 / 本地时间
  setInterval(() => {
    const now = Date.now();
    const dt = Math.max(0.001, (now - tickAt) / 1000);
    const f = startedAt ? Math.round(frames / dt) : '--';
    const kbps = startedAt ? Math.round(bytes / dt / 1024) : '--';
    frames = 0; bytes = 0; tickAt = now;
    paintOsd(f, kbps);
  }, 1000);

  [selSize, selFps].forEach((s) => {
    s.addEventListener('change', () => { save(); retry = 0; connect(); });
  });

  // 切换摄像头: 只有一个时按钮是隐藏的(服务端按数量决定), 这里也兜一层
  function syncSwitch() {
    if (DEVS.length < 2) { btnSwitch.style.display = 'none'; return; }
    btnSwitch.textContent = `🔄 切换 · ${devIdx + 1}/${DEVS.length}`;
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
  $('cam-reconnect').addEventListener('click', () => { retry = 0; connect(); });

  // 切到后台就断开: 省电, 也别让摄像头在没人看的时候还开着
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopped = true;
      clearTimeout(timer);
      if (ws) { try { ws.onclose = null; ws.close(); } catch (e) { /* ignore */ } }
      if (audioOn) setAudio(false);      // 切后台就别再收着音了
      st.textContent = '已暂停(切后台)';
    } else {
      stopped = false;
      retry = 0;
      connect();
    }
  });

  // ---- 全屏 / 横屏观看 ----
  // 全屏目标是整个 .cam-wrap(画面 + 下方信息条), 这样全屏时信息条还在
  const wrap = $('cam-wrap');
  const btnFs = $('cam-fs');
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
    btnFs.textContent = inFs() ? '⛶ 退出' : '⛶ 全屏';
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

  // ---- 声音(默认关, 点了才采) ----
  // 走 HTTP 流式 MP3: 浏览器原生解码, 手机上省电。代价是 1~2 秒缓冲延迟。
  const audioEl = $('cam-audio-el');
  const btnAudio = $('cam-audio'), btnAudioFs = $('cam-fs-audio');
  const volEl = $('cam-vol'), audioState = $('cam-audio-state');
  let audioOn = false;
let silentOnly = false;   // 本机不出声, 只发手机(服务端走虚拟输出)

  function syncAudio() {
    const canUse = micReady !== false;          // 检测中(null)也允许点
    btnAudio.disabled = !canUse;
    btnAudio.style.opacity = canUse ? '1' : '.4';
    btnAudio.textContent = audioOn ? '🔊' : '🔕';
    btnAudio.title = !canUse ? '没有可用的麦克风'
      : (audioOn ? '关闭麦克风' : '开启电脑麦克风');
    btnAudio.classList.toggle('on', audioOn);
    btnAudioFs.textContent = audioOn ? '🔊' : '🔕';
    btnAudioFs.title = audioOn ? '关闭麦克风' : '开启麦克风';
    btnAudioFs.classList.toggle('on', audioOn);
    audioState.hidden = !audioOn;
    volEl.disabled = !audioOn;
    volEl.style.opacity = audioOn ? '1' : '.4';
  }

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

  // 麦克风可用性: null=还没检测完 / {label,src}=可用 / false=不可用
  // **实测**出来的(服务端会真的试采一下) —— 设备存在不代表采得到声音,
  // 这台机器的内置 DMIC 就是"设备在、一采就挂起"。
  const micInfoEl = $('cam-mic-info');
  let micReady = null;

  async function probeMic() {
    try {
      const r = await fetch('/api/audio/mics', { cache: 'no-store' });
      const j = await r.json();
      const list = j.mics || [];
      if (j.usable) {
        micReady = j.usable;
        micInfoEl.textContent = '麦克风可用: ' + (j.usable.label || j.usable.src);
        micInfoEl.className = 'mic-info ok';
        micInfoEl.title = j.usable.src;
      } else {
        micReady = false;
        micInfoEl.textContent = list.length
          ? `没有可用的麦克风（检测到 ${list.length} 个设备，但都采不到声音）`
          : '没有检测到麦克风设备';
        micInfoEl.className = 'mic-info bad';
        micInfoEl.title = (list.map((m) => `${m.src} → ${m.max_volume}dB`).join('\n'));
      }
    } catch (e) {
      micReady = false;
      micInfoEl.textContent = '麦克风检测失败';
      micInfoEl.className = 'mic-info bad';
    }
    syncAudio();
  }

  function setAudio(on) {
    if (on && micReady === false) {
      // 明确告诉用户为什么不能开, 而不是让它静默失败
      errEl.textContent = '⚠ ' + (micInfoEl.textContent || '没有可用的麦克风');
      micInfoEl.className = 'mic-info bad';
      return;
    }
    audioOn = on;
    if (on) {
      initAnalyser();                     // 必须在用户手势里建 AudioContext
      // 加时间戳: 别让浏览器/中间代理拿缓存的旧流
      audioEl.src = '/api/audio?src=' + (silentOnly ? 'silent' : 'default')
        + '&_=' + Date.now();
      const pr = audioEl.play();
      if (pr && pr.catch) {
        pr.catch((e) => {
          audioOn = false;
          syncAudio();
          errEl.textContent = '⚠ 播放声音失败: ' + (e && e.message ? e.message : e);
        });
      }
    } else {
      audioEl.pause();
      audioEl.removeAttribute('src');
      audioEl.load();          // 关掉连接 —— 服务端检测到就会停止采集
    }
    syncAudio();
  }

  // 实时电平: 用 WebAudio 量一下手机端实际播出来的声音。
  // 采到静音源(选成扬声器回环/麦克风被静音)时, 这条会一直是空的 ——
  // 比"听不到声音但不知道为什么"强得多。
  const levelBox = $('cam-level'), levelBar = $('cam-level-bar');
  let actx = null, analyser = null;

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

  setInterval(() => {
    if (!audioOn || !analyser) {
      levelBar.style.width = '0%';
      levelBox.classList.remove('dead');
      return;
    }
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const d = (buf[i] - 128) / 128;
      sum += d * d;
    }
    const rms = Math.sqrt(sum / buf.length);
    const pct = Math.min(100, rms * 400);
    levelBar.style.width = pct.toFixed(0) + '%';
    // 开启了一段时间却始终静悄悄 -> 标灰提示
    levelBox.classList.toggle('dead', pct < 2);
  }, 200);

  audioEl.addEventListener('error', () => {
    if (!audioOn) return;      // 停止时会触发一次无害的 error, 忽略
    errEl.textContent = '⚠ 声音中断(可能没有麦克风或采集失败)';
    audioOn = false;
    syncAudio();
  });

  [btnAudio, btnAudioFs].forEach((b) => {
    b.addEventListener('click', () => setAudio(!audioOn));
  });
  syncAudio();

  const snap = () => {
    if (!img.src) return;
    const a = document.createElement('a');
    a.href = img.src;
    a.download = `cam-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
    a.click();
  };
  $('cam-snap').addEventListener('click', snap);
  $('cam-fs-snap').addEventListener('click', snap);   // 全屏时左上角那个

  // 离开页面时**主动**断开。
  // 不能指望浏览器一定发 WebSocket close 帧 —— 点"返回"/关标签/跳转时,
  // 有的浏览器只是把 TCP 放着, 服务端要等超时才反应过来, 摄像头会白开一段。
  // pagehide 是移动端最可靠的"页面要走"信号(beforeunload 在手机上常常不触发)。
  let byeSent = false;
  function sayBye() {
    if (byeSent) return;
    byeSent = true;
    stopped = true;
    clearTimeout(timer);
    if (audioOn) setAudio(false);
    try {
      if (ws) { ws.onclose = null; ws.close(); }
    } catch (e) { /* ignore */ }
  }
  window.addEventListener('pagehide', sayBye);
  window.addEventListener('beforeunload', sayBye);

  connect();
  // 进页面就实测一遍麦克风(要试采几秒), 结果出来前按钮先按"可用"处理
  probeMic();
})();
