# Nova infra

## GCS browser upload CORS (`gcs-cors.json`)

Allows production and local web origins to upload directly to signed GCS URLs.
The `x-goog-if-generation-match` header makes each upload key create-only; keep
it in the allowlist or browsers will fall back to the slower API relay (where
the uploader permits it — see the preview-domain note below).

```bash
gcloud storage buckets update gs://$STORAGE_BUCKET --cors-file=infra/gcs-cors.json
gcloud storage buckets describe gs://$STORAGE_BUCKET --format='default(cors_config)'
```

Apply this configuration before enabling a web build that sends the precondition
header. Vercel preview domains are intentionally excluded. Generative uploads
there still fall back to the relay, but plan-path uploads (v0.22.4.0) gate the
relay on every origin: local-dev hosts (`localhost`, `*.local`, `*.ts.net`,
RFC-1918 LAN addresses — see `isLocalDevHost` in `src/apps/web/src/lib/plan-api.ts`)
relay unconditionally; everywhere else — previews and production alike — only
files ≤4MB relay, because the Vercel proxy caps request bodies at ~4.5MB. Larger
clips whose direct PUT fails surface a retry message instead of dying cryptically
mid-relay (tracked in TODOS.md "Upload follow-ups").

## GCS bucket lifecycle (`gcs-lifecycle.json`)

Deletes per-job objects on a per-prefix age. Scoped by prefix so curated assets
persist. This is the retention list published in the Privacy Policy (§8) at
`/privacy` — keep the two in sync when either changes. This section is the
per-prefix table CLAUDE.md's "Storage retention" points at; every rule in
`gcs-lifecycle.json` must appear below.

**Deleted after 1 day:**
- `dev-user/*` — raw uploads and rendered clips from anonymous job submissions
- `music-jobs/*` — final music-sync outputs
- `music-lyrics-previews/*` — lyric-preview renders
- `voiceover-uploads/*` — user-recorded voiceover audio
- `training-exports/*` — generated edit-training artifact bundles
- `transcript-cache/*` — cached Whisper transcripts, keyed by content hash (see
  `pipeline/transcribe.py::transcribe_whisper_cached`). Content-hash keying means
  these entries have no link back to a user, so account deletion can't find and
  purge them — the 24h TTL is what actually bounds the exposure.

**Deleted after 30 days:**
- `jobs/*` — template-mode job inputs and outputs
- `00000000-0000-0000-0000-000000000001/*` — the anonymous upload prefix

**Persists forever (not matched by any bucket rule — auth landed, see
"Re-evaluate when" below; account deletion is the removal path for live assets,
see `routes/me.py::confirm_account_deletion` + `docs/legal/README.md`):**
- `users/{user_id}/*` — plan clips, plan-pool footage, activation seed batches
- `generative-jobs/{job_id}/*` — rendered outputs + preprocessed sources
- `job-posters/{job_id}/*` — Library tile thumbnails extracted from a job's video
- `music/*` — admin-curated music track library
- `templates/*` — template assets (posters, audio)

`job-posters/` is deliberately outside every video prefix (v0.59.1.0). Posters
used to be written as a sibling of the video (`<source>.poster.jpg`), so they
inherited the source's rule and a Library tile went blank when the source was
deleted — 24h for `music-jobs/*`, 30 days for `jobs/*`. A thumbnail has to
outlive its source, so it lives on a prefix no rule matches. The prefix is
listed in `JOB_OUTPUT_PREFIXES` (`app/services/job_storage_paths.py`), so
account deletion still removes a user's posters along with their videos.
Consequence: a music/template tile can now show a real thumbnail for a source
MP4 the lifecycle rule already deleted — playback fails cleanly, and the
retention question is tracked in TODOS.md.

The application deletes one narrow superseded-asset class inside persistent job
prefixes: immutable `job-posters/<job-id>/<sha1(source)>.poster.backfill-<uuid>.jpg`
objects (pre-v0.59.1.0 runs wrote `<source>.poster.backfill-<uuid>.jpg` siblings;
both shapes are read, and the key stored on the Job is authoritative). A renderer
journals a durable replacement receipt on the Job before changing the visible
poster; the five-minute bounded maintenance sweep verifies the replacement and
removes the old object. Do not delete or rewrite these receipts manually. See
[`docs/runbooks/video-poster-backfill.md`](../docs/runbooks/video-poster-backfill.md).

### Apply

```bash
gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET
gsutil lifecycle get gs://$STORAGE_BUCKET   # verify
```

This is a one-time operation, run manually after the PR merges. The rule is
re-read by GCS on each lifecycle scan (roughly once per day); the first scan
after install will start chewing through the existing backlog.

### Re-evaluate when

- A user-facing "my videos" gallery is added → retention has to grow to match
  whatever lifetime the gallery promises. (Already the case today — `users/`
  and `generative-jobs/` persist indefinitely for exactly this reason.)
