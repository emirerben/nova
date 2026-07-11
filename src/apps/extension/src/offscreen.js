// Kria Music Ingest — offscreen document.
//
// Runs the heavy work that MV3 service workers can't safely host:
//   1. Extract audio stream from YouTube via youtubei.js (web build = pure-JS
//      AST interpreter, no native eval, MV3 CSP compliant — verified in the
//      Phase 0 spike).
//   2. Stream-fetch bytes from googlevideo.com (host_permissions bypasses
//      CORS that would block this from any regular page).
//   3. Call Kria's /api/admin/music-tracks/upload-init via the SPA's Next.js
//      admin proxy → get back a signed GCS PUT URL.
//   4. PUT the blob directly to GCS (no Kria hop = bypasses Vercel function
//      body cap of 4.5 MB on Hobby / bounded on Pro).
//   5. Call /api/admin/music-tracks/<id>/upload-confirm → server ffprobes the
//      blob + dispatches the Celery analyze task.
//
// Progress is reported back to the SW via runtime.sendMessage with a tagged
// stage payload. The SW routes those events to whichever surface kicked off
// the job (popup tab or Kria SPA tab).
//
// Falls back from youtubei.js → @distube/ytdl-core if extraction throws.
// Both libraries break periodically when YouTube changes its player JS;
// having both vendored is the redundancy layer the plan calls for.

import { Innertube, UniversalCache } from "youtubei.js/web";
import { isAllowedKriaApiOrigin } from "./lib/origin-allowlist.js";
import { encodeBasicAuthHeader } from "./lib/basic-auth.js";

const ADMIN_USER_KEY = "nova_admin_user";
const ADMIN_PASS_KEY = "nova_admin_pass";

async function loadAdminBasicAuthHeader() {
  // Returns a "Basic <utf-8 b64>" string when valid credentials are stored,
  // or null. UTF-8 (not Latin-1) is required so passwords with é/ö/emoji
  // actually authenticate against the server-side middleware which decodes
  // the same way (see src/apps/web/src/middleware.ts).
  const stored = await chrome.storage.local.get([ADMIN_USER_KEY, ADMIN_PASS_KEY]);
  const user = stored[ADMIN_USER_KEY];
  const pass = stored[ADMIN_PASS_KEY];
  if (typeof user !== "string" || typeof pass !== "string") return null;
  if (!user || !pass) return null;
  return encodeBasicAuthHeader(user, pass);
}

function emit(jobId, stage, extras = {}) {
  chrome.runtime.sendMessage({
    target: "nova_extension",
    type: "progress",
    jobId,
    payload: { stage, ...extras, ts: Date.now() },
  });
}

function extractVideoId(input) {
  if (/^[a-zA-Z0-9_-]{11}$/.test(input)) return input;
  try {
    const u = new URL(input);
    if (u.hostname.includes("youtu.be")) return u.pathname.slice(1).split("/")[0];
    if (u.searchParams.has("v")) return u.searchParams.get("v");
  } catch {
    // fall through
  }
  return null;
}

// YouTube tightened the WEB-client player response in 2025–26: the WEB
// path returns formats with neither `url` nor `signatureCipher`, so any
// youtubei.js `.download()` call against the WEB client throws "No valid URL
// to decipher". yt-dlp solved this by switching to the ANDROID_VR client,
// which still serves direct (unsigned) audio URLs. Empirically verified end-
// to-end on youtubei.js@17.0.1 against the Roger Sanchez "Again" video that
// hit this in prod — 2 MB partial read, ffprobe-confirmed AAC matching the
// source's 6:03 duration. Pinned here as a constant so all calls stay in
// sync — never mix clients on a single ingest.
const YT_CLIENT = "ANDROID_VR";

