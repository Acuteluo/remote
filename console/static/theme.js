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
  // 句库(尽量短, 保证一行放得下) + 词库(颜文字/可爱语气词), 拼起来用。
  // 66 句 × 30 个尾巴 ≈ 上千种组合, 再加上"最近 8 条不重复", 基本不会看着眼熟。
  const SAYS = [
    '今天要不吃汉堡？', '要不要给自己泡杯茶？', '窗外的云今天像什么？',
    '上一次笑出声是什么时候？', '手机里有没有三个月没打开的应用？', '要不要给家里打个电话？',
    '今晚几点睡才算够本？', '桌上那杯水是不是放太久了？', '要不要把明天的待办先写下来？',
    '最近有没有从头到尾听完一张专辑？', '给窗边的植物转个方向吧？', '要不要出去走十分钟？',
    '你有多久没用纸笔记东西了？', '冰箱里那个还剩一半的还能吃吗？', '要不要把椅子调高一点点？',
    '今天有没有夸过谁？', '要不要给旧照片建个相册？', '这首曲子今天听第几遍了？',
    '有没有一句话想写给以后的自己？', '要不要把手机翻过来放一会儿？', '午饭吃到七分饱了没？',
    '今天看到的第一个笑话是什么？', '要不要把桌面收拾出一块空地？', '楼下那家新开的店你尝过了吗？',
    '有没有一本书读到一半就搁下了？', '要不要现在就伸个懒腰？', '今天的风是从哪边吹过来的？',
    '耳机线是不是又缠成一团了？', '今天想换条路走吗？', '有没有想见却很久没联系的人？',
    '要不要给自己买束花？', '上次看日落是什么时候？', '今天喝够水了吗？',
    '要不要把闹钟往后调十分钟？', '衣柜里有没有一年没穿的衣服？', '有没有一句话一直没说出口？',
    '要不要学着做一道新菜？', '今天有没有走够六千步？', '要不要给手机换个壁纸？',
    '最近有没有抬头看过星星？', '要不要把窗子开一会儿？', '有没有想重看一遍的老电影？',
    '今天的天气适合出门吗？', '要不要记下今天最开心的一件事？', '有没有想吃很久的店？',
    '要不要给明天的自己留张小纸条？', '今天有没有遇到有趣的人？', '要不要把常听的歌换个顺序？',
    '有没有想养却没养的植物？', '要不要试试十分钟不看屏幕？', '今天有没有好好吃早饭？',
    '有没有一件小事让你笑了？', '要不要把椅子挪到有光的地方？', '最近有没有写过超过三行的话？',
    '要不要给水杯换个位置？', '今天想听点什么样的音乐？', '有没有想去却一直没去的地方？',
    '要不要把明天的衣服先拿出来？', '今天有没有对自己好一点？', '要不要现在就关掉一个通知？',
    '有没有想推荐给别人的东西？', '要不要数数今天叹了几次气？', '今天有没有走得比昨天远一点？',
    '有没有想学的奇怪小技能？', '要不要把窗户擦一擦？', '今天想早点睡还是晚点睡？',
    '今天想吃点甜的吗？', '要不要给耳机充个电？', '有没有想再看一遍的剧？',
    '今天有没有晒太阳？', '要不要把水壶灌满？', '有没有想给谁写封信？',
    '今天听歌单曲循环了吗？', '要不要换条路去买咖啡？', '有没有想拍的日常照片？',
    '今天有没有跟猫对视过？', '要不要把窗帘拉开一点？', '有没有放很久没翻的杂志？',
    '今天有没有夸夸自己？', '要不要把常用应用挪个位置？', '有没有想学的简单乐器？',
    '今天有没有好好呼吸？', '要不要给阳台添个小凳子？', '有没有想去公园坐坐？',
    '今天有没有吃到喜欢的东西？', '要不要把闹钟换个铃声？', '有没有想一起吃饭的人？',
    '今天有没有走神去哪儿了？', '要不要把手机提醒清一清？', '今天有没有遇到小幸运？',
    '要不要把旧笔记翻出来看看？', '有没有想重新开始的爱好？', '今天有没有对陌生人笑过？',
    '要不要给房间换个味道？', '有没有想看的展或电影？', '今天有没有被什么打动？',
    '要不要把最难那件先做掉？', '有没有想养的宠物名字？',
  ];

  const TAILS = [
    'owo', 'OwO', '0w0', 'xwx',
    '>w<', '^w^', 'UwU', 'qwq',
    'TwT', 'awa', 'OvO', '>_<',
    '(๑•̀ㅂ•́)و', '(=^･ω･^=)', '(*/ω＼*)', '(´･ω･`)',
    '（￣▽￣）', '(๑´ㅂ`๑)', '嗷呜', '喵呜',
    '嗷喵？', '咪呜', '嗷嗷', '呜喵',
    '咪嗷', '喵嗷', '咕噜咕噜', '呼噜呼噜',
    '嗷呜～', '咪呜～', '喵喵', '啾啾',
    '咪咪', '呜嗷', '嗷喵', '喵咪',
    '啾呜', '咪啾', '咕噜', '噜噜',
    '呼噜', '咻咻', '哼唧', '唧唧',
    '嗷呜嗷呜', '喵喵喵', '啾啾啾', '咪呜咪呜',
    '喵呜喵呜', '(=①ω①=)', '(*´ω｀*)', '(๑･ω･๑)',
    '(๑˃ᴗ˂)', '(๑>؂<๑)', '(づ｡◕‿‿◕｡)づ', 'ᕕ(',
    'ᐛ', ')ᕗ', '(*^▽^*)', 'U・ω・U',
    '>ㅅ<', '♡', '♪', '✧',
  ];

  // 注意: 别叫 pick —— 上面已经有 const pick = $('th-pick')(指示器元素),
  // 重名会让整个文件 SyntaxError(被 node --check 抓到过)。
  const oneOf = (a) => a[Math.floor(Math.random() * a.length)];
  const recent = [];                    // 最近出现过的几条, 避免"怎么又是这句"

  function compose() {
    const body = oneOf(SAYS), tail = oneOf(TAILS), r = Math.random();
    // 句尾是问号/叹号时用空格接, 否则读起来会别扭("…吗？,owo")
    const sep = /[？?!！]$/.test(body) ? ' ' : '，';
    if (r < 0.24) return body;                 // 纯句子
    if (r < 0.64) return body + ' ' + tail;    // 句尾挂个颜文字
    if (r < 0.86) return tail + sep + body;    // 语气词在前
    return body + sep + tail;                  // 句子后面接词
  }

  function newSentence() {
    let s = '';
    for (let i = 0; i < 16; i++) {
      s = compose();
      if (!recent.includes(s)) break;
    }
    recent.push(s);
    if (recent.length > 8) recent.shift();
    // 淡一下再显示, 换句不突兀
    sayEl.style.opacity = '0.2';
    sayEl.textContent = s;
    fitOneLine();
    setTimeout(() => { sayEl.style.opacity = '1'; }, 40);
  }

  // 一行放不下就把字号往下调(最多缩到 13px)。手机上句子长短 + 屏宽都不一样,
  // 与其让随机出来的句子换行把版面顶开, 不如自己缩一点。
  function fitOneLine() {
    const max = sayEl.clientWidth;
    let size = 19;
    sayEl.style.fontSize = size + 'px';
    while (size > 13 && sayEl.scrollWidth > max) {
      size -= 1;
      sayEl.style.fontSize = size + 'px';
    }
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
    fitOneLine();           // 换了尺寸重新收一下那句话的字号
  }

  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(layout, 200);
  });

  layout();
  // ★ 一进页面就随机挑一句。之前漏了这一步 —— 只有拖动/点按钮才换, 于是每次
  //   进来看到的都是 HTML 里那句写死的默认文字, 看着就像"固定不变"。
  newSentence();
  // 闲着也换: 每 8 秒来一句新的, 不打扰正在拖动的人, 切后台也不换。
  setInterval(() => {
    if (!dragging && !document.hidden) newSentence();
  }, 8000);

  fetch('/api/theme', { cache: 'no-store' })
    .then((r) => r.json())
    .then((j) => {
      apply(j && j.ok ? j.acc : DEFAULT, { keepPos: false });
      st.textContent = '拖动圆盘试试';
    })
    .catch(() => { apply(DEFAULT, { keepPos: false }); });
})();
