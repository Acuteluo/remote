/* 主题色圆盘(页面逻辑)。
 *
 * 颜色数学全在 theme-color.js 里(纯函数, Node 里也能跑), 这里只管交互和渲染。
 * 关键点: 盘面渲染和取色用**同一个** ThemeColor.colorAt() —— 所以指示器小圆圈
 * 里的颜色和它指向的盘面颜色永远一致, 不会"看着是 A 取出来是 B"。
 * 服务端还会再拦一道亮度(见 server.py 的 THEME_MIN_LUM), 那是防止绕过页面直接调接口;
 * 盘面本身的颜色下限(FLOOR)比服务端门槛更高, 所以**页面上能选的一定存得进去**。
 */
(() => {
  const T = window.ThemeColor;
  const $ = (id) => document.getElementById(id);
  const cv = $('th-disc'), holder = $('th-holder'), pick = $('th-pick'),
        hexEl = $('th-hex'), sayEl = $('th-say'), st = $('th-status'),
        defEl = $('th-def');
  const DEFAULT = (defEl.textContent || '#4da3ff').trim();

  let SIZE = 320;              // 圆盘逻辑边长(resize 时重算)
  let cur = DEFAULT;           // 当前颜色 #rrggbb
  let dragging = false, sentAt = 0, sayAt = 0, rt = null;

  // ---- 画盘面 ----
  function draw() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(SIZE * dpr);
    cv.height = Math.round(SIZE * dpr);
    cv.style.width = SIZE + 'px';
    cv.style.height = SIZE + 'px';
    holder.style.width = SIZE + 'px';
    holder.style.height = SIZE + 'px';
    const ctx = cv.getContext('2d'),
          img = ctx.createImageData(cv.width, cv.height),
          d = img.data, half = cv.width / 2;
    for (let y = 0; y < cv.height; y++) {
      const dy = y - half + 0.5;
      for (let x = 0; x < cv.width; x++) {
        const dx = x - half + 0.5, dist = Math.hypot(dx, dy),
              i = (y * cv.width + x) * 4;
        if (dist > half) { d[i + 3] = 0; continue; }        // 圆外留透明
        const [r, g, b] = T.colorAt(dist / half, Math.atan2(dy, dx) * 180 / Math.PI);
        d[i] = r; d[i + 1] = g; d[i + 2] = b; d[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  // ---- 指示器(小圆圈) ----
  function polar(rad, ang) {
    const a = ang * Math.PI / 180, r = Math.max(0, Math.min(1, rad)) * SIZE / 2;
    return { x: SIZE / 2 + Math.cos(a) * r, y: SIZE / 2 + Math.sin(a) * r };
  }

  function placeIndicator(x, y, color) {
    pick.style.left = x + 'px';
    pick.style.top = y + 'px';
    if (color) pick.style.background = color;
  }

  // 由颜色反推盘面位置 —— 只用于放**初始**那个指示器(服务端存的颜色)。
  // 之后每次取色都是正向换算, 精确一致。
  function placeFromHex(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return;
    const v = parseInt(m[1], 16);
    const rgb = [(v >> 16) & 255, (v >> 8) & 255, v & 255];
    const p = polar(T.radiusOf(rgb), T.rgbToHsl(rgb[0], rgb[1], rgb[2])[0]);
    placeIndicator(p.x, p.y, hex);
  }

  // ---- 随机句子 ----
  const SAYS = [
    '今天要不吃汉堡？',
    '要不要给自己泡杯茶？',
    '窗外的云今天像什么？',
    '上一次笑出声是什么时候？',
    '手机里有没有三个月没打开的应用？',
    '要不要给家里打个电话？',
    '今晚几点睡才算够本？',
    '桌上那杯水是不是放太久了？',
    '要不要把明天的待办先写下来？',
    '最近有没有从头到尾听完一张专辑？',
    '给窗边的植物转个方向吧？',
    '要不要出去走十分钟？',
    '你有多久没用纸笔记东西了？',
    '冰箱里那个还剩一半的还能吃吗？',
    '要不要把椅子调高一点点？',
    '今天有没有夸过谁？',
    '要不要给旧照片建个相册？',
    '这首曲子今天听第几遍了？',
    '有没有一句话想写给以后的自己？',
    '要不要把手机翻过来放一会儿？',
    '午饭吃到七分饱了没？',
    '今天看到的第一个笑话是什么？',
    '要不要把桌面收拾出一块空地？',
    '楼下那家新开的店你尝过了吗？',
    '有没有一本书读到一半就搁下了？',
    '要不要现在就伸个懒腰？',
    '今天的风是从哪边吹过来的？',
    '耳机线是不是又缠成一团了？',
  ];

  function newSentence() {
    const old = sayEl.textContent;
    let s = old;
    for (let i = 0; i < 8 && s === old; i++) {
      s = SAYS[Math.floor(Math.random() * SAYS.length)];
    }
    sayEl.textContent = s;
  }

  // 拖动时别每帧都换句子(会闪), 500ms 换一次 —— 取色变了这句就跟着变
  function maybeSentence() {
    const now = Date.now();
    if (now - sayAt < 500) return;
    sayAt = now;
    newSentence();
  }

  // ---- 保存 ----
  function post(force) {
    const now = Date.now();
    if (!force && now - sentAt < 250) return;      // 拖动中攒批, 松手时必定补一次
    sentAt = now;
    fetch('/api/theme', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ acc: cur }),
    }).then((r) => r.json()).then((j) => {
      st.textContent = (j && j.ok) ? '已保存' : ('⚠ ' + ((j && j.err) || '保存失败'));
    }).catch(() => { st.textContent = '保存失败(服务没起?)'; });
  }

  function apply(hex, opts) {
    cur = hex;
    document.documentElement.style.setProperty('--acc', hex);   // 整页立刻跟着变
    hexEl.textContent = hex;
    if (!(opts && opts.keepPos)) placeFromHex(hex);
    if (opts && opts.say) newSentence();
  }

  // ---- 取色 ----
  function pickAt(clientX, clientY) {
    const r = cv.getBoundingClientRect(), hw = r.width / 2;
    const dx = clientX - (r.left + hw), dy = clientY - (r.top + hw);
    const dist = Math.hypot(dx, dy), rad = Math.min(1, dist / hw),
          ang = Math.atan2(dy, dx) * 180 / Math.PI;
    const hex = T.toHex(T.colorAt(rad, ang));
    const p = polar(rad, ang);
    cur = hex;
    document.documentElement.style.setProperty('--acc', hex);
    hexEl.textContent = hex;
    placeIndicator(p.x, p.y, hex);       // 指示器就落在这里, 颜色即取到的颜色
    maybeSentence();
    post(false);
  }

  cv.addEventListener('pointerdown', (e) => {
    dragging = true;
    try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    pickAt(e.clientX, e.clientY);
    st.textContent = '松手保存';
  });
  cv.addEventListener('pointermove', (e) => { if (dragging) pickAt(e.clientX, e.clientY); });
  const stop = () => { if (dragging) { dragging = false; post(true); } };
  cv.addEventListener('pointerup', stop);
  cv.addEventListener('pointercancel', stop);

  // ---- 按钮 ----
  $('th-random').addEventListener('click', () => {
    // 半径取 sqrt 才面积均匀, 否则取色都挤在圆心
    const rad = Math.sqrt(Math.random()) * 0.97, ang = Math.random() * 360;
    const hex = T.toHex(T.colorAt(rad, ang)), p = polar(rad, ang);
    placeIndicator(p.x, p.y, hex);
    apply(hex, { say: true, keepPos: true });
    post(true);
    st.textContent = '随便挑了一个';
  });
  $('th-reset').addEventListener('click', () => {
    apply(DEFAULT, { say: true });
    post(true);
    st.textContent = '已恢复默认蓝';
  });

  // ---- 初始化 / 窗口变化 ----
  function layout() {
    SIZE = Math.round(Math.max(190, Math.min(320,
      window.innerWidth * 0.8, window.innerHeight * 0.52)));
    draw();
    placeFromHex(cur);
  }

  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(layout, 200);
  });

  layout();
  fetch('/api/theme', { cache: 'no-store' })
    .then((r) => r.json())
    .then((j) => {
      apply(j && j.ok ? j.acc : DEFAULT, { keepPos: false });
      st.textContent = '拖动圆盘试试';
    })
    .catch(() => { apply(DEFAULT, { keepPos: false }); });
})();
