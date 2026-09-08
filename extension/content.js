(function () {
  if (globalThis.__ytmusicPersonalMixBridgeLoaded) return;
  globalThis.__ytmusicPersonalMixBridgeLoaded = true;

  const DASHBOARD_ORIGINS = new Set([
    "https://personal-music-mix.michaelovsky55555.chatgpt.site",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
  ]);
  if (DASHBOARD_ORIGINS.has(location.origin)) {
    // The dashboard cannot reach the extension service worker directly. A
    // same-origin page message gives its Refresh button an immediate signal
    // while keeping the bridge's account-reading code on YouTube Music only.
    window.addEventListener("message", (event) => {
      if (event.source !== window || event.origin !== location.origin || event.data?.type !== "ytmusic-personal-mix-refresh") return;
      const requestId = typeof event.data.request_id === "string" ? event.data.request_id : "";
      const message = {
        type: "dashboard-refresh-request",
        request_id: requestId,
        requested_at: event.data.requested_at || new Date().toISOString(),
      };
      Promise.resolve(chrome.runtime.sendMessage(message))
        .then((result) => {
          window.postMessage({
            type: "ytmusic-personal-mix-refresh-result",
            request_id: requestId,
            ok: Boolean(result?.ok),
            tabs: Number(result?.tabs || 0),
            acknowledged: Number(result?.acknowledged || 0),
            error: result?.error || null,
          }, location.origin);
        })
        .catch((error) => {
          window.postMessage({
            type: "ytmusic-personal-mix-refresh-result",
            request_id: requestId,
            ok: false,
            tabs: 0,
            acknowledged: 0,
            error: String(error || "bridge_unavailable"),
          }, location.origin);
        });
    });
    return;
  }

  const LIMIT = 500;
  let timer = null;
  let lastFingerprint = "";
  let pendingFingerprint = "";
  let monitoring = false;
  let forceSync = false;
  let syncWaiters = [];
  let syncWaiterTimer = null;

  function settleSyncWaiters(result) {
    if (!syncWaiters.length) return;
    const waiters = syncWaiters;
    syncWaiters = [];
    if (syncWaiterTimer !== null) {
      window.clearTimeout(syncWaiterTimer);
      syncWaiterTimer = null;
    }
    for (const respond of waiters) {
      try { respond(result); } catch (_) {}
    }
  }

  function queueSyncWaiter(respond) {
    if (typeof respond !== "function") return;
    syncWaiters.push(respond);
    if (syncWaiterTimer !== null) return;
    syncWaiterTimer = window.setTimeout(() => {
      syncWaiterTimer = null;
      forceSync = false;
      settleSyncWaiters({ ok: false, error: "No rendered YouTube Music rows became available." });
    }, 10000);
  }

  function text(node, selectors) {
    for (const selector of selectors) {
      const found = node.querySelector(selector);
      if (found?.textContent?.trim()) return found.textContent.trim();
    }
    return "";
  }

  function attribute(node, selectors, names) {
    for (const selector of selectors) {
      const found = node.querySelector(selector);
      if (!found) continue;
      for (const name of names) {
        const value = found.getAttribute(name);
        if (value?.trim()) return value.trim();
      }
    }
    return "";
  }

  function onHistoryPage() {
    return location.hostname === "music.youtube.com" && (location.pathname.replace(/\/+$/, "") === "/history" || onFavoritesPage());
  }

  function onFavoritesPage() { return location.pathname.replace(/\/+$/, "") === '/playlist' && new URL(location.href).searchParams.get('list') === 'LM'; }

  function historyRows() {
    const root = document.querySelector('ytmusic-browse-response[active]') || document.querySelector('ytmusic-browse-response');
    if (!root) return [];
    return [...root.querySelectorAll('ytmusic-history-item-renderer, ytmusic-shelf-renderer ytmusic-responsive-list-item-renderer, ytmusic-playlist-shelf-renderer ytmusic-responsive-list-item-renderer, [data-history-item]')]
      .filter(node => !node.closest('ytmusic-player-queue, ytmusic-player-queue-item, ytmusic-carousel-shelf-renderer'));
  }

  function readLiked(node) {
    if (node.getAttribute('data-liked') === 'true') return true;
    if (node.getAttribute('data-liked') === 'false') return false;
    const controls = [...node.querySelectorAll('[aria-label], [title]')].filter(control => {
      const label = control.getAttribute('aria-label') || control.getAttribute('title') || '';
      return !/dislike|thumbs down/i.test(label) && /^(like|unlike|remove like|thumbs up)(\b|$)/i.test(label);
    });
    if (controls.length !== 1) return null;
    const selected = controls[0].getAttribute('aria-pressed') ?? controls[0].getAttribute('aria-checked');
    if (selected === 'true') return true;
    if (selected === 'false') return false;
    return /^(unlike|remove like)\b/i.test(controls[0].getAttribute('aria-label') || controls[0].getAttribute('title') || '') ? true : null;
  }

  function startMonitoring() {
    if (monitoring) return;
    monitoring = true;
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
    window.addEventListener("scroll", schedule, { passive: true });
    schedule();
  }

  function readPlayedAt(node) {
    return attribute(node, ["time[datetime]", "[datetime]", "[data-played-at]", "[data-timestamp]"], ["datetime", "data-played-at", "data-timestamp"])
      || text(node, ["time", ".time", ".timestamp", ".played-at"]);
  }

  function readHistoryId(node) {
    return node.getAttribute('data-history-id') || node.getAttribute('data-event-id') || attribute(node, ["[data-history-id]", "[data-event-id]"], ["data-history-id", "data-event-id"]);
  }

  function readArtist(node) {
    const firstColumn = node.querySelector(".secondary-flex-columns .flex-column");
    const linkedArtists = [...(firstColumn?.querySelectorAll("a[href*='channel/']") || [])]
      .map((link) => link.textContent?.trim())
      .filter(Boolean);
    if (linkedArtists.length) return [...new Set(linkedArtists)].join(", ");
    return text(node, [".subtitle", ".byline", ".secondary", ".secondary-flex-columns", "[class*='artist' i]"])
      .split(/\n|•|·/).map((value) => value.trim()).filter(Boolean)[0] || "";
  }

  function readAlbum(node) {
    return node.querySelector('a[href*="/browse/"]')?.textContent?.trim() || "";
  }

  function collect() {
    if (!onHistoryPage()) {
      settleSyncWaiters({ ok: false, error: "The YouTube Music history page is no longer open." });
      return;
    }
    const nodes = [...new Set(historyRows())];
    const items = nodes.map((node, position) => {
      const titleNode = node.querySelector("[data-title], .title, .ytmusic-item-title, yt-formatted-string.title, [class*='title' i], [title]");
      const title = titleNode?.textContent?.trim() || titleNode?.getAttribute("title") || "";
      const url = node.querySelector('a[href*="watch"], a[href*="song/"], a[href*="podcast/"], a[href*="youtu.be/"]')?.href || (node.getAttribute("data-video-id") ? `https://music.youtube.com/watch?v=${encodeURIComponent(node.getAttribute("data-video-id"))}` : "");
      const likeState = onFavoritesPage() ? true : readLiked(node);
      return { title, artist: readArtist(node), album: readAlbum(node), url, liked:likeState === true, like_state_known:likeState !== null, position, played_at: readPlayedAt(node), history_id: readHistoryId(node) };
    }).filter((item) => item.title);
    const fingerprint = JSON.stringify(items.map((item) => [item.title, item.artist, item.url, item.liked, item.like_state_known, item.played_at, item.history_id, item.position]));
    if (!items.length) {
      window.setTimeout(schedule, 2500);
      return;
    }
    if (pendingFingerprint || (!forceSync && fingerprint === lastFingerprint)) return;
    forceSync = false;
    pendingFingerprint = fingerprint;
    const payload = { page: location.href, kind:onFavoritesPage() ? 'favorites' : 'history', complete:false, captured_at: new Date().toISOString(), items: items.slice(0, LIMIT) };
    chrome.runtime.sendMessage(
      { type: "ytmusic-history", payload },
      (response) => {
        const failed = chrome.runtime.lastError || !response?.ok;
        if (failed) {
          // Only an acknowledged local-app response can advance the snapshot.
          // An opaque no-CORS response cannot distinguish acceptance from 401/500.
          pendingFingerprint = "";
          forceSync = true;
          settleSyncWaiters({ ok: false, error: response?.error || chrome.runtime.lastError?.message || "Local app rejected the history sync." });
          window.setTimeout(schedule, 5000);
          return;
        }
        lastFingerprint = fingerprint;
        pendingFingerprint = "";
        settleSyncWaiters({ ok: true, items: items.length });
      },
    );
  }

  function sendHeartbeat(visibleItems = 0) {
    if (!onHistoryPage()) return;
    const bodyText = document.body?.innerText || "";
    const signInPrompt = Boolean(document.querySelector("a[href*='ServiceLogin'], [aria-label*='sign in' i], [title*='sign in' i]"))
      || /sign in to view your history/i.test(bodyText);
    const visibleRows = historyRows().length;
    chrome.runtime.sendMessage({
      type: "ytmusic-bridge-heartbeat",
      payload: {
        page: location.href,
        extension_version: chrome.runtime.getManifest().version,
        sent_at: new Date().toISOString(),
        authenticated: visibleRows > 0 ? true : (signInPrompt ? false : null),
        visible_items: visibleItems || visibleRows,
      },
    }).catch(() => {});
  }

  function schedule() { window.clearTimeout(timer); timer = window.setTimeout(collect, 900); }
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "sync-now") return false;
    queueSyncWaiter(sendResponse);
    forceSync = true;
    schedule();
    return true;
  });
  window.addEventListener("popstate", () => { if (onHistoryPage()) startMonitoring(); });
  window.addEventListener("yt-navigate-finish", () => { if (onHistoryPage()) startMonitoring(); });
  window.setInterval(() => {
    if (onHistoryPage()) {
      startMonitoring();
      schedule();
      sendHeartbeat();
    }
  }, 2000);
  if (onHistoryPage()) {
    startMonitoring();
    sendHeartbeat();
  }
})();
