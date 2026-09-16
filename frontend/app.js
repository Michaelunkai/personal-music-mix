const $ = (selector) => document.querySelector(selector);
const PLAYER_QUEUE_STORAGE_KEY = "ytmusic-personal-mix-player-v1";
const PLAYER_QUEUE_STORAGE_VERSION = 2;
const state = { latestPlan: null, catalog: null, schedulerRunning: false, recommendations: [], mixRecommendations: [], ranked: [], library: [], view: 'mix', query: '', favorites: new Set(), favoritesReady:false, queue: [], queueIndex: 0, player: null, youtubePlayer:null, playerReady: null, audioElement:null, audioAdapter:null, audioHandlersReady:false, preloadedAudio:null, currentSource:null, queueMode:null, playerLoading: false, refreshing: false, refreshRequestId:null, playbackState: 'idle', restoredPosition: 0, queueRestored: false, playedTrackKeys: new Set(), playbackControlsReady: false, pauseButton: null, stopButton: null, mediaSessionInstalled: false, positionTimer: null, lastPositionPersistedAt: 0, lastEndedIndex: null, stopRequested: false, pauseRequested: false, playerNeedsLoad: false, feedbackTrackKey:null, feedbackListenedSeconds:0, feedbackLastPosition:0, feedbackLastProgressAt:0, completedFeedbackKey:null };

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 3800);
}

function setPlayerStatus(message) {
  const node = $("#player-status");
  if (node) node.textContent = message;
}

