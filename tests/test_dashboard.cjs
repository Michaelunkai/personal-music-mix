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
  const loads = [], jumps = [], configs = [];
  const tracks = ['aaaaaaaaaaa', 'bbbbbbbbbbb'].map((id, i) => ({ track: { track_key: `video:${id}`, video_id: id, title: `Song ${i}`, artist: 'Artist' }, score: 0.5 }));
  const context = vm.createContext({
    URL, console, AbortSignal,
    document: { querySelector: node, addEventListener() {} },
    window: {
      location: { origin: 'http://127.0.0.1:8000' }, setTimeout: () => 1, clearTimeout() {},
      YT: { Player: class {
        constructor(_id, config) { configs.push(config); Promise.resolve().then(() => config.events.onReady()); }
        loadPlaylist(ids, index) { loads.push({ ids: Array.from(ids), index }); }
        getPlaylistIndex() { return 1; }
        playVideoAt(index) { jumps.push(index); }
        destroy() {}
      } },
    },
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
  return { context, node, loads, jumps, configs };
}

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