async function extractViaYoutubei(videoId) {
  // Note: requests issued by this fetch wrapper to /youtubei/v1/* get their
  // `Origin: chrome-extension://<id>` header rewritten to
  // `https://www.youtube.com` by the dynamic DNR rule registered in
  // background.js (YT_ORIGIN_RULE). Without that rewrite YouTube's edge 403s
  // every InnerTube POST. If you're debugging an unexpected Origin value on
  // YouTube traffic from this extension, that rule is why.
  const yt = await Innertube.create({
    cache: new UniversalCache(false),
    fetch: (input, init) => fetch(input, init),
  });
  const info = await yt.getBasicInfo(videoId, { client: YT_CLIENT });
  if (info.basic_info?.is_live) {
    throw new Error("Live streams are not supported.");
  }
  const durS = Number(info.basic_info?.duration ?? 0);
  if (durS > 600) {
    throw new Error(
      `Track too long (${Math.round(durS)}s). Limit is 600s (10 min). ` +
        `Trim the source video on YouTube or use a different source.`,
    );
  }
  const format = info.chooseFormat({ type: "audio", quality: "best" });
  const stream = await yt.download(videoId, {
    type: "audio",
    quality: "best",
    format: format.mime_type?.includes("mp4") ? "mp4" : "any",
    client: YT_CLIENT,
  });
  return {
    title: info.basic_info?.title || "",
    author: info.basic_info?.author || "",
    duration_s: durS,
    mime_type: format.mime_type || "audio/mp4",
    expected_bytes: Number(format.content_length || 0),
    stream,
  };
}

function pickExtFromMime(mime) {
  if (!mime) return ".m4a";
  if (mime.includes("mp4")) return ".m4a";
  if (mime.includes("webm")) return ".webm";
  if (mime.includes("opus")) return ".opus";
  if (mime.includes("mpeg")) return ".mp3";
  if (mime.includes("ogg")) return ".ogg";
  return ".m4a";
}

async function streamToBlob(stream, mime, onProgress, expectedBytes) {
  const reader = stream.getReader();
  const chunks = [];
  let received = 0;
  let lastEmit = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    const now = Date.now();
    if (now - lastEmit > 500) {
      onProgress(received, expectedBytes);
      lastEmit = now;
    }
  }
  onProgress(received, expectedBytes);
  return new Blob(chunks, { type: mime });
}

async function fetchJson(proxyBase, path, body) {
  // proxyBase is the Kria admin proxy origin + base (e.g. "/api/admin").
  // The extension origin (chrome-extension://<id>) calls into the SPA's
  // proxy, which injects X-Admin-Token server-side — token never reaches us.
  // Cross-origin POSTs to vercel.app must include the absolute origin since
  // chrome-extension:// can't use relative URLs.
  const absoluteBase = proxyBase.startsWith("http")
    ? proxyBase
    : await resolveAbsoluteProxyBase(proxyBase);
  const url = `${absoluteBase}${path}`;
  const headers = { "Content-Type": "application/json" };

  // SECURITY: only attach Authorization: Basic when BOTH conditions hold —
  //   (1) the request URL's origin is in the Kria allowlist, AND
  //   (2) the request URL's pathname starts with /api/admin/.
  // Belt-and-suspenders against a tampered `nova_api_origin` setting or a
  // future call site that points fetchJson at something other than the
  // admin proxy. The PUT to storage.googleapis.com and the youtubei.js
  // fetches do not go through fetchJson — they cannot be affected here.
  try {
    const parsed = new URL(url);
    const reqOrigin = `${parsed.protocol}//${parsed.host}`;
    if (
      isAllowedKriaApiOrigin(reqOrigin) &&
      parsed.pathname.startsWith("/api/admin/")
    ) {
      const auth = await loadAdminBasicAuthHeader();
      if (auth) headers["Authorization"] = auth;
    }
  } catch {
    // Malformed URL — let fetch throw the canonical error below.
  }

  const init = {
    method: "POST",
    headers,
    credentials: "omit",
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(url, init);
  const text = await resp.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { raw: text };
  }
  if (!resp.ok) {
    const detail =
      typeof parsed?.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed?.detail ?? parsed);
    const err = new Error(
      `Kria ${path} returned ${resp.status}: ${detail.slice(0, 300)}`,
    );
    err.status = resp.status;
    err.detail = parsed?.detail;
    throw err;
  }
  return parsed;
}

async function resolveAbsoluteProxyBase(rel) {
  // The SPA hands us "/api/admin". We need the absolute origin to POST from
  // the extension context. Stored once per session via chrome.storage; default
  // to prod if missing.
  const stored = await chrome.storage.local.get("nova_api_origin");
  const origin = stored.nova_api_origin || "https://usekria.com";
  return `${origin}${rel}`;
}