function getPersistentStorage() {
  try {
    const storage = window.localStorage || (typeof localStorage !== "undefined" ? localStorage : null);
    return storage && typeof storage.getItem === "function" && typeof storage.setItem === "function" ? storage : null;
  } catch { return null; }
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clampQueueIndex(index, length) {
  if (!length) return 0;
  return Math.min(length - 1, Math.max(0, Math.floor(finiteNumber(index, 0))));
}

function getTrackKey(track) {
  const id = videoId(track || {});
  return track?.track_key || (id ? `video:${id}` : "");
}

function audioStreamUrl(track) {
  const value=String(track?.audio_url||'');
  return /^https:\/\/api\.audius\.co\/v1\/tracks\/[A-Za-z0-9_-]{1,80}\/stream\?app_name=[A-Za-z0-9_-]{1,80}$/.test(value)?value:null;
}

function isPlayableTrack(track) { return Boolean(audioStreamUrl(track)||videoId(track||{})); }

function snapshotTrack(track) {
  if (!track || typeof track !== "object") return null;
  const audioUrl=audioStreamUrl(track);
  if(audioUrl) return {
    track_key:String(track.track_key||track.candidate_key||''),provider:'audius',recording_key:String(track.recording_key||''),
    audio_url:audioUrl,provider_url:String(track.provider_url||track.url||''),
    title:String(track.title||'Untitled'),artist:String(track.artist||'Unknown artist'),album:track.album?String(track.album):'',
    duration_seconds:Number.isFinite(Number(track.duration_seconds))?Number(track.duration_seconds):null,
    artwork_url:String(track.artwork_url||track.thumbnail||''),
  };
  const id = videoId(track);
  if (!id) return null;
  return {
    track_key: String(track.track_key || `video:${id}`),
    video_id: id,
    title: String(track.title || "Untitled"),
    artist: String(track.artist || "Unknown artist"),
    album: track.album ? String(track.album) : "",
    provider:'youtube',recording_key:String(track.recording_key||''),
    url: String(track.url || track.canonical_url || `https://music.youtube.com/watch?v=${id}`),
    thumbnail: String(track.thumbnail || track.thumbnail_url || track.artwork_url || ""),
  };
}

function currentPlayerTime() {
  try {
    return state.player && typeof state.player.getCurrentTime === "function" ? Math.max(0, finiteNumber(state.player.getCurrentTime(), 0)) : 0;
  } catch { return 0; }
}

function persistQueue() {
  const storage = getPersistentStorage();
  const queue = state.queue.map(snapshotTrack).filter(Boolean);
  if (!storage || !queue.length) return false;
  try {
    storage.setItem(PLAYER_QUEUE_STORAGE_KEY, JSON.stringify({
      version: PLAYER_QUEUE_STORAGE_VERSION,
      queue,
      queueIndex: clampQueueIndex(state.queueIndex, queue.length),
      currentTime: currentPlayerTime(),
      playbackState: state.playbackState,
      playedTrackKeys: Array.from(state.playedTrackKeys || []).slice(-5000),
      savedAt: Date.now(),
    }));
    state.lastPositionPersistedAt = Date.now();
    return true;
  } catch { return false; }
}

function restoreQueue() {
  const storage = getPersistentStorage();
  if (!storage) return false;
  let saved;
  try { saved = JSON.parse(storage.getItem(PLAYER_QUEUE_STORAGE_KEY) || "null"); } catch { return false; }
  if (!saved || (saved.version && ![1,PLAYER_QUEUE_STORAGE_VERSION].includes(saved.version)) || !Array.isArray(saved.queue)) return false;
  const queue = saved.queue.map(snapshotTrack).filter(Boolean);
  if (!queue.length) return false;
  // Reuse the page's current array realm when one exists. Besides keeping the
  // queue plain and serializable, this avoids cross-realm collection quirks in
  // embedded test hosts while remaining a normal browser array in production.
  const queueContainer = Array.isArray(state.recommendations) ? state.recommendations.slice(0, 0) : [];
  queueContainer.push(...queue);
  state.queue = queueContainer;
  state.queueIndex = clampQueueIndex(saved.queueIndex, queue.length);
  const activeTrack=queue[state.queueIndex];
  state.currentSource=audioStreamUrl(activeTrack)?'audius':videoId(activeTrack)?'youtube':null;
  state.queueMode=queue.every(videoId)?'youtube':queue.every(audioStreamUrl)?'audio':'mixed';
  state.restoredPosition = Math.max(0, finiteNumber(saved.currentTime, 0));
  state.playedTrackKeys = new Set(Array.isArray(saved.playedTrackKeys) ? saved.playedTrackKeys.filter(Boolean) : []);
  state.queueRestored = true;
  state.stopRequested = false;
  state.pauseRequested = false;
  state.playbackState = saved.playbackState === "stopped" ? "stopped" : "paused";
  updatePlayingInfo();
  setPlayerStatus("Queue restored. Press Resume to continue playback.");
  return true;
}

async function api(path, options = {}) {
  const response = await fetch(path, { signal: AbortSignal.timeout(20000), headers: { "Content-Type": "application/json", "X-Mix-Mode": "fresh", ...(options.headers || {}) }, ...options });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { throw new Error("Your session may have expired. Reload the page to reconnect."); }
  if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`);
  return data;
}

function formatNumber(value) { return Number(value || 0).toLocaleString(); }

function renderOverview(data) {
  $("#track-count").textContent = formatNumber(data.track_count);
  $("#play-count").textContent = formatNumber(data.play_count);
  $("#liked-count").textContent = formatNumber(data.liked_track_count);
  $("#taste-copy").textContent = data.liked_track_count
    ? `Your mix starts with your most-listened songs and uses ${formatNumber(data.provider_liked_track_count)} imported YouTube likes plus ${formatNumber(data.local_favorite_count)} dashboard favorites. Every song in For you is new to your listening history.`
    : "No confirmed favorites are imported yet. Your mix starts with your most-listened songs; add a heart or import YouTube likes to give discovery stronger preference signals. For you only shows songs you have not heard.";
}

function renderRecommendations(items) {
  state.recommendations = items;
  // Direct callers (including the initial render in older integrations) may
  // not have populated ranked yet; keep their first mix as the saved queue.
  if (state.view === 'mix' && !state.ranked.length) state.mixRecommendations = items.slice();
  const mixItems = state.mixRecommendations.length ? state.mixRecommendations : items;
  $("#play-mix").disabled = !mixItems.some(item => isPlayableTrack(item.track || item));
  const root = $("#recommendations");
  $("#recommendation-count").textContent = `${items.length} songs`;
  if (!items.length) { root.className = "recommendations empty"; root.textContent = state.query ? "No songs match this search. Try another title or artist." : state.view === 'favorites' ? "Your favorites will appear here. Save a song with the heart button to get started." : "No new songs are ready yet. Press Refresh mix to request another fresh batch."; return; }
  root.className = "recommendations";
  const rendered = items.map((item, index) => {
    const track = item.track || {};
    const reasons = (item.reasons || []).slice(0, 3).join(" • ");
    const favorite = state.favorites.has(track.track_key);
    const provider=audioStreamUrl(track)?'audius':'youtube';
    const canFavorite=provider==='youtube';
    const favoriteControl=canFavorite?`<button class="secondary" data-favorite="${escapeHtml(track.track_key)}" ${state.favoritesReady ? "" : "disabled"} aria-label="${favorite ? 'Remove favorite' : 'Favorite'} ${escapeHtml(track.title)}" aria-pressed="${favorite}">${favorite ? "♥ Favorited" : "♡ Favorite"}</button>`:'';
    return `<article class="recommendation"><div class="rank">${String(index + 1).padStart(2, "0")}</div><div class="track-detail"><div class="track-title">${escapeHtml(track.title || "Untitled")}</div><div class="track-meta">${escapeHtml(track.artist || "Unknown artist")}${track.album ? ` · ${escapeHtml(track.album)}` : ""} · ${provider==='audius'?'Audius full track':'YouTube'}</div><div class="track-reasons">${escapeHtml(reasons)}</div><div class="track-actions"><button class="secondary" data-play="${index}" ${isPlayableTrack(track) ? "" : "disabled"} aria-label="Play ${escapeHtml(track.title)}">▶ Play</button><button class="secondary" data-feedback="like" data-track-key="${escapeHtml(track.track_key)}" data-provider="${provider}" data-title="${escapeHtml(track.title)}" data-artist="${escapeHtml(track.artist||'Unknown artist')}" data-duration="${finiteNumber(track.duration_seconds,0)}" aria-label="Like ${escapeHtml(track.title)}">👍 Like</button><button class="secondary" data-feedback="dislike" data-track-key="${escapeHtml(track.track_key)}" data-provider="${provider}" data-title="${escapeHtml(track.title)}" data-artist="${escapeHtml(track.artist||'Unknown artist')}" data-duration="${finiteNumber(track.duration_seconds,0)}" aria-label="Not for me: ${escapeHtml(track.title)}">👎 Not for me</button>${favoriteControl}</div></div></article>`;
  }).join("");
  if (root.innerHTML !== rendered) root.innerHTML = rendered;
}

function renderRuns(items) {
  const root = $("#runs");
  if (!items.length) { root.className = "runs empty"; root.textContent = "No scans yet."; return; }
  root.className = "runs";
  root.innerHTML = items.slice(0, 8).map((run) => `<div class="run ${run.status === "failed" ? "failed" : ""}"><strong>${escapeHtml(run.status)}</strong> · ${formatNumber(run.items_seen)} items<span>${escapeHtml(run.message || "No details")}${run.error_code ? ` · ${escapeHtml(run.error_code)}` : ""}</span></div>`).join("");
}

function renderLatestPlaylist(data) {
  const root = $("#playlist-preview");
  if (!data?.available || !data.plan) {
    root.className = "preview empty";
    root.textContent = "A fresh playlist preview appears when unseen recommendations arrive.";
    state.latestPlan = null;
    $("#write-button").disabled = true;
    return;
  }
  const plan = data.plan;
  const items = Array.isArray(plan.items) ? plan.items : [];
  const recommendations = Array.isArray(plan.recommendations) ? plan.recommendations : [];
  const tracks = recommendations.length ? recommendations : items;
  state.latestPlan = plan;
  if (!state.playlistNameDirty) $("#playlist-name").value = plan.name || "Your personal mix";
  root.className = "preview";
  root.innerHTML = `<strong>Your mix: ${formatNumber(tracks.length)} songs.</strong><br>${escapeHtml(tracks.slice(0, 5).map((item) => {
    const track = item.track || item;
    return `${track.title || "Untitled"} — ${track.artist || "Unknown artist"}`;
  }).join(" · "))}${tracks.length > 5 ? " · …" : ""}<br><span class="muted">Play this mix here. Saving it to YouTube Music is a separate action.</span>`;
  $("#write-button").disabled = !data.write_enabled;
  $("#write-button").hidden = !data.write_enabled;
  $("#playlist-write-help").textContent = data.write_enabled ? "You can also save this playlist to your YouTube Music account." : "Your playlist is saved here. Use Play mix to listen.";
}

function renderHealth(data) {
  const pill = $("#health-pill");
  const ok = Boolean(data.ok && data.database?.ok);
  pill.classList.toggle("healthy", ok); pill.classList.toggle("error", !ok);
  pill.querySelector("span:last-child").textContent = ok ? "Music library connected" : "Connection needs attention";
}

function renderConnection(data) {
  const copy = $("#scan-copy");
  if (!copy || !data) return;
  const suffix = data.state === "history_ingested"
    ? ` ${formatNumber(data.history_events)} listening events are stored locally.`
    : "";
  copy.textContent = `${data.message || "Connector status unavailable."}${suffix}`;
}

function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[char])); }

function applyFreshRecommendations(items) {
  const next = Array.isArray(items) ? items.slice() : [];
  const current = state.mixRecommendations.length ? state.mixRecommendations : state.ranked;
  // A background/visibility refresh must not make a complete fresh mix look
  // empty or partial while the provider is still expanding it. The active
  // player queue is separate state and is never rebuilt by this helper.
  if (current.length >= 50 && next.length < 50) return false;
  state.ranked = next;
  state.mixRecommendations = next.slice();
  return true;
}

async function refresh({ silent = false } = {}) {
  // A save during an older poll must await a fresh response after that poll.
  state.refreshRequested = true;
  state.refreshNotify = state.refreshNotify || !silent;
  if (state.refreshing) return state.refreshing;
  state.refreshing = (async () => {
    do {
      state.refreshRequested = false;
      const results = await Promise.allSettled(['health','overview','recommendations','runs','status','playlists/latest','connection','favorites','library','catalog'].map(path => api(`/api/${path}`)));
      const [health, overview, recs, runs, status, latestPlaylist, connection, favorites, library, catalog] = results.map(result => result.status === 'fulfilled' ? result.value : null);
      if (catalog) state.catalog = catalog.catalog || null;
      state.favoritesReady = Boolean(favorites);
      if (favorites) state.favorites = new Set(favorites.track_keys || []);
      if (recs) applyFreshRecommendations(recs.items);
      if (library) state.library = library.items || [];
      renderHealth(health || {ok:false});
      if (overview) renderOverview(overview);
      renderCollection();
      if (runs) renderRuns(runs.items || []);
      if (latestPlaylist) renderLatestPlaylist(latestPlaylist);
      if (connection) renderConnection(connection);
      if (status) {
        state.schedulerRunning = Boolean(status.scheduler_running);
        const schedulerButton = $("#scheduler-button");
        const bridgeDriven = status.scheduler_mode === "browser_bridge_event_driven";
        schedulerButton.disabled = bridgeDriven;
        schedulerButton.hidden = bridgeDriven;
        schedulerButton.textContent = bridgeDriven ? "Automatic bridge sync" : (state.schedulerRunning ? "Pause auto-scan" : "Start auto-scan");
        schedulerButton.dataset.action = state.schedulerRunning ? "stop" : "start";
      }
      const failure = results.find(result => result.status === 'rejected');
      if (failure && state.refreshNotify) toast(`Some information could not refresh: ${failure.reason.message}`);
    } while (state.refreshRequested);
  })().finally(() => { state.refreshing = false; state.refreshNotify = false; });
  return state.refreshing;
}

function renderCollection() {
  let items = state.view === 'mix' ? state.ranked : state.library;
  if (state.view === 'mix') state.mixRecommendations = items.slice();
  if (state.view === 'favorites') items = items.filter(item => state.favorites.has(item.track.track_key) || item.track.liked_count > 0 || item.track.like_events > 0);
  const query = state.query.trim().toLocaleLowerCase();
  if (query) items = items.filter(item => `${item.track.title} ${item.track.artist} ${item.track.album || ''}`.toLocaleLowerCase().includes(query));
  renderRecommendations(items);
}

function createRefreshRequestId() {
  try { return window.crypto?.randomUUID?.() || `mix-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`; }
  catch { return `mix-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`; }
}

async function requestFreshMix(requestId, deadline) {
  while(Date.now()<deadline) {
    const remaining=Math.max(1,deadline-Date.now());
    const result=await api('/api/mix/refresh',{method:'POST',body:JSON.stringify({request_id:requestId}),signal:AbortSignal.timeout(remaining)});
    if(result.status!=='pending') return result;
    await new Promise(resolve=>window.setTimeout(resolve,Math.min(250,Math.max(1,deadline-Date.now()))));
  }
  throw new Error('Refresh did not finish within 9 seconds. Your current mix is unchanged; press Refresh mix to retry.');
}

async function scan() {
  const button = $("#scan-button"); button.disabled = true; button.textContent = "Refreshing…";
  state.refreshRequestId=state.refreshRequestId||createRefreshRequestId();
  const requestId=state.refreshRequestId;
  try {
    const result=await requestFreshMix(requestId,Date.now()+9000);
    if(result.status==='unavailable') { state.refreshRequestId=null;toast(result.message||'No complete fresh mix is available yet. Your current mix is unchanged.');return; }
    if(result.status!=='completed'||!Array.isArray(result.recommendations)||result.recommendations.length!==50)
      throw new Error('The server did not return 50 verified fresh songs. Your current mix is unchanged.');
    state.refreshRequestId=null;
    applyFreshRecommendations(result.recommendations);
    renderCollection();
    toast('Fresh mix ready with 50 new songs.');
    // Refresh secondary counts and connection status outside the 9-second mix path.
    void refresh({silent:true}).catch(()=>{});
  } catch(error) {
    toast(error?.name==='TimeoutError'||error?.name==='AbortError'
      ? 'Refresh did not finish within 9 seconds. Your current mix is unchanged; press Refresh mix to retry.'
      : error.message);
  } finally { button.disabled=false;button.textContent='Refresh mix'; }
}

async function toggleScheduler() {
  const button = $("#scheduler-button"); button.disabled = true;
  try { await api("/api/scheduler", { method: "POST", body: JSON.stringify({ action: button.dataset.action }) }); await refresh(); }
  catch (error) { toast(error.message); } finally { button.disabled = false; }
}

async function previewPlaylist() {
  const root = $("#playlist-preview"); root.textContent = "Building preview…";
  const button = $("#preview-button"); button.disabled = true;
  const requestedName = $("#playlist-name").value;
  try {
    const plan = await api("/api/playlists/preview", { method: "POST", body: JSON.stringify({ name: requestedName }) });
    if ($("#playlist-name").value === requestedName) state.playlistNameDirty = false;
    renderLatestPlaylist({ available: true, plan: plan.plan, write_enabled: plan.write_enabled });
    toast("Playlist saved. Ready to play.");
  } catch (error) { root.className = "preview empty"; root.textContent = error.message; $("#write-button").disabled = true; }
  finally { button.disabled = false; }
}

async function writePlaylist() {
  if (!state.latestPlan || !window.confirm("Create a private playlist in your YouTube Music account? This is an external write.")) return;
  try { const result = await api("/api/playlists/write", { method: "POST", body: JSON.stringify({ name: $("#playlist-name").value, confirm: true }) }); toast(result.message || "Playlist write finished"); await refresh(); }
  catch (error) { toast(error.message); }
}

function videoId(track) {
  if (/^[A-Za-z0-9_-]{11}$/.test(track.video_id || "")) return track.video_id;
  try {
    const url = new URL(track.url || track.canonical_url);
    if (!["music.youtube.com", "www.youtube.com", "youtube.com", "youtu.be"].includes(url.hostname)) return null;
    const id = url.hostname === "youtu.be" ? url.pathname.slice(1) : url.searchParams.get("v") || (/^\/(podcast|song)\//.test(url.pathname) ? url.pathname.split("/")[2] : "");
    return /^[A-Za-z0-9_-]{11}$/.test(id || "") ? id : null;
  } catch { return null; }
}

function getMediaSession() {
  try { return window.navigator?.mediaSession || (typeof navigator !== "undefined" ? navigator.mediaSession : null) || null; } catch { return null; }
}

function setMediaSessionPlaybackState(playbackState) {
  const mediaSession = getMediaSession();
  if (!mediaSession) return;
  try { mediaSession.playbackState = playbackState; } catch { /* optional browser API */ }
}

function updateMediaSessionMetadata(track) {
  const mediaSession = getMediaSession();
  const Metadata = window.MediaMetadata || (typeof MediaMetadata === "function" ? MediaMetadata : null);
  if (!mediaSession || typeof Metadata !== "function" || !track) return;
  const artworkSource = track.thumbnail || track.thumbnail_url || track.artwork_url || (() => {
    const id = videoId(track);
    return id ? `https://i.ytimg.com/vi/${id}/hqdefault.jpg` : "";
  })();
  const metadata = {
    title: String(track.title || "Untitled"),
    artist: String(track.artist || "Unknown artist"),
    album: String(track.album || "Personal mix"),
  };
  if (artworkSource) metadata.artwork = [{ src: String(artworkSource) }];
  try { mediaSession.metadata = new Metadata(metadata); } catch { /* optional browser API */ }
}

