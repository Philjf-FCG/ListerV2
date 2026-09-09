const photoGrid = document.getElementById("photo-grid");
const listingList = document.getElementById("listing-list");
const scanBtn = document.getElementById("scan-btn");
const generateBar = document.getElementById("generate-bar");
const generateCountLabel = document.getElementById("generate-count-label");
const syncList = document.getElementById("sync-list");

let activePollTimer = null;
const selectedPhotos = new Map(); // key: `${source}:${ref}` -> PhotoItem

function currentFilters() {
  return {
    date: document.getElementById("filter-date").value || null,
    taken_today: document.getElementById("filter-today").checked,
    after_time: document.getElementById("filter-after").value || null,
    before_time: document.getElementById("filter-before").value || null,
  };
}

function matchesFilters(photo, filt) {
  if (!photo.taken_at) return true;
  const takenAt = new Date(photo.taken_at);
  if (filt.taken_today) {
    const today = new Date();
    if (takenAt.toDateString() !== today.toDateString()) return false;
  }
  if (filt.date) {
    const target = new Date(filt.date);
    if (takenAt.toDateString() !== target.toDateString()) return false;
  }
  const hhmm = takenAt.getHours() * 60 + takenAt.getMinutes();
  if (filt.after_time) {
    const [h, m] = filt.after_time.split(":").map(Number);
    if (hhmm < h * 60 + m) return false;
  }
  if (filt.before_time) {
    const [h, m] = filt.before_time.split(":").map(Number);
    if (hhmm > h * 60 + m) return false;
  }
  return true;
}

function stopPolling() {
  if (activePollTimer) {
    clearTimeout(activePollTimer);
    activePollTimer = null;
  }
}

async function scanPhotos() {
  const src = document.querySelector('input[name="src"]:checked').value;
  stopPolling();

  if (src === "local") {
    const params = new URLSearchParams();
    const filt = currentFilters();
    if (filt.date) params.set("date", filt.date);
    if (filt.taken_today) params.set("taken_today", "true");
    if (filt.after_time) params.set("after_time", filt.after_time);
    if (filt.before_time) params.set("before_time", filt.before_time);
    const res = await fetch(`/photos/local?${params.toString()}`);
    const photos = await res.json();
    renderPhotos(photos);
    return;
  }

  // Google Photos: verify connection, open the picker, then poll until the
  // user has finished selecting photos in that tab.
  let connected;
  try {
    const statusRes = await fetch("/photos/google/status");
    if (!statusRes.ok) throw new Error(`backend returned ${statusRes.status}`);
    ({ connected } = await statusRes.json());
  } catch (err) {
    alert(`Couldn't reach the Lister backend to check Google Photos connection status: ${err}`);
    return;
  }
  if (!connected) {
    alert("Google Photos isn't connected yet. Click 'Connect Google Photos' and finish the Google consent screen, then click Scan again.");
    return;
  }

  scanBtn.disabled = true;
  photoGrid.innerHTML = '<p class="meta">Creating a Google Photos picker session...</p>';
  try {
    const sessionRes = await fetch("/photos/google/session", { method: "POST" });
    if (!sessionRes.ok) {
      const body = await sessionRes.text();
      throw new Error(`backend returned ${sessionRes.status}: ${body}`);
    }
    const session = await sessionRes.json();
    window.open(session.pickerUri, "_blank");
    photoGrid.innerHTML = '<p class="meta">Pick your photos in the Google Photos tab that just opened - this page will update automatically once you\'re done.</p>';
    await pollGoogleSession(session.id);
  } catch (err) {
    photoGrid.innerHTML = `<p class="meta status-failed">Couldn't create a Google Photos picker session: ${err}</p>`;
  } finally {
    scanBtn.disabled = false;
  }
}

