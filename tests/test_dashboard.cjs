const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness(options = {}) {
  const nodes = new Map();
  const nodeListeners = new Map();
  const documentListeners = new Map();
  const windowIntervals = [];
  const fetches = [];
  const playerCalls = [];
  const loads = [], jumps = [], configs = [], posts = [];
  const windowListeners = [];
  const storageValues = new Map(Object.entries(options.storageData || {}));
  const storage = options.storage || {
    getItem: key => storageValues.has(String(key)) ? storageValues.get(String(key)) : null,
    setItem: (key, value) => storageValues.set(String(key), String(value)),
    removeItem: key => storageValues.delete(String(key)),
    clear: () => storageValues.clear(),
    key: index => Array.from(storageValues.keys())[index] ?? null,
    get length() { return storageValues.size; },
  };
  const mediaSession = options.mediaSession ? {
    handlers: new Map(), metadata: null, playbackState: 'none',
    setActionHandler: (action, handler) => mediaSession.handlers.set(action, handler),
    setPositionState() {},
  } : undefined;
  const addNodeListener = (selector, name, listener) => {
    const byName = nodeListeners.get(selector) || new Map();
    const handlers = byName.get(name) || [];
    handlers.push(listener);
    byName.set(name, handlers);
    nodeListeners.set(selector, byName);
  };
  const node = selector => {
    if (!nodes.has(selector)) {
      const element = {
        textContent: '', innerHTML: '', disabled: false, hidden: false, value: '', href: '', dataset: {},
        classList: { add() {}, remove() {}, toggle() {} },
        querySelector: childSelector => node(selector + ' ' + childSelector),
        addEventListener: (name, listener) => addNodeListener(selector, name, listener),
        setAttribute: (name, value) => { element[name] = String(value); },
        dispatchEvent: event => {
          const handlers = nodeListeners.get(selector)?.get(event.type) || [];
          const results = handlers.map(listener => listener({ ...event, target: event.target || element, currentTarget: element }));
          return results.length === 1 ? results[0] : Promise.all(results);
        },
      };
      nodes.set(selector, element);
    }
    return nodes.get(selector);
  };
  const documentObject = {
    hidden: false, visibilityState: 'visible', querySelector: node,
    querySelectorAll: () => [],
    getElementById: id => node(`#${id}`),
    addEventListener: (name, listener) => {
      const listeners = documentListeners.get(name) || [];
      listeners.push(listener);
      documentListeners.set(name, listeners);
    },
    dispatchEvent: event => {
      const handlers = documentListeners.get(event.type) || [];
      const results = handlers.map(listener => listener(event));
      return results.length === 1 ? results[0] : Promise.all(results);
    },
  };
  const windowObject = {
    location: { origin: 'http://127.0.0.1:8000' }, setTimeout: () => 1, clearTimeout() {},
    setInterval: (listener, ms) => { const timer = { listener, ms }; windowIntervals.push(timer); return timer; },
    clearInterval: timer => { const index = windowIntervals.indexOf(timer); if (index >= 0) windowIntervals.splice(index, 1); },
    addEventListener: (name, listener) => { if (name === 'message') windowListeners.push(listener); },
    removeEventListener: (name, listener) => { if (name === 'message') { const index = windowListeners.indexOf(listener); if (index >= 0) windowListeners.splice(index, 1); } },
    postMessage: (message, origin) => {
      posts.push({ message, origin });
      if (message.type === 'ytmusic-personal-mix-refresh') {
        const result = { type: 'ytmusic-personal-mix-refresh-result', request_id: message.request_id, ok: true, tabs: 1, acknowledged: 1 };
        windowListeners.slice().forEach(listener => listener({ source: windowObject, origin, data: result }));
      }
    },
  };
  const tracks = ['aaaaaaaaaaa', 'bbbbbbbbbbb'].map((id, i) => ({ track: { track_key: `video:${id}`, video_id: id, title: `Song ${i}`, artist: 'Artist' }, score: 0.5 }));
  let playerIndex = 0;
  windowObject.YT = { Player: class {
    constructor(_id, config) { configs.push(config); Promise.resolve().then(() => config.events.onReady()); }
    loadPlaylist(ids, index) { playerIndex = index; loads.push({ ids: Array.from(ids), index }); playerCalls.push({ method: 'loadPlaylist', ids: Array.from(ids), index }); }
    getPlaylistIndex() { return playerIndex; }
    playVideo() { playerCalls.push({ method: 'playVideo' }); }
    pauseVideo() { playerCalls.push({ method: 'pauseVideo' }); }
    stopVideo() { playerCalls.push({ method: 'stopVideo' }); }
    playVideoAt(index) { playerIndex = index; jumps.push(index); playerCalls.push({ method: 'playVideoAt', index }); }
    nextVideo() { playerIndex += 1; playerCalls.push({ method: 'nextVideo' }); }
    previousVideo() { playerIndex = Math.max(0, playerIndex - 1); playerCalls.push({ method: 'previousVideo' }); }
    destroy() {}
  } };
  const context = vm.createContext({
    URL, console, AbortSignal,
    document: documentObject,
    window: windowObject,
    navigator: mediaSession ? { mediaSession } : {},
    MediaMetadata: mediaSession ? class MediaMetadata { constructor(metadata) { Object.assign(this, metadata); } } : undefined,
    localStorage: storage,
    sessionStorage: storage,
    fetch: async url => { fetches.push(url); return { ok: true, text: async () => JSON.stringify({
      '/api/health': { ok: true, database: { ok: true } },
      '/api/overview': { liked_track_count: 0 }, '/api/recommendations': { items: [] },
      '/api/runs': { items: [] }, '/api/status': {}, '/api/playlists/latest': {},
      '/api/connection': {}, '/api/favorites': { track_keys: [] },
    }[url] || {}) }; },
  });
  windowObject.document = documentObject;
  windowObject.localStorage = storage;
  windowObject.sessionStorage = storage;
  if (mediaSession) windowObject.navigator = { mediaSession };
  if (mediaSession) windowObject.MediaMetadata = context.MediaMetadata;
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8'), context);
  context.tracks = tracks;
  vm.runInContext('renderRecommendations(tracks)', context);
  const click = async selector => {
    const handlers = nodeListeners.get(selector)?.get('click') || [];
    if (!handlers.length) throw new Error(`No click handler bound for ${selector}`);
    for (const listener of handlers) await listener({ type: 'click', target: node(selector), currentTarget: node(selector) });
  };
  const ready = async () => {
    const handlers = documentListeners.get('DOMContentLoaded') || [];
    for (const listener of handlers) listener({ type: 'DOMContentLoaded' });
    const refresh = vm.runInContext('state.refreshing', context);
    if (refresh && typeof refresh.then === 'function') await refresh;
  };
  const storageSnapshot = () => Object.fromEntries(storageValues);
  return {
    context, node, nodes, nodeListeners, documentListeners, windowIntervals, fetches, playerCalls,
    loads, jumps, configs, posts, windowListeners, windowObject, document: documentObject,
    localStorage: storage, mediaSession, click, ready, storageSnapshot,
    setPlayerIndex: index => { playerIndex = index; },
  };
}

