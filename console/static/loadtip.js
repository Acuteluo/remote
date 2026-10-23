// 进入页面时的加载进度(小框, 只显示百分比)。
//
// 观感沿用远程桌面那个"已按住左键"的提示框: 半透明圆角、白字、pointer-events:none
// 不挡操作。区别是**做成一个小胶囊**, 内容只有百分比(0% -> 30% -> ... -> 100%),
// 当前在等哪一步放在 title 里(长按/悬停能看到), 不占版面。
// 到 100% 后停留 1 秒再渐隐。
//
// 这个文件由服务端自动注入到每个页面的 </head> 前(见 server.py 的 render),
// 所以新加的页面不用手动引。
//
// 用法(模块想报得更细时调用; 不调用就走下面的默认行为):
//   LoadTip.start(['页面', '样式', '建立连接', '首帧画面']);
//   LoadTip.step('建立连接');
//   LoadTip.fail('建立连接', '连不上');     // 变红并显示原因
//
// '页面' 和 '样式' 两步**不用自己 step** —— 这个文件会替所有页面标完成(见 autoBase)。
(function () {
  'use strict';

  var SHOW_DELAY = 0;      // 立刻出现。以前这里设 100ms "秒开就不闪", 结果采集模块
                           // 这种几十毫秒就好的页面**永远看不到**提示(用户反馈过)
  var HOLD_MS = 1000;      // 到 100% 后停留多久再渐隐(用户要求 1 秒)
  var MAX_MS = 30000;      // 兜底: 30 秒还没完成就别一直挂着
  var el = null, nm = null, num = null, bar = null;
  var steps = [], doneMap = {}, finished = false, used = false, startedAt = 0;
  var showTimer = 0, holdTimer = 0, maxTimer = 0;

  function build() {
    if (el) return;
    el = document.createElement('div');
    el.id = 'load-tip';
    el.className = 'load-tip';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = '<span class="lt-nm"></span><span class="lt-num"></span>'
                  + '<span class="lt-bar"><i></i></span>';
    nm = el.querySelector('.lt-nm');
    num = el.querySelector('.lt-num');
    bar = el.querySelector('.lt-bar > i');
    (document.body || document.documentElement).appendChild(el);
  }

  function percent() {
    if (!steps.length) return 100;
    var n = 0;
    for (var i = 0; i < steps.length; i++) if (doneMap[steps[i]]) n++;
    return Math.round(100 * n / steps.length);
  }
  function currentStep() {
    for (var j = 0; j < steps.length; j++) if (!doneMap[steps[j]]) return steps[j];
    return '';
  }

  function paint() {
    if (!el) return;
    var p = percent();
    var cur = currentStep();
    // 小框里只放"当前步骤 + 百分比", 整句说明放 title(长按/悬停可见)
    nm.textContent = cur ? cur : '已就绪';
    num.textContent = p + '%';
    bar.style.width = p + '%';
    el.title = (cur ? '正在' + cur + '…' : '已就绪')
             + '\n' + steps.map(function (x) {
                 return (doneMap[x] ? '✓ ' : '· ') + x;
               }).join('\n');
  }

  function reveal() {
    build();
    el.classList.remove('gone', 'bad');
    el.classList.add('on');
    paint();
  }

  function hide(afterMs) {
    if (holdTimer) return;
    holdTimer = setTimeout(function () {
      if (!el) return;
      el.classList.remove('on');
      el.classList.add('gone');
    }, afterMs);
  }

  // 所有步骤都完成 -> 收尾。返回 true 表示"确实完成了"。
  function finishIfDone() {
    if (currentStep()) return false;
    finished = true;
    clearTimeout(maxTimer);
    clearTimeout(showTimer);
    // 注意: 这里**不能**因为"还没露面"就跳过 —— 用户要求每次进页面都能看到,
    // 哪怕它 10ms 就加载完(那就直接显示 100%)。
    build();
    el.classList.remove('bad');
    el.classList.add('on', 'good');
    paint();
    hide(HOLD_MS);                        // 到 100% 后 1 秒渐隐
    return true;
  }

  function mark(name) {
    if (doneMap[name]) return;
    doneMap[name] = true;
    if (!finishIfDone() && el) paint();
  }

  // '页面' / '样式' 由这里统一标完成:
  //   页面 —— 页面脚本能跑起来就说明 DOM 已经好了(脚本都在 </body> 前);
  //   样式 —— app.css 是 <head> 里的阻塞资源, 这里再确认一下 CSS 变量真的生效了。
  // 以前漏了这步, 结果进度永远停在 (n-1)/n, 小框一直挂着不消失。
  function autoBase() {
    var markPage = function () {
      mark('页面');
      if (steps.indexOf('样式') >= 0) {
        var acc = '';
        try {
          acc = getComputedStyle(document.documentElement)
                  .getPropertyValue('--acc').trim();
        } catch (e) { /* 拿不到就当作没生效 */ }
        mark('样式');                     // 拿到了或拿不到都算这步走完(别卡住)
      }
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', markPage, { once: true });
    } else {
      markPage();
    }
  }

  var api = {
    start: function (list) {
      used = true;
      steps = (list && list.length) ? list.slice() : ['页面', '样式'];
      doneMap = {};
      finished = false;
      startedAt = Date.now();
      clearTimeout(holdTimer); holdTimer = 0;
      clearTimeout(maxTimer);
      clearTimeout(showTimer);
      showTimer = setTimeout(function () { if (!finished) reveal(); }, SHOW_DELAY);
      maxTimer = setTimeout(function () {
        if (finished) return;
        build();
        el.classList.remove('good');
        el.classList.add('on', 'bad');
        nm.textContent = '加载超时';
        num.textContent = '超时';
        el.title = '加载超时，请刷新重试';
        hide(3000);
      }, MAX_MS);
      autoBase();
      return api;
    },
    step: function (name) { mark(name); return api; },
    fail: function (name, why) {
      used = true;
      clearTimeout(showTimer);
      clearTimeout(maxTimer);
      finished = true;
      build();
      el.classList.remove('good');
      el.classList.add('on', 'bad');
      if (name) doneMap[name] = true;
      nm.textContent = name || '加载';
      num.textContent = '失败';
      el.title = (name ? name + '失败' : '加载失败') + (why ? '：' + why : '');
      hide(2500);
      return api;
    },
  };
  window.LoadTip = api;

  // 默认行为: 没人显式 start 的话, 按"页面 + 样式"报一次
  function auto() {
    if (used) return;
    api.start(['页面', '样式']);
    if (document.readyState === 'complete') api.step('样式');
    else window.addEventListener('load', function () { api.step('样式'); });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(auto, 0); });
  } else {
    setTimeout(auto, 0);
  }
})();