function pollGoogleSession(sessionId, elapsedMs = 0) {
  const TIMEOUT_MS = 5 * 60 * 1000;
  const INTERVAL_MS = 2500;
  return new Promise((resolve) => {
    const check = async () => {
      let session;
      try {
        const sessionRes = await fetch(`/photos/google/session/${sessionId}`);
        if (!sessionRes.ok) throw new Error(`backend returned ${sessionRes.status}`);
        session = await sessionRes.json();
      } catch (err) {
        photoGrid.innerHTML = `<p class="meta status-failed">Lost contact with the Lister backend while waiting for your photo picks: ${err}</p>`;
        resolve();
        return;
      }
      if (session.mediaItemsSet) {
        try {
          const itemsRes = await fetch(`/photos/google/items/${sessionId}`);
          if (!itemsRes.ok) throw new Error(`backend returned ${itemsRes.status}`);
          const items = await itemsRes.json();
          const filt = currentFilters();
          renderPhotos(items.filter((p) => matchesFilters(p, filt)));
        } catch (err) {
          photoGrid.innerHTML = `<p class="meta status-failed">Photos were picked, but Lister couldn't fetch them: ${err}</p>`;
        }
        resolve();
        return;
      }
      if (elapsedMs >= TIMEOUT_MS) {
        photoGrid.innerHTML = '<p class="meta status-failed">Timed out waiting for photos to be picked - click Scan to try again.</p>';
        resolve();
        return;
      }
      activePollTimer = setTimeout(() => {
        elapsedMs += INTERVAL_MS;
        check();
      }, INTERVAL_MS);
    };
    check();
  });
}

function renderPhotos(photos) {
  photoGrid.innerHTML = "";
  selectedPhotos.clear();
  updateGenerateBar();
  if (!photos.length) {
    photoGrid.innerHTML = '<p class="meta">No photos matched your filters.</p>';
    return;
  }
  for (const photo of photos) {
    const key = `${photo.source}:${photo.ref}`;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <img src="${photo.thumbnail_url}" alt="photo" />
      <div class="meta">${photo.taken_at ?? "unknown date"}</div>
      <label><input type="checkbox" class="select-photo" /> Include in listing</label>
    `;
    const checkbox = card.querySelector(".select-photo");
    checkbox.addEventListener("change", () => {
      card.classList.toggle("selected", checkbox.checked);
      if (checkbox.checked) {
        selectedPhotos.set(key, photo);
      } else {
        selectedPhotos.delete(key);
      }
      updateGenerateBar();
    });
    photoGrid.appendChild(card);
  }
}

function updateGenerateBar() {
  const count = selectedPhotos.size;
  generateBar.style.display = count ? "flex" : "none";
  generateCountLabel.textContent = `${count} photo${count === 1 ? "" : "s"} selected`;
}

async function generateSelectedListing() {
  const platforms = [...document.querySelectorAll(".generate-plat:checked")].map((el) => el.value);
  const item_hint = document.getElementById("generate-hint").value || null;
  if (!selectedPhotos.size) return;
  if (!platforms.length) {
    alert("Pick at least one platform (Vinted and/or eBay) before generating.");
    return;
  }
  const items = [...selectedPhotos.values()].map((p) => ({ source: p.source, ref: p.ref }));
  const btn = document.getElementById("generate-selected-btn");
  btn.disabled = true;
  btn.textContent = "Thinking (asking Ollama)...";
  try {
    const res = await fetch("/listings/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, platforms, item_hint }),
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`backend returned ${res.status}: ${body}`);
    }
    await loadListings();
  } catch (err) {
    alert(`Couldn't generate listing copy: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate listing copy";
  }
}

async function connectGoogle() {
  window.open("/auth/google/login", "_blank");
}

async function connectEbay() {
  window.open("/auth/ebay/login", "_blank");
}