test('an activity error preserves successfully loaded music', async () => {
  const h = harness();
  const original = h.context.fetch;
  h.context.fetch = async url => {
    if (url === '/api/runs') throw new Error('Activity unavailable');
    if (url === '/api/recommendations' || url === '/api/library') return {ok:true,text:async()=>JSON.stringify({items:h.context.tracks})};
    return original(url);
  };
  await vm.runInContext('refresh()', h.context);
  assert.equal(vm.runInContext('state.recommendations.length', h.context),2);
  assert.match(h.node('#toast').textContent,/Activity unavailable/);
});

test('unknown favorites disable hearts until preferences recover', async () => {
  const h=harness(); const original=h.context.fetch;
  h.context.fetch=async url=> {
    if(url==='/api/favorites') throw new Error('Preferences unavailable');
    if(url==='/api/recommendations') return {ok:true,text:async()=>JSON.stringify({items:h.context.tracks})};
    return original(url);
  };
  await vm.runInContext('refresh()',h.context);
  assert.match(h.node('#recommendations').innerHTML,/data-favorite="[^"]+" disabled/);
  assert.equal(vm.runInContext('state.favoritesReady',h.context),false);
  h.context.fetch=original;
  await vm.runInContext('refresh()',h.context);
  assert.equal(vm.runInContext('state.favoritesReady',h.context),true);
});

