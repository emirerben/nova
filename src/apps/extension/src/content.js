// Nova Music Ingest — content script.
//
// MV3 doesn't let extensions push messages directly to a webpage's `window`
// (chrome.runtime.sendMessage works only inside extension contexts; the
// externally_connectable response channel is single-shot). The canonical
// pattern is:
//
//   extension SW / offscreen
//      |  chrome.runtime.sendMessage / chrome.tabs.sendMessage
//      ↓
//   THIS content script (runs in the Nova SPA's tab, isolated world)
//      |  window.postMessage  (page can listen via 'message' event)
//      ↓
//   Nova SPA (music-api.ts extensionIngest)
//
// Without this bridge, every "extracting / uploading / analyzing" progress
// event the offscreen doc emits is silently dropped before reaching the
// SPA — the SPA's promise never resolves and the user sees a forever spinner.

const NOVA_EVENT = "nova_ingest_event";

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== NOVA_EVENT) return;
  // Echo into the page world. Targeting our own origin (not "*") because the
  // payload contains the track_id from our admin API — no need to leak it to
  // every frame that might be embedded.
  window.postMessage(
    {
      type: NOVA_EVENT,
      stage: msg.stage,
      jobId: msg.jobId,
      payload: msg.payload,
    },
    window.location.origin,
  );
});

// Tiny readiness ping so the SPA can race a one-shot detection against the
// content script being injected (faster than chrome.runtime.sendMessage cold-
// start through the SW). The SPA can either ping the SW directly OR sniff
// this banner if it appeared before the SPA's detect call landed.
window.dispatchEvent(
  new CustomEvent("nova-extension-ready", { detail: { version: 1 } }),
);