async function loadListings() {
  const res = await fetch("/listings");
  const listings = await res.json();
  listingList.innerHTML = "";
  for (const listing of listings) {
    const card = document.createElement("div");
    card.className = "card";
    const thumbsHtml = listing.thumbnail_urls
      .map((url) => `<img src="${url}" alt="thumb" class="thumb-small"/>`)
      .join("");
    let nextStepHtml = "";
    let statusErrorHtml = "";

    if (listing.status === "reviewed_ready") {
      nextStepHtml = listing.platform === "ebay"
        ? `<div class="ebay-push" data-listing-id="${listing.id}">
             <button class="suggest-category-btn">Get eBay category suggestions</button>
             <select class="category-select" style="display:none"></select>
             <button class="push-ebay-btn" style="display:none">Push to eBay (draft)</button>
           </div>`
        : `<div class="meta status-reviewed_ready">Next: click the Lister extension icon in your browser toolbar, then pick this listing to open ${listing.platform} and auto-fill the draft.</div>`;
    } else if (listing.status === "posted_as_draft") {
      if (listing.error && listing.error.startsWith("published:")) {
        const itemId = listing.error.split(":")[1];
        nextStepHtml = `<div class="meta status-reviewed_ready"><strong>Live on eBay!</strong> <a href="https://www.ebay.co.uk/itm/${itemId}" target="_blank">View Item ${itemId} on eBay</a></div>`;
      } else {
        nextStepHtml = `<div class="meta status-posted_as_draft">
          <strong>Draft offer created on eBay!</strong><br/>
          <small>(Note: eBay's website only shows web-wizard drafts under "Seller Hub > Drafts", but your item is safely saved in eBay's Inventory DB).</small><br/>
          <button class="publish-ebay-btn" style="margin-top: 6px;">Publish to Live eBay Now</button>
        </div>`;
      }
    } else if (listing.error && !listing.error.startsWith("offer_id:")) {
      statusErrorHtml = `<div class="meta status-failed">${listing.error}</div>`;
    }

    card.innerHTML = `
      <span class="platform-badge">${listing.platform}</span>
      <span class="status-${listing.status}"> ${listing.status}</span>
      <div class="thumb-row">${thumbsHtml}</div>
      <input type="text" class="title" value="${listing.title ?? ""}" placeholder="title" />
      <textarea class="description">${listing.description ?? ""}</textarea>
      <input type="text" class="price" value="${listing.price ?? ""}" placeholder="price (GBP)" />
      <input type="text" class="condition" value="${listing.condition ?? ""}" placeholder="condition" />
      ${statusErrorHtml}
      ${nextStepHtml}
      <div class="row">
        <button class="save-btn">Save edits</button>
        <button class="approve-btn">Mark ready for draft</button>
        <button class="delete-btn">Delete</button>
      </div>
    `;
    card.querySelector(".save-btn").addEventListener("click", () => saveListing(listing.id, card));
    card.querySelector(".approve-btn").addEventListener("click", () => approveListing(listing.id, card));
    card.querySelector(".delete-btn").addEventListener("click", () => deleteListing(listing.id));
    const suggestBtn = card.querySelector(".suggest-category-btn");
    if (suggestBtn) suggestBtn.addEventListener("click", () => loadEbayCategorySuggestions(listing, card));
    const publishBtn = card.querySelector(".publish-ebay-btn");
    if (publishBtn) publishBtn.addEventListener("click", () => publishEbayListing(listing.id, publishBtn));
    listingList.appendChild(card);
  }
}

async function publishEbayListing(listingId, btn) {
  btn.disabled = true;
  btn.textContent = "Publishing to live eBay...";
  try {
    const res = await fetch(`/ebay/listings/${listingId}/publish`, { method: "POST" });
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    const data = await res.json();
    alert(`Published! Your item is live on eBay: ${data.itemUrl}`);
    await loadListings();
  } catch (err) {
    alert(`Couldn't publish to eBay: ${err}`);
    btn.disabled = false;
    btn.textContent = "Publish to Live eBay";
  }
}

