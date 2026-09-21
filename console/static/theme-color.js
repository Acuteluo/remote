/* 主题色圆盘的颜色数学。
 *
 * 故意写成**纯函数、不碰 DOM**(同时兼容 Node 的 module.exports) —— 这样
 * console/tests/theme_check.py 能直接在 Node 里跑它, 验证"盘面上取到的颜色
 * 一定亮得起来", 不用开浏览器。
 *
 * 盘面规则: 角度 = 色相, 半径 = 离白心的距离。
 *   圆心: 近白(最浅)   边缘: 最鲜艳
 * 还要保证整盘**相对亮度 >= FLOOR**: 主题色要当文字颜色用, 暗底上深色看不清。
 * 注意不能只看明度 —— WCAG 相对亮度里绿通道权重占 0.715, 饱和蓝/紫天生就低:
 * hsl(240,90%,62%) 看着是"中等蓝", 相对亮度只有 0.12。所以蓝色那几段要单独顶亮
 * (MINL 表), 而红黄绿可以保持更艳 —— 这样既不会出现"能选却存不进去"
 * (服务端门槛 0.30, 这里 FLOOR 留了余量), 也不至于整盘都发白。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;  // Node
  root.ThemeColor = api;                                                  // 浏览器
})(typeof window !== "undefined" ? window : globalThis, function () {
  const SAT = 92;            // 边缘最大饱和度(%)
  const SAT_SLOPE = 1.15;    // 饱和度随半径的斜率(先到顶, 之后保持)
  const L_BASE = 92;         // 圆心明度(%)
  const L_DROP = 26;         // 从圆心到边缘掉多少明度
  const FLOOR = 0.33;        // 盘面相对亮度下限(服务端门槛 0.30, 留余量)
  const L_MIN = 40;

  function hslToRgb(h, s, l) {            // h:0~360, s/l:0~1
    h = ((h % 360) + 360) % 360;
    const c = (1 - Math.abs(2 * l - 1)) * s, m = l - c / 2,
      x = c * (1 - Math.abs((h / 60) % 2 - 1));
    let r = 0, g = 0, b = 0;
    if (h < 60) [r, g, b] = [c, x, 0];
    else if (h < 120) [r, g, b] = [x, c, 0];
    else if (h < 180) [r, g, b] = [0, c, x];
    else if (h < 240) [r, g, b] = [0, x, c];
    else if (h < 300) [r, g, b] = [x, 0, c];
    else [r, g, b] = [c, 0, x];
    return [Math.round((r + m) * 255), Math.round((g + m) * 255),
      Math.round((b + m) * 255)];
  }

  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) {
      if (mx === r) h = 60 * (((g - b) / d + 6) % 6);
      else if (mx === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    const l = (mx + mn) / 2;
    return [((h % 360) + 360) % 360,
      d ? Math.min(1, d / (1 - Math.abs(2 * l - 1))) : 0, l];
  }

  // WCAG 相对亮度(0~1) —— 和服务端 theme_luminance() 是同一把尺子
  function lum(rgb) {
    const f = (c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
  }

  // 每个色相的"明度下限"(按最大饱和度算最坏情况)。蓝紫那几段会明显高于别的。
  const MINL = (() => {
    const t = new Float32Array(360);
    for (let h = 0; h < 360; h++) {
      let l = L_MIN;
      for (; l < 100; l += 0.25) {
        if (lum(hslToRgb(h, SAT / 100, l / 100)) >= FLOOR) break;
      }
      t[h] = l;
    }
    return t;
  })();

  // 半径 rad(0~1) + 角度 ang(度) → 颜色。盘面渲染与取色共用, 所以永远一致。
  function colorAt(rad, ang) {
    const r = Math.max(0, Math.min(1, rad));
    const h = ((ang % 360) + 360) % 360;
    const s = (SAT * Math.min(1, r * SAT_SLOPE)) / 100;
    const l = Math.max(L_BASE - L_DROP * r, MINL[Math.round(h) % 360]) / 100;
    return hslToRgb(h, s, l);
  }

  // 由颜色反推半径(算色相定角度 + 由明度反解半径)。只用于放下初始指示器。
  function radiusOf(rgb) {
    const [, , l] = rgbToHsl(rgb[0], rgb[1], rgb[2]);
    return Math.max(0, Math.min(1, (L_BASE - l * 100) / L_DROP));
  }

  const toHex = (rgb) =>
    "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");

  return { colorAt, radiusOf, hslToRgb, rgbToHsl, lum, toHex,
    MINL, FLOOR, SAT, L_BASE, L_DROP };
});
