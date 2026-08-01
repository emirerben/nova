# Nova infra

## GCS bucket lifecycle (`gcs-lifecycle.json`)

Deletes per-job objects after 1 day. Scoped by prefix so curated assets persist.
This is the retention list published in the Privacy Policy (§8) at `/privacy` —
keep the two in sync when either changes.

**Deleted after 1 day:**
- `dev-user/*` — raw uploads and rendered clips from anonymous job submissions
- `music-jobs/*` — final music-sync outputs
- `music-lyrics-previews/*` — lyric-preview renders
- `voiceover-uploads/*` — user-recorded voiceover audio
- `transcript-cache/*` — cached Whisper transcripts, keyed by content hash (see
  `pipeline/transcribe.py::transcribe_whisper_cached`). Content-hash keying means
  these entries have no link back to a user, so account deletion can't find and
  purge them — the 24h TTL is what actually bounds the exposure.

**Persists forever (not matched by any rule — auth landed, see "Re-evaluate
when" below; account deletion is the removal path for these, see
`routes/me.py::confirm_account_deletion` + `docs/legal/README.md`):**
- `users/{user_id}/*` — plan clips, plan-pool footage, activation seed batches
- `generative-jobs/{job_id}/*` — rendered outputs + preprocessed sources
- `music/*` — admin-curated music track library
- `templates/*` — template assets (posters, audio)

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
