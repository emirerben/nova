// Popup UI for the Nova Music Ingest extension. The primary entry point is the
// Nova admin SPA itself (which calls in via externally_connectable); this popup
// is a debugging / standalone fallback.

const STORAGE_KEY = "nova_popup_v1";
const ORIGIN_KEY = "nova_api_origin";

const $url = document.getElementById("url");
const $title = document.getElementById("title");
const $origin = document.getElementById("origin");
const $run = document.getElementById("run");
const $status = document.getElementById("status");

(async function restore() {
  const stored = await chrome.storage.local.get([STORAGE_KEY, ORIGIN_KEY]);
  const s = stored[STORAGE_KEY] || {};
  if (s.url) $url.value = s.url;
  if (s.title) $title.value = s.title;
  $origin.value = stored[ORIGIN_KEY] || "https://nova-video.vercel.app";
})();

function log(line, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  const ts = new Date().toLocaleTimeString("en-GB", { hour12: false });
  div.textContent = `[${ts}] ${line}`;
  $status.appendChild(div);
  $status.scrollTop = $status.scrollHeight;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type !== "progress") return;
  const { stage, detail, percent, track_id } = msg.payload || {};
  const pct = percent != null ? ` (${Math.round(percent * 100)}%)` : "";
  const cls = stage === "failed" ? "err" : stage === "ready" ? "ok" : null;
  log(`${stage}${pct}${detail ? `  ${detail}` : ""}${track_id ? `  [${track_id}]` : ""}`, cls);
});

$run.addEventListener("click", async () => {
  const url = $url.value.trim();
  if (!url) {
    log("Enter a YouTube URL first", "err");
    return;
  }
  const origin = $origin.value.trim() || "https://nova-video.vercel.app";
  await chrome.storage.local.set({
    [STORAGE_KEY]: { url, title: $title.value },
    [ORIGIN_KEY]: origin,
  });
  $status.innerHTML = "";
  $run.disabled = true;
  log(`Submitting: ${url}`);
  try {
    const resp = await chrome.runtime.sendMessage({
      target: "nova_extension",
      type: "ingest",
      payload: {
        url,
        title: $title.value || undefined,
        proxy_base: `${origin}/api/admin`,
      },
    });
    if (!resp?.ok) log(`SW rejected: ${resp?.error || "unknown"}`, "err");
  } catch (err) {
    log(`Send failed: ${String(err)}`, "err");
  } finally {
    setTimeout(() => ($run.disabled = false), 1500);
  }
});