async function loadEbayCategorySuggestions(listing, card) {
  const btn = card.querySelector(".suggest-category-btn");
  const select = card.querySelector(".category-select");
  const pushBtn = card.querySelector(".push-ebay-btn");
  btn.disabled = true;
  btn.textContent = "Looking up categories...";
  try {
    const res = await fetch(`/ebay/category-suggestions?q=${encodeURIComponent(listing.title ?? "")}`);
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    const suggestions = await res.json();
    if (!suggestions.length) {
      alert("eBay didn't suggest any categories for this title - try editing the title to be more specific.");
      return;
    }
    select.innerHTML = suggestions
      .map((s) => `<option value="${s.category_id}">${s.full_path}</option>`)
      .join("");
    select.style.display = "";
    pushBtn.style.display = "";
    pushBtn.onclick = () => pushListingToEbay(listing.id, select.value, pushBtn);
    select.addEventListener("change", () => { pushBtn.onclick = () => pushListingToEbay(listing.id, select.value, pushBtn); });
  } catch (err) {
    alert(`Couldn't get eBay category suggestions: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Get eBay category suggestions";
  }
}

async function pushListingToEbay(listingId, categoryId, btn) {
  btn.disabled = true;
  btn.textContent = "Creating draft on eBay...";
  try {
    const res = await fetch(`/ebay/listings/${listingId}/push?category_id=${encodeURIComponent(categoryId)}`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    const data = await res.json();
    alert("Draft created in eBay Inventory Service! You can now click 'Publish to Live eBay' on the listing card when you're ready to list it live.");
    await loadListings();
  } catch (err) {
    alert(`Couldn't push to eBay: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Push to eBay (draft)";
  }
}

async function saveListing(id, card) {
  await fetch(`/listings/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: card.querySelector(".title").value,
      description: card.querySelector(".description").value,
      price: card.querySelector(".price").value,
      condition: card.querySelector(".condition").value,
    }),
  });
  await loadListings();
}

async function approveListing(id, card) {
  await saveListing(id, card);
  await fetch(`/listings/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "reviewed_ready" }),
  });
  await loadListings();
}

async function deleteListing(id) {
  await fetch(`/listings/${id}`, { method: "DELETE" });
  await loadListings();
}

async function loadSyncList() {
  syncList.innerHTML = '<p class="meta">Checking against your live eBay listings...</p>';
  try {
    const res = await fetch("/sync/vinted/items");
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    const items = await res.json();
    renderSyncList(items);
  } catch (err) {
    syncList.innerHTML = `<p class="meta status-failed">Couldn't check eBay listings: ${err}</p>`;
  }
}

function renderSyncList(items) {
  syncList.innerHTML = "";
  if (!items.length) {
    syncList.innerHTML = '<p class="meta">No Vinted listings synced yet - use the extension popup while on your Vinted "For sale" page.</p>';
    return;
  }
  const missing = items.filter((i) => !i.on_ebay);
  if (!missing.length) {
    syncList.innerHTML = '<p class="meta status-reviewed_ready">Everything on Vinted already has a match on eBay.</p>';
    return;
  }
  for (const item of missing) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      ${item.photo_url ? `<img src="${item.photo_url}" alt="thumb" />` : ""}
      <div class="meta">${item.title}</div>
      <div class="meta">£${item.price ?? "?"}</div>
      <button class="import-btn">Generate eBay draft copy from this</button>
    `;
    card.querySelector(".import-btn").addEventListener("click", (e) => importVintedItem(item.id, e.target));
    syncList.appendChild(card);
  }
}

async function importVintedItem(vintedItemId, btn) {
  btn.disabled = true;
  btn.textContent = "Asking Ollama...";
  try {
    const res = await fetch(`/sync/vinted/import/${vintedItemId}`, { method: "POST" });
    if (!res.ok) throw new Error(`backend returned ${res.status}: ${await res.text()}`);
    await loadListings();
    await loadSyncList();
  } catch (err) {
    alert(`Couldn't import this item: ${err}`);
    btn.disabled = false;
    btn.textContent = "Generate eBay draft copy from this";
  }
}

document.getElementById("scan-btn").addEventListener("click", scanPhotos);
document.getElementById("google-connect-btn").addEventListener("click", connectGoogle);
document.getElementById("ebay-connect-btn").addEventListener("click", connectEbay);
document.getElementById("generate-selected-btn").addEventListener("click", generateSelectedListing);
document.getElementById("refresh-sync-btn").addEventListener("click", loadSyncList);
loadListings();
loadSyncList();