test('a refresh after saving waits for fresh favorites even if an older poll is pending', async () => {
  const h = harness();
  const original = h.context.fetch;
  let release, calls=0;
  h.context.fetch = async url => {
    if (url !== '/api/favorites') return original(url);
    calls++;
    if (calls === 1) await new Promise(resolve => {release=resolve;});
    const keys = calls === 1 ? [] : ['video:aaaaaaaaaaa'];
    return {ok:true,text:async()=>JSON.stringify({track_keys:keys})};
  };
  const poll = vm.runInContext('refresh({silent:true})',h.context);
  const saved = vm.runInContext('refresh()',h.context);
  release();
  await Promise.all([poll,saved]);
  assert.equal(calls,2);
  assert.equal(vm.runInContext("state.favorites.has('video:aaaaaaaaaaa')",h.context),true);
});

test('playlist preview reports available songs rather than requested size', () => {
  const h=harness();
  h.context.plan={available:true,plan:{requested_count:20,items:h.context.tracks}};
  vm.runInContext('renderLatestPlaylist(plan)',h.context);
  assert.match(h.node('#playlist-preview').innerHTML,/Your mix: 2 songs/);
  h.context.plan.plan.items=[];
  vm.runInContext('renderLatestPlaylist(plan)',h.context);
  assert.match(h.node('#playlist-preview').innerHTML,/Your mix: 0 songs/);
});

test('saved playlist names reload without overwriting edits during polling or saving', async () => {
  const h=harness();
  h.context.plan={available:true,plan:{name:'Evening music',items:h.context.tracks}};
  vm.runInContext('renderLatestPlaylist(plan)',h.context);
  assert.equal(h.node('#playlist-name').value,'Evening music');
  h.node('#playlist-name').value='New draft';
  vm.runInContext('state.playlistNameDirty=true; renderLatestPlaylist(plan)',h.context);
  assert.equal(h.node('#playlist-name').value,'New draft');
  let release;
  h.context.fetch=async()=>{await new Promise(resolve=>{release=resolve;});return {ok:true,text:async()=>JSON.stringify({plan:{name:'New draft',items:h.context.tracks},write_enabled:false})};};
  const saving=vm.runInContext('previewPlaylist()',h.context);
  assert.equal(h.node('#preview-button').disabled,true);
  h.node('#playlist-name').value='Next draft';
  release();await saving;
  assert.equal(h.node('#playlist-name').value,'Next draft');
  assert.equal(h.node('#preview-button').disabled,false);
  assert.equal(h.node('#write-button').hidden,true);
});

test('Play builds the correct queue; polling does not stop or replace playback', async () => {
  const h = harness();
  assert.match(h.node('#recommendations').innerHTML, /data-play="1"/);
  assert.match(h.node('#recommendations').innerHTML, /data-favorite="video:bbbbbbbbbbb"/);
  await vm.runInContext('playTrack(1)', h.context);
  assert.deepEqual(h.loads, [{ ids: ['aaaaaaaaaaa', 'bbbbbbbbbbb'], index: 1 }]);
  assert.equal(h.node('#now-playing').textContent, 'Song 1 — Artist');
  assert.equal(h.configs[0].playerVars.origin, 'http://127.0.0.1:8000');
  await vm.runInContext('refresh({silent:true})', h.context);
  assert.equal(h.loads.length, 1);
  assert.equal(vm.runInContext('state.queue.length', h.context), 2);
  vm.runInContext('changeTrack(-1)', h.context);
  assert.deepEqual(h.jumps, [0]);
});

test('site playback controls expose play, pause, stop, previous, and next actions', async () => {
  const h = harness();
  await h.ready();
  vm.runInContext("state.ranked = tracks; state.mixRecommendations = tracks.slice(); renderCollection()", h.context);
  await vm.runInContext('playTrack(0)', h.context);

  const candidates = {
    play: ['#play-mix', '#play-track', '#play-button', '#player-play'],
    pause: ['#pause-track', '#pause-button', '#player-pause'],
    stop: ['#stop-track', '#stop-button', '#player-stop'],
    previous: ['#previous-track', '#previous-button', '#player-previous'],
    next: ['#next-track', '#next-button', '#player-next'],
  };
  const bound = selectors => selectors.find(selector => h.nodeListeners.get(selector)?.get('click')?.length);
  const controls = Object.fromEntries(Object.entries(candidates).map(([name, selectors]) => [name, bound(selectors)]));
  for (const [name, selector] of Object.entries(controls)) {
    assert.ok(selector, `Missing site ${name} playback control handler (checked ${candidates[name].join(', ')})`);
  }

  h.playerCalls.length = 0;
  await h.click(controls.play);
  assert.ok(h.playerCalls.some(call => call.method === 'playVideo' || call.method === 'loadPlaylist'), 'Play control did not start the player');
  await h.click(controls.pause);
  assert.ok(h.playerCalls.some(call => call.method === 'pauseVideo'), 'Pause control did not pause the player');
  await h.click(controls.stop);
  assert.ok(h.playerCalls.some(call => call.method === 'stopVideo'), 'Stop control did not stop the player');

  vm.runInContext('state.queueIndex = 0', h.context);
  h.setPlayerIndex(0);
  await h.click(controls.next);
  assert.equal(vm.runInContext('state.queueIndex', h.context), 1);
  assert.ok(h.playerCalls.some(call => (call.method === 'playVideoAt' && call.index === 1) || call.method === 'nextVideo'), 'Next control did not advance the player');
  await h.click(controls.previous);
  assert.equal(vm.runInContext('state.queueIndex', h.context), 0);
  assert.ok(h.playerCalls.some(call => (call.method === 'playVideoAt' && call.index === 0) || call.method === 'previousVideo'), 'Previous control did not rewind the player');
});

