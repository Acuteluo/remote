const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

const vnc = fs.readFileSync(path.join(__dirname, '../static/vnc.js'), 'utf8');
const displaySrc = fs.readFileSync(path.join(__dirname, '../static/vendor/novnc/core/display.js'), 'utf8');

function extractFunction(src, signature) {
  const start = src.indexOf(signature);
  assert(start >= 0, `missing ${signature}`);
  const brace = src.indexOf('{', start);
  let depth = 1, end = brace + 1;
  for (; depth && end < src.length; end++) {
    if (src[end] === '{') depth++;
    else if (src[end] === '}') depth--;
  }
  assert.equal(depth, 0, `unterminated ${signature}`);
  return src.slice(start, end);
}

const fitFn = extractFunction(vnc, 'function scheduleScreenFit()');
const autoscaleFn = extractFunction(displaySrc, 'autoscale(containerWidth, containerHeight)');

let rafs = [], fits = 0, transformDuringFit;
const screen = { style: { transform: 'scale(1.4) translate(12px, 8px)' } };
const viewport = { width: 1000, height: 600 };
const target = { style: { width: '', height: '' } };
const display = {
  _viewportLoc: { w: 1600, h: 900 },
  _target: target,
  _rescale(factor) {
    this._scale = factor;
    target.style.width = factor * this._viewportLoc.w + 'px';
    target.style.height = factor * this._viewportLoc.h + 'px';
  },
};
vm.createContext(display);
display.autoscale = vm.runInContext(`({${autoscaleFn}}).autoscale`, display);
const rfb = {};
Object.defineProperty(rfb, 'scaleViewport', {
  set(value) {
    assert.equal(value, true);
    transformDuringFit = screen.style.transform;
    fits++;
    display.autoscale(viewport.width, viewport.height);
  },
});

const context = {
  screenFitFrame: 0,
  rfb,
  connected: true,
  invalidateRect() {},
  requestAnimationFrame(fn) { rafs.push(fn); return rafs.length; },
  $() { return screen; },
  screen,
  zClamp() {}, zApply() {}, virtClamp() {}, placeCursor() {}, edgeUpdate() {},
};
vm.createContext(context);
vm.runInContext(fitFn, context);
function flushFrame() {
  const frame = rafs.shift();
  assert(frame, 'expected a queued animation frame');
  frame();
}

// Initial fit establishes a baseline, then a smaller viewport and restore must return to it.
context.scheduleScreenFit();
flushFrame();
const baseline = display._scale;
assert.equal(baseline, 1000 / 1600);

viewport.width = 900; viewport.height = 500;
context.scheduleScreenFit();
flushFrame();
const smallerScale = display._scale;
assert.equal(smallerScale, 500 / 900);
viewport.width = 1000; viewport.height = 600;
context.scheduleScreenFit();
flushFrame();
assert.equal(display._scale, baseline, 'restoring viewport should restore the original scale');

// Multiple visibility/layout requests before one frame are coalesced into a single fit.
const beforeBurst = fits;
context.scheduleScreenFit(); context.scheduleScreenFit(); context.scheduleScreenFit();
assert.equal(rafs.length, 1);
flushFrame();
assert.equal(fits - beforeBurst, 1);

// During fullscreen fit the visual transform is removed, then restored afterward.
assert.equal(transformDuringFit, '');
assert.equal(screen.style.transform, 'scale(1.4) translate(12px, 8px)');

// A queued frame after disconnect must be skipped.
const beforeDisconnect = fits;
context.scheduleScreenFit();
context.connected = false;
flushFrame();
assert.equal(fits, beforeDisconnect);

console.log('PASS screen fit baseline/restore, RAF coalescing, fullscreen transform restore, disconnect guard');
