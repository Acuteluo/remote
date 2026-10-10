const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../static/vnc.js'), 'utf8');

function fn(name) {
  const asyncStart = src.indexOf(`async function ${name}(`);
  const start = asyncStart >= 0 ? asyncStart : src.indexOf(`function ${name}(`);
  assert(start >= 0, `missing function ${name}`);
  const brace = src.indexOf('{', start);
  let depth = 1, end = brace + 1;
  for (; depth && end < src.length; end++) {
    if (src[end] === '{') depth++;
    else if (src[end] === '}') depth--;
  }
  assert.equal(depth, 0, `unterminated function ${name}`);
  return src.slice(start, end);
}

function setup(outcomes, version = 3) {
  let now = 1_800_000_000_000;
  const calls = [];
  class FakeAbortController { constructor() { this.signal = {}; } abort() {} }
  const c = {
    inputSession: null,
    Date: { now: () => now },
    Math,
    Number,
    JSON,
    encodeURIComponent,
    AbortController: FakeAbortController,
    setTimeout: () => 1,
    clearTimeout() {},
    crypto: { getRandomValues(bytes) { bytes.fill(0x2a); return bytes; } },
    showInputAlert() {},
    async sleep(ms) { now += ms; },
    fetch: async (url, options) => {
      calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      const next = outcomes.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error('unexpected fetch');
      return { status: next.status, redirected: false, json: async () => next.json };
    },
  };
  vm.createContext(c);
  vm.runInContext(`let inputSession = null;\n${fn('rememberInputSession')}\n${fn('inputRequest')}\n${fn('submitType')}`, c);
  c.now = () => now;
  c.calls = calls;
  c.health = { status: 200, json: { ok: true, inputVersion: version, boot: 'boot-current', serverTimeMs: now } };
  return c;
}

function done(ok = true, extra = {}) { return { status: 200, json: { ok, ...extra } }; }

(async () => {
  {
    const c = setup([null]);
    c.fetch = async (url, options) => {
      c.calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      if (c.calls.length === 1) return { status: 200, redirected: false, json: async () => c.health.json };
      if (c.calls.length === 2) throw new Error('POST response lost');
      if (c.calls.length === 3) return { status: 200, redirected: false, json: async () => ({ ok: true, state: 'done' }) };
      throw new Error('unexpected extra request');
    };
    const [status, result] = await c.submitType('hello', 'auto');
    assert.equal(status, 200); assert.equal(result.state, 'done');
    assert.deepEqual(c.calls.map(x => x.method), ['GET', 'POST', 'GET']);
  }
  {
    const c = setup([]);
    c.fetch = async (url, options) => {
      c.calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      if (c.calls.length === 1) return { status: 200, redirected: false, json: async () => c.health.json };
      if (c.calls.length === 2) throw new Error('POST response lost');
      if (c.calls.length === 3) return { status: 404, redirected: false, json: async () => ({ ok: false, boot: 'boot-current' }) };
      if (c.calls.length === 4) return { status: 200, redirected: false, json: async () => ({ ok: true, state: 'done' }) };
      throw new Error('unexpected extra request');
    };
    const [status, result] = await c.submitType('same payload', 'auto');
    assert.equal(status, 200); assert.equal(result.state, 'done');
    assert.equal(c.calls[1].body.requestId, c.calls[3].body.requestId);
    assert.deepEqual(c.calls[1].body, c.calls[3].body);
  }
  {
    const c = setup([]);
    c.fetch = async (url, options) => {
      c.calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      const n = c.calls.length;
      const reply = n === 1 ? c.health.json : n === 2 ? { ok: false, state: 'pending' } : { ok: true, state: 'done' };
      return { status: n === 2 ? 202 : 200, redirected: false, json: async () => reply };
    };
    const [status, result] = await c.submitType('pending', 'auto');
    assert.equal(status, 200); assert.equal(result.state, 'done');
    assert.deepEqual(c.calls.map(x => x.method), ['GET', 'POST', 'GET']);
  }
  {
    const c = setup([]);
    c.fetch = async (url, options) => {
      c.calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      const reply = c.calls.length === 1 ? c.health.json : { ok: false, err: 'old boot' };
      return { status: c.calls.length === 1 ? 200 : 409, redirected: false, json: async () => reply };
    };
    assert.equal((await c.submitType('old', 'auto'))[0], 409);
    assert.deepEqual(c.calls.map(x => x.method), ['GET', 'POST']);
    assert.equal(vm.runInContext('inputSession', c), null);
  }
  {
    const c = setup([], 2);
    c.fetch = async (url, options) => {
      c.calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
      return { status: 200, redirected: false,
        json: async () => c.calls.length === 1 ? c.health.json : { ok: true, legacy: true } };
    };
    const [status, result] = await c.submitType('legacy', 'auto');
    assert.equal(status, 200); assert.equal(result.legacy, true);
    assert.deepEqual(c.calls.map(x => x.method), ['GET', 'POST']);
    assert.equal(c.calls[1].body.requestId, undefined);
  }
  console.log('PASS input receipt retry: lost response, same-ID retry after 404, pending poll, 409 stop, legacy single POST');
})().catch(e => { console.error(e); process.exit(1); });
