// Fills the Vinted "new item" form from a Lister listing. NEVER clicks the final
// "Upload"/"Post" button - only fills fields and, if present, clicks an explicit
// "save as draft" control. Vinted's DOM changes over time; adjust SELECTORS below
// if fields stop being found (check the console for warnings).
const LISTER_API_BASE = "http://localhost:8010";
const SELECTORS = {
  title: 'input[name="title"], input[data-testid="title-input"]',
  description: 'textarea[name="description"], textarea[data-testid="description-input"]',
  price: 'input[name="price"], input[data-testid="price-input"]',
  photoInput: 'input[type="file"][accept*="image"], input[type="file"]',
  saveDraftButton: 'button[data-testid="save-draft-button"]',
  // Deliberately NOT targeting the publish/upload button - we never click it.
};

const FORBIDDEN_BUTTON_TEXT = /\b(upload|publish|post item|list it)\b/i;

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
    background: "#2f855a", color: "white", padding: "10px 14px",
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
    console.warn("[Lister] photo upload input not found - selectors may need updating");
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

async function fillFromPendingListing() {
  const key = "lister_pending_vinted";
  const stored = await chrome.storage.local.get(key);
  const listing = stored[key];
  if (!listing) return;

  try {
    const titleEl = await waitForElement(SELECTORS.title);
    setNativeValue(titleEl, listing.title ?? "");

    const descEl = await waitForElement(SELECTORS.description);
    setNativeValue(descEl, listing.description ?? "");

    if (listing.price) {
      try {
        const priceEl = await waitForElement(SELECTORS.price, 5000);
        setNativeValue(priceEl, listing.price);
      } catch {
        console.warn("[Lister] price field not found, skipping");
      }
    }

    await uploadPhotos(listing);

    showBanner("Lister filled the title/description/price/photos. Review and save as draft yourself.");

    const draftBtn = document.querySelector(SELECTORS.saveDraftButton);
    if (draftBtn && !FORBIDDEN_BUTTON_TEXT.test(draftBtn.textContent || "")) {
      draftBtn.click();
      chrome.runtime.sendMessage({ type: "LISTER_MARK_POSTED", id: listing.id });
      showBanner("Saved as draft on Vinted.");
    }
  } catch (err) {
    console.warn("[Lister] Could not fill Vinted form:", err);
    showBanner("Lister couldn't find the Vinted form fields - selectors may need updating.");
  } finally {
    await chrome.storage.local.remove(key);
  }
}

fillFromPendingListing();

