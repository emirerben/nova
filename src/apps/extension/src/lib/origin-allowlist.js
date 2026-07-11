// Shared origin allowlist for the Kria Music Ingest extension.
//
// Imported by popup.js (gates "Test connection") and offscreen.js (gates
// whether Authorization: Basic gets attached to outgoing /api/admin/* calls).
// Defense in depth so a tampered or mistyped `nova_api_origin` setting in
// chrome.storage.local cannot trick the extension into shipping the admin
// BasicAuth credential to an attacker-controlled origin.

const ALLOWED = new Set([
  "https://usekria.com",
  "https://nova-video.vercel.app",
  "http://localhost:3000",
]);

export function isAllowedKriaApiOrigin(originOrUrl) {
  try {
    const u = new URL(originOrUrl);
    return ALLOWED.has(`${u.protocol}//${u.host}`);
  } catch {
    return false;
  }
}

export function allowedOriginsList() {
  return Array.from(ALLOWED);
}