test('Media Session action handlers mirror site playback actions when the API is available', async t => {
  const h = harness({ mediaSession: true });
  if (!h.context.navigator?.mediaSession || typeof h.context.navigator.mediaSession.setActionHandler !== 'function') {
    return t.skip('Media Session is unavailable in this harness');
  }
  await h.ready();
  vm.runInContext("state.ranked = tracks; state.mixRecommendations = tracks.slice(); renderCollection()", h.context);
  await vm.runInContext('playTrack(0)', h.context);

  const actions = ['play', 'pause', 'stop', 'previoustrack', 'nexttrack'];
  for (const action of actions) assert.equal(typeof h.mediaSession.handlers.get(action), 'function', `Missing Media Session ${action} handler`);

  h.playerCalls.length = 0;
  await h.mediaSession.handlers.get('play')();
  await h.mediaSession.handlers.get('pause')();
  await h.mediaSession.handlers.get('stop')();
  vm.runInContext('state.queueIndex = 0', h.context);
  h.setPlayerIndex(0);
  await h.mediaSession.handlers.get('nexttrack')();
  await h.mediaSession.handlers.get('previoustrack')();
  assert.ok(h.playerCalls.some(call => call.method === 'playVideo'), 'Media Session play did not start the player');
  assert.ok(h.playerCalls.some(call => call.method === 'pauseVideo'), 'Media Session pause did not pause the player');
  assert.ok(h.playerCalls.some(call => call.method === 'stopVideo'), 'Media Session stop did not stop the player');
  assert.ok(h.playerCalls.some(call => (call.method === 'nextVideo') || (call.method === 'playVideoAt' && call.index === 1)), 'Media Session nexttrack did not advance');
  assert.ok(h.playerCalls.some(call => (call.method === 'previousVideo') || (call.method === 'playVideoAt' && call.index === 0)), 'Media Session previoustrack did not rewind');
});

test('hidden tabs skip background refresh and visible return preserves playback', async () => {
  const h = harness();
  await h.ready();
  vm.runInContext("state.ranked = tracks; state.mixRecommendations = tracks.slice(); renderCollection()", h.context);
  await vm.runInContext('playTrack(0)', h.context);
  const player = vm.runInContext('state.player', h.context);
  const loadCount = h.loads.length;
  h.fetches.length = 0;
  const poll = h.windowIntervals.find(timer => timer.ms === 30000);
  assert.ok(poll, 'Dashboard did not register its background refresh poll');
  const visibilityListeners = h.documentListeners.get('visibilitychange') || [];
  assert.ok(visibilityListeners.length, 'Dashboard did not register visibility handling');

  h.document.hidden = true;
  h.document.visibilityState = 'hidden';
  poll.listener();
  await Promise.resolve();
  assert.equal(h.fetches.length, 0, 'Hidden-tab polling refreshed the dashboard');
  assert.equal(vm.runInContext('state.player', h.context), player);
  assert.equal(h.loads.length, loadCount, 'Hidden-tab polling replaced the active queue');
  visibilityListeners[0]({ type: 'visibilitychange' });
  await Promise.resolve();
  assert.equal(h.fetches.length, 0, 'Hidden-tab visibility handling refreshed the dashboard');

  h.document.hidden = false;
  h.document.visibilityState = 'visible';
  visibilityListeners[0]({ type: 'visibilitychange' });
  const refresh = vm.runInContext('state.refreshing', h.context);
  if (refresh && typeof refresh.then === 'function') await refresh;
  assert.ok(h.fetches.length > 0, 'Visible-tab return did not refresh the dashboard');
  assert.equal(vm.runInContext('state.player', h.context), player);
  assert.equal(h.loads.length, loadCount, 'Visible-tab refresh replaced the active queue');
});

