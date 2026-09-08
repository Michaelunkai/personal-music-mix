const DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/browser/sync";
const HEARTBEAT_ENDPOINT = "http://127.0.0.1:8000/api/browser/heartbeat";
const HISTORY_URL_PATTERN = "https://music.youtube.com/history*";
const DASHBOARD_ORIGINS = new Set([
  "https://personal-music-mix.michaelovsky55555.chatgpt.site",
  "http://127.0.0.1:8000",
  "http://localhost:8000",
]);

function isHistoryUrl(url) {
  try {
    const parsed = new URL(url || "");
    return parsed.protocol === "https:" && parsed.hostname === "music.youtube.com" && (parsed.pathname.replace(/\/+$/, "") === "/history" || (parsed.pathname === '/playlist' && parsed.searchParams.get('list') === 'LM'));
  } catch (_) {
    return false;
  }
}

async function requestHistorySync(tabId) {
  if (!tabId) return { ok: false, error: "History tab has no id" };
  const send = async () => {
    const response = await chrome.tabs.sendMessage(tabId, { type: "sync-now" });
    // Older unpacked bridge versions did not acknowledge sync-now. Treat a
    // delivered message as queued so a dashboard refresh remains compatible
    // while the current bridge waits for local-app ingestion.
    return response === undefined ? { ok: true, queued: true } : response;
  };
  try {
    return await send();
  } catch (_) {
    // Restored tabs can predate the unpacked bridge content script. Inject it
    // only into the exact history page, then retry without user interaction.
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      return await send();
    } catch (_) {
      // The next alarm/tab update retries.
      return { ok: false, error: "History tab bridge is unavailable" };
    }
  }
}

async function requestHistoryTabsSync() {
  const tabs = await chrome.tabs.query({ url: [HISTORY_URL_PATTERN, 'https://music.youtube.com/playlist*'] });
  const targets = tabs.filter((tab) => isHistoryUrl(tab.url));
  const results = await Promise.all(targets.map((tab) => requestHistorySync(tab.id)));
  return {
    count: targets.length,
    acknowledged: results.filter((result) => result?.ok).length,
  };
}

function isDashboardUrl(url) {
  try { return DASHBOARD_ORIGINS.has(new URL(url || "").origin); } catch (_) { return false; }
}

async function sendHeartbeat(payload) {
  const config = await chrome.storage.local.get({ token: "" });
  const headers = { "Content-Type": "application/json" };
  if (config.token) headers["X-YouTube-Music-Bridge-Token"] = config.token;
  const response = await fetch(HEARTBEAT_ENDPOINT, { method: "POST", headers, body: JSON.stringify(payload), signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(`Local app returned ${response.status}`);
  return response.json();
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("ytmusic-history-sync", { periodInMinutes: 1 });
  requestHistoryTabsSync().catch(() => {});
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("ytmusic-history-sync", { periodInMinutes: 1 });
  requestHistoryTabsSync().catch(() => {});
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "ytmusic-history-sync") requestHistoryTabsSync().catch(() => {});
});
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if ((changeInfo.status === "complete" || changeInfo.url) && isHistoryUrl(tab.url || changeInfo.url)) {
    requestHistorySync(tabId);
  }
});

async function sendToLocalApp(payload) {
  const config = await chrome.storage.local.get({ endpoint: DEFAULT_ENDPOINT, token: "" });
  const headers = { "Content-Type": "application/json" };
  if (config.token) headers["X-YouTube-Music-Bridge-Token"] = config.token;
  const response = await fetch(config.endpoint || DEFAULT_ENDPOINT, { method: "POST", headers, body: JSON.stringify(payload), signal: AbortSignal.timeout(15000) });
  const text = await response.text();
  let result = {};
  try { result = text ? JSON.parse(text) : {}; } catch { result = { message: text }; }
  if (!response.ok) throw new Error(result.detail || result.message || `Local app returned ${response.status}`);
  if (!['completed','partial'].includes(result.status)) throw new Error(result.message || "Local app rejected the history sync");
  return result;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "ytmusic-bridge-heartbeat") {
    sendHeartbeat(message.payload || {})
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "dashboard-refresh-request") {
    const senderUrl = _sender?.url || _sender?.tab?.url || "";
    if (!isDashboardUrl(senderUrl)) {
      sendResponse({ ok: false, error: "Dashboard origin is not allowed" });
      return false;
    }
    requestHistoryTabsSync()
      .then((result) => sendResponse({ ok: true, tabs: result.count, acknowledged: result.acknowledged, request_id: message.request_id || "" }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type !== "ytmusic-history") return false;
  sendToLocalApp(message.payload)
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
});

chrome.action.onClicked.addListener(async (tab) => {
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: "sync-now" }).catch(() => {});
});
