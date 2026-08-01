# TikTok direct publishing rollout

Nova can connect a creator's TikTok account, publish the exact finalized video
they approved, reconcile TikTok processing and visibility, and sync official
public metrics. Every backend capability ships off by default. Public posting
is a release gate, not a configuration shortcut: keep unaudited users on
`SELF_ONLY` until TikTok approves Nova for audited Direct Post access.

## What ships

- OAuth through TikTok Login Kit with one-use, 10-minute state values.
- Direct Post from an immutable GCS snapshot of an owned, generation-checked
  Nova render.
- Signed webhook reconciliation with rate-limited polling as a fallback.
- Library status for processing, moderation/visibility, and public metrics.
- Official account and latest-30-video sync every 12 hours.
- Low-confidence edit correlations from age-aligned, linked Nova posts. These
  associations never override a user-edited style.

Nova deliberately does not support scheduling, draft upload, photo posts,
comment management, TikTok metadata edits/deletes, or watch-time and audience
demographics in this release.

## API reference

Browser calls go through the authenticated Next.js plan proxy. TikTok calls the
callback, webhook, and media URLs directly on FastAPI.

| Method | FastAPI path | Purpose |
| --- | --- | --- |
| `GET` | `/tiktok/connection` | Connection metadata, scopes, rollout state, and separate publish/analyze capabilities |
| `POST` | `/tiktok/oauth/start` | Create one-use OAuth state and return TikTok's authorization URL |
| `GET` | `/tiktok/oauth/callback` | Consume state, exchange credentials, and redirect to the allowlisted Library |
| `DELETE` | `/tiktok/connection` | Revoke best-effort, erase credentials, and start account-data cleanup |
| `GET` | `/tiktok/publish-options?job_id=&variant_id=` | Resolve the owned final render and fetch fresh creator capabilities |
| `POST` | `/tiktok/publications` | Create an idempotent publication from an approved `source_revision` |
| `GET` | `/tiktok/publications` | Return the user's 100 most recent publication records |
| `GET` | `/tiktok/publications/{publication_id}` | Read one owned publication's lifecycle and metrics |
| `POST` | `/tiktok/sync` | Queue a rate-limited official metrics sync |
| `GET`, `HEAD` | `/tiktok/media/{publication_id}/{token}.mp4` | Serve the immutable snapshot to TikTok, including one byte range |
| `POST` | `/tiktok/webhook` | Verify and reconcile TikTok lifecycle or deauthorization events |

Every user-owned endpoint enforces Nova authentication and ownership. Media
access uses the short-lived opaque token in its URL; webhook access uses
TikTok's timestamped signature. Never log either value or a full webhook body.

## External release gates

Complete these in TikTok's developer console before enabling a beta user:

1. Configure Login Kit and the Display API scopes used by Nova:
   `user.info.basic`, `user.info.profile`, `user.info.stats`, and `video.list`.
2. Configure Content Posting API / Direct Post and request `video.publish`.
3. Register the exact static callback:
   `https://<api-domain>/tiktok/oauth/callback`.
4. Register the signed webhook endpoint:
   `https://<api-domain>/tiktok/webhook`.
5. Verify the API domain and the media URL prefix:
   `https://<api-domain>/tiktok/media`.
6. Complete TikTok's Content Posting audit before allowing public privacy
   options. Until approval, Nova exposes only `SELF_ONLY`.

The API domain must be owned and verifiable by TikTok. The media endpoint
streams bytes directly from GCS and cannot sit behind a redirecting URL. If the
current Fly hostname cannot satisfy TikTok's verification, provision an owned
custom API domain before enabling publishing.

## Required configuration

Generate a Fernet key once and store it with the API and worker secrets:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the following values in the production secret store. Do not commit their
values or paste them into logs:

```text
TOKEN_ENCRYPTION_KEY=<generated-fernet-key>
TIKTOK_CLIENT_KEY=<tiktok-client-key>
TIKTOK_CLIENT_SECRET=<tiktok-client-secret>
TIKTOK_REDIRECT_URI=https://<api-domain>/tiktok/oauth/callback
TIKTOK_WEB_APP_URL=https://<web-domain>/library
TIKTOK_MEDIA_BASE_URL=https://<api-domain>/tiktok/media
TIKTOK_PUBLISHING_ENABLED=false
TIKTOK_CONTENT_POSTING_AUDITED=false
TIKTOK_PERFORMANCE_SYNC_ENABLED=false
TIKTOK_PUBLISHING_BETA_USER_IDS=<comma-separated-or-json-user-uuids>
```

`TIKTOK_WEB_APP_URL` must be the `/library` path on an origin already present
in `ALLOWED_ORIGINS`. The callback fails closed to the local Library URL when
the scheme, credentials, path, or origin is not allowlisted.

`TOKEN_ENCRYPTION_KEY` and the TikTok client credentials are mandatory for a
working connection. Token encryption fails closed when the key is missing or
invalid. Rotate this key only with a credential migration or by disconnecting
and reconnecting every TikTok account; replacing it in place makes stored
tokens unreadable.

## Rollout order

The frontend has no `NEXT_PUBLIC` copy of these flags. It derives availability
and partial-scope capabilities from `GET /tiktok/connection`.

### 1. OAuth-only beta

- Keep all three capability flags `false`.
- Add test users' Nova UUIDs to `TIKTOK_PUBLISHING_BETA_USER_IDS`.
- Deploy API and worker, then connect from the Library.
- Confirm the connected nickname and granted scopes. A partial grant is valid:
  the UI prompts for reconnection only for the missing capability.

