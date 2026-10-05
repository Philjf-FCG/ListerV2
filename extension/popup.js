const LISTER_API_BASE = "http://localhost:8010";
const listEl = document.getElementById("list");

const SITE_URLS = {
  vinted: "https://www.vinted.co.uk/items/new",
  // eBay has no single static "new listing" form URL like Vinted - selling starts
  // in Seller Hub, where you search/match a category before reaching the actual
  // title/description/price fields. The content script waits for those fields to
  // appear on whichever page you land on, however many clicks that takes.
  ebay: "https://www.ebay.co.uk/sh/lst/active",
};

async function loadReadyListings() {
  try {
    const res = await fetch(`${LISTER_API_BASE}/listings?status=reviewed_ready`);
    const listings = await res.json();
    render(listings);
  } catch (err) {
    listEl.innerHTML = `<p class="empty">Can't reach Lister backend at ${LISTER_API_BASE}. Is it running?</p>`;
  }
}

function render(listings) {
  if (!listings.length) {
    listEl.innerHTML = '<p class="empty">No listings marked "reviewed_ready" yet. Approve some in the Lister web UI first.</p>';
    return;
  }
  listEl.innerHTML = "";
  for (const listing of listings) {
    const div = document.createElement("div");
    div.className = "listing";
    div.innerHTML = `
      <div class="platform">${listing.platform}</div>
      <div class="title">${listing.title ?? "(untitled)"}</div>
      <button data-id="${listing.id}" data-platform="${listing.platform}">Open ${listing.platform} & fill draft</button>
    `;
    div.querySelector("button").addEventListener("click", () => openAndFill(listing));
    listEl.appendChild(div);
  }
}

async function openAndFill(listing) {
  // Stash the listing payload for the content script to pick up once the tab loads.
  // Includes a timestamp so the content script can give up after a while if the
  // user navigates away entirely instead of retrying forever.
  try {
    await chrome.storage.local.set({
      [`lister_pending_${listing.platform}`]: { ...listing, stashedAt: Date.now() },
    });
  } catch (e) {
    console.error("Storage set failed", e);
  }

  const url = SITE_URLS[listing.platform];
  if (url) {
    chrome.tabs.create({ url });
  }
}

async function sendVintedMessage(tabId, message) {
  try {
    console.log(`[Lister] Sending message to tab ${tabId}:`, message);
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (err) {
    console.warn(`[Lister] Failed to send message to tab ${tabId}:`, err);
    // If content script wasn't injected yet (e.g. tab opened before extension reload), inject it dynamically
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["content_scripts/vinted.js"],
      });
      console.log(`[Lister] Content script injected into tab ${tabId}`);
      return await chrome.tabs.sendMessage(tabId, message);
    } catch (injectErr) {
      console.error("[Lister] Failed to inject content script:", injectErr);
      throw injectErr;
    }
  }
}

