/* 自定义背景页: 选图 -> 调参 -> 实时预览 -> 应用 / 恢复默认。
 *
 * 预览和真实页面用**同一套算法**(底图 + 一层暗化 + 面板 alpha), 所以预览里长什么样,
 * 应用后就是什么样:
 *   底图      : background-size: cover, 固定不动(position: fixed)
 *   暗化      : 一层 rgba(11,15,20, D) —— 对应"底图亮度"滑块
 *   面板 alpha: 1 - 透明度滑块/100 —— 对应卡片底色的不透明度
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const f = $('f'), pvImg = $('pv-img'), pvDim = $('pv-dim'), pvCard = $('pv-card'),
        curEl = $('cur'), st = $('bg-status'),
        sT = $('s-trans'), sD = $('s-dim'), sB = $('s-blur'),
        vT = $('v-trans'), vD = $('v-dim'), vB = $('v-blur'),
        btnApply = $('apply'), btnReset = $('reset'), btnClear = $('clear');

  let hasImg = false;      // 服务器上已经有图
  let file = null;         // 这次新选的图(还没上传)
  let objUrl = null;       // 本地预览用的 blob 地址
  let ver = Date.now();    // 服务器图的版本号, 当 cache buster

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // 文件 -> base64(走 JSON 上传; 二进制 body 会被前面那层 JSON 解析卡住)
  function toB64(g) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result).split(',')[1] || '');
      r.onerror = () => rej(new Error('读文件失败'));
      r.readAsDataURL(g);
    });
  }

  function paint() {
    const t = clamp(Number(sT.value), 0, 90);
    const d = clamp(Number(sD.value), 0, 80);
    const b = clamp(Number(sB.value), 0, 20);
    vT.textContent = t + '%';
    vD.textContent = d + '%';
    vB.textContent = b + 'px';

    const src = objUrl || (hasImg ? '/bg/img?v=' + ver : '');
    pvImg.style.backgroundImage = src ? `url("${src}")` : 'none';
    pvImg.style.filter = b ? `blur(${b}px)` : '';
    pvDim.style.background = `rgba(11,15,20,${(d / 100).toFixed(2)})`;
    // 面板 alpha = 1 - 透明度
    pvCard.style.background = `rgb(19 26 35 / ${(1 - t / 100).toFixed(2)})`;
  }

  [sT, sD, sB].forEach((s) => s.addEventListener('input', paint));

  function setStatus(msg) { st.textContent = msg; }

  // ---- 读服务器当前状态 ----
  function load() {
    fetch('/api/bg', { cache: 'no-store' })
      .then((r) => r.json())
      .then((j) => {
        if (!j || !j.ok) { setStatus('⚠ 读不到背景设置'); return; }
        hasImg = !!j.has;
        ver = j.ver || Date.now();
        sT.value = String(clamp(j.trans | 0, 0, 90));
        sD.value = String(clamp(j.dim | 0, 0, 80));
        sB.value = String(clamp(j.blur | 0, 0, 20));
        curEl.textContent = hasImg
          ? `已设置背景图(${Math.round((j.bytes || 0) / 1024)} KB) · 透明度 ${
            sT.value}% · 亮度 ${sD.value}% · 模糊 ${sB.value}px`
          : '还没有自定义背景(纯色底)';
        btnClear.disabled = !hasImg;
        paint();
      })
      .catch(() => setStatus('⚠ 服务没起来'));
  }

  // ---- 选图: 本地立刻预览, 不急着上传 ----
  f.addEventListener('change', () => {
    const g = f.files && f.files[0];
    if (!g) return;
    if (!/^image\//.test(g.type)) { setStatus('⚠ 请选图片文件'); return; }
    if (g.size > 8 * 1024 * 1024) { setStatus('⚠ 图太大了(最多 8 MB)'); return; }
    if (objUrl) URL.revokeObjectURL(objUrl);
    objUrl = URL.createObjectURL(g);
    file = g;
    curEl.textContent = `待上传: ${g.name}(${Math.round(g.size / 1024)} KB) —— 调好参数点"应用"`;
    setStatus('预览用的是本地这张图');
    paint();
  });

  // ---- 应用: 先传图(如果有新的), 再存参数 ----
  btnApply.addEventListener('click', () => {
    btnApply.disabled = true;
    setStatus('应用…');
    const upload = file
      ? toB64(file).then((b64) => fetch('/api/bg', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ img: b64, mime: file && file.type }),
      })).then((r) => r.json()).then((j) => {
        if (!j || !j.ok) throw new Error((j && j.err) || '上传失败');
        hasImg = true; ver = j.ver || Date.now();
        if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; }
        file = null;
      })
      : Promise.resolve();

    upload.then(() => fetch('/api/bg', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        params: { trans: Number(sT.value), dim: Number(sD.value),
          blur: Number(sB.value) },
      }),
    })).then((r) => r.json()).then((j) => {
      btnApply.disabled = false;
      if (!j || !j.ok) { setStatus('⚠ ' + ((j && j.err) || '保存参数失败')); return; }
      setStatus('已应用 ✓ 回首页看看效果');
      load();
    }).catch((e) => {
      btnApply.disabled = false;
      setStatus('⚠ ' + (e.message || '应用失败'));
    });
  });

  // ---- 清除图片(保留参数) ----
  btnClear.addEventListener('click', () => {
    fetch('/api/bg', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }) }).then((r) => r.json()).then((j) => {
      if (j && j.ok) { hasImg = false; file = null;
        if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; }
        setStatus('图片已清除'); load(); }
    }).catch(() => setStatus('⚠ 清除失败'));
  });

  // ---- 恢复默认: 清图 + 参数回默认 ----
  btnReset.addEventListener('click', () => {
    fetch('/api/bg', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true, reset: true }) })
      .then((r) => r.json()).then((j) => {
        if (j && j.ok) {
          hasImg = false; file = null;
          if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; }
          setStatus('已恢复默认背景');
          load();
        }
      }).catch(() => setStatus('⚠ 恢复失败'));
  });

  load();
})();