### 2. Private Direct Post beta

- Keep `TIKTOK_CONTENT_POSTING_AUDITED=false`.
- Set `TIKTOK_PUBLISHING_ENABLED=true`.
- Keep the beta UUID allowlist narrow.
- Publish a finalized plan-item variant and confirm the TikTok media pull,
  processing state, and final private visibility in the Library.

With the audited flag off, the API rejects every privacy value except
`SELF_ONLY`, even if TikTok's creator-info response lists public options.

### 3. Performance sync

- Confirm the connection granted `user.info.basic` and `video.list`.
- Set `TIKTOK_PERFORMANCE_SYNC_ENABLED=true`.
- Use the Library's manual sync once, then confirm `last_synced_at` and public
  video metrics. Manual sync is limited to one request per user per five
  minutes; scheduled sync becomes due every 12 hours.

### 4. Audited posting

Only after TikTok confirms the Content Posting audit:

- Set `TIKTOK_CONTENT_POSTING_AUDITED=true`.
- Keep `TIKTOK_PUBLISHING_ENABLED=true`.
- Verify fresh creator-info responses expose the expected privacy and
  interaction capabilities before broadening access.

The audited flag makes publishing available beyond the beta allowlist. Treat
that change as the broad-launch switch.

## Publishing lifecycle

The user previews a finalized render first. Nova returns an opaque
`source_revision`, reads fresh creator capabilities, and requires a manual
privacy choice plus music, commercial-content, and AIGC declarations. On
submission Nova rechecks the source object's GCS generation and ETag. A changed
render returns `409` and must be previewed again.

Accepted publications return `202`. The worker copies the approved generation
to `tiktok-publish/<publication-id>.mp4`, mints a two-hour media token, and lets
TikTok pull it with `GET`, `HEAD`, or one byte range. The API never redirects or
buffers the complete video in memory.

Processing and visibility are separate:

- Processing: `queued`, `snapshotting`, `submitting`, `processing`, `complete`,
  `submission_unknown`, or `failed`.
- Visibility: `unknown`, `private`, `public`, or `removed`.

`PUBLISH_COMPLETE` means TikTok finished ingestion; it does not prove a post is
public. Live metrics are stored when TikTok's authorized video list returns a
linked post; the learning snapshot remains strictly gated on confirmed public
visibility and maturity.

Nova retries only definite transient errors and validated media-pull failures,
at most three times. An ambiguous submission timeout becomes
`submission_unknown` and is never retried automatically because a retry could
create a duplicate TikTok post. The creator must check TikTok before starting a
new publication.

## Metrics and bounded learning

The scheduled sync stores official account totals and the latest 30 authorized
videos. Live views, likes, comments, and shares update the Library. Nova freezes
one evaluation snapshot on the first sync 72–84 hours after a linked post became
public so comparisons use similar post ages.

Generic profile analysis needs at least five videos and a changed input
fingerprint. Nova-specific associations additionally need five currently public,
mature, linked Nova posts, at least three examples in each compared bucket, and
variation across two supported buckets. Output includes sample size, time window,
provenance, and low-confidence wording. Weak or conflicting support produces no
recommendation. Automatic style derivation runs at most weekly for a changed
mature-post fingerprint; user-edited style always wins.

## Disconnect, deauthorization, and retention

Disconnect and TikTok deauthorization erase encrypted credentials immediately,
stop new refresh/sync work, invalidate media tokens, and cancel work that has not
been submitted. Connected-account metrics and automatic analysis are removed,
while user-edited style remains intact.

- Unsubmitted snapshots are deleted during cleanup.
- Submitted snapshots become cleanup-eligible after the publication row has
  been unchanged in a terminal state for 24 hours. Metric updates can move that
  clock; the seven-day absolute limit is the hard deletion backstop.
- Revoked-account publication data is minimized asynchronously within 24 hours.
- Minimal publication and consent audit rows remain for 30 days; cleanup then
  clears TikTok identifiers and residual account metadata.

The Celery beat polls due publications every minute, schedules account syncs
every 15 minutes, and runs snapshot/audit cleanup daily. Database recovery sweeps
make broker dispatch best-effort rather than a durability boundary.

## Verification checklist

- `GET /tiktok/connection` reports the expected beta, audit, scopes, and
  `can_publish` / `can_analyze` values.
- OAuth denial, expired state, replay, and duplicate-account connection fail
  without exposing codes or tokens.
- The callback redirects only to the allowlisted Library URL.
- Publish options show the exact variant and a fresh source revision.
- Changing the render after preview produces `409`.
- TikTok can issue `HEAD` and final-byte range requests without a redirect.
- Signed webhook duplicates are harmless and stale or replayed signatures fail.
- A private beta post reaches `complete` + `private`; it never becomes eligible
  for a learning snapshot.
- A public audited post reaches `complete` + `public`, receives metrics, and can
  freeze one mature evaluation snapshot in the 72–84-hour window.
- Disconnect clears credentials immediately and cleanup removes snapshots and
  derived account data on schedule.

For code-level verification, run:

```bash
cd src/apps/api
pytest tests/test_tiktok_direct_publishing.py tests/routes/test_me_jobs.py -q

cd ../web
npm test -- --runInBand src/__tests__/tiktok src/__tests__/lib/tiktok-api.test.ts
```

CI and local testing use mocked TikTok responses. A live TikTok account is a
release-environment check, not a CI dependency.