function updateMediaSessionPosition() {
  const mediaSession = getMediaSession();
  const player = state.player;
  if (!mediaSession || typeof mediaSession.setPositionState !== "function" || !player || typeof player.getDuration !== "function" || typeof player.getCurrentTime !== "function") return;
  let duration;
  let currentTime;
  let playbackRate = 1;
  try {
    duration = finiteNumber(player.getDuration(), 0);
    currentTime = finiteNumber(player.getCurrentTime(), 0);
    if (typeof player.getPlaybackRate === "function") playbackRate = finiteNumber(player.getPlaybackRate(), 1);
  } catch { return; }
  if (!(duration > 0)) return;
  const position = Math.min(duration, Math.max(0, currentTime));
  try { mediaSession.setPositionState({ duration, position, playbackRate: playbackRate > 0 ? playbackRate : 1 }); } catch { /* duration can race player readiness */ }
}

function startPositionUpdates() {
  if (state.positionTimer !== null || typeof window.setInterval !== "function") return;
  state.positionTimer = window.setInterval(() => {
    if (!['playing', 'buffering'].includes(state.playbackState)) return stopPositionUpdates();
    updateMediaSessionPosition();
    observePlaybackProgress();
    if (Date.now() - state.lastPositionPersistedAt >= 5000) persistQueue();
  }, 1000);
}

