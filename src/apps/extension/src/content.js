// Kria Music Ingest — content script.
//
// MV3 doesn't let extensions push messages directly to a webpage's `window`
// (chrome.runtime.sendMessage works only inside extension contexts; the
// externally_connectable response channel is single-shot). The canonical
// pattern is:
//
//   extension SW / offscreen
//      |  chrome.runtime.sendMessage / chrome.tabs.sendMessage
//      ↓
//   THIS content script (runs in the Kria SPA's tab, isolated world)
//      |  window.postMessage  (page can listen via 'message' event)
//      ↓
//   Kria SPA (music-api.ts extensionIngest)
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

// Expose the extension ID to the Kria SPA via the DOM.
//
// Content scripts run in an isolated JavaScript world: globals written to
// `window` here are not visible to the page's own scripts. DOM mutations
// (attributes, elements) ARE visible to both worlds. The Kria SPA reads
// `document.documentElement.getAttribute("data-nova-extension-id")` from
// music-api.ts's detectExtension to know which extension ID to ping. This
// matters because the manifest's `key` is deferred — unpacked installs get
// random per-machine IDs, so the SPA cannot hardcode the ID.
//
// Also dispatch `nova-extension-ready` on document so the SPA can wait
// event-driven instead of polling. content_scripts[].run_at = "document_start"
// in the manifest covers the common case; the event is the safety net for
// slow profiles + reloads.
document.documentElement.setAttribute(
  "data-nova-extension-id",
  chrome.runtime.id,
);
document.dispatchEvent(
  new CustomEvent("nova-extension-ready", {
    detail: { extensionId: chrome.runtime.id, version: 1 },
  }),
);
