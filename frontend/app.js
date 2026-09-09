const $ = (selector) => document.querySelector(selector);
const state = { latestPlan: null, schedulerRunning: false, recommendations: [], mixRecommendations: [], ranked: [], library: [], view: 'mix', query: '', favorites: new Set(), favoritesReady:false, queue: [], queueIndex: 0, player: null, playerReady: null, playerLoading: false, refreshing: false };

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 3800);
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
  $("#play-mix").disabled = !mixItems.some(item => videoId(item.track || {}));
  const root = $("#recommendations");
  $("#recommendation-count").textContent = `${items.length} songs`;
  if (!items.length) { root.className = "recommendations empty"; root.textContent = state.query ? "No songs match this search. Try another title or artist." : state.view === 'favorites' ? "Your favorites will appear here. Save a song with the heart button to get started." : "No new songs are ready yet. Press Refresh mix to request another fresh batch."; return; }
  root.className = "recommendations";
  const rendered = items.map((item, index) => {
    const track = item.track || {};
    const reasons = (item.reasons || []).slice(0, 3).join(" • ");
    const favorite = state.favorites.has(track.track_key);
    return `<article class="recommendation"><div class="rank">${String(index + 1).padStart(2, "0")}</div><div class="track-detail"><div class="track-title">${escapeHtml(track.title || "Untitled")}</div><div class="track-meta">${escapeHtml(track.artist || "Unknown artist")}${track.album ? ` · ${escapeHtml(track.album)}` : ""}</div><div class="track-reasons">${escapeHtml(reasons)}</div><div class="track-actions"><button class="secondary" data-play="${index}" ${videoId(track) ? "" : "disabled"} aria-label="Play ${escapeHtml(track.title)}">▶ Play</button><button class="secondary" data-favorite="${escapeHtml(track.track_key)}" ${state.favoritesReady ? "" : "disabled"} aria-label="${favorite ? 'Remove favorite' : 'Favorite'} ${escapeHtml(track.title)}" aria-pressed="${favorite}">${favorite ? "♥ Favorited" : "♡ Favorite"}</button></div></div><div class="score">${item.score == null ? '—' : Math.round(item.score * 100)}<span class="confidence">${item.score == null ? 'in your library' : 'match / 100'}</span></div></article>`;
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

async function refresh({ silent = false } = {}) {
  // A save during an older poll must await a fresh response after that poll.
  state.refreshRequested = true;
  state.refreshNotify = state.refreshNotify || !silent;
  if (state.refreshing) return state.refreshing;
  state.refreshing = (async () => {
    do {
      state.refreshRequested = false;
      const results = await Promise.allSettled(['health','overview','recommendations','runs','status','playlists/latest','connection','favorites','library'].map(path => api(`/api/${path}`)));
      const [health, overview, recs, runs, status, latestPlaylist, connection, favorites, library] = results.map(result => result.status === 'fulfilled' ? result.value : null);
      state.favoritesReady = Boolean(favorites);
      if (favorites) state.favorites = new Set(favorites.track_keys || []);
      if (recs) {
        state.ranked = recs.items || [];
        // The fresh mix is independent of the current library/favorites view.
        // Keep Play mix pointed at the latest server batch even when refresh
        // runs while the user is browsing another collection.
        state.mixRecommendations = state.ranked.slice();
      }
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

function requestBridgeRefresh({ timeoutMs = 4000 } = {}) {
  // The installed bridge can hear this same-origin message on the dashboard
  // and immediately ask the YouTube Music history/liked-music tabs to resend
  // their rendered rows. Wait for its small acknowledgement so a hosted scan
  // does not rebuild from the previous snapshot when the bridge is online.
  // The bounded timeout keeps cached mode responsive when no bridge is loaded.
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      window.removeEventListener?.("message", onMessage);
      resolve(result || { ok: false, timed_out: true });
    };
    const onMessage = (event) => {
      if (event.source !== window || event.origin !== window.location.origin || event.data?.type !== "ytmusic-personal-mix-refresh-result" || event.data?.request_id !== requestId) return;
      finish(event.data);
    };
    const timer = window.setTimeout(() => finish({ ok: false, timed_out: true }), timeoutMs);
    window.addEventListener?.("message", onMessage);
    window.postMessage({ type: "ytmusic-personal-mix-refresh", request_id: requestId, requested_at: new Date().toISOString() }, window.location.origin);
  });
}

async function waitForHostedRefresh({ timeoutMs = 15000, intervalMs = 750 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    try { latest = await api("/api/connection"); } catch { return latest; }
    // The local app has no hosted discovery queue. A hosted response only
    // needs waiting when its companion is online and still acknowledging the
    // request made by this refresh.
    if (!latest?.hosted || latest?.discovery?.pending !== true || latest?.companion?.online !== true) return latest;
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await new Promise(resolve => window.setTimeout(resolve, Math.min(intervalMs, remaining)));
  }
  return latest;
}

function renderCollection() {
  let items = state.view === 'mix' ? state.ranked : state.library;
  if (state.view === 'mix') state.mixRecommendations = items.slice();
  if (state.view === 'favorites') items = items.filter(item => state.favorites.has(item.track.track_key) || item.track.liked_count > 0 || item.track.like_events > 0);
  const query = state.query.trim().toLocaleLowerCase();
  if (query) items = items.filter(item => `${item.track.title} ${item.track.artist} ${item.track.album || ''}`.toLocaleLowerCase().includes(query));
  renderRecommendations(items);
}

async function scan() {
  const button = $("#scan-button"); button.disabled = true; button.textContent = "Refreshing…";
  try {
    const bridge = await requestBridgeRefresh();
    const result = await api("/api/scan", { method: "POST", body: JSON.stringify({ include_related: true }) });
    if (["failed", "blocked"].includes(result.status)) throw new Error(result.message || result.run?.message || "Mix could not be refreshed");
    if (Array.isArray(result.recommendations)) {
      // Consume the authoritative scan response immediately.  The hosted
      // worker returns the existing complete mix when a provider response is
      // partial, so the page never drops below the fifty-song target while
      // discovery is still expanding.
      state.ranked = result.recommendations.slice();
      state.mixRecommendations = state.ranked.slice();
      renderCollection();
    }
    // A live bridge acknowledgement or a public discovery request means the
    // local publisher has work to deliver. Give that bounded request window
    // time to acknowledge a complete replacement, while a preserved full mix
    // is already safe to keep visible during provider expansion.
    const preservedFullMix = result.preserved_previous_mix === true;
    const hasFreshBatch = Array.isArray(result.recommendations) && result.recommendations.length >= 50 && !preservedFullMix;
    const hosted = hasFreshBatch || preservedFullMix ? null : await waitForHostedRefresh({ timeoutMs: 60000 });
    const hostedPending = Boolean(hosted?.hosted && hosted?.discovery?.pending);
    const providerExhausted = hosted?.discovery?.state === "exhausted";
    const bridgeOffline = !bridge?.ok;
    if (preservedFullMix) toast("Your complete 50-song mix remains ready while more new songs arrive.");
    else if (providerExhausted) toast("No new unseen songs are available in the current provider pool yet. Refresh again later for another discovery batch.");
    else if (bridgeOffline && hostedPending) toast("Refresh requested from saved history; the browser bridge and hosted discovery are still reconnecting.");
    else if (bridgeOffline) toast(result.recommendations?.length ? "Fresh mix ready from saved history. Connect YouTube Music for live account updates." : "Refresh requested from saved history. New songs will appear when provider discovery arrives.");
    else if (hostedPending) toast("Live history received. Fresh discovery is still arriving; your mix will update automatically.");
    else toast(result.recommendations?.length ? "Fresh mix ready with new songs." : "Fresh request sent. New songs will appear when the provider discovery batch arrives.");
    await refresh();
  }
  catch (error) { toast(error.message); } finally { button.disabled = false; button.textContent = "Refresh mix"; }
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

function updatePlayingInfo() {
  const track = state.queue[state.queueIndex];
  if (!track) return;
  $("#now-playing").textContent = `${track.title} — ${track.artist || "Unknown artist"}`;
  const link = $("#open-playing");
  link.href = `https://music.youtube.com/watch?v=${videoId(track)}`;
  link.hidden = false;
  $("#previous-track").disabled = state.queueIndex <= 0;
  $("#next-track").disabled = state.queueIndex >= state.queue.length - 1;
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
    state.player = new window.YT.Player("youtube-player", {
      width: "100%", height: "360",
      playerVars: { controls: 1, playsinline: 1, origin: window.location.origin },
      events: {
        onReady: () => { window.clearTimeout(timer); resolve(state.player); },
        onStateChange: event => {
          const index = state.player.getPlaylistIndex();
          if (Number.isInteger(index) && index >= 0 && index < state.queue.length) { state.queueIndex = index; updatePlayingInfo(); }
          const statuses = { 0: "Song ended.", 1: "Playing", 2: "Paused", 3: "Buffering…", 5: "Ready. Press play in the YouTube player." };
          $("#player-status").textContent = statuses[event.data] || "Press play in the YouTube player to listen.";
        },
        onAutoplayBlocked: () => { $("#player-status").textContent = "Your browser paused autoplay. Press play inside the YouTube player."; },
        onError: event => {
          const restricted = [100, 101, 150].includes(event.data);
          $("#player-status").textContent = restricted
            ? "YouTube cannot play this song here. Choose Next or open it in YouTube Music."
            : `YouTube playback failed (${event.data}). Try Next or open the song in YouTube Music.`;
        },
      },
    });
  }).catch(error => {
    state.player?.destroy(); state.player = null; state.playerReady = null;
    $("#player-frame").innerHTML = '<div id="youtube-player"></div>';
    throw error;
  });
  return state.playerReady;
}

async function playTrack(index) {
  return playTrackFrom(state.recommendations, index);
}

async function playTrackFrom(items, index) {
  if (state.playerLoading) return;
  const selected = items[index]?.track;
  state.queue = items.map(item => item.track).filter(track => videoId(track));
  state.queueIndex = Math.max(0, state.queue.findIndex(track => track.track_key === selected?.track_key));
  if (!state.queue.length) return toast("No playable YouTube song IDs are available in this mix.");
  updatePlayingInfo();
  state.playerLoading = true;
  $("#player-status").textContent = "Loading YouTube player…";
  try {
    const player = await ensurePlayer();
    player.loadPlaylist(state.queue.map(videoId), state.queueIndex);
    $("#player-status").textContent = "Starting song… If playback pauses, press play inside the player.";
  } catch (error) { $("#player-status").textContent = error.message; }
  finally { state.playerLoading = false; }
}

function playMix() {
  const items = state.mixRecommendations.length ? state.mixRecommendations : state.ranked;
  if (!items.length) return toast("No fresh songs are ready in this mix yet.");
  return playTrackFrom(items, 0);
}

function changeTrack(delta) {
  if (!state.player || state.playerLoading) return;
  const index = state.queueIndex + delta;
  if (index < 0 || index >= state.queue.length) return;
  state.queueIndex = index;
  state.player.playVideoAt(index);
  updatePlayingInfo();
}

document.addEventListener("DOMContentLoaded", () => {
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
  window.setInterval(() => { if (!document.hidden && !state.refreshing) refresh({ silent: true }); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh({ silent: true }); });
});