test('active playback queue persists and is restored on a fresh dashboard load', async t => {
  const h = harness();
  if (!h.context.localStorage || typeof h.context.localStorage.setItem !== 'function' || typeof h.context.localStorage.getItem !== 'function') {
    return t.skip('Persistent storage is unavailable in this harness');
  }
  await h.ready();
  vm.runInContext("state.ranked = tracks; state.mixRecommendations = tracks.slice(); renderCollection()", h.context);
  await vm.runInContext('playTrack(1)', h.context);
  const stored = JSON.stringify(h.storageSnapshot());
  assert.match(stored, /aaaaaaaaaaa/, 'Persisted playback state does not include the first queued track');
  assert.match(stored, /bbbbbbbbbbb/, 'Persisted playback state does not include the selected queued track');

  const reloaded = harness({ storage: h.localStorage });
  await reloaded.ready();
  assert.deepEqual(vm.runInContext('state.queue.map(track => track.video_id)', reloaded.context), ['aaaaaaaaaaa', 'bbbbbbbbbbb']);
  assert.equal(vm.runInContext('state.queueIndex', reloaded.context), 1);
});

test('an ended track advances to the next queued song', async () => {
  const h = harness();
  await vm.runInContext('playTrack(0)', h.context);
  assert.equal(typeof h.configs[0]?.events?.onStateChange, 'function', 'YouTube player did not register a state-change handler');
  h.setPlayerIndex(0);
  vm.runInContext('state.queueIndex = 0', h.context);
  h.playerCalls.length = 0;
  h.configs[0].events.onStateChange({ data: 0 });
  assert.equal(vm.runInContext('state.queueIndex', h.context), 1);
  assert.equal(h.node('#now-playing').textContent, 'Song 1 — Artist');
  assert.ok(h.playerCalls.some(call => (call.method === 'playVideoAt' && call.index === 1) || call.method === 'nextVideo'), 'Ended playback did not advance the player');
});

test('Play mix keeps the saved fresh mix when browsing the library', async () => {
  const h = harness();
  const libraryTrack = { track: { track_key: 'video:ccccccccccc', video_id: 'ccccccccccc', title: 'Library song', artist: 'Other' }, score: 0.2 };
  h.context.libraryTrack = libraryTrack;
  vm.runInContext("state.library = [...tracks, libraryTrack]; state.view = 'all'; renderCollection()", h.context);
  await vm.runInContext('playMix()', h.context);
  assert.deepEqual(h.loads, [{ ids: ['aaaaaaaaaaa', 'bbbbbbbbbbb'], index: 0 }]);
});

test('refresh while browsing the library replaces the playable fresh mix', async () => {
  const h = harness();
  const freshTrack = { track: { track_key: 'video:ddddddddddd', video_id: 'ddddddddddd', title: 'Fresh song', artist: 'New artist' }, score: 0.9 };
  const original = h.context.fetch;
  vm.runInContext("state.ranked = tracks.slice(); state.mixRecommendations = tracks.slice(); state.view = 'all'; renderCollection()", h.context);
  h.context.fetch = async url => {
    if (url === '/api/recommendations') return { ok: true, text: async () => JSON.stringify({ items: [freshTrack] }) };
    if (url === '/api/library') return { ok: true, text: async () => JSON.stringify({ items: h.context.tracks }) };
    return original(url);
  };
  await vm.runInContext('refresh()', h.context);
  assert.equal(vm.runInContext('state.ranked[0].track.track_key', h.context), 'video:ddddddddddd');
  assert.equal(vm.runInContext('state.mixRecommendations[0].track.track_key', h.context), 'video:ddddddddddd');
  await vm.runInContext('playMix()', h.context);
  assert.deepEqual(h.loads, [{ ids: ['ddddddddddd'], index: 0 }]);
});

test('restricted playback and autoplay blocking have visible recovery instructions', async () => {
  const h = harness();
  await vm.runInContext('playTrack(0)', h.context);
  h.configs[0].events.onError({ data: 150 });
  assert.match(h.node('#player-status').textContent, /Choose Next or open it/);
  assert.equal(h.node('#open-playing').href, 'https://music.youtube.com/watch?v=aaaaaaaaaaa');
  h.configs[0].events.onAutoplayBlocked();
  assert.match(h.node('#player-status').textContent, /Press play inside/);
});

