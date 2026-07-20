const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

async function policy() {
  const source = fs.readFileSync(path.join(
    __dirname, "..", "website", "assets", "release-policy.js"), "utf8");
  const url = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
  return import(url);
}

test("public downloads accept only stable and supported prerelease channels", async () => {
  const { isPublicRelease, isStableRelease } = await policy();

  assert.equal(isPublicRelease({ channel: "stable", url: "https://example/a.exe" }), true);
  assert.equal(isPublicRelease({ channel: "", url: "https://example/a.exe" }), true);
  assert.equal(isStableRelease({ channel: null }), true);
  for (const channel of ["alpha", "beta", "rc", " ALPHA "]) {
    assert.equal(isPublicRelease({ channel, url: "https://example/a.exe" }), true);
    assert.equal(isStableRelease({ channel }), false);
  }
});

test("internal, unknown, and DONOTPUBLISH rows never reach the website", async () => {
  const { isPublicRelease } = await policy();

  for (const channel of ["debug", "nightly", "canary", 7, {}, []]) {
    assert.equal(isPublicRelease({ channel, url: "https://example/a.exe" }), false);
  }
  assert.equal(isPublicRelease({
    channel: "alpha",
    url: "https://example/BookCapture-debug-DONOTPUBLISH.apk",
  }), false);
  assert.equal(isPublicRelease(null), false);
});
