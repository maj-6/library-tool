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

  for (const channel of ["debug", "nightly", "canary", " ", "\t", 7, {}, []]) {
    assert.equal(isPublicRelease({ channel, url: "https://example/a.exe" }), false);
  }
  assert.equal(isPublicRelease({
    channel: "alpha",
    url: "https://example/BookCapture-debug-DONOTPUBLISH.apk",
  }), false);
  assert.equal(isPublicRelease({
    channel: "stable",
    url: "https://example/BookCapture-debug-%44%4f%4e%4f%54%50%55%42%4c%49%53%48.apk",
  }), false);
  assert.equal(isPublicRelease(null), false);
});

test("public rows require an absolute HTTP or HTTPS download URL", async () => {
  const { isPublicRelease } = await policy();

  for (const url of [
    "",
    "BookCapture-0.5.1.apk",
    "/downloads/BookCapture-0.5.1.apk",
    "javascript:alert(1)",
    "data:text/plain,download",
    "ftp://example/BookCapture-0.5.1.apk",
    "https://example/%ZZ.apk",
  ]) {
    assert.equal(isPublicRelease({ channel: "stable", url }), false);
  }
  assert.equal(isPublicRelease({
    channel: "stable",
    url: "http://localhost/LibraryTool-Setup-0.5.1.exe",
  }), true);
  assert.equal(isPublicRelease({
    channel: "stable",
    url: "https://example/LibraryTool-Setup-0.5.1.exe",
  }), true);
});

test("invalid rows are filtered before newest-per-platform selection", () => {
  const source = fs.readFileSync(path.join(
    __dirname, "..", "website", "assets", "downloads.js"), "utf8");

  const filter = source.indexOf(".filter(isPublicRelease)");
  const selectNewest = source.indexOf("newest(rows)", filter);
  assert.notEqual(filter, -1);
  assert.notEqual(selectNewest, -1);
  assert.ok(filter < selectNewest);
});