test('invalid provider IDs are rejected and no-favorites state is explicit', () => {
  const h = harness();
  assert.equal(vm.runInContext("videoId({url:'https://evil.example/watch?v=aaaaaaaaaaa'})", h.context), null);
  assert.equal(vm.runInContext("videoId({url:'https://music.youtube.com/podcast/aaaaaaaaaaa'})", h.context), 'aaaaaaaaaaa');
  vm.runInContext('renderOverview({liked_track_count:0})', h.context);
  assert.match(h.node('#taste-copy').textContent, /No confirmed favorites/);
});

test('library search and favorites view filter the actual collection', () => {
  const h=harness();
  vm.runInContext("state.library=tracks; state.view='library'; state.query='song 1'; renderCollection()",h.context);
  assert.equal(vm.runInContext('state.recommendations.length',h.context),1);
  assert.match(h.node('#recommendations').innerHTML,/Song 1/);
  vm.runInContext("state.query=''; state.view='favorites'; state.favorites.add('video:aaaaaaaaaaa'); renderCollection()",h.context);
  assert.equal(vm.runInContext('state.recommendations[0].track.title',h.context),'Song 0');
});

test('Refresh mix signals the installed bridge before requesting a new batch', async () => {
  const h = harness();
  await vm.runInContext('scan()', h.context);
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].message.type, 'ytmusic-personal-mix-refresh');
  assert.equal(h.posts[0].origin, 'http://127.0.0.1:8000');
});

test('Refresh remains usable but explains when the live bridge is unavailable', async () => {
  const h = harness();
  h.windowObject.postMessage = (message, origin) => {
    h.posts.push({ message, origin });
    if (message.type !== 'ytmusic-personal-mix-refresh') return;
    const result = { type: 'ytmusic-personal-mix-refresh-result', request_id: message.request_id, ok: false, tabs: 0, acknowledged: 0, timed_out: true };
    h.windowListeners.slice().forEach(listener => listener({ source: h.windowObject, origin, data: result }));
  };
  await vm.runInContext('scan()', h.context);
  assert.match(h.node('#toast').textContent, /saved history/);
});

test('Refresh uses a returned complete fresh batch without waiting on background discovery', async () => {
  const h = harness();
  vm.runInContext('globalThis.waitCalls = 0; waitForHostedRefresh = async () => { globalThis.waitCalls += 1; return {}; }', h.context);
  const original = h.context.fetch;
  const fullBatch = Array.from({ length: 50 }, (_, index) => ({
    track: { track_key: `video:full-${String(index).padStart(2, '0')}`, video_id: 'aaaaaaaaaaa', title: `Full song ${index}`, artist: 'Artist' },
  }));
  h.context.fetch = async url => {
    if (url === '/api/scan') return { ok: true, text: async () => JSON.stringify({ status: 'completed', recommendations: fullBatch, preserved_previous_mix: false }) };
    if (url === '/api/recommendations') return { ok: true, text: async () => JSON.stringify({ items: h.context.tracks }) };
    return original(url);
  };
  await vm.runInContext('scan()', h.context);
  assert.equal(vm.runInContext('globalThis.waitCalls', h.context), 0);
});

test('Refresh waits when only a partial fresh batch arrives', async () => {
  const h = harness();
  vm.runInContext("state.ranked=[tracks[1]]; state.mixRecommendations=[tracks[1]]; renderCollection()", h.context);
  vm.runInContext('globalThis.waitCalls = 0; waitForHostedRefresh = async () => { globalThis.waitCalls += 1; return {}; }', h.context);
  const original = h.context.fetch;
  h.context.fetch = async url => {
    if (url === '/api/scan') return { ok: true, text: async () => JSON.stringify({ status: 'completed', recommendations: [h.context.tracks[0]] }) };
    if (url === '/api/recommendations') return { ok: true, text: async () => JSON.stringify({ items: [h.context.tracks[0]] }) };
    return original(url);
  };
  await vm.runInContext('scan()', h.context);
  assert.equal(vm.runInContext('globalThis.waitCalls', h.context), 1);
  assert.equal(vm.runInContext('state.ranked[0].track.track_key', h.context), 'video:aaaaaaaaaaa');
  assert.notEqual(vm.runInContext('state.ranked[0].track.track_key', h.context), 'video:bbbbbbbbbbb');
});
