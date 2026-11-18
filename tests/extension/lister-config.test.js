// Run with: node --test tests/extension
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function load(stored) {
  const calls = [];
  const context = {
    chrome: { storage: { local: { get: async () => stored } } },
    fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, status: 200 }; },
    TextEncoder, btoa, console,
  };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(__dirname, "../../extension/lister-config.js"), "utf8");
  vm.runInContext(source + "\n;this.api = { listerSettings, listerFetch, listerBasicAuth, listerHttpError, LISTER_DEFAULT_SERVER };", context);
  return { api: context.api, calls };
}

test("defaults to localhost:8010 with no credentials when nothing is configured", async () => {
  const { api } = load({});
  const { base, headers } = await api.listerSettings();
  assert.strictEqual(base, "http://localhost:8010");
  assert.deepStrictEqual({ ...headers }, {});
});

test("uses the configured server, strips trailing slashes, and sends Basic auth", async () => {
  const { api, calls } = load({ lister_server: "http://192.168.1.37:8011///", lister_user: "bob", lister_password: "p@ss:w0rd" });
  await api.listerFetch("/listings?status=x", { method: "GET" });
  assert.strictEqual(calls[0].url, "http://192.168.1.37:8011/listings?status=x");
  const expected = "Basic " + Buffer.from("bob:p@ss:w0rd").toString("base64");
  assert.strictEqual(calls[0].options.headers.Authorization, expected);
});

test("username falls back to 'lister' and non-ASCII passwords are UTF-8 encoded", async () => {
  const { api } = load({});
  assert.strictEqual(api.listerBasicAuth("lister", "pässword"), "Basic " + Buffer.from("lister:pässword", "utf8").toString("base64"));
  const { api: api2, calls } = load({ lister_password: "x" });
  await api2.listerFetch("/a");
  assert.strictEqual(calls[0].options.headers.Authorization, "Basic " + Buffer.from("lister:x").toString("base64"));
});

test("caller headers are merged with, and can add to, the auth header", async () => {
  const { api, calls } = load({ lister_password: "x" });
  await api.listerFetch("/a", { method: "POST", headers: { "Content-Type": "application/json" } });
  assert.strictEqual(calls[0].options.headers["Content-Type"], "application/json");
  assert.ok(calls[0].options.headers.Authorization.startsWith("Basic "));
  assert.strictEqual(calls[0].options.method, "POST");
});

test("401 gets an actionable message", () => {
  const { api } = load({});
  assert.match(api.listerHttpError(401), /password/);
  assert.strictEqual(api.listerHttpError(500), "backend returned 500");
});