function beginTrackObservation(track,position=0) {
  state.feedbackTrackKey=getTrackKey(track)||null;
  state.feedbackListenedSeconds=0;
  state.feedbackLastPosition=Math.max(0,finiteNumber(position,0));
  state.feedbackLastProgressAt=0;
  state.completedFeedbackKey=null;
}

function feedbackEventId() {
  try { if(typeof window.crypto?.randomUUID==='function') return window.crypto.randomUUID(); } catch {}
  return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

async function submitFeedback(track,event,listenedSeconds=0) {
  if(!track?.track_key||!String(track.title||'').trim()||!String(track.artist||'').trim()) return false;
  const provider=audioStreamUrl(track)?'audius':videoId(track)?'youtube':track.provider;
  if(!['youtube','audius'].includes(provider)) return false;
  try {
    await api('/api/feedback',{method:'POST',body:JSON.stringify({event_id:feedbackEventId(),track_key:String(track.track_key),provider,event,
      title:String(track.title),artist:String(track.artist),listened_seconds:Math.max(0,Math.floor(finiteNumber(listenedSeconds,0))),
      duration_seconds:finiteNumber(track.duration_seconds,finiteNumber(state.player?.getDuration?.(),0))})});
    return true;
  } catch { return false; }
}

function observePlaybackProgress() {
  const track=state.queue[state.queueIndex];
  const key=getTrackKey(track);
  if(!track||!key||state.playbackState!=='playing') return;
  if(state.feedbackTrackKey!==key) beginTrackObservation(track,currentPlayerTime());
  let position=currentPlayerTime();
  if(!(position>0)) return;
  const delta=position-state.feedbackLastPosition;
  if(delta>0&&delta<=5) state.feedbackListenedSeconds+=delta;
  state.feedbackLastPosition=position;
  const whole=Math.floor(state.feedbackListenedSeconds/30)*30;
  if(whole>=30&&whole>state.feedbackLastProgressAt) {
    state.feedbackLastProgressAt=whole;
    void submitFeedback(track,'play_progress',whole);
  }
}

function recordIntentionalSkip() {
  const track=state.queue[state.queueIndex];
  if(!track||state.feedbackListenedSeconds<5) return;
  const duration=finiteNumber(track.duration_seconds,finiteNumber(state.player?.getDuration?.(),0));
  const position=currentPlayerTime();
  if(duration>0&&position/duration>=0.85) return;
  void submitFeedback(track,'skipped',state.feedbackListenedSeconds);
}

function stopPositionUpdates() {
  if (state.positionTimer === null) return;
  if (typeof window.clearInterval === "function") window.clearInterval(state.positionTimer);
  state.positionTimer = null;
}

function seekPlayback(offsetOrPosition, { relative = false, fastSeek = false } = {}) {
  const player = state.player;
  if (!player || typeof player.seekTo !== "function" || typeof player.getDuration !== "function") {
    setPlayerStatus("Seeking is available after the song is ready.");
    return false;
  }
  let duration;
  let current;
  try {
    duration = finiteNumber(player.getDuration(), 0);
    current = typeof player.getCurrentTime === "function" ? finiteNumber(player.getCurrentTime(), 0) : 0;
  } catch {
    setPlayerStatus("Seeking is available after the song is ready.");
    return false;
  }
  if (!(duration > 0)) {
    setPlayerStatus("Seeking is available after the song is ready.");
    return false;
  }
  const requested = finiteNumber(offsetOrPosition, NaN);
  if (!Number.isFinite(requested)) return false;
  const position = Math.min(duration, Math.max(0, relative ? current + requested : requested));
  try {
    player.seekTo(position, Boolean(fastSeek));
    state.restoredPosition = position;
    persistQueue();
    updateMediaSessionPosition();
    setPlayerStatus(`Seeking to ${Math.floor(position)} seconds.`);
    return true;
  } catch {
    setPlayerStatus("Seeking is unavailable for this song.");
    return false;
  }
}

function installMediaSessionHandlers() {
  const mediaSession = getMediaSession();
  if (!mediaSession || typeof mediaSession.setActionHandler !== "function") return false;
  const handlers = {
    play: () => resumePlayback(),
    pause: () => pausePlayback(),
    stop: () => stopPlayback(),
    nexttrack: () => changeTrack(1),
    previoustrack: () => changeTrack(-1),
    seekbackward: details => seekPlayback(-Math.max(1, finiteNumber(details?.seekOffset, 10)), { relative: true }),
    seekforward: details => seekPlayback(Math.max(1, finiteNumber(details?.seekOffset, 10)), { relative: true }),
    seekto: details => {
      const seekTime = Number(details?.seekTime);
      return Number.isFinite(seekTime) ? seekPlayback(seekTime, { fastSeek: Boolean(details?.fastSeek) }) : false;
    },
  };
  Object.entries(handlers).forEach(([action, handler]) => {
    try { mediaSession.setActionHandler(action, handler); } catch { /* unsupported action on this browser */ }
  });
  state.mediaSessionInstalled = true;
  return true;
}

function updatePlaybackControls() {
  const hasQueue = state.queue.length > 0;
  const previous = $("#previous-track");
  const next = $("#next-track");
  if (previous) previous.disabled = !hasQueue || state.queueIndex <= 0;
  if (next) next.disabled = !hasQueue || state.queueIndex >= state.queue.length - 1;
  const active = ['playing', 'loading', 'buffering'].includes(state.playbackState);
  const pauseButton = state.pauseButton || $("#pause-track") || $("#pause-resume") || $("#pause-button") || $("#player-pause");
  if (pauseButton) {
    pauseButton.disabled = !hasQueue;
    pauseButton.textContent = active ? "Pause" : (hasQueue ? "Resume" : "Play");
    pauseButton.dataset.action = active ? "pause" : "resume";
    pauseButton.dataset.playbackAction = active ? "pause" : "resume";
    pauseButton.setAttribute?.("aria-label", active ? "Pause playback" : (hasQueue ? "Resume playback" : "Play current song"));
    pauseButton.setAttribute?.("aria-pressed", String(active));
  }
  const stopButton = state.stopButton || $("#stop-track") || $("#stop-playback") || $("#stop-button") || $("#player-stop");
  if (stopButton) {
    stopButton.disabled = !hasQueue || state.playbackState === "idle";
    stopButton.dataset.playbackAction = "stop";
  }
}

function ensurePlaybackControls() {
  if (state.playbackControlsReady) return;
  const container = document.querySelector(".player-actions");
  if (!container) return;
  const canCreate = typeof document.createElement === "function" && typeof container.appendChild === "function";
  let pauseButton = document.querySelector("#pause-track") || document.querySelector("#pause-resume") || document.querySelector("#pause-button") || document.querySelector("#player-pause");
  let stopButton = document.querySelector("#stop-track") || document.querySelector("#stop-playback") || document.querySelector("#stop-button") || document.querySelector("#player-stop");
  if (!pauseButton && canCreate) {
    pauseButton = document.createElement("button");
    pauseButton.type = "button";
    pauseButton.id = "pause-resume";
    pauseButton.className = "secondary";
    pauseButton.textContent = "Resume";
    container.appendChild(pauseButton);
  }
  if (!stopButton && canCreate) {
    stopButton = document.createElement("button");
    stopButton.type = "button";
    stopButton.id = "stop-playback";
    stopButton.className = "secondary";
    stopButton.textContent = "Stop";
    container.appendChild(stopButton);
  }
  if (!pauseButton && !stopButton) return;
  state.pauseButton = pauseButton;
  state.stopButton = stopButton;
  if (pauseButton && typeof pauseButton.addEventListener === "function") pauseButton.addEventListener("click", () => {
    if (['playing', 'loading', 'buffering'].includes(state.playbackState)) pausePlayback();
    else resumePlayback();
  });
  if (stopButton && typeof stopButton.addEventListener === "function") stopButton.addEventListener("click", stopPlayback);
  state.playbackControlsReady = true;
  updatePlaybackControls();
}

function updatePlayingInfo() {
  const track = state.queue[state.queueIndex];
  if (!track) { updatePlaybackControls(); return; }
  $("#now-playing").textContent = `${track.title} — ${track.artist || "Unknown artist"}`;
  const link = $("#open-playing");
  const id=videoId(track);
  link.href=id?`https://music.youtube.com/watch?v=${id}`:String(track.provider_url||track.url||'');
  link.textContent=id?'Open song in YouTube Music ↗':'Open full track source ↗';
  link.hidden=!link.href;
  updateMediaSessionMetadata(track);
  updatePlaybackControls();
}

function ensureAudioPlayer() {
  if(state.audioElement) return {audio:state.audioElement,player:state.audioAdapter};
  const audio=$("#audio-player");
  if(!audio||typeof audio.play!=='function') return null;
  state.audioElement=audio;
  state.audioAdapter={
    getCurrentTime:()=>finiteNumber(audio.currentTime,0),
    getDuration:()=>finiteNumber(audio.duration,0),
    getPlaybackRate:()=>finiteNumber(audio.playbackRate,1),
    seekTo:(position)=>{audio.currentTime=Math.max(0,finiteNumber(position,0));},
    playVideo:()=>audio.play(),
    pauseVideo:()=>audio.pause(),
    stopVideo:()=>{audio.pause();try{audio.currentTime=0;}catch{}},
  };
  if(!state.audioHandlersReady) {
    state.audioHandlersReady=true;
    audio.addEventListener?.('playing',()=>{
      if(state.currentSource!=='audius') return;
      state.playbackState='playing';state.lastEndedIndex=null;state.restoredPosition=0;
      markTrackPlayed();setMediaSessionPlaybackState('playing');startPositionUpdates();persistQueue();
      setPlayerStatus('Playing full-track audio. Device media controls are available.');updatePlaybackControls();preloadNextAudioTrack();
    });
    audio.addEventListener?.('waiting',()=>{
      if(state.currentSource!=='audius') return;
      state.playbackState='buffering';setMediaSessionPlaybackState('playing');startPositionUpdates();
      setPlayerStatus('Buffering the full-track audio stream…');updatePlaybackControls();
    });
    audio.addEventListener?.('pause',()=>{
      if(state.currentSource!=='audius'||audio.ended||state.stopRequested) return;
      state.playbackState='paused';state.restoredPosition=currentPlayerTime();stopPositionUpdates();
      setMediaSessionPlaybackState('paused');persistQueue();setPlayerStatus('Paused. Press Resume or a device play control to continue.');updatePlaybackControls();
    });
    audio.addEventListener?.('ended',()=>{if(state.currentSource==='audius') handlePlaybackEnded();});
    audio.addEventListener?.('timeupdate',()=>{
      if(state.currentSource!=='audius') return;
      updateMediaSessionPosition();
      observePlaybackProgress();
      if(Date.now()-state.lastPositionPersistedAt>=5000) persistQueue();
    });
    audio.addEventListener?.('error',()=>{
      if(state.currentSource!=='audius'||state.stopRequested) return;
      state.playbackState='paused';stopPositionUpdates();setMediaSessionPlaybackState('none');
      setPlayerStatus('This full-track stream is unavailable. Choose Next or try another song.');updatePlaybackControls();
    });
  }
  return {audio,player:state.audioAdapter};
}

function preloadNextAudioTrack() {
  const next=state.queue[state.queueIndex+1];
  const source=audioStreamUrl(next);
  if(!source||typeof window.Audio!=='function') return;
  if(state.preloadedAudio?.src===source) return;
  try {
    state.preloadedAudio=new window.Audio(source);
    state.preloadedAudio.preload='auto';
    state.preloadedAudio.load?.();
  } catch { state.preloadedAudio=null; }
}

function prepareRestoredAudioPosition(audio,position) {
  const expectedSource=audioStreamUrl(state.queue[state.queueIndex]);
  const expectedIndex=state.queueIndex;
  const seek=()=>{
    if(state.currentSource!=='audius'||state.queueIndex!==expectedIndex||audio.src!==expectedSource||!(position>0)) return;
    try {audio.currentTime=Math.min(finiteNumber(audio.duration,position),position);} catch {}
  };
  if(Number(audio.readyState)>=1) seek();
  else audio.addEventListener?.('loadedmetadata',seek,{once:true});
}

function loadYouTubeAPI() {
  if (window.YT?.Player) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    const fail = () => { window.clearTimeout(timer); script.remove(); reject(new Error("YouTube player could not load. Check your connection or open the song in YouTube Music.")); };
    const timer = window.setTimeout(fail, 15000);
    window.onYouTubeIframeAPIReady = () => { window.clearTimeout(timer); resolve(); };
    script.src = "https://www.youtube.com/iframe_api";
    script.onerror = fail;
    document.head.appendChild(script);
  });
}

