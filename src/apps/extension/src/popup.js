// Popup UI for the Kria Music Ingest extension. The primary entry point is the
// Kria admin SPA itself (which calls in via externally_connectable); this popup
// is a debugging / standalone fallback AND the place admins enter their Kria
// admin BasicAuth credentials (used by offscreen.js when calling /api/admin/*).

import {
  isAllowedKriaApiOrigin,
  allowedOriginsList,
} from "./lib/origin-allowlist.js";
import { encodeBasicAuthHeader } from "./lib/basic-auth.js";

const STORAGE_KEY = "nova_popup_v1";
const ORIGIN_KEY = "nova_api_origin";
const ADMIN_USER_KEY = "nova_admin_user";
const ADMIN_PASS_KEY = "nova_admin_pass";

const $url = document.getElementById("url");
const $title = document.getElementById("title");
const $origin = document.getElementById("origin");
const $adminUser = document.getElementById("admin-user");
const $adminPass = document.getElementById("admin-pass");
const $testConn = document.getElementById("test-conn");
const $run = document.getElementById("run");
const $status = document.getElementById("status");

(async function restore() {
  const stored = await chrome.storage.local.get([
    STORAGE_KEY,
    ORIGIN_KEY,
    ADMIN_USER_KEY,
    ADMIN_PASS_KEY,
  ]);
  const s = stored[STORAGE_KEY] || {};
  if (s.url) $url.value = s.url;
  if (s.title) $title.value = s.title;
  $origin.value = stored[ORIGIN_KEY] || "https://usekria.com";
  $adminUser.value = stored[ADMIN_USER_KEY] || "";
  $adminPass.value = stored[ADMIN_PASS_KEY] || "";
})();

function log(line, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  const ts = new Date().toLocaleTimeString("en-GB", { hour12: false });
  div.textContent = `[${ts}] ${line}`;
  $status.appendChild(div);
  $status.scrollTop = $status.scrollHeight;
}

async function saveSettings() {
  await chrome.storage.local.set({
    [ORIGIN_KEY]: $origin.value.trim() || "https://usekria.com",
    [ADMIN_USER_KEY]: $adminUser.value,
    [ADMIN_PASS_KEY]: $adminPass.value,
  });
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type !== "progress") return;
  const { stage, detail, percent, track_id } = msg.payload || {};
  const pct = percent != null ? ` (${Math.round(percent * 100)}%)` : "";
  const cls = stage === "failed" ? "err" : stage === "ready" ? "ok" : null;
  log(`${stage}${pct}${detail ? `  ${detail}` : ""}${track_id ? `  [${track_id}]` : ""}`, cls);
});

$testConn.addEventListener("click", async () => {
  const origin = $origin.value.trim();
  const user = $adminUser.value;
  const pass = $adminPass.value;

  // SECURITY: validate the origin BEFORE persisting it to chrome.storage.
  // Otherwise a mistyped or hostile origin would be saved and later read by
  // offscreen.js (which has its own allowlist re-check, so credentials still
  // wouldn't leak, but stale bad state is a UX papercut and a foot-gun for
  // future code paths that might trust the stored origin).
  if (!isAllowedKriaApiOrigin(origin)) {
    log(
      `Kria API origin not allowlisted: ${origin}. Allowed: ${allowedOriginsList().join(", ")}.`,
      "err",
    );
    return;
  }
  if (!user || !pass) {
    log("Enter Kria admin username AND password first.", "err");
    return;
  }
  await saveSettings();
  $testConn.disabled = true;
  log(`Testing ${origin}/api/admin/music-tracks?limit=1 ...`);
  try {
    const auth = encodeBasicAuthHeader(user, pass);
    const resp = await fetch(`${origin}/api/admin/music-tracks?limit=1`, {
      method: "GET",
      headers: { Authorization: auth, Accept: "application/json" },
      credentials: "omit",
    });
    if (resp.status === 200) {
      log("OK — credentials accepted.", "ok");
    } else if (resp.status === 401) {
      log("401 — invalid credentials.", "err");
    } else {
      log(`Unexpected status ${resp.status}.`, "err");
    }
  } catch (err) {
    log(`Test failed: ${String(err)}`, "err");
  } finally {
    setTimeout(() => ($testConn.disabled = false), 800);
  }
});

$run.addEventListener("click", async () => {
  const url = $url.value.trim();
  if (!url) {
    log("Enter a YouTube URL first", "err");
    return;
  }
  const origin = $origin.value.trim() || "https://usekria.com";
  await chrome.storage.local.set({
    [STORAGE_KEY]: { url, title: $title.value },
    [ORIGIN_KEY]: origin,
    [ADMIN_USER_KEY]: $adminUser.value,
    [ADMIN_PASS_KEY]: $adminPass.value,
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
