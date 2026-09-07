(function () {
  if (globalThis.__ytmusicPersonalMixBridgeLoaded) return;
  globalThis.__ytmusicPersonalMixBridgeLoaded = true;
  const LIMIT = 500;
  let timer = null;
  let lastFingerprint = "";
  let pendingFingerprint = "";
  let monitoring = false;

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
    return location.hostname === "music.youtube.com" && location.pathname.replace(/\/+$/, "") === "/history";
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
    return attribute(node, ["[data-history-id]", "[data-event-id]", "[data-video-id]", "[data-id]"], ["data-history-id", "data-event-id", "data-video-id", "data-id"]);
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
    const selectors = [
      "ytmusic-history-item-renderer",
      "ytmusic-responsive-list-item-renderer",
      "ytmusic-two-row-item-renderer",
      "ytmusic-shelf-renderer ytmusic-responsive-list-item-renderer",
      "ytmusic-player-queue-item",
      ".ytmusic-history-item-renderer",
    ];
    let nodes = [...new Set(selectors.flatMap((selector) => [...document.querySelectorAll(selector)]))];
    if (!nodes.length) {
      nodes = [...new Set(document.querySelectorAll("[data-history-item], [data-video-id]"))];
    }
    const items = nodes.map((node, position) => {
      const titleNode = node.querySelector("[data-title], .title, .ytmusic-item-title, yt-formatted-string.title, [class*='title' i], [title]");
      const title = titleNode?.textContent?.trim() || titleNode?.getAttribute("title") || "";
      const url = node.querySelector('a[href*="watch"], a[href*="song/"], a[href*="podcast/"], a[href*="youtu.be/"]')?.href || (node.getAttribute("data-video-id") ? `https://music.youtube.com/watch?v=${encodeURIComponent(node.getAttribute("data-video-id"))}` : "");
      const likeNode = node.querySelector("[aria-label*='like' i], [title*='like' i], [data-liked], [aria-pressed]");
      const liked = /true|liked|thumbs up|unlike/i.test(likeNode?.getAttribute("aria-pressed") || likeNode?.getAttribute("data-liked") || likeNode?.getAttribute("aria-label") || likeNode?.getAttribute("title") || node.getAttribute("aria-label") || "") || /liked/i.test(node.textContent || "");
      return { title, artist: readArtist(node), album: readAlbum(node), url, liked, position, played_at: readPlayedAt(node), history_id: readHistoryId(node) };
    }).filter((item) => item.title);
    const fingerprint = JSON.stringify(items.map((item) => [item.title, item.artist, item.url, item.liked, item.played_at, item.history_id, item.position]));
    if (!items.length) {
      window.setTimeout(schedule, 2500);
      return;
    }
    if (fingerprint === lastFingerprint || fingerprint === pendingFingerprint) return;
    pendingFingerprint = fingerprint;
    const payload = { page: location.href, captured_at: new Date().toISOString(), items: items.slice(0, LIMIT) };
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
    const visibleRows = document.querySelectorAll("ytmusic-history-item-renderer, ytmusic-responsive-list-item-renderer, ytmusic-two-row-item-renderer").length;
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
  chrome.runtime.onMessage.addListener((message) => { if (message?.type === "sync-now") schedule(); });
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
