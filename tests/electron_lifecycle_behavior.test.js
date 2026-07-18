const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
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

function lifecycleApi() {
  const context = vm.createContext({});
  const helpers = block(
    mainSource,
    "function createSingleFlightGate()",
    "// --- End testable process/window lifecycle guards",
  );
  vm.runInContext(
    `${helpers}\nthis.api = { createSingleFlightGate, superviseChildProcess };`,
    context,
  );
  return context.api;
}

test("close confirmation gate permits only one in-flight request", () => {
  const gate = lifecycleApi().createSingleFlightGate();
  assert.equal(gate.isActive(), false);
  assert.equal(gate.enter(), true);
  assert.equal(gate.isActive(), true);
  assert.equal(gate.enter(), false);
  gate.leave();
  assert.equal(gate.isActive(), false);
  assert.equal(gate.enter(), true);
});

test("spawn errors reject startup immediately through one path", async () => {
  const child = new EventEmitter();
  const neverReady = new Promise(() => {});
  let unexpected = 0;
  const startup = lifecycleApi().superviseChildProcess(
    child, neverReady, () => { unexpected += 1; });

  child.emit("error", new Error("ENOENT"));

  await assert.rejects(startup, /Could not launch the backend: ENOENT/);
  assert.equal(unexpected, 0);
});

test("an exit before readiness rejects startup even with code zero", async () => {
  const child = new EventEmitter();
  const neverReady = new Promise(() => {});
  let unexpected = 0;
  const startup = lifecycleApi().superviseChildProcess(
    child, neverReady, () => { unexpected += 1; });

  child.emit("exit", 0, null);

  await assert.rejects(startup, /before it became ready \(code 0\)/);
  assert.equal(unexpected, 0);
});

test("any exit after readiness is reported once, including code zero", async () => {
  const child = new EventEmitter();
  const ends = [];
  await lifecycleApi().superviseChildProcess(
    child, Promise.resolve(), (end) => ends.push(end));

  child.emit("exit", 0, null);
  child.emit("error", new Error("late duplicate"));

  assert.equal(ends.length, 1);
  assert.equal(ends[0].type, "exit");
  assert.equal(ends[0].code, 0);
});

test("Electron main wires the tested guards into startup and close", () => {
  assert.match(mainSource, /superviseChildProcess\(sidecar, waitForServer/);
  assert.match(mainSource, /if \(!closeConfirmGate\.enter\(\)\) return;/);
  assert.match(mainSource, /\.finally\(\(\) => closeConfirmGate\.leave\(\)\)/);
  assert.doesNotMatch(mainSource, /if \(code && !app\.isQuitting\)/);
});
