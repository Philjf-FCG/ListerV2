// Fills the eBay "sell" form from a Lister listing. NEVER clicks "List it" /
// "Publish" - only fills fields and, if present, clicks an explicit
// "Save for later" (draft) control. eBay's DOM changes over time; adjust
// SELECTORS below if fields stop being found (check the console for warnings).
const LISTER_API_BASE = "http://localhost:8010";
const SELECTORS = {
  // eBay's create-listing flow starts with a search box that suggests a category -
  // there's no static blank form until after you pick one from that dropdown.
  // Real placeholder observed: "Tell us what you're selling".
  searchInput: 'input[placeholder*="selling" i], input[aria-label*="selling" i], input[name="q"]',
  title: 'input[name="title"], input#title-input, textarea[name="title"]',
  // eBay's description box is a contenteditable rich-text div, not a textarea/iframe.
  description: 'div[data-testid="richEditor"][contenteditable="true"], div[aria-label="Description"][contenteditable="true"], iframe#desc_ifr, textarea[name="description"]',
  price: 'input[name="price"], input#price-input',
  photoInput: 'input[type="file"][accept*="image"], input[type="file"]',
  saveDraftButton: 'button[data-testid="save-for-later-button"], button[aria-label*="Save for later" i]',
};

const FORBIDDEN_BUTTON_TEXT = /\b(list it|publish|submit listing)\b/i;

function setNativeValue(el, value) {
  const proto = Object.getPrototypeOf(el);
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  setter ? setter.call(el, value) : (el.value = value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function showBanner(text) {
  const banner = document.createElement("div");
  banner.textContent = text;
  Object.assign(banner.style, {
    position: "fixed", top: "10px", right: "10px", zIndex: 999999,
    background: "#2b6cb0", color: "white", padding: "10px 14px",
    borderRadius: "6px", fontFamily: "sans-serif", fontSize: "13px",
    boxShadow: "0 2px 8px rgba(0,0,0,0.2)", maxWidth: "320px",
  });
  document.body.appendChild(banner);
  setTimeout(() => banner.remove(), 8000);
}

function waitForElement(selector, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(selector);
    if (existing) return resolve(existing);
    const observer = new MutationObserver(() => {
      const el = document.querySelector(selector);
      if (el) {
        observer.disconnect();
        resolve(el);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => {
      observer.disconnect();
      reject(new Error(`Timed out waiting for ${selector}`));
    }, timeoutMs);
  });
}

function fetchPhotoAsFile(url, filename) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type: "LISTER_FETCH_PHOTO", url }, (response) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!response?.ok) return reject(new Error(response?.error || "photo fetch failed"));
      const bytes = Uint8Array.from(atob(response.base64), (c) => c.charCodeAt(0));
      resolve(new File([bytes], filename, { type: response.mime || "image/jpeg" }));
    });
  });
}

async function uploadPhotos(listing) {
  const photoCount = listing.photo_items?.length ?? 0;
  if (!photoCount) return;
  const input = document.querySelector(SELECTORS.photoInput);
  if (!input) {
    console.warn("[Lister] photo upload input not found on this page yet");
    return;
  }
  const files = [];
  for (let i = 0; i < photoCount; i++) {
    try {
      const file = await fetchPhotoAsFile(
        `${LISTER_API_BASE}/listings/${listing.id}/photos/${i}`,
        `photo-${i + 1}.jpg`
      );
      files.push(file);
    } catch (err) {
      console.warn(`[Lister] couldn't fetch photo ${i + 1} for upload:`, err);
    }
  }
  if (!files.length) return;
  const dataTransfer = new DataTransfer();
  files.forEach((file) => dataTransfer.items.add(file));
  input.files = dataTransfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  input.dispatchEvent(new Event("input", { bubbles: true }));
  showBanner(`Lister uploaded ${files.length} photo(s).`);
}

function fillDescription(el, text) {
  if (el.tagName === "IFRAME") {
    // Some older eBay flows use a TinyMCE-style iframe editor.
    const doc = el.contentDocument;
    if (doc?.body) {
      doc.body.innerHTML = text.replace(/\n/g, "<br>");
    }
  } else if (el.isContentEditable) {
    // Setting innerHTML directly doesn't fire the events eBay's React editor
    // listens for, so dispatch them manually afterwards.
    el.classList.remove("placeholder");
    el.innerHTML = text.replace(/\n/g, "<br>");
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  } else {
    setNativeValue(el, text);
  }
}

async function fillFromPendingListing() {
  const key = "lister_pending_ebay";
  const stored = await chrome.storage.local.get(key);
  const listing = stored[key];
  if (!listing) return;

  // eBay's create-listing flow spans several pages (search/match a category
  // before you reach the real form) - only give up entirely after a while,
  // not just because this particular page doesn't have the fields yet.
  const PENDING_TIMEOUT_MS = 10 * 60 * 1000;
  if (listing.stashedAt && Date.now() - listing.stashedAt > PENDING_TIMEOUT_MS) {
    await chrome.storage.local.remove(key);
    return;
  }

  // On the category-search page, pre-fill the search box so the user doesn't have
  // to retype the title - they still need to pick the right category themselves.
  try {
    const searchEl = await waitForElement(SELECTORS.searchInput, 3000);
    if (!searchEl.value) {
      setNativeValue(searchEl, listing.title ?? "");
      showBanner("Lister filled the search box - pick a matching category to continue.");
    }
    return; // the real form fields won't exist on this page yet
  } catch {
    // no search box here - this might already be the real listing details page
  }

  try {
    const titleEl = await waitForElement(SELECTORS.title, 8000);
    setNativeValue(titleEl, listing.title ?? "");

    const descEl = await waitForElement(SELECTORS.description);
    fillDescription(descEl, listing.description ?? "");

    if (listing.price) {
      try {
        const priceEl = await waitForElement(SELECTORS.price, 5000);
        setNativeValue(priceEl, listing.price);
      } catch {
        console.warn("[Lister] price field not found, skipping");
      }
    }

    await uploadPhotos(listing);

    showBanner("Lister filled the title/description/price/photos. Review and save for later yourself.");

    const draftBtn = document.querySelector(SELECTORS.saveDraftButton);
    if (draftBtn && !FORBIDDEN_BUTTON_TEXT.test(draftBtn.textContent || "")) {
      draftBtn.click();
      chrome.runtime.sendMessage({ type: "LISTER_MARK_POSTED", id: listing.id });
      showBanner("Saved as a draft on eBay.");
    }
    // Only clear the pending listing once we've actually filled the real form -
    // a "not found on this page" result leaves it in place for the next page.
    await chrome.storage.local.remove(key);
  } catch (err) {
    console.warn("[Lister] eBay form fields not on this page yet:", err);
  }
}

fillFromPendingListing();

