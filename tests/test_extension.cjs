const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../extension', name), 'utf8');

function contentHarness({label='', pressed=null, title='A song', favorites=false} = {}) {
  const timers = new Map();
  const messages = [];
  const listeners = [];
  let nextTimer = 0;
  let fetches = 0;
  const row = {
    querySelector(selector) {
      if (selector.startsWith('[data-title]')) return { textContent: title };
      if (selector.startsWith('a[href*="watch"')) return { href: 'https://music.youtube.com/watch?v=abc12345678' };
      return null;
    },
    getAttribute: () => null,
    textContent: title,
    closest: () => null,
    querySelectorAll: selector => selector === '[aria-label], [title]' && label ? [{getAttribute:name=>name==='aria-label'?label:name==='aria-pressed'?pressed:null}] : [],
  };
  const context = vm.createContext({
    URL,
    location: { hostname: 'music.youtube.com', pathname: favorites ? '/playlist' : '/history', href: favorites ? 'https://music.youtube.com/playlist?list=LM' : 'https://music.youtube.com/history' },
    document: {
      documentElement: {}, body: { innerText: 'History' },
      querySelector: selector => selector.startsWith('ytmusic-browse-response') ? {querySelectorAll:()=>[row]} : null,
      querySelectorAll: selector => selector.includes('ytmusic-responsive-list-item-renderer') ? [row] : [],
    },
    MutationObserver: class { observe() {} },
    window: {
      setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; },
      clearTimeout: id => timers.delete(id),
      setInterval() {}, addEventListener() {},
    },
    chrome: { runtime: {
      getManifest: () => ({ version: '0.1.1' }),
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
  assert.equal(h.messages.length, 3, 'Explicit sync resends acknowledged rows for recovery');
});

test('sync-now acknowledges only after the local app accepts the rendered rows', () => {
  const h = contentHarness();
  vm.runInContext(source('content.js'), h.context);
  let response;
  h.listeners[0]({ type: 'sync-now' }, {}, value => { response = value; });
  h.runTimer(900);
  assert.equal(response, undefined);
  h.messages[0].callback({ ok: true });
  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true, items: 1 });
});

test('only a selected positive like control or liked-collection membership marks favorites', () => {
  for (const [options,expected,known] of [
    [{label:'Like',pressed:'true'},true,true], [{label:'Like',pressed:'false'},false,true],
    [{label:'Disliked',pressed:'true'},false,false], [{label:'Thumbs up'},false,false],
    [{title:'I liked it'},false,false], [{label:'Shuffle',pressed:'true'},false,false],
    [{favorites:true},true,true],
  ]) {
    const h=contentHarness(options); vm.runInContext(source('content.js'),h.context);h.runTimer(900);
    assert.equal(h.messages[0].message.payload.items[0].liked,expected,JSON.stringify(options));
    assert.equal(h.messages[0].message.payload.items[0].like_state_known,known,JSON.stringify(options));
    if(options.favorites) assert.equal(h.messages[0].message.payload.kind,'favorites');
  }
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

test('dashboard refresh signal is same-origin and reaches the service worker', async () => {
  const listeners = [];
  const messages = [];
  const posted = [];
  const windowObject = {
    addEventListener: (name, fn) => { if (name === 'message') listeners.push(fn); },
    postMessage: (message, origin) => posted.push({ message, origin }),
  };
  const context = vm.createContext({
    URL,
    location: { origin: 'https://personal-music-mix.michaelovsky55555.chatgpt.site' },
    window: windowObject,
    chrome: { runtime: { sendMessage: message => { messages.push(message); return Promise.resolve({ ok: true, tabs: 2, acknowledged: 2 }); } } },
  });
  vm.runInContext(source('content.js'), context);
  assert.equal(listeners.length, 1);
  listeners[0]({
    source: windowObject,
    origin: 'https://personal-music-mix.michaelovsky55555.chatgpt.site',
    data: { type: 'ytmusic-personal-mix-refresh', request_id: 'refresh-1', requested_at: '2026-09-08T00:00:00.000Z' },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(messages)), [{ type: 'dashboard-refresh-request', request_id: 'refresh-1', requested_at: '2026-09-08T00:00:00.000Z' }]);
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [{
    message: { type: 'ytmusic-personal-mix-refresh-result', request_id: 'refresh-1', ok: true, tabs: 2, acknowledged: 2, error: null },
    origin: 'https://personal-music-mix.michaelovsky55555.chatgpt.site',
  }]);
  listeners[0]({ source: windowObject, origin: 'https://evil.example', data: { type: 'ytmusic-personal-mix-refresh' } });
  await Promise.resolve();
  assert.equal(messages.length, 1);
});

test('background accepts dashboard refresh only from approved origins', async () => {
  const listeners = {};
  const event = name => ({ addListener: fn => { listeners[name] = fn; } });
  const sent = [];
  const context = vm.createContext({
    URL, AbortSignal,
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    chrome: {
      runtime: { onInstalled: event('installed'), onStartup: event('startup'), onMessage: event('message') },
      alarms: { create() {}, onAlarm: event('alarm') },
      tabs: {
        onUpdated: event('updated'),
        query: async () => [
          { id: 10, url: 'https://music.youtube.com/history' },
          { id: 11, url: 'https://music.youtube.com/playlist?list=LM' },
          { id: 12, url: 'https://music.youtube.com/watch?v=abc12345678' },
        ],
        sendMessage: async (id, message) => { sent.push({ id, message }); },
      },
      scripting: { executeScript: async () => {} },
      storage: { local: { get: async defaults => defaults } },
      action: { onClicked: event('clicked') },
    },
  });
  vm.runInContext(source('background.js'), context);
  const accepted = await new Promise(resolve => listeners.message(
    { type: 'dashboard-refresh-request', request_id: 'refresh-2' },
    { url: 'https://personal-music-mix.michaelovsky55555.chatgpt.site/' },
    resolve,
  ));
  assert.deepEqual(JSON.parse(JSON.stringify(accepted)), { ok: true, tabs: 2, acknowledged: 2, request_id: 'refresh-2' });
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), [
    { id: 10, message: { type: 'sync-now' } },
    { id: 11, message: { type: 'sync-now' } },
  ]);
  const denied = await new Promise(resolve => listeners.message(
    { type: 'dashboard-refresh-request' },
    { url: 'https://evil.example/' },
    resolve,
  ));
  assert.equal(denied.ok, false);
  assert.equal(sent.length, 2);
});