/* ---- 自动感知"服务端更新了" ----
   这些页面是**常驻**的(挂着 WebSocket), 不会自己刷新。于是服务端更新完,
   手机上看到的还是打开那一刻的旧界面 —— 用户就踩过: 加好的折叠面板在手机上
   根本没有("我的折叠呢?")。

   做法: 拿自己 script 标签上的 ?v= 当"我加载时的版本", 每 60 秒问一次
   服务端当前版本。不一样就提示刷新; 页面在后台(看不见)时直接静默刷新。
   注意 /api/version 也带上时间戳参数 —— 用户那台网络里有设备会缓存响应。 */
(function () {
  // 服务端注入的"这一版"版本号(所有资源里最新的那个)。**不能**拿 loadtip.js
  // 自己的 ?v= 当基准 —— 那是单个文件的修改时间, 跟"最新"不是一回事, 会导致
  // 提示永远消不掉(用户遇到过: "刷新好几次还一直提示")。
  const myV = window.__MEOW_V || (function () {
    const me = document.currentScript ||
      Array.prototype.slice.call(document.scripts).filter(function (s) {
        return /loadtip\.js/.test(s.src); }) [0];
    const m = me && me.src && me.src.match(/[?&]v=(\d+)/);
    return m ? m[1] : '';
  })();
  if (!myV) return;                       // 拿不到版本就不折腾
  let hinted = false;

  function hint() {
    if (hinted || document.getElementById('ver-hint')) return;
    hinted = true;
    const d = document.createElement('div');
    d.id = 'ver-hint';
    d.className = 'load-tip';
    d.style.cssText = 'pointer-events:auto;cursor:pointer;opacity:1;' +
      'bottom:calc(14px + env(safe-area-inset-bottom));top:auto;left:50%;right:auto;' +
      'transform:translateX(-50%);';
    d.innerHTML = '<span class="lt-nm">页面已更新</span>' +
      '<span class="lt-num">点这里刷新</span>';
    d.addEventListener('click', function () { location.reload(); });
    document.body.appendChild(d);
  }

  function check() {
    fetch('/api/version?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || !j.v || j.v === myV) return;
        // **只提示, 绝不自动刷新。**
        // 想过"用户没在看就直接刷新", 但那样会打断正在进行的会话 —— 比如采集
        // 模块在后台收音(手机锁屏时还在录), 一刷新就断了。宁可让他自己点一下。
        hint();
      })
      .catch(function () { /* 网络不好就算了, 下次再说 */ });
  }
  setTimeout(check, 5000);
  setInterval(check, 60000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') check();
  });
})();