// --- Scraping the user's own Vinted listings page (for the Vinted<->eBay sync
// feature) - triggered on-demand from the popup, not run automatically. Vinted's
// DOM changes over time; adjust these heuristics if scraping stops finding items.
function scrapeVintedListings() {
  const seen = new Set();
  const items = [];
  
  // First, log the full structure of a sample item to understand Vinted's DOM
  console.log("[Lister] === DOM STRUCTURE DEBUG ===");
  const sampleAnchor = document.querySelector('a[href*="/items/"]');
  if (sampleAnchor) {
    const container = sampleAnchor.closest("li, article, div[data-testid*='item'], div[class*='feed-grid'], div[class*='item-box'], div[class*='closet'], div[class*='grid__item']") ||
                     sampleAnchor.parentElement ||
                     sampleAnchor;
    
    console.log("[Lister] Sample container:", container);
    console.log("[Lister] Container outerHTML (first 2000 chars):", container.outerHTML.substring(0, 2000));
  }
  
  document.querySelectorAll('a[href*="/items/"]').forEach((anchor) => {
    const href = anchor.href;
    if (seen.has(href) || !/\/items\/\d+/.test(href)) return;
    seen.add(href);
    const container =
      anchor.closest("li, article, div[data-testid*='item'], div[class*='feed-grid'], div[class*='item-box'], div[class*='closet'], div[class*='grid__item']") ||
      anchor.parentElement ||
      anchor;
    
    // Debug: Log container structure to understand Vinted's DOM
    console.log(`[Lister] Container type: ${container.tagName}, class: ${container.className}`);
    
    // Check if this is a carousel/grid item with multiple images
    const carousel = container.querySelector('[class*="carousel"], [class*="gallery"], [class*="slider"]');
    
    // Log the full container HTML for debugging (first 3000 chars)
    console.log(`[Lister] Container outerHTML preview:`, container.outerHTML.substring(0, 3000));
    
    let photo_urls = [];
    // Check for embedded JSON data that might contain multiple photos
    let jsonPhotos = [];
    const metaJson = document.querySelector('script[type="application/json"][id*="initial-state"], script[data-ssr]');
    if (metaJson) {
      try {
        const jsonData = JSON.parse(metaJson.textContent);
        console.log(`[Lister] Found embedded JSON for ${href}`, jsonData);
      } catch (e) {
        // No valid JSON
      }
    }
    
    // Also check data attributes on container itself
    if (container.dataset && container.dataset.json) {
      try {
        const jsonData = JSON.parse(container.dataset.json);
        console.log(`[Lister] Container has JSON data:`, jsonData);
        if (jsonData.photos && Array.isArray(jsonData.photos)) {
          jsonPhotos = jsonData.photos.map(p => p.url || p).filter(u => u);
        }
      } catch (e) {
        // No valid JSON
      }
    }
    
    let photo_urls = [];
    
    if (carousel) {
      console.log(`[Lister] Found carousel/slider in container for ${href}`);
      // Try to extract all image URLs from carousel items
      const carouselImages = carousel.querySelectorAll('img, [data-image]');
      console.log(`[Lister] Carousel has ${carouselImages.length} image elements`);
      carouselImages.forEach((img, idx) => {
        let url = img.src || img.getAttribute("data-src") || img.getAttribute("data-lazy-src");
        if (url && !photo_urls.includes(url)) {
          url = url.replace(/\.webp(\?.*)?$/, ".jpg$1");
          photo_urls.push(url);
          console.log(`[Lister] Carousel image ${idx}: ${url.substring(0, 60)}...`);
        }
      });
    }
    
    // Collect ALL images in the container (not just img tags - also check for background-image)
    const allImages = Array.from(container.querySelectorAll("img"));
    console.log(`[Lister] Container has ${allImages.length} img elements total`);
    
    for (const img of allImages) {
      // Check multiple attributes that might contain image URLs
      let url = img.src || 
                img.getAttribute("data-src") || 
                img.getAttribute("data-lazy-src") ||
                img.getAttribute("srcset")?.split(",")[0]?.trim().split(" ")[0] ||
                null;
      
      // If srcset exists, get highest resolution
      if (img.srcset && !url) {
        const srcsetEntries = img.srcset.split(",").map(entry => entry.trim());
        url = srcsetEntries.sort((a, b) => {
          const wA = parseInt(a.match(/(\d+)w$/)?.[1] || "0");
          const wB = parseInt(b.match(/(\d+)w$/)?.[1] || "0");
          return wB - wA;
        })[0]?.split(" ")[0] || null;
      }
      
      // Check data attributes for more image URLs
      if (!url) {
        for (const key in img.dataset) {
          const val = img.dataset[key];
          if (val && /https?:\/\/.*\.(jpg|jpeg|png|webp)/i.test(val)) {
            url = val;
            break;
          }
        }
      }
      
      if (url && !photo_urls.includes(url)) {
        // Normalize URL - Vinted uses webp, convert to jpg for consistency
        url = url.replace(/\.webp(\?.*)?$/, ".jpg$1");
        photo_urls.push(url);
        console.log(`[Lister] Found image: ${url.substring(0, 60)}...`);
      }
    }
    
    // Also check for background images in the container
    const bgImages = Array.from(container.querySelectorAll("[style*='background-image'], [data-background*='url']"))
      .map(el => {
        let style = el.getAttribute("style");
        let url = null;
        
        if (style) {
          const match = style.match(/url\(['"]?([^'")]+)['"]?\)/);
          if (match) url = match[1];
        }
        
        // Check data-background attribute
        if (!url) {
          const bgData = el.getAttribute("data-background");
          if (bgData) {
            const match = bgData.match(/url\(['"]?([^'")]+)['"]?\)/);
            if (match) url = match[1];
          }
        }
        
        return url;
      })
      .filter(url => url && !photo_urls.includes(url))
      // Normalize background image URLs
      .map(url => url.replace(/\.webp(\?.*)?$/, ".jpg$1"));
    
    console.log(`[Lister] Background images found:`, bgImages);
    photo_urls.push(...bgImages);
    
    console.log(`[Lister] Total photos found for ${href}: ${photo_urls.length}`);
    
    let title = container.querySelector("img")?.alt || anchor.getAttribute("title") || anchor.getAttribute("aria-label") || "";
    if (!title || title.length < 2) {
      const titleEl = container.querySelector("[data-testid*='title'], [class*='title'], p, span");
      title = titleEl?.textContent?.trim() || anchor.textContent?.trim() || "";
    }
    if (!title) title = "Untitled Vinted item";
    
    let price = null;
    const priceMatch = (container.textContent || "").match(/[£$€]\s?\d+([.,]\d{2})?/);
    if (priceMatch) {
      price = priceMatch[0].replace(/[£$€]\s?/, "");
    } else {
      // Fallback: try to extract from title
      const titlePrice = title.match(/[£$€]\s?\d+([.,]\d{2})?/);
      if (titlePrice) {
        price = titlePrice[0].replace(/[£$€]\s?/, "");
      }
    }
    
    // Pass all photo URLs so the backend can download them all
    items.push({ url: href, title: title.slice(0, 120), price, photo_urls });
  });
  return items;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "LISTER_SCRAPE_VINTED") {
    console.log("[Lister] Received LISTER_SCRAPE_VINTED message");
    const items = scrapeVintedListings();
    console.log(`[Lister] Returning ${items.length} items`, items);
    sendResponse({ items: items });
    return true;
  }
  return false;
});
