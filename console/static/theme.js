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
  // 句库 = 手写的固定句 + 三组模板的组合。
  // 为什么用模板: 手写硬堆到 400 条, 越到后面越勉强 —— 之前"语料库有点奇怪"就是这么来的。
  // 模板里每一块都是能自然拼接的完整短语, 拼出来近 800 句, 而且读着不别扭。
  const FIXED = [
    '今天要不吃汉堡？', '要不要给自己泡杯茶？', '窗外的云今天像什么？',
    '上一次笑出声是什么时候？', '要不要给家里打个电话？', '桌上的水是不是放太久了？',
    '要不要出去走十分钟？', '要不要给旧照片建个相册？', '今天有没有夸过谁？',
    '要不要现在就伸个懒腰？', '今天的风是从哪边吹过来的？', '耳机线是不是又缠成一团了？',
    '今天想换条路走吗？', '要不要给自己买束花？', '上次看日落是什么时候？',
    '今天喝够水了吗？', '要不要把闹钟往后调十分钟？', '要不要学着做一道新菜？',
    '要不要给手机换个壁纸？', '最近有没有抬头看过星星？', '要不要把窗子开一会儿？',
    '今天的天气适合出门吗？', '有没有想吃很久的店？', '今天有没有遇到有趣的人？',
    '要不要把常听的歌换个顺序？', '有没有想养却没养的植物？', '要不要试试十分钟不看屏幕？',
    '有没有一件小事让你笑了？', '要不要数数今天叹了几次气？', '有没有想学的奇怪小技能？',
    '要不要把窗户擦一擦？', '今天想早点睡还是晚点睡？', '今天想吃点甜的吗？',
    '要不要给耳机充个电？', '有没有想再看一遍的剧？', '今天有没有晒太阳？',
    '有没有想给谁写封信？', '今天听歌单曲循环了吗？', '有没有想拍的日常照片？',
    '今天有没有跟猫对视过？', '要不要把窗帘拉开一点？', '今天有没有夸夸自己？',
    '有没有想学的简单乐器？', '今天有没有好好呼吸？', '有没有想去公园坐坐？',
    '今天有没有吃到喜欢的东西？', '有没有想一起吃饭的人？', '今天有没有走神去哪儿了？',
    '今天有没有遇到小幸运？', '有没有想重新开始的爱好？', '今天有没有对陌生人笑过？',
    '今天有没有被什么打动？', '有没有想养的宠物名字？', '最近有没有听过好听的风声？',
    '今天有没有摸到毛茸茸的东西？', '今天有没有睡到自然醒？',
  ];

  // 提议: 要不要 + 动作 + 语气
  const ASK_PRE = ['要不要', '想不想', '要不要试试', '是不是该', '不妨'];
  const ASK_ACT = [
    '给自己泡杯茶', '出去走十分钟', '晒会儿太阳', '抱抱自己', '早点睡',
    '吃块小蛋糕', '给花浇点水', '听听喜欢的歌', '伸个懒腰', '看看窗外',
    '给朋友发条消息', '睡个小午觉', '泡个热水澡', '换个心情', '买杯喝的',
    '慢慢吃顿饭', '揉揉眼睛', '深呼吸三次', '把手机放下一会儿', '翻两页书',
    '写三行字', '给房间透透气', '找首歌单曲循环', '夸夸自己', '抱一会儿猫',
    '摸摸头发', '哼两句歌', '喝口温水', '换个姿势坐着', '把桌面理一理',
    '给明天的自己留张小纸条', '走另一条路回家', '抬头看看云', '找找今天的月亮',
    '拍一张随手照', '听雨声发会儿呆', '把窗帘拉开一点', '把袜子换成软的', '早点关灯',
    '对着镜子笑一下',
  ];
  const ASK_END = ['？', '呀？', '呢？', '嘛？'];

  // 关心: 今天 + 有没有 + 事情
  const HAVE_PRE = ['今天', '最近', '这几天', '这两天'];
  const HAVE_ACT = [
    '好好吃饭', '喝够水', '睡够觉', '出门走走', '晒到太阳',
    '笑出声', '夸过自己', '给自己留点时间', '抬头看天', '慢慢吃完一顿饭',
    '听到喜欢的歌', '想起某个人', '做一件小小的好事', '对镜子笑一下', '摸摸小猫',
    '认真发过呆', '闻过花香', '把喜欢的事做一会儿', '跟自己说声辛苦了', '抱一抱自己',
    '早睡过一次', '被风吹到过',
  ];

  // 状态: 今天 + 是不是 + 感受
  const MOOD_PRE = ['今天', '最近', '这会儿'];
  const MOOD_ACT = [
    '有点累', '有点困', '有点饿', '有点想家', '有点开心',
    '有点闷', '有点忙', '有点走神', '有点想出门', '有点想安静一会儿',
  ];

  const SAYS = (() => {
    const out = FIXED.slice();
    for (const p of ASK_PRE) for (const a of ASK_ACT) for (const e of ASK_END) {
      out.push(p + a + e);
    }
    for (const p of HAVE_PRE) for (const a of HAVE_ACT) out.push(p + '有没有' + a + '？');
    for (const p of MOOD_PRE) for (const a of MOOD_ACT) out.push(p + '是不是' + a + '？');
    return out;
  })();

  // 词库分两组 —— 关键: **不是所有语气词都能放句首**。
  // 反例(用户点出来的): "哼唧 要不要把桌子收一收？" 很怪 —— 哼唧是撒娇/抱怨声,
  // 当不了提议的引子。所以能开头的只留"叫一声"类的中性可爱音, 其余一律只放句尾。
  const TAILS_FRONT = [
    '喵喵', '喵呜', '咪呜', '嗷呜', '啾啾', '咪咪',
    '嗷喵', '喵咪', '咕噜咕噜', '喵呜喵呜', '咪呜咪呜', '啾啾啾',
  ];
  const TAILS_END = [
    'owo', 'OwO', '0w0', 'xwx', '>w<', '^w^', 'UwU', 'qwq', 'TwT', 'awa',
    'OvO', '>_<', '(๑•̀ㅂ•́)و', '(=^･ω･^=)', '(*/ω＼*)', '(´･ω･`)',
    '（￣▽￣）', '(๑´ㅂ`๑)', '(=①ω①=)', '(*´ω｀*)', '(๑･ω･๑)', 'U・ω・U',
    '>ㅅ<', '♡', '♪', '✧', '哼唧', '唧唧', '咻咻', '呼噜呼噜', '噜噜',
    '喵嗷', '呜喵', '啾呜', '咪啾', '喵喵', '咪呜～',
  ];

  const oneOf = (a) => a[Math.floor(Math.random() * a.length)];
  const recent = [];                    // 最近出现过的几条, 避免"怎么又是这句"

  function compose() {
    const body = oneOf(SAYS), r = Math.random();
    if (r < 0.22) return body;                                  // 纯句子
    if (r < 0.62) return body + ' ' + oneOf(TAILS_END);          // 句尾挂颜文字
    if (r < 0.72) return oneOf(TAILS_FRONT) + '，' + body;        // 叫一声 + 提议
    return body + ' ' + oneOf(TAILS_FRONT);                     // 句尾挂叫声
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
