// Shared by the popup, the options page and the background worker: where the Lister server
// is and how to authenticate to it. Settings live in chrome.storage.local and are edited on
// the extension's options page (right-click the extension > Options).
const LISTER_DEFAULT_SERVER = "http://localhost:8010";

function listerBasicAuth(user, password) {
  const bytes = new TextEncoder().encode(`${user}:${password}`); // UTF-8 safe, unlike btoa(string)
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return "Basic " + btoa(binary);
}

async function listerSettings() {
  const stored = await chrome.storage.local.get(["lister_server", "lister_user", "lister_password"]);
  const base = (stored.lister_server || LISTER_DEFAULT_SERVER).replace(/\/+$/, "");
  const headers = stored.lister_password
    ? { Authorization: listerBasicAuth(stored.lister_user || "lister", stored.lister_password) }
    : {};
  return { base, headers };
}

async function listerFetch(path, options = {}) {
  const { base, headers } = await listerSettings();
  return fetch(base + path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
}

function listerHttpError(status) {
  return status === 401
    ? "the server rejected the password - check the Lister extension options"
    : `backend returned ${status}`;
}
