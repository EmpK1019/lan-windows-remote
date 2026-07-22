import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import {fileURLToPath} from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "web", "index.html"), "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
assert.ok(scripts.length > 0, "web/index.html must contain an inline script");
const pageScript = scripts.at(-1)[1];

// Parse the real page script so malformed JavaScript fails this test immediately.
new vm.Script(pageScript, {filename: "web/index.html"});

const transportStart = pageScript.indexOf("const INPUT_REQUEST_TIMEOUT_MS");
const transportEnd = pageScript.indexOf("async function lockRemoteWorkstation", transportStart);
assert.ok(transportStart >= 0 && transportEnd > transportStart, "input transport source was not found");
const transportSource = pageScript.slice(transportStart, transportEnd);

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function hangingResponse(signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    signal.addEventListener("abort", () => reject(abortError()), {once: true});
  });
}

function createHarness(fetchImpl) {
  const statuses = [];
  const context = vm.createContext({
    AbortController,
    JSON,
    Promise,
    clearTimeout,
    setTimeout,
    fetch: fetchImpl,
    endpoint: value => value,
    setRemoteStatus: (message, failed) => statuses.push({message, failed}),
    state: {
      inputGeneration: 1,
      inputAbortController: null,
      inputAbortReason: "",
      inputInFlightType: "",
      pendingMouseMove: null,
      mouseMoveQueued: false,
      inputQueue: Promise.resolve(false),
      session: {connected: true, viewOnly: false, token: "test-token"}
    }
  });
  new vm.Script(
    `${transportSource}\nglobalThis.__transport = {resetInputTransport, sendInput};`,
    {filename: "input-transport-excerpt.js"}
  ).runInContext(context);
  return {context, statuses, api: context.__transport};
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

{
  const payloadTypes = [];
  const harness = createHarness((url, options) => {
    const payload = JSON.parse(options.body);
    payloadTypes.push(payload.type);
    if (payloadTypes.length === 1) return hangingResponse(options.signal);
    return Promise.resolve({ok: true, status: 200});
  });

  const staleMove = harness.api.sendInput({type: "mouse_move", x: 20, y: 30});
  await delay(0);
  const keyResult = await harness.api.sendInput({type: "key_down", key: "Escape", code: "Escape"});
  const staleMoveResult = await staleMove;

  assert.equal(staleMoveResult, false, "a superseded mouse move should be cancelled");
  assert.equal(keyResult, true, "a critical key event must pass a stalled mouse move");
  assert.deepEqual(payloadTypes, ["mouse_move", "key_down"]);
  assert.equal(harness.statuses.length, 0, "superseding stale motion is not an error");
}

{
  let requestCount = 0;
  const harness = createHarness((url, options) => {
    requestCount += 1;
    if (requestCount === 1) return hangingResponse(options.signal);
    return Promise.resolve({ok: true, status: 200});
  });

  const startedAt = Date.now();
  const timedOut = await harness.api.sendInput({type: "key_down", key: "F1", code: "F1"});
  const elapsed = Date.now() - startedAt;
  const recovered = await harness.api.sendInput({type: "key_up", key: "F1", code: "F1"});

  assert.equal(timedOut, false, "a non-responsive input request should time out");
  assert.ok(elapsed >= 1500 && elapsed < 3500, `timeout should be bounded, observed ${elapsed} ms`);
  assert.equal(recovered, true, "the queue must continue after a timed-out request");
  assert.ok(harness.statuses.some(item => item.failed), "the timeout should be visible to the user");
}

{
  const fallbackStart = pageScript.indexOf("async function finishNativeFallback");
  const fallbackEnd = pageScript.indexOf("async function pollNativeVideoStatus", fallbackStart);
  assert.ok(fallbackStart >= 0 && fallbackEnd > fallbackStart, "native fallback source was not found");
  const fallbackSource = pageScript.slice(fallbackStart, fallbackEnd);
  const order = [];
  const stage = {style: {}};
  const image = {
    naturalWidth: 1920,
    naturalHeight: 1080,
    onload: null,
    onerror: null,
    set src(value) {
      this.currentSrc = value;
      order.push("snapshot_loaded");
      this.onload?.();
    }
  };
  const session = {connected: true, monitorId: "all", scaleMode: "fit"};
  const state = {
    session,
    frameRequestRevision: 3,
    screenAbortController: null,
    frameObjectUrl: "",
    nativeFallbackPending: false,
    nativeVideoActive: true,
    nativeVideoRevision: 1,
    nativeVideoStatusTimer: 0,
    nativeVideoDiagnostics: null,
    loadingFrame: false,
    refreshTimer: 0
  };
  const context = vm.createContext({
    AbortController,
    Promise,
    clearTimeout,
    setTimeout,
    state,
    endpoint: path => path,
    fetch: async () => ({ok: true, status: 200, blob: async () => ({type: "image/jpeg"})}),
    URL: {
      createObjectURL: () => "blob:secure-frame",
      revokeObjectURL: () => order.push("snapshot_revoked")
    },
    $: id => id === "remoteStage" ? stage : image,
    document: {body: {classList: {remove: () => order.push("native_class_removed")}}},
    window: {pywebview: {api: {
      configure_native_video: async () => {
        order.push("native_stop_start");
        await Promise.resolve();
        order.push("native_stop_done");
        return true;
      },
      set_native_overlay_state: async () => order.push("native_overlay_hidden")
    }}},
    releaseFrameObjectUrl: () => {},
    sessionScaleMode: () => "fit",
    sessionControlFps: () => 60,
    markScreenReady: () => order.push("snapshot_ready"),
    setRemoteStatus: () => {},
    startDesktopScreenStream: (activeSession, options) => {
      assert.equal(activeSession, session);
      assert.equal(options.firstFrameReady, true);
      order.push("stream_start");
    },
    scheduleNativeVideoRetry: () => order.push("native_retry_scheduled")
  });
  new vm.Script(
    `${fallbackSource}\nglobalThis.__fallback = {startMjpegFallback};`,
    {filename: "native-fallback-excerpt.js"}
  ).runInContext(context);
  await context.__fallback.startMjpegFallback(session, "secure desktop requested", {
    secureTransition: true,
    preserveNative: true
  });
  assert.equal(stage.style.backgroundImage, 'url("blob:secure-frame")');
  assert.ok(
    order.indexOf("snapshot_ready") < order.indexOf("native_stop_start"),
    "a valid secure frame must be visible before native video is stopped"
  );
  assert.ok(
    order.indexOf("native_stop_done") < order.indexOf("stream_start"),
    "MJPEG must start only after the native child window has stopped"
  );
}

console.log("INPUT_TRANSPORT_RUNTIME_TESTS_OK");
