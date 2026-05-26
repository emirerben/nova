// Nova Music Ingest — MV3 service worker (coordinator only).
//
// All heavy work (ytdl extraction, googlevideo fetch, signed-URL PUT) runs in
// the offscreen document so it survives MV3's ~5-minute service-worker timeout.
// The SW is a pure message router between (a) the Nova SPA (via
// externally_connectable), (b) the popup, and (c) the offscreen doc.
//
// Message protocol:
//
//   SPA → SW         { target: "nova_extension", type: "ping" }
//                    → reply { ok: true, version }
//
//   SPA → SW         { target: "nova_extension", type: "ingest",
//                       payload: { url, title?, artist?, proxy_base } }
//                    → reply { ok: true } (just acknowledges pickup)
//                    → later: window.postMessage events tagged
//                      { type: "nova_ingest_event", stage, payload }
//
//   popup → SW       { target: "nova_extension", type: "ingest", ... }
//                    same flow as above
//
//   offscreen → SW   { target: "nova_extension", type: "progress",
//                       tabId, payload: IngestProgress }
//                    forwarded to the originating tab via tabs.sendMessage
//                    (popup also receives via runtime.onMessage broadcast).

const OFFSCREEN_URL = "offscreen.html";
const VERSION = chrome.runtime.getManifest().version;

// Runtime sender verification. externally_connectable.matches in the manifest
// already restricts WHICH ORIGINS can send messages, but not WHICH PATHS on
// those origins, and not which COMMANDS they can invoke. Defense in depth so
// a compromised non-admin Nova page (or a future regression of the manifest's
// `matches`) can't trigger privileged ingest work.
const ALLOWED_NOVA_ORIGINS = new Set([
  "https://nova-video.vercel.app",
  "http://localhost:3000",
]);
const ADMIN_PATH_PREFIX = "/admin/";
const ALLOWED_EXTERNAL_COMMANDS = new Set(["ping", "ingest"]);

function isAllowedSender(sender) {
  try {
    if (!sender?.url) return false;
    const u = new URL(sender.url);
    const origin = `${u.protocol}//${u.host}`;
    if (!ALLOWED_NOVA_ORIGINS.has(origin)) return false;
    if (!u.pathname.startsWith(ADMIN_PATH_PREFIX)) return false;
    return true;
  } catch {
    return false;
  }
}

function isSupportedYouTubeUrl(s) {
  if (typeof s !== "string" || !s) return false;
  try {
    const u = new URL(s);
    return /(^|\.)(youtube\.com|youtu\.be)$/i.test(u.hostname);
  } catch {
    return false;
  }
}

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["BLOBS"],
    justification:
      "Long-running fetch of YouTube audio stream and signed-URL PUT to GCS. " +
      "Service workers are killed after ~5 min of activity; an offscreen " +
      "DOM document is required to safely run multi-minute downloads.",
  });
}

// ── In-flight tracking so progress events can be routed back to the right tab.
//
//   { jobId: { tabId, sourceType: "spa" | "popup" } }
// jobId is generated per request and embedded in messages to/from offscreen
// so concurrent ingests (rare but possible if an admin opens two tabs) don't
// cross-pollute.
const inflight = new Map();

function newJobId() {
  return `j_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

async function dispatchIngest(payload, source, preferredJobId) {
  // If the caller (Nova SPA) provided a jobId, use it so the SPA's progress
  // listener can filter events that belong to its in-flight ingest vs ones
  // from another tab's concurrent ingest. Popup callers don't provide one.
  const jobId = preferredJobId || newJobId();
  inflight.set(jobId, source);
  await ensureOffscreen();
  // Fire-and-forget to offscreen; progress comes back via "progress" messages
  chrome.runtime.sendMessage({
    target: "offscreen",
    type: "ingest",
    jobId,
    payload,
  });
  return jobId;
}

function forwardProgress(jobId, event) {
  const source = inflight.get(jobId);
  if (!source) return;
  if (event.stage === "ready" || event.stage === "failed") {
    inflight.delete(jobId);
  }
  // The popup listens to runtime.onMessage directly, so it receives this
  // already via chrome.runtime.sendMessage broadcast. For SPA tabs we have
  // to forward over tabs.sendMessage AND also re-broadcast as a window
  // message (since the SPA listens to window.message events the way the
  // music-api helper expects).
  if (source.sourceType === "spa" && source.tabId != null) {
    chrome.tabs
      .sendMessage(source.tabId, {
        type: "nova_ingest_event",
        stage: event.stage,
        jobId,
        // include jobId inside payload too so window.postMessage receivers
        // can filter on it even if a wrapper strips top-level fields
        payload: { ...event, jobId },
      })
      .catch(() => {
        // Tab probably navigated away. Drop silently.
      });
  }
}

// External (from Nova SPA): handle pings + ingest requests.
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== "nova_extension") return;

  // Runtime sender + command verification. See top-of-file comment.
  if (!isAllowedSender(sender)) {
    sendResponse({ ok: false, error: "sender not allowed" });
    return false;
  }
  if (!ALLOWED_EXTERNAL_COMMANDS.has(msg.type)) {
    sendResponse({ ok: false, error: "unknown command" });
    return false;
  }

  if (msg.type === "ping") {
    sendResponse({ ok: true, version: VERSION });
    return false;
  }

  if (msg.type === "ingest") {
    const url = msg.payload?.url;
    if (!isSupportedYouTubeUrl(url)) {
      sendResponse({
        ok: false,
        error: "unsupported ingest URL (expected youtube.com or youtu.be)",
      });
      return false;
    }
    (async () => {
      try {
        await dispatchIngest(
          msg.payload || {},
          { sourceType: "spa", tabId: sender.tab?.id },
          msg.jobId,
        );
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
    })();
    return true; // async sendResponse
  }
});

// Internal (popup + offscreen): handle popup ingest requests + offscreen progress.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== "nova_extension") return;

  if (msg.type === "ingest") {
    (async () => {
      try {
        await dispatchIngest(msg.payload || {}, { sourceType: "popup" }, msg.jobId);
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
    })();
    return true;
  }

  if (msg.type === "progress" && msg.jobId) {
    forwardProgress(msg.jobId, msg.payload || { stage: "failed" });
    return false;
  }
});

console.log(`[nova-extension] background SW loaded v${VERSION}`);