async function ensurePlayer() {
  if (state.playerReady) return state.playerReady;
  await loadYouTubeAPI();
  $("#player-frame").hidden = false;
  state.playerReady = new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("YouTube player did not become ready. Open this song in YouTube Music or try Play again.")), 15000);
    state.youtubePlayer = new window.YT.Player("youtube-player", {
      width: "100%", height: "360",
      playerVars: { controls: 1, playsinline: 1, origin: window.location.origin },
      events: {
        onReady: () => {
          window.clearTimeout(timer);
          try { state.youtubePlayer?.getIframe?.()?.setAttribute?.("allow", "autoplay; encrypted-media; picture-in-picture; fullscreen"); } catch { /* optional iframe surface */ }
          if(state.currentSource==='youtube') state.player=state.youtubePlayer;
          updatePlaybackControls();
          resolve(state.youtubePlayer);
        },
        onStateChange: event => {
          if(state.currentSource!=='youtube') return;
          const index = getPlayerPlaylistIndex();
          if (Number.isInteger(index) && index >= 0 && index < state.queue.length) {
            if(index!==state.queueIndex) beginTrackObservation(state.queue[index],0);
            state.queueIndex = index; updatePlayingInfo();
          }
          const statuses = { 0: "Song ended.", 1: "Playing", 2: "Paused", 3: "Buffering…", 5: "Ready. Press play in the YouTube player." };
          if (event.data === 0) return handlePlaybackEnded();
          if (event.data === 1) {
            if (state.stopRequested) {
              if (typeof state.youtubePlayer?.stopVideo === "function") state.youtubePlayer.stopVideo();
              return;
            }
            if (state.pauseRequested) {
              if (typeof state.youtubePlayer?.pauseVideo === "function") state.youtubePlayer.pauseVideo();
              state.playbackState = "paused";
              stopPositionUpdates();
              setMediaSessionPlaybackState("paused");
              persistQueue();
              setPlayerStatus("Paused. Press Resume to continue.");
              updatePlaybackControls();
              return;
            }
            state.playbackState = "playing";
            state.lastEndedIndex = null;
            state.restoredPosition = 0;
            markTrackPlayed();
            setMediaSessionPlaybackState("playing");
            startPositionUpdates();
            preloadNextAudioTrack();
            persistQueue();
          } else if (event.data === 2) {
            if (state.stopRequested) return;
            state.playbackState = "paused";
            state.restoredPosition = currentPlayerTime();
            stopPositionUpdates();
            setMediaSessionPlaybackState("paused");
            persistQueue();
          } else if (event.data === 3) {
            if (!state.stopRequested) {
              state.playbackState = "buffering";
              setMediaSessionPlaybackState("playing");
              startPositionUpdates();
            }
          } else if (event.data === 5 && !state.stopRequested) {
            if (state.playbackState === "loading") state.playbackState = "paused";
            setMediaSessionPlaybackState("paused");
          }
          setPlayerStatus(statuses[event.data] || "Press play in the YouTube player to listen.");
          updatePlaybackControls();
        },
        onAutoplayBlocked: () => {
          if(state.currentSource!=='youtube') return;
          state.playbackState = "paused";
          stopPositionUpdates();
          setMediaSessionPlaybackState("paused");
          setPlayerStatus("Your browser paused autoplay. Press play inside the YouTube player or use Resume.");
          updatePlaybackControls();
        },
        onError: event => {
          if(state.currentSource!=='youtube') return;
          const restricted = [100, 101, 150].includes(event.data);
          state.playbackState = "paused";
          stopPositionUpdates();
          setMediaSessionPlaybackState("none");
          setPlayerStatus(restricted
            ? "YouTube cannot play this song here. Choose Next or open it in YouTube Music."
            : `YouTube playback failed (${event.data}). Try Next or open the song in YouTube Music.`);
          updatePlaybackControls();
        },
      },
    });
  }).catch(error => {
    if (typeof state.youtubePlayer?.destroy === "function") state.youtubePlayer.destroy();
    state.youtubePlayer=null;
    if(state.currentSource==='youtube') state.player=null;
    state.playerReady = null; state.playerNeedsLoad = true;
    $("#player-frame").innerHTML = '<div id="youtube-player"></div>';
    throw error;
  });
  return state.playerReady;
}

