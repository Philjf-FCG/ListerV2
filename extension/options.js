const $ = (id) => document.getElementById(id);

function setStatus(text, ok) {
  const el = $("status");
  el.textContent = text;
  el.className = ok === undefined ? "" : ok ? "ok" : "bad";
}

function normaliseServer(raw) {
  const url = new URL(raw.trim()); // throws on junk
  if (!/^https?:$/.test(url.protocol)) throw new Error("The address must start with http:// or https://");
  return url.origin;
}

async function load() {
  const stored = await chrome.storage.local.get(["lister_server", "lister_user", "lister_password"]);
  $("server").value = stored.lister_server || LISTER_DEFAULT_SERVER;
  $("user").value = stored.lister_user || "lister";
  $("password").value = stored.lister_password || "";
}

async function saveAndTest() {
  let server;
  try {
    server = normaliseServer($("server").value);
  } catch (err) {
    setStatus(`That doesn't look like a valid address: ${err.message}`, false);
    return;
  }
  // Chrome only lets the extension call servers it has been given permission for. This must
  // be requested straight from the click, before any other await.
  const granted = await chrome.permissions.request({ origins: [`${server}/*`] });
  if (!granted) {
    setStatus("Permission to contact that server was declined, so nothing was saved.", false);
    return;
  }
  await chrome.storage.local.set({
    lister_server: server,
    lister_user: $("user").value.trim() || "lister",
    lister_password: $("password").value,
  });

  setStatus("Saved. Testing the connection...");
  try {
    const health = await listerFetch("/healthz");
    if (!health.ok) throw new Error(`the server answered ${health.status}`);
    const auth = await listerFetch("/listings?status=__connection_test__");
    if (!auth.ok) throw new Error(listerHttpError(auth.status));
    setStatus(`Connected to ${server} and the password is accepted.`, true);
  } catch (err) {
    setStatus(`Saved, but the test failed: ${err.message || err}`, false);
  }
}

$("save").addEventListener("click", saveAndTest);
load();
