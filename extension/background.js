// Central place for the backend base URL so it's easy to change.
const LISTER_API_BASE = "http://localhost:8010";

chrome.runtime.onInstalled.addListener(() => {
  console.log("Lister draft assistant installed. Backend expected at", LISTER_API_BASE);
});

// Relay so content scripts (which can't always hit localhost due to CORS/site CSP)
// can ask the background service worker to fetch on their behalf.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "LISTER_MARK_POSTED") {
    fetch(`${LISTER_API_BASE}/listings/${message.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "posted_as_draft" }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // keep the message channel open for the async response
  }
  if (message?.type === "LISTER_FETCH_PHOTO") {
    // Fetched here (not in the content script) because a page's Content-Security-Policy
    // can block outbound fetches from the content script's isolated world; the
    // background service worker's fetches are governed by host_permissions instead.
    fetch(message.url)
      .then(async (r) => {
        if (!r.ok) throw new Error(`backend returned ${r.status}`);
        const buf = await r.arrayBuffer();
        const mime = r.headers.get("content-type") || "image/jpeg";
        let binary = "";
        const bytes = new Uint8Array(buf);
        for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
        sendResponse({ ok: true, base64: btoa(binary), mime });
      })
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // keep the message channel open for the async response
  }
  return false;
});
