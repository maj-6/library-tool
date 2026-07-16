const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const appSource = fs.readFileSync(
  path.join(__dirname, "..", "tools", "whl_explorer", "static", "app.js"),
  "utf8",
);

function block(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function cacheHarness(enabled = { cprs: true, nypl: false }) {
  const clock = { now: 1_000_000 };
  const requests = [];
  const responders = [];
  const storage = new Map();
  let refreshes = 0;

  class FakeDate extends Date {
    static now() { return clock.now; }
  }

  const context = vm.createContext({
    Date: FakeDate,
    URLSearchParams,
    fetch: (url) => new Promise((resolve) => {
      requests.push(String(url));
      responders.push(resolve);
    }),
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
    },
    scheduleCrRefresh: () => { refreshes += 1; },
    state: { settings: { copyrightSources: enabled } },
  });

  const source = block(
    appSource,
    'const REG_KEY = "whl_reg_cache_v2";',
    "// --- copyright renewal-status cache",
  );
  vm.runInContext(`${source}
    this.api = {
      cachedReg,
      queueReg,
      regKey,
      inFlight: () => _regInFlight,
      pending: () => _regPending.size,
      queued: () => _regQueue.length,
      ttl: REG_NEGATIVE_TTL_MS,
    };`, context);

  return {
    api: context.api,
    clock,
    requests,
    responders,
    storage,
    refreshes: () => refreshes,
  };
}

function respond(harness, index, result) {
  harness.responders[index]({
    ok: true,
    status: 200,
    json: () => Promise.resolve(result),
  });
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
}

const BOOK = {
  title: "Fauna and Flora of the Bible",
  author: "United Bible Societies",
  year: "1980",
};
const MISS = { found: false, sources: [], match: null };


test("registration lookup suppresses duplicate queued and in-flight requests", async () => {
  const harness = cacheHarness();
  const key = harness.api.regKey(BOOK);

  harness.api.queueReg(BOOK);
  harness.api.queueReg(BOOK);
  harness.api.queueReg(BOOK);

  assert.equal(harness.requests.length, 1);
  assert.equal(harness.api.pending(), 1);
  assert.equal(harness.api.inFlight(), 1);
  assert.match(harness.requests[0], /sources=cprs/);

  respond(harness, 0, MISS);
  await settle();

  assert.equal(harness.api.pending(), 0);
  assert.equal(harness.api.inFlight(), 0);
  assert.equal(harness.api.cachedReg(key).found, false);
  assert.equal(harness.refreshes(), 1);
});

test("a negative registration result expires and retries without an app reload", async () => {
  const harness = cacheHarness();
  const key = harness.api.regKey(BOOK);

  harness.api.queueReg(BOOK);
  respond(harness, 0, MISS);
  await settle();

  harness.clock.now += harness.api.ttl - 1;
  harness.api.queueReg(BOOK);
  assert.equal(harness.requests.length, 1, "unexpired miss stays cached");

  harness.clock.now += 2;
  harness.api.queueReg(BOOK);
  assert.equal(harness.requests.length, 2, "expired miss is retried in-session");
  assert.equal(harness.api.pending(), 1);

  respond(harness, 1, {
    found: true,
    sources: ["cprs"],
    match: { reg_number: "TX0000520976" },
  });
  await settle();

  assert.equal(harness.api.pending(), 0);
  assert.equal(harness.api.cachedReg(key).found, true);
  assert.equal(harness.refreshes(), 2);
});

test("disabled registration sources do not enqueue or fetch", () => {
  const harness = cacheHarness({ cprs: false, nypl: false });

  harness.api.queueReg(BOOK);

  assert.equal(harness.requests.length, 0);
  assert.equal(harness.api.pending(), 0);
  assert.equal(harness.api.queued(), 0);
});

test("disabling sources releases queued registration keys", async () => {
  const enabled = { cprs: true, nypl: false };
  const harness = cacheHarness(enabled);
  for (let i = 0; i < 4; i += 1) {
    harness.api.queueReg({ ...BOOK, title: `${BOOK.title} ${i}` });
  }
  assert.equal(harness.requests.length, 3);
  assert.equal(harness.api.pending(), 4);
  assert.equal(harness.api.queued(), 1);

  enabled.cprs = false;
  respond(harness, 0, MISS);
  await settle();

  assert.equal(harness.requests.length, 3, "no queued request starts after disable");
  assert.equal(harness.api.queued(), 0);
  assert.equal(harness.api.pending(), 2, "only the two already in flight remain");

  respond(harness, 1, MISS);
  respond(harness, 2, MISS);
  await settle();
  assert.equal(harness.api.pending(), 0);
});
