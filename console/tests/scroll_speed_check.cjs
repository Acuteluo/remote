const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '../static/vnc.js'), 'utf8');
const start = src.indexOf('const BASE_STEP =');
const end = src.indexOf('// 滚轮档位也按帧合并发送。', start);
assert(start >= 0 && end > start, 'scroll accumulator code must exist');
const scrollCode = src.slice(start, end) + `
this.setScrollAcc = (axis, value) => { SCROLL[axis] = value; };
this.runScrollDrain = () => drainScroll();
this.getScrollAcc = (axis) => SCROLL[axis];
`;
const yExpr = src.match(/const dAcc = ([^;]+);/)?.[1];
const xExpr = src.match(/const dAccX = ([^;]+);/)?.[1];
assert(yExpr && xExpr, 'pointer scroll accumulation expressions must exist');

function notchCount(speed, axis) {
  const steps = [];
  const context = {
    S: { scrollSpeed: speed },
    SCROLL: { acc: 0, accX: 0 },
    wheelStep: (y, x) => steps.push([y, x]),
    Math,
  };
  vm.createContext(context);
  vm.runInContext(scrollCode, context);
  context.dy = 40;
  context.dx = 40;
  context.dir = 1;
  const delta = vm.runInContext(axis === 'y' ? yExpr : xExpr, context);
  context.setScrollAcc(axis === 'y' ? 'acc' : 'accX', delta);
  context.runScrollDrain();
  assert.strictEqual(context.getScrollAcc(axis === 'y' ? 'acc' : 'accX'), 0);
  return steps.length;
}

for (const axis of ['x', 'y']) {
  const counts = [0.5, 1, 2].map((speed) => notchCount(speed, axis));
  assert.deepStrictEqual(counts, [1, 2, 4],
    `${axis}-axis scroll should scale linearly at 0.5x/1x/2x`);
}
console.log('PASS touchpad scroll gain is linear at 0.5x, 1x, and 2x');
