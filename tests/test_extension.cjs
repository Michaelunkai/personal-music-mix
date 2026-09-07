const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../extension', name), 'utf8');

function contentHarness() {
  const timers = new Map();
  const messages = [];
  const listeners = [];
  let nextTimer = 0;
  let fetches = 0;
  const row = {
    querySelector(selector) {
      if (selector.startsWith('[data-title]')) return { textContent: 'A song' };
      if (selector.startsWith('a[href*="watch"')) return { href: 'https://music.youtube.com/watch?v=abc12345678' };
      return null;
    },
    getAttribute: () => null,
    textContent: 'A song',
  };
  const context = vm.createContext({
    location: { hostname: 'music.youtube.com', pathname: '/history', href: 'https://music.youtube.com/history' },
    document: {
      documentElement: {}, body: { innerText: 'History' },
      querySelector: () => null,
      querySelectorAll: selector => selector.includes('ytmusic-responsive-list-item-renderer') ? [row] : [],
    },
    MutationObserver: class { observe() {} },
    window: {
      setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; },
      clearTimeout: id => timers.delete(id),
      setInterval() {}, addEventListener() {},
    },
    chrome: { runtime: {
      getManifest: () => ({ version: '0.1.0' }),
      onMessage: { addListener: fn => listeners.push(fn) },
      sendMessage(message, callback) {
        if (message.type === 'ytmusic-history') messages.push({ message, callback });
        return Promise.resolve({ ok: true });
      },
    } },
    fetch: async () => { fetches++; return { type: 'opaque' }; },
  });
  const runTimer = ms => {
    const entry = [...timers.entries()].find(([, value]) => value.ms === ms);
    assert.ok(entry, `Expected ${ms}ms timer`);
    timers.delete(entry[0]); entry[1].fn();
  };
  return { context, messages, listeners, runTimer, fetches: () => fetches };
}

test('failed delivery retries identical rows and only acknowledged delivery deduplicates', () => {
  const h = contentHarness();
  vm.runInContext(source('content.js'), h.context);
  h.runTimer(900);
  assert.equal(h.messages.length, 1);
  h.messages[0].callback({ ok: false, error: 'Local app returned 401' });
  assert.equal(h.fetches(), 0, 'Do not treat opaque HTTP errors as successful delivery');
  h.runTimer(5000); h.runTimer(900);
  assert.equal(h.messages.length, 2);
  h.messages[1].callback({ ok: true });
  h.listeners[0]({ type: 'sync-now' }); h.runTimer(900);
  assert.equal(h.messages.length, 2);
});

test('reinjection does not create duplicate collectors or message listeners', () => {
  const h = contentHarness();
  vm.runInContext(source('content.js'), h.context);
  vm.runInContext(source('content.js'), h.context);
  assert.equal(h.listeners.length, 1);
  h.runTimer(900);
  assert.equal(h.messages.length, 1);
});

test('background request failure returns a retryable error and uses a deadline', async () => {
  const listeners = {};
  const event = name => ({ addListener: fn => { listeners[name] = fn; } });
  let requestSignal;
  const context = vm.createContext({
    URL, AbortSignal,
    fetch: async (_url, options) => { requestSignal = options.signal; throw new Error('timeout'); },
    chrome: {
      runtime: { onInstalled: event('installed'), onStartup: event('startup'), onMessage: event('message') },
      alarms: { create() {}, onAlarm: event('alarm') },
      tabs: { onUpdated: event('updated') },
      storage: { local: { get: async defaults => defaults } },
      action: { onClicked: event('clicked') },
    },
  });
  vm.runInContext(source('background.js'), context);
  const response = await new Promise(resolve => listeners.message({ type: 'ytmusic-history', payload: {} }, {}, resolve));
  assert.equal(response.ok, false);
  assert.match(response.error, /timeout/);
  assert.ok(requestSignal instanceof AbortSignal);
});
