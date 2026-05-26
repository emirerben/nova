# Nova Music Ingest extension

Production Chrome extension that lets admins ingest YouTube tracks into Nova
through their **own browser** instead of through Nova's data-center IP. Fly.io
gets flagged by YouTube as automated traffic; admin residential IPs do not.

Plan: `~/.claude/plans/sen-k-demli-bir-yaz-l-m-rosy-acorn.md`

## Architecture (one paragraph)

The extension is two contexts. A Manifest V3 **service worker** routes messages
between the Nova SPA (via `externally_connectable`) and an **offscreen
document** that does the heavy work. Offscreen runs `youtubei.js` (web build,
pure-JS AST interpreter — MV3 CSP friendly) to extract the audio stream URL,
fetches the bytes from `*.googlevideo.com` (CORS bypassed via the manifest's
`host_permissions`), POSTs `/api/admin/music-tracks/upload-init` through the
Nova proxy to mint a signed GCS PUT URL, uploads the blob directly to GCS, and
calls `/upload-confirm` to trigger the Celery analysis task. Total touch points
on Nova itself: two tiny JSON requests through the existing admin proxy. The
bytes never see Vercel.

The offscreen doc, not the service worker, holds the network operation —
service workers get killed after ~5 minutes of activity, which kills long
downloads. Offscreen documents are kept alive by their declared `reasons`
(here, `BLOBS`).

## Build + load locally

```bash
cd src/apps/extension
npm install --legacy-peer-deps
npm run build
```

Then open `chrome://extensions`, enable Developer mode, click "Load unpacked",
and pick `src/apps/extension/dist/`.

After loading, click the extension icon to open the popup and enter the Nova
admin BasicAuth username + password (same creds the browser prompts you for on
`/admin/*` pages). The "Test connection" button validates them locally without
making a network request to non-allowlisted origins. Without creds set, every
`/api/admin/*` call from the extension's offscreen doc will 401 against the
middleware gate.

## Rebuilding the admin install zip

`/admin/extension/install` distributes a pre-built zip so admins don't have to
clone the repo. When extension source changes, refresh it with:

```bash
./scripts/build-admin-extension-zip.sh
```

The script (1) builds the extension, (2) runs a secrets audit (gitleaks if
available + a hard pattern grep on the source tree — exits non-zero on any
match), (3) zips `dist/*` with `manifest.json` at archive root, (4) asserts
the packaged manifest version equals the source manifest version, and (5)
writes `src/apps/web/public/admin/extension/extension-info.json` with version,
build timestamp, and source commit. Output goes to
`src/apps/web/public/admin/extension/nova-extension.zip` (committed).

The install page itself lives behind the BasicAuth middleware gate, so only
authenticated admins can download the zip.

CI automation of this rebuild is a follow-up — for now, rerun the script
after editing anything under `src/apps/extension/src/`. `manifest.key` is
intentionally deferred; when Phase 2 distribution lands, the middleware's
`chrome-extension://*` CSRF allowance can lock to a single deterministic
extension ID.

For dev with the Nova SPA at `http://localhost:3000`:

1. Open `chrome://extensions`, click the Nova extension's "Details", and copy
   the extension ID.
2. In Nova: `window.__NOVA_EXTENSION_ID__ = "<extension-id>"` (set via a dev
   helper, or by injecting a content script — Phase 2 packaging makes this
   automatic via a deterministic `key`-derived ID).
3. Hit the admin music page → URL tab → "Ingest via extension".

## Production packaging (Phase 2)

Self-hosted CRX + `update_url` is the deploy story (see plan §"Phase 2"):

- `scripts/package-extension.sh` produces `.crx` + `updates.xml` from `dist/`.
- Files get served from `public/admin/extension/` on `nova-video.vercel.app`.
- The manifest's `update_url` points at the served `updates.xml`; Chrome
  auto-checks daily.

This bypasses Chrome Web Store review (overkill for ~5 internal admins) while
keeping admin upgrades automatic.

## Cross-references in this repo

- Server endpoints: `src/apps/api/app/routes/admin_music.py` →
  `browser_upload_init` and `browser_upload_confirm`.
- Server tests: `src/apps/api/tests/routes/test_admin_music_browser_upload.py`.
- SPA client helpers: `src/apps/web/src/lib/music-api.ts` →
  `detectExtension`, `extensionUploadInit`, `extensionUploadConfirm`,
  `extensionIngest`.
- SPA UI: `src/apps/web/src/app/admin/music/page.tsx` ("Ingest via extension"
  button + 3-stage progress widget).
- Phase 0 spike (throwaway, .gitignored): `tools/nova-ytdl-spike/`.