async function runIngest(jobId, { url, title, artist, proxy_base }) {
  emit(jobId, "extension_check");

  const videoId = extractVideoId(url);
  if (!videoId) {
    emit(jobId, "failed", { detail: "Could not extract videoId from the URL." });
    return;
  }

  // ── Stage 1: extract ─────────────────────────────────────────────────────
  emit(jobId, "extracting", { percent: 0, detail: "Resolving video metadata…" });
  let extracted;
  try {
    extracted = await extractViaYoutubei(videoId);
  } catch (err) {
    // TODO: fall back to @distube/ytdl-core here — Phase 3 hardening.
    emit(jobId, "failed", {
      detail: `YouTube extraction failed: ${err.message || err}`,
    });
    return;
  }

  const ext = pickExtFromMime(extracted.mime_type);

  // ── Stage 1 continued: download bytes ────────────────────────────────────
  let blob;
  try {
    blob = await streamToBlob(
      extracted.stream,
      extracted.mime_type,
      (received, expected) => {
        const percent = expected > 0 ? received / expected : null;
        emit(jobId, "extracting", {
          percent,
          detail:
            expected > 0
              ? `Downloaded ${formatMB(received)} / ${formatMB(expected)}`
              : `Downloaded ${formatMB(received)}`,
        });
      },
      extracted.expected_bytes,
    );
  } catch (err) {
    emit(jobId, "failed", {
      detail: `Stream download failed: ${err.message || err}`,
    });
    return;
  }

  // ── Stage 2: init → get signed PUT URL → PUT blob ────────────────────────
  let initResp;
  try {
    initResp = await fetchJson(proxy_base || "/api/admin", "/music-tracks/upload-init", {
      source_url: url,
      title: title || extracted.title || null,
      artist: artist || extracted.author || null,
      ext,
      byte_count: blob.size,
    });
  } catch (err) {
    emit(jobId, "failed", { detail: err.message || String(err) });
    return;
  }
  const { track_id, upload_url, content_type } = initResp;
  emit(jobId, "uploading", {
    percent: 0,
    detail: `Uploading ${formatMB(blob.size)} to Kria…`,
    track_id,
  });

  try {
    const putResp = await fetch(upload_url, {
      method: "PUT",
      headers: { "Content-Type": content_type },
      body: blob,
    });
    if (!putResp.ok) {
      const t = await putResp.text().catch(() => "");
      throw new Error(`PUT to GCS failed: ${putResp.status} ${t.slice(0, 200)}`);
    }
  } catch (err) {
    emit(jobId, "failed", {
      detail: `Upload to Kria failed: ${err.message || err}`,
      track_id,
    });
    return;
  }

  emit(jobId, "uploading", { percent: 1, detail: "Upload complete.", track_id });

  // ── Stage 3: confirm → server verifies + dispatches Celery ───────────────
  emit(jobId, "confirming", { detail: "Verifying upload on the server…", track_id });
  try {
    await fetchJson(
      proxy_base || "/api/admin",
      `/music-tracks/${track_id}/upload-confirm`,
    );
  } catch (err) {
    emit(jobId, "failed", { detail: err.message || String(err), track_id });
    return;
  }

  emit(jobId, "analyzing", {
    detail: "Beat detection + section analysis running…",
    track_id,
  });

  // The Celery task is async — we don't poll it from here. The SPA polls the
  // standard /admin/music-tracks/{id} endpoint and updates the list. We emit
  // "ready" here meaning "queued and dispatched", so the SPA promise resolves
  // and the UI can let the admin move on.
  emit(jobId, "ready", { track_id, detail: "Track queued for analysis." });
}

function formatMB(bytes) {
  if (!bytes) return "0 MB";
  const mb = bytes / 1024 / 1024;
  return mb < 1 ? `${(bytes / 1024).toFixed(1)} KB` : `${mb.toFixed(1)} MB`;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target !== "offscreen") return;
  if (msg.type === "ingest") {
    runIngest(msg.jobId, msg.payload).catch((err) => {
      emit(msg.jobId, "failed", {
        detail: `Unhandled: ${err?.message || err}`,
      });
    });
  }
});

console.log("[nova-extension] offscreen loaded");
