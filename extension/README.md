# YouTube Music browser bridge

This optional Manifest V3 extension is the most direct way to scan the account already signed in to Chrome. Load `extension/` as an unpacked extension in the approved Chrome Profile 2 / Person 1 and keep the local app running on port 8000. On extension startup, its one-minute recovery alarm, or a dashboard Refresh, the service worker keeps one inactive `https://music.youtube.com/history` tab available whenever no exact history or YouTube Music liked collection tab is already open. It does not take focus, replace existing tabs, or read cookies, passwords, or browser storage. The content script reads only rendered history rows and sends them to the local API.

You may still open `https://music.youtube.com/history` yourself; the bridge reuses that tab instead of creating another one. If the background tab is closed, the next alarm or Refresh recreates it so the signed-in session can continue sending listening updates.

If `YTMUSIC_BROWSER_BRIDGE_TOKEN` is configured, set the same value in the extension's service-worker storage from DevTools before syncing. With no token configured the bridge remains local-only and accepts requests from localhost.

Each accepted sync updates the local recommendation model and automatically saves a dry-run playlist preview. A dashboard Refresh waits for this local acknowledgement before its hosted rebuild, so an online bridge cannot race the previous snapshot. Provider-side playlist creation remains a separate, explicitly confirmed action in the dashboard.
