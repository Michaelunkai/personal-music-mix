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
  const audioListeners = new Map();
  const audioElement = {
    src:'',currentTime:0,duration:180,playbackRate:1,paused:true,ended:false,readyState:1,preload:'metadata',hidden:true,
    addEventListener:(name,listener)=>{const list=audioListeners.get(name)||[];list.push(listener);audioListeners.set(name,list);},
    dispatch:name=>(audioListeners.get(name)||[]).map(listener=>listener({type:name,target:audioElement})),
    play(){this.paused=false;this.ended=false;this.dispatch('playing');return Promise.resolve();},
    pause(){if(this.paused)return;this.paused=true;this.dispatch('pause');},
    load(){},
  };
  nodes.set('#audio-player',audioElement);
  windowObject.Audio = class { constructor(src){this.src=src;this.preload='';} load(){} };
  windowObject.YT = { Player: class {
    constructor(_id, config) { configs.push(config); Promise.resolve().then(() => config.events.onReady()); }
    loadPlaylist(ids, index) { playerIndex = index; loads.push({ ids: Array.from(ids), index }); playerCalls.push({ method: 'loadPlaylist', ids: Array.from(ids), index }); }
    loadVideoById(options) { playerCalls.push({ method: 'loadVideoById', ...options }); }
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
    loads, jumps, configs, posts, windowListeners, windowObject, document: documentObject,audioElement,audioListeners,
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

test('mixed Audius and YouTube queue keeps full audio playing while hidden and exposes device controls', async () => {
  const h=harness({mediaSession:true});
  await h.ready();
  const stream='https://api.audius.co/v1/tracks/audio-track/stream?app_name=personal-music-mix';
  h.context.mixedItems=[
    {track:{track_key:'audius:audio-track',provider:'audius',title:'Full song',artist:'Audio artist',audio_url:stream,duration_seconds:180}},
    h.context.tracks[0],
  ];
  await vm.runInContext('playTrackFrom(mixedItems,0)',h.context);
  assert.equal(vm.runInContext("state.currentSource",h.context),'audius');
  assert.equal(h.audioElement.paused,false);
  h.document.hidden=true;h.document.visibilityState='hidden';
  h.document.dispatchEvent({type:'visibilitychange'});
  assert.equal(h.audioElement.paused,false,'switching away from the page paused audio');
  assert.equal(vm.runInContext('seekPlayback(30)',h.context),true);
  assert.equal(h.audioElement.currentTime,30);
  await h.mediaSession.handlers.get('pause')();
  assert.equal(h.audioElement.paused,true);
  await h.mediaSession.handlers.get('play')();
  assert.equal(h.audioElement.paused,false);
  await h.mediaSession.handlers.get('nexttrack')();
  assert.equal(vm.runInContext('state.currentSource',h.context),'youtube');
  assert.ok(h.playerCalls.some(call=>call.method==='loadVideoById'&&call.videoId==='aaaaaaaaaaa'));
  await h.mediaSession.handlers.get('previoustrack')();
  assert.equal(vm.runInContext('state.currentSource',h.context),'audius');
  assert.equal(h.audioElement.paused,false);
  h.audioElement.ended=true;h.audioElement.paused=true;h.audioElement.dispatch('ended');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(vm.runInContext('state.currentSource',h.context),'youtube','natural end did not advance to the next provider');
});

test('recommendations expose source-aware like and dislike feedback controls', async () => {
  const h=harness();await h.ready();
  const track={track_key:'audius:feedback-track',provider:'audius',title:'Feedback song',artist:'Feedback artist',audio_url:'https://api.audius.co/v1/tracks/feedback-track/stream?app_name=personal-music-mix'};
  h.context.items=[{track}];
  vm.runInContext('renderRecommendations(items)',h.context);
  const html=h.node('#recommendations').innerHTML;
  assert.match(html,/Audius full track/);
  assert.match(html,/data-feedback="like"/);
  assert.match(html,/data-feedback="dislike"/);
  assert.doesNotMatch(html,/data-favorite=/);
  const requests=[];
  h.context.fetch=async(url,options={})=>{requests.push({url,body:options.body&&JSON.parse(options.body)});return {ok:true,text:async()=>JSON.stringify({saved:true})};};
  const button={disabled:false,textContent:'👍 Like',dataset:{feedback:'like',trackKey:track.track_key,provider:'audius',title:track.title,artist:track.artist,duration:'180'},setAttribute(name,value){this[name]=value;}};
  const target={closest(selector){return selector==='[data-feedback]'?button:null;}};
  await h.node('#recommendations').dispatchEvent({type:'click',target});
  assert.equal(requests[0].url,'/api/feedback');
  assert.equal(requests[0].body.event,'like');
  assert.equal(requests[0].body.provider,'audius');
  assert.match(requests[0].body.event_id,/^[A-Za-z0-9_-]{16,100}$/);
  assert.equal(button.disabled,true);
  assert.equal(button['aria-pressed'],'true');
});

test('listening progress is counted from playback and explicit early Next sends skip feedback', async () => {
  const h=harness();
  const requests=[];
  h.context.fetch=async(url,options={})=>{if(url==='/api/feedback')requests.push(JSON.parse(options.body));return {ok:true,text:async()=>JSON.stringify({saved:true})};};
  h.context.items=[
    {track:{track_key:'audius:listen-track',provider:'audius',title:'Listening song',artist:'Audio artist',audio_url:'https://api.audius.co/v1/tracks/listen-track/stream?app_name=personal-music-mix',duration_seconds:180}},
    h.context.tracks[0],
  ];
  await vm.runInContext('playTrackFrom(items,0)',h.context);
  for(let i=0;i<10;i++){h.audioElement.currentTime+=3;vm.runInContext('observePlaybackProgress()',h.context);}
  assert.ok(requests.some(event=>event.event==='play_progress'&&event.listened_seconds>=30));
  await vm.runInContext('changeTrack(1)',h.context);
  assert.ok(requests.some(event=>event.event==='skipped'&&event.listened_seconds>=5));
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

test('Refresh uses a stable hosted request without requiring the browser bridge', async () => {
  const h = harness();
  const calls=[];
  h.context.fetch=async(url,options={})=>{
    if(url==='/api/mix/refresh') {calls.push(JSON.parse(options.body));return {ok:true,text:async()=>JSON.stringify({status:'unavailable',message:'Only 12 new songs are ready; current mix unchanged.'})};}
    return {ok:true,text:async()=>JSON.stringify({})};
  };
  await vm.runInContext('scan()', h.context);
  assert.equal(calls.length,1);
  assert.match(calls[0].request_id,/^[A-Za-z0-9_-]{16,100}$/);
  assert.equal(h.posts.length,0);
  assert.match(h.node('#toast').textContent,/current mix unchanged/);
});

test('Refresh applies a complete 50-song hosted batch without waiting for replenishment', async () => {
  const h = harness();
  const fullBatch=Array.from({length:50},(_,index)=>({track:{track_key:`video:mix-${String(index).padStart(3,'0')}`,video_id:`mix${String(index).padStart(8,'0')}`,title:`Fresh ${index}`,artist:'Artist',provider:'youtube'}}));
  assert.equal(new Set(fullBatch.map(row=>row.track.video_id)).size,50);
  const original=h.context.fetch;
  h.context.fetch=async(url,options={})=>{
    if(url==='/api/mix/refresh') return {ok:true,text:async()=>JSON.stringify({status:'completed',request_id:JSON.parse(options.body).request_id,recommendations:fullBatch})};
    return original(url);
  };
  await vm.runInContext('scan()', h.context);
  assert.equal(vm.runInContext('state.ranked.length',h.context),50);
  assert.equal(new Set(vm.runInContext('state.ranked.map(row=>row.track.video_id)',h.context)).size,50);
  assert.match(h.node('#toast').textContent,/50 new songs/);
});

test('an unavailable refresh preserves the current mix instead of showing a partial batch', async () => {
  const h = harness();
  vm.runInContext("state.ranked=[tracks[1]]; state.mixRecommendations=[tracks[1]]; renderCollection()", h.context);
  const original = h.context.fetch;
  h.context.fetch = async (url,options={}) => {
    if (url === '/api/mix/refresh') return { ok: true, text: async () => JSON.stringify({status:'unavailable',message:'Only 3 eligible songs are ready. The current mix is unchanged.'}) };
    return original(url);
  };
  await vm.runInContext('scan()', h.context);
  assert.equal(vm.runInContext('state.ranked[0].track.track_key', h.context), 'video:bbbbbbbbbbb');
  assert.match(h.node('#toast').textContent,/current mix is unchanged/);
});
