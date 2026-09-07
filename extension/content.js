(function () {
  if (globalThis.__ytmusicPersonalMixBridgeLoaded) return;
  globalThis.__ytmusicPersonalMixBridgeLoaded = true;
  const LIMIT = 500;
  let timer = null;
  let lastFingerprint = "";
  let pendingFingerprint = "";
  let monitoring = false;
  let forceSync = false;

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
    if (!onHistoryPage()) return;
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
          window.setTimeout(schedule, 5000);
          return;
        }
        lastFingerprint = fingerprint;
        pendingFingerprint = "";
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
  chrome.runtime.onMessage.addListener((message) => { if (message?.type === "sync-now") { forceSync = true; schedule(); } });
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