function getPlayerPlaylistIndex() {
  try {
    if(state.queueMode!=='youtube') return state.queueIndex;
    const index = state.youtubePlayer && typeof state.youtubePlayer.getPlaylistIndex === "function" ? state.youtubePlayer.getPlaylistIndex() : state.queueIndex;
    return Number.isInteger(index) && index >= 0 ? index : state.queueIndex;
  } catch { return state.queueIndex; }
}

async function playQueueTrack(index,{startSeconds=0}={}) {
  if(index<0||index>=state.queue.length) return false;
  const track=state.queue[index];
  const audioUrl=audioStreamUrl(track);
  const id=videoId(track);
  const source=audioUrl?'audius':id?'youtube':null;
  if(!source) return false;
  state.queueIndex=index;
  state.currentSource=source;
  state.queueMode=state.queue.every(item=>videoId(item))?'youtube':state.queue.every(item=>audioStreamUrl(item))?'audio':'mixed';
  state.stopRequested=false;state.pauseRequested=false;state.lastEndedIndex=null;
  state.restoredPosition=Math.max(0,finiteNumber(startSeconds,0));
  beginTrackObservation(track,state.restoredPosition);
  state.playbackState='loading';updatePlayingInfo();persistQueue();
  const audioState=ensureAudioPlayer();
  if(source==='audius') {
    if(!audioState) {state.playbackState='paused';setPlayerStatus('This browser cannot play the full-track audio element. Choose a YouTube song instead.');updatePlaybackControls();return false;}
    if(state.youtubePlayer&&typeof state.youtubePlayer.pauseVideo==='function') {try{state.youtubePlayer.pauseVideo();}catch{}}
    const {audio,player}=audioState;
    state.player=player;$('#player-frame').hidden=true;audio.hidden=false;
    try {
      if(audio.src!==audioUrl) {audio.src=audioUrl;audio.preload='auto';audio.load?.();}
      prepareRestoredAudioPosition(audio,state.restoredPosition);
      const pending=audio.play();
      if(pending&&typeof pending.then==='function') await pending;
      if(state.currentSource!=='audius') return false;
      if(!audio.paused) {
        state.playbackState='playing';state.restoredPosition=0;markTrackPlayed();
        setMediaSessionPlaybackState('playing');startPositionUpdates();preloadNextAudioTrack();
        setPlayerStatus('Playing full-track audio. Device media controls are available.');
      } else state.playbackState='paused';
      persistQueue();updatePlaybackControls();return !audio.paused;
    } catch(error) {
      if(state.currentSource==='audius') {
        state.playbackState='paused';stopPositionUpdates();setMediaSessionPlaybackState('paused');
        setPlayerStatus(error?.name==='NotAllowedError'?'Press Resume to allow this full-track audio stream to play.':'This full-track stream could not start. Choose Next or try another song.');
        updatePlaybackControls();persistQueue();
      }
      return false;
    }
  }
  const audio=audioState?.audio;
  if(audio&&!audio.paused) {try{audio.pause();}catch{}}
  if(audio) audio.hidden=true;
  $('#player-frame').hidden=false;
  try {
    const player=await ensurePlayer();
    if(state.currentSource!=='youtube') return false;
    state.player=player;
    if(typeof player.loadVideoById==='function') player.loadVideoById({videoId:id,startSeconds:Math.max(0,finiteNumber(startSeconds,0))});
    else if(typeof player.loadPlaylist==='function') player.loadPlaylist([id],0);
    if(typeof player.playVideo==='function') player.playVideo();
    state.playerNeedsLoad=false;state.restoredPosition=0;state.playbackState='loading';
    setMediaSessionPlaybackState('playing');persistQueue();updatePlaybackControls();
    setPlayerStatus('Starting the YouTube song. YouTube-only playback follows YouTube restrictions.');
    return true;
  } catch(error) {
    if(state.currentSource==='youtube') {
      state.playbackState='paused';stopPositionUpdates();setMediaSessionPlaybackState('paused');
      setPlayerStatus(error.message||'YouTube could not play this song. Choose Next or open it in YouTube Music.');updatePlaybackControls();
    }
    return false;
  }
}

