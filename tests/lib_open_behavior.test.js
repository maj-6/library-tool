// The desktop .lib open flow's argv parsing (desktop/main.js): a double-
// clicked .lib arrives as an argv entry (first launch) or via second-instance
// argv, mixed in with Chromium switches. Exercised under plain Node the same
// way the lifecycle guards are: the helper is extracted from the source and
// run against a fake fs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const mainSource = fs.readFileSync(
  path.join(__dirname, "..", "desktop", "main.js"), "utf8");

function block(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function libArgvApi(existingFiles) {
  const files = new Set(existingFiles.map((p) => path.resolve(p)));
  const context = vm.createContext({
    path,
    process: { cwd: () => path.resolve("/fallback") },
    fs: {
      statSync: (p) => {
        if (files.has(path.resolve(p))) return { isFile: () => true };
        throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      },
    },
  });
  const helper = block(
    mainSource, "function libPathFromArgv(", "function flushLibOpen(");
  vm.runInContext(`${helper}\nthis.api = { libPathFromArgv };`, context);
  return context.api;
}

test("a trailing .lib path resolves against the caller's cwd", () => {
  const p = path.resolve("/books/herbal.lib");
  const { libPathFromArgv } = libArgvApi([p]);
  assert.equal(libPathFromArgv(["app.exe", "herbal.lib"], path.resolve("/books")), p);
  assert.equal(libPathFromArgv(["app.exe", p], path.resolve("/elsewhere")), p);
});

test("switches, non-lib args, and argv[0] never match", () => {
  const p = path.resolve("/books/x.lib");
  const { libPathFromArgv } = libArgvApi([p]);
  // argv[0] is the exe even if it somehow ends in .lib
  assert.equal(libPathFromArgv([p], path.resolve("/books")), null);
  assert.equal(
    libPathFromArgv(["app.exe", "--squirrel-firstrun.lib"], path.resolve("/")),
    null);
  assert.equal(libPathFromArgv(["app.exe", "notes.txt"], path.resolve("/")), null);
});

test("a .lib that is not on disk is not delivered", () => {
  const { libPathFromArgv } = libArgvApi([]);
  assert.equal(
    libPathFromArgv(["app.exe", "ghost.lib"], path.resolve("/books")), null);
});

test("the last existing .lib argument wins", () => {
  const a = path.resolve("/books/a.lib");
  const b = path.resolve("/books/b.lib");
  const { libPathFromArgv } = libArgvApi([a, b]);
  assert.equal(
    libPathFromArgv(["app.exe", "a.lib", "b.lib"], path.resolve("/books")), b);
});