async function syncVintedListings() {
  const statusEl = document.getElementById("sync-status");
  statusEl.textContent = "Scanning current tab...";
  try {
    console.log("[Lister] Starting Vinted sync...");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    console.log(`[Lister] Found tab: ${tab?.url}`);
    if (!tab?.url?.includes("vinted.")) {
      statusEl.textContent = "Open your Vinted Profile page in this tab first.";
      return;
    }
    const response = await sendVintedMessage(tab.id, { type: "LISTER_SCRAPE_VINTED" });
    console.log("[Lister] Got response from content script:", response);
    const items = response?.items ?? [];
    console.log(`[Lister] Found ${items.length} items`);
    if (!items.length) {
      statusEl.textContent = "Found 0 items on this page - make sure you're on your Vinted Profile page showing your items.";
      return;
    }
    const res = await fetch(`${LISTER_API_BASE}/sync/vinted/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    console.log(`[Lister] Sync API response: ${res.status} ${await res.text()}`);
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    statusEl.textContent = `Sent ${items.length} item(s) to Lister. Check the web UI's sync section.`;
  } catch (err) {
    console.error("[Lister] Sync error:", err);
    statusEl.textContent = `Couldn't sync: ${err.message || err}`;
  }
}

// --- Wardrobe rotation ------------------------------------------------------------
const rotationStatusEl = document.getElementById("rotation-status");
const rotationListEl = document.getElementById("rotation-list");

async function apiJson(path, options) {
  const res = await fetch(`${LISTER_API_BASE}${path}`, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `backend returned ${res.status}`);
  return body;
}

async function scanWardrobe() {
  rotationStatusEl.textContent = "Scrolling your wardrobe and scanning - this can take a minute...";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url?.includes("vinted.")) {
      rotationStatusEl.textContent = "Open your Vinted profile page (showing your items) in this tab first.";
      return;
    }
    const response = await sendVintedMessage(tab.id, { type: "LISTER_SCAN_WARDROBE" });
    const items = response?.items ?? [];
    if (!items.length) {
      rotationStatusEl.textContent = "Found 0 items - make sure this tab is your Vinted profile page.";
      return;
    }
    await apiJson("/rotate/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    rotationStatusEl.textContent = `Scanned ${items.length} live item(s). Check that matches your wardrobe, then pick.`;
    await loadRotation();
  } catch (err) {
    rotationStatusEl.textContent = `Scan failed: ${err.message || err}`;
  }
}

async function planRotation() {
  rotationStatusEl.textContent = "Backing up photos and writing fresh copy - this can take a few minutes...";
  try {
    const result = await apiJson("/rotate/plan", { method: "POST" });
    const skipped = result.skipped.length ? ` ${result.skipped.length} skipped (no photos or copy failed).` : "";
    rotationStatusEl.textContent = `${result.message}${skipped}`;
    await loadRotation();
  } catch (err) {
    rotationStatusEl.textContent = `Couldn't pick: ${err.message || err}`;
  }
}

const STAGE_TEXT = {
  draft_pending: "Review & approve the new draft in the Lister web UI first.",
  reviewed_ready: "Approved - use 'Open Vinted & fill draft' above, then publish it yourself.",
  posted_as_draft: "Draft filled on Vinted - publish it yourself.",
};

async function loadRotation() {
  rotationListEl.textContent = "";
  let status;
  try {
    status = await apiJson("/rotate/status");
  } catch {
    return;
  }
  for (const rot of status.open_rotations) {
    const div = document.createElement("div");
    div.className = "listing";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = rot.original_title;
    const stage = document.createElement("div");
    stage.className = "note";
    stage.textContent = STAGE_TEXT[rot.listing_status] ?? rot.listing_status;
    div.append(title, stage);

    if (rot.delete_ready) {
      const open = document.createElement("button");
      open.textContent = "Open ORIGINAL on Vinted (to delete it)";
      open.addEventListener("click", async () => {
        const vintedId = rot.url.match(/\/items\/(\d+)/)?.[1];
        await chrome.storage.local.set({ lister_rotation_original: { vintedId } });
        chrome.tabs.create({ url: rot.url });
      });
      const done = document.createElement("button");
      done.textContent = "I've deleted the original";
      done.addEventListener("click", async () => {
        if (!confirm(`Only confirm if you've already deleted "${rot.original_title}" on Vinted. Continue?`)) return;
        try {
          await apiJson(`/rotate/${rot.vinted_item_id}/deleted`, { method: "POST" });
          await loadRotation();
        } catch (err) {
          rotationStatusEl.textContent = `Couldn't update: ${err.message || err}`;
        }
      });
      div.append(open, done);
    }
    rotationListEl.appendChild(div);
  }
}

document.getElementById("sync-vinted-btn").addEventListener("click", syncVintedListings);
document.getElementById("scan-wardrobe-btn")?.addEventListener("click", scanWardrobe);
document.getElementById("plan-rotation-btn")?.addEventListener("click", planRotation);
loadReadyListings();
loadRotation();
