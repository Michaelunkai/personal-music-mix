const DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/browser/sync";
const HEARTBEAT_ENDPOINT = "http://127.0.0.1:8000/api/browser/heartbeat";
const HISTORY_URL_PATTERN = "https://music.youtube.com/history*";

function isHistoryUrl(url) {
  try {
    const parsed = new URL(url || "");
    return parsed.protocol === "https:" && parsed.hostname === "music.youtube.com" && (parsed.pathname.replace(/\/+$/, "") === "/history" || (parsed.pathname === '/playlist' && parsed.searchParams.get('list') === 'LM'));
  } catch (_) {
    return false;
  }
}

async function requestHistorySync(tabId) {
  if (!tabId) return;
  try {
    await chrome.tabs.sendMessage(tabId, { type: "sync-now" });
  } catch (_) {
    // Restored tabs can predate the unpacked bridge content script. Inject it
    // only into the exact history page, then retry without user interaction.
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      await chrome.tabs.sendMessage(tabId, { type: "sync-now" });
    } catch (_) {
      // The next alarm/tab update retries.
    }
  }
}

async function requestHistoryTabsSync() {
  const tabs = await chrome.tabs.query({ url: [HISTORY_URL_PATTERN, 'https://music.youtube.com/playlist*'] });
  await Promise.all(tabs.filter((tab) => isHistoryUrl(tab.url)).map((tab) => requestHistorySync(tab.id)));
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
  if (message?.type !== "ytmusic-history") return false;
  sendToLocalApp(message.payload)
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
});

chrome.action.onClicked.addListener(async (tab) => {
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: "sync-now" }).catch(() => {});
});
