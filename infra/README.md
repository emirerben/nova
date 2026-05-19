# Nova infra

## GCS bucket lifecycle (`gcs-lifecycle.json`)

Deletes per-job objects after 1 day. Scoped by prefix so curated assets persist.

**Deleted after 1 day:**
- `dev-user/*` — raw uploads and rendered clips from anonymous job submissions
- `music-jobs/*` — final music-sync outputs

**Persists forever (not matched by either rule):**
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

- Auth/login lands → consider keeping authenticated-user objects under a new
  `users/{user_id}/` prefix that is NOT matched by the delete rule.
- A user-facing "my videos" gallery is added → retention has to grow to match
  whatever lifetime the gallery promises.