function markTrackPlayed() {
  const key = getTrackKey(state.queue[state.queueIndex]);
  if (key) state.playedTrackKeys.add(key);
}

function handlePlaybackEnded() {
  stopPositionUpdates();
  const endedIndex = state.queueMode==='youtube' ? getPlayerPlaylistIndex() : state.queueIndex;
  if (state.lastEndedIndex === endedIndex && ['loading', 'playing', 'ended', 'stopped'].includes(state.playbackState)) return;
  state.lastEndedIndex = endedIndex;
  if (Number.isInteger(endedIndex) && endedIndex >= 0 && endedIndex < state.queue.length) state.queueIndex = endedIndex;
  markTrackPlayed();
  if (state.stopRequested) {
    state.playbackState = "stopped";
    setMediaSessionPlaybackState("none");
    setPlayerStatus("Playback stopped. Press Resume to continue this queue.");
    persistQueue();
    updatePlayingInfo();
    return;
  }
  const endedTrack=state.queue[state.queueIndex];
  if(endedTrack&&state.completedFeedbackKey!==getTrackKey(endedTrack)) {
    state.completedFeedbackKey=getTrackKey(endedTrack);
    void submitFeedback(endedTrack,'completed',state.feedbackListenedSeconds);
  }
  const nextIndex = state.queueIndex + 1;
  if (nextIndex < state.queue.length && state.queueMode==='youtube' && state.youtubePlayer && typeof state.youtubePlayer.playVideoAt === "function") {
    state.queueIndex = nextIndex;
    beginTrackObservation(state.queue[nextIndex],0);
    state.playbackState = "loading";
    state.restoredPosition = 0;
    state.pauseRequested = false;
    updatePlayingInfo();
    setPlayerStatus("Playing next song…");
    try {
      state.youtubePlayer.playVideoAt(nextIndex);
      state.playbackState = "playing";
      setMediaSessionPlaybackState("playing");
      startPositionUpdates();
      persistQueue();
      updatePlaybackControls();
    } catch {
      state.playbackState = "paused";
      stopPositionUpdates();
      setMediaSessionPlaybackState("paused");
      setPlayerStatus("The next song could not start. Press Resume or choose Next.");
      updatePlaybackControls();
    }
    return;
  }
  if(nextIndex<state.queue.length) {
    setPlayerStatus('Playing next song…');
    void playQueueTrack(nextIndex);
    return;
  }
  state.playbackState = "ended";
  state.restoredPosition = 0;
  setMediaSessionPlaybackState("none");
  updatePlayingInfo();
  setPlayerStatus("End of queue. Refresh mix for more unseen songs; served songs will not be recycled.");
  persistQueue();
}

async function resumePlayback() {
  if (!state.queue.length) {
    setPlayerStatus("Choose Play on a song, or play the whole mix.");
    return false;
  }
  const hadPlayer = Boolean(state.player);
  const savedPosition = state.restoredPosition;
  state.stopRequested = false;
  state.pauseRequested = false;
  state.lastEndedIndex = null;
  state.playbackState = "loading";
  setPlayerStatus("Resuming playback…");
  updatePlaybackControls();
  if(state.queueMode!=='youtube') return playQueueTrack(state.queueIndex,{startSeconds:savedPosition});
  try {
    const player = await ensurePlayer();
    const needsPlaylist = !hadPlayer || state.queueRestored || state.playerNeedsLoad;
    if (needsPlaylist) {
      player.loadPlaylist(state.queue.map(videoId), state.queueIndex);
      state.queueRestored = false;
      state.playerNeedsLoad = false;
    } else if (typeof player.playVideo === "function") {
      player.playVideo();
    }
    if (savedPosition > 0 && typeof player.seekTo === "function") player.seekTo(savedPosition, true);
    state.restoredPosition = 0;
    persistQueue();
    setPlayerStatus("Starting song… If playback pauses, press play inside the player.");
    updatePlaybackControls();
    return true;
  } catch (error) {
    state.playbackState = "paused";
    setPlayerStatus(error.message);
    updatePlaybackControls();
    return false;
  }
}

function pausePlayback() {
  if (!state.queue.length) {
    setPlayerStatus("There is no active queue to pause.");
    return false;
  }
  state.pauseRequested = true;
  state.stopRequested = false;
  if (state.player && typeof state.player.pauseVideo === "function") {
    try { state.player.pauseVideo(); } catch { /* player may be between iframe states */ }
  }
  state.playbackState = "paused";
  state.restoredPosition = currentPlayerTime();
  stopPositionUpdates();
  setMediaSessionPlaybackState("paused");
  persistQueue();
  setPlayerStatus("Paused. Press Resume to continue.");
  updatePlaybackControls();
  return true;
}

function stopPlayback() {
  if (!state.queue.length) {
    setPlayerStatus("There is no active playback to stop.");
    return false;
  }
  state.stopRequested = true;
  state.pauseRequested = false;
  if (state.player) {
    try {
      if (typeof state.player.stopVideo === "function") state.player.stopVideo();
      else if (typeof state.player.pauseVideo === "function") state.player.pauseVideo();
    } catch { /* player may already be unloading */ }
  }
  state.playbackState = "stopped";
  state.restoredPosition = 0;
  state.playerNeedsLoad = true;
  stopPositionUpdates();
  setMediaSessionPlaybackState("none");
  persistQueue();
  setPlayerStatus("Playback stopped. Press Resume to continue this queue.");
  updatePlaybackControls();
  return true;
}

