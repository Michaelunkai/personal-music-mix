const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { textContent: '', innerHTML: '', disabled: false, hidden: false, dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, querySelector: () => node(selector + ' child') });
    return nodes.get(selector);
  };
  const loads = [], jumps = [], configs = [], posts = [];
  const windowListeners = [];
  const windowObject = {
    location: { origin: 'http://127.0.0.1:8000' }, setTimeout: () => 1, clearTimeout() {},
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
  windowObject.YT = { Player: class {
    constructor(_id, config) { configs.push(config); Promise.resolve().then(() => config.events.onReady()); }
    loadPlaylist(ids, index) { loads.push({ ids: Array.from(ids), index }); }
    getPlaylistIndex() { return 1; }
    playVideoAt(index) { jumps.push(index); }
    destroy() {}
  } };
  const context = vm.createContext({
    URL, console, AbortSignal,
    document: { querySelector: node, addEventListener() {} },
    window: windowObject,
    fetch: async url => ({ ok: true, text: async () => JSON.stringify({
      '/api/health': { ok: true, database: { ok: true } },
      '/api/overview': { liked_track_count: 0 }, '/api/recommendations': { items: [] },
      '/api/runs': { items: [] }, '/api/status': {}, '/api/playlists/latest': {},
      '/api/connection': {}, '/api/favorites': { track_keys: [] },
    }[url] || {}) }),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8'), context);
  context.tracks = tracks;
  vm.runInContext('renderRecommendations(tracks)', context);
  return { context, node, loads, jumps, configs, posts, windowListeners, windowObject };
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

test('Play mix keeps the saved fresh mix when browsing the library', async () => {
  const h = harness();
  const libraryTrack = { track: { track_key: 'video:ccccccccccc', video_id: 'ccccccccccc', title: 'Library song', artist: 'Other' }, score: 0.2 };
  h.context.libraryTrack = libraryTrack;
  vm.runInContext("state.library = [...tracks, libraryTrack]; state.view = 'all'; renderCollection()", h.context);
  await vm.runInContext('playMix()', h.context);
  assert.deepEqual(h.loads, [{ ids: ['aaaaaaaaaaa', 'bbbbbbbbbbb'], index: 0 }]);
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