async function playTrack(index) {
  return playTrackFrom(state.recommendations, index);
}

async function playTrackFrom(items, index) {
  if (state.playerLoading) return;
  const selected = items[index]?.track || items[index];
  state.queue = items.map(item => item?.track || item).filter(isPlayableTrack);
  state.queueIndex = Math.max(0, state.queue.findIndex(track => getTrackKey(track) === getTrackKey(selected)));
  if (!state.queue.length) return toast("No playable songs are available in this mix.");
  state.queueMode=state.queue.every(videoId)?'youtube':state.queue.every(audioStreamUrl)?'audio':'mixed';
  state.currentSource=audioStreamUrl(state.queue[state.queueIndex])?'audius':'youtube';
  state.playedTrackKeys = new Set();
  state.restoredPosition = 0;
  state.queueRestored = false;
  state.playerNeedsLoad = false;
  state.stopRequested = false;
  state.pauseRequested = false;
  state.lastEndedIndex = null;
  state.playbackState = "loading";
  beginTrackObservation(state.queue[state.queueIndex]);
  updatePlayingInfo();
  persistQueue();
  state.playerLoading = true;
  setPlayerStatus(state.currentSource==='audius'?"Loading full-track audio…":"Loading YouTube player…");
  try {
    if(state.queueMode==='youtube') {
      const audio=ensureAudioPlayer()?.audio;if(audio){audio.pause?.();audio.hidden=true;}
      $('#player-frame').hidden=false;
      const player = await ensurePlayer();
      player.loadPlaylist(state.queue.map(videoId), state.queueIndex);
      state.playerNeedsLoad = false;
      if (typeof player.playVideo === "function") player.playVideo();
    } else await playQueueTrack(state.queueIndex);
    persistQueue();
    setPlayerStatus("Starting song… If playback pauses, press play inside the player.");
    updatePlaybackControls();
  } catch (error) {
    state.playbackState = "paused";
    setPlayerStatus(error.message);
    updatePlaybackControls();
  }
  finally { state.playerLoading = false; }
}

function playMix() {
  const items = state.mixRecommendations.length ? state.mixRecommendations : state.ranked;
  if (!items.length) return toast("No fresh songs are ready in this mix yet.");
  return playTrackFrom(items, 0);
}

async function changeTrack(delta) {
  if (state.playerLoading || !state.queue.length) return false;
  const index = state.queueIndex + delta;
  if (index < 0 || index >= state.queue.length) return false;
  recordIntentionalSkip();
  beginTrackObservation(state.queue[index],0);
  if(state.currentSource==='audius'&&state.audioElement&&!state.audioElement.paused) {try{state.audioElement.pause();}catch{}}
  if(state.currentSource==='youtube'&&state.youtubePlayer?.pauseVideo) {try{state.youtubePlayer.pauseVideo();}catch{}}
  state.queueIndex = index;
  state.restoredPosition = 0;
  state.lastEndedIndex = null;
  updatePlayingInfo();
  if(state.queueMode!=='youtube') return playQueueTrack(index);
  if (!state.player) return resumePlayback();
  state.stopRequested = false;
  state.pauseRequested = false;
  state.playbackState = "loading";
  try {
    if (typeof state.youtubePlayer?.playVideoAt !== "function") return resumePlayback();
    state.youtubePlayer.playVideoAt(index);
    state.playerNeedsLoad = false;
    state.playbackState = "playing";
    setMediaSessionPlaybackState("playing");
    startPositionUpdates();
    persistQueue();
    setPlayerStatus("Starting selected song…");
    updatePlaybackControls();
    return true;
  } catch {
    state.playbackState = "paused";
    setPlayerStatus("The selected song could not start. Press Resume to try again.");
    updatePlaybackControls();
    return false;
  }
}

function isDocumentHidden() {
  return document.visibilityState === "hidden" || document.hidden === true;
}

document.addEventListener("DOMContentLoaded", () => {
  ensurePlaybackControls();
  installMediaSessionHandlers();
  restoreQueue();
  $("#playlist-name").addEventListener("input", () => { state.playlistNameDirty = true; });
  $("#song-search").addEventListener("input", event => { state.query = event.target.value; renderCollection(); });
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    state.view = button.dataset.view;
    document.querySelectorAll('[data-view]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    renderCollection();
  }));
  $("#play-mix").addEventListener("click", playMix);
  $("#previous-track").addEventListener("click", () => changeTrack(-1));
  $("#next-track").addEventListener("click", () => changeTrack(1));
  $("#recommendations").addEventListener("click", async event => {
    const play = event.target.closest("[data-play]");
    if (play) return playTrack(Number(play.dataset.play));
    const feedback=event.target.closest("[data-feedback]");
    if(feedback) {
      feedback.disabled=true;
      const track={track_key:feedback.dataset.trackKey,provider:feedback.dataset.provider,title:feedback.dataset.title,artist:feedback.dataset.artist,duration_seconds:Number(feedback.dataset.duration)||null};
      const eventName=feedback.dataset.feedback;
      const saved=await submitFeedback(track,eventName,0);
      if(saved) {
        feedback.textContent=eventName==='like'?'👍 Liked':'👎 Not for me';
        feedback.setAttribute('aria-pressed','true');
        toast(eventName==='like'?'Preference saved. Future mixes will learn from it.':'Feedback saved. This recording will be excluded from future mixes.');
      } else {feedback.disabled=false;toast('Your feedback could not be saved. Try again when connected.');}
      return;
    }
    const favorite = event.target.closest("[data-favorite]");
    if (!favorite) return;
    if (!state.favoritesReady) return toast('Favorites could not load yet. Refresh the page to reconnect.');
    favorite.disabled = true;
    try {
      const result = await api("/api/favorites", { method: "POST", body: JSON.stringify({ track_key: favorite.dataset.favorite, liked: !state.favorites.has(favorite.dataset.favorite) }) });
      toast(result.mix_status === "completed" ? "Favorite saved. Mix updated." : "Favorite saved. Press Refresh mix when the current scan finishes.");
      await refresh();
    } catch (error) { toast(error.message); } finally { favorite.disabled = false; }
  });
  $("#scan-button").addEventListener("click", scan);
  $("#scheduler-button").addEventListener("click", toggleScheduler);
  $("#preview-button").addEventListener("click", previewPlaylist);
  $("#write-button").addEventListener("click", writePlaylist);
  refresh();
  // Bridge sync is event-driven in the extension; polling keeps the visible
  // dashboard current without requiring a user refresh or button click.
  window.setInterval(() => { if (!isDocumentHidden() && !state.refreshing) refresh({ silent: true }); }, 30000);
  document.addEventListener("visibilitychange", () => {
    // Backgrounding is not a playback command. Persist the queue and leave
    // the iframe/player alone; only the dashboard refresh waits for visibility.
    persistQueue();
    if (!isDocumentHidden()) {
      updatePlaybackControls();
      refresh({ silent: true });
    }
  });
  window.addEventListener?.("pagehide", persistQueue);
  window.addEventListener?.("beforeunload", persistQueue);
});
