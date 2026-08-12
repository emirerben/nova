# TikTok Content Posting review and rollout

Kria connects a creator's TikTok account and sends the exact finalized video
they approved by either of TikTok's Content Posting paths:

- **Post now** uses Direct Post (`video.publish`). Until TikTok grants audited
  access, the API and UI allow only `SELF_ONLY`.
- **Finish in TikTok** uses Upload API (`video.upload`). TikTok creates an inbox
  notification; the creator opens TikTok and finishes the draft there. Kria
  never describes a successful upload as a published post. The notification is
  visible **only in the TikTok mobile app** — it never creates an entry in the
  Drafts tab and never appears on tiktok.com in a desktop browser. Kria's UI
  must say so explicitly wherever this path is surfaced (2026-08-12 incident:
  a user searched tiktok.com for a delivered video labeled "TikTok drafts" and
  could not find it — see agents/DECISIONS.md).

Both paths use the same immutable GCS snapshot, generation check, lifecycle
receipt, and creator music-rights confirmation.

## Review configuration: exact products and scopes

The August 2026 resubmission must contain only these products:

1. Login Kit
2. Content Posting API

The OAuth request and the TikTok Developer Portal must show exactly:

| Scope | Why Kria needs it | What the review video must show |
| --- | --- | --- |
| `user.info.basic` | Identify the connected TikTok account | Login Kit consent and the connected nickname in Kria |
| `video.publish` | Submit the approved render as a Direct Post | Post now → Only you → processing receipt → private TikTok post |
| `video.upload` | Send the approved render to TikTok for creator completion | Finish in TikTok → inbox notification in the TikTok mobile app → open TikTok's composer |

Content Posting API automatically adds both content scopes in TikTok's portal,
so both must be implemented and recorded. Do **not** add `user.info.profile`,
`user.info.stats`, or `video.list` to this review. Performance sync remains
disabled and is deferred to a separate future review.

The backend scope list is pinned in `app/routes/tiktok.py`; its OAuth test must
assert this exact three-scope set.

## Sandbox checklist

TikTok requires the first review to be demonstrated in a sandbox. Before
recording, open the sandbox named **Kria Review Demo** and verify:

- Sandbox ID: `7669267012891740181`
- Target user: `emirerben`
- Products: Login Kit and Content Posting API only
- Direct Post switch: enabled
- Scopes: only `user.info.basic`, `video.publish`, `video.upload`
- Website URL: `https://www.usekria.com/tiktok`
- Redirect URI: `https://nova-video.fly.dev/tiktok/oauth/callback`

Remove the sandbox's manually selected `user.info.profile`,
`user.info.stats`, and `video.list` scopes. Do not select Webhooks as a
standalone product for this review. Kria's signed lifecycle endpoint can remain
implemented without expanding the products shown in the review. Never select a
product merely because code exists for a future rollout.

The deployed API and the account used in the recording must be configured with
the **sandbox** client key and secret. Do not reveal either credential in the
recording or paste it into logs.

## Reviewer-facing Website URL

Use `https://www.usekria.com/tiktok`. It is public and does not require a Kria
login. It demonstrates account connection plus both selected Content Posting
scopes and links to the live product, Terms, and Privacy Policy. It contains no
profile analytics or video-list claims.

Suggested Apply Reason (under TikTok's character limit):

```text
We aligned the integration and review materials with the exact sandbox scope set. Kria now requests only user.info.basic, video.publish, and video.upload. The public reviewer workspace at https://www.usekria.com/tiktok demonstrates both Content Posting paths: (1) a creator-confirmed Direct Post restricted to “Only you” during unaudited review, and (2) Upload API handoff, where TikTok creates an inbox notification and the creator must finish the draft inside TikTok. We removed the unused profile, statistics, and video-list scopes and all analytics claims. The attached video starts in the Kria Review Demo sandbox, shows the target user and exact scopes, completes Login Kit consent, then demonstrates both paths end to end, including the private TikTok result and TikTok inbox/draft composer. No public post is created during review.
```

## Required review video — one continuous recording

Record a new video after the sandbox settings and deployed build are live. Do
not reuse `kria-tiktok-demo-final.mov`; it documents the rejected scope set.

1. Open TikTok Developer Portal → **Kria Review Demo** sandbox.
2. Show the sandbox name/ID, target user, selected products, Direct Post switch,
   and the exact three scopes. Keep secrets hidden.
3. Open `https://www.usekria.com/tiktok` and briefly show both delivery choices.
4. Open the live Kria product, sign in, and connect TikTok through Login Kit.
5. Show the TikTok consent screen containing the three requested permissions,
   approve it with the sandbox target user, and show the connected nickname.
6. Choose a finalized Kria video and select **Post now**.
7. Show the exact preview, choose **Only you**, confirm music rights, review the
   summary, submit, wait for processing, and open the resulting private post in
   TikTok. Keep the account and video consistent throughout.
8. Return to the same finalized video and select **Finish in TikTok**.
9. Show the handoff explanation, confirm that the creator must continue in
   TikTok, send the video, and wait for the Kria draft receipt.
10. Open TikTok, show the inbox notification, enter the draft composer, and
    stop before publishing. Explicitly narrate that Kria uploaded a draft and
    the creator retains final control.

Narrate each permission by name and point to the matching visible behavior.
Avoid cuts that make the sandbox account, selected video, or result ambiguous.
Keep browser tabs, system notifications, and secrets outside the capture area.

## Submission gate

Do not click **Submit for Review** until every item below is true:

- Portal products/scopes match the table exactly.
- Sandbox target user can complete OAuth.
- Deployed Kria uses sandbox credentials for the recording.
- Direct Post produces a private result and a terminal Kria receipt.
- Draft upload creates the TikTok inbox notification and opens in TikTok's
  composer without Kria claiming it was published.
- The new video visibly demonstrates all three scopes and both products.
- Website URL and Apply Reason match this runbook.
- Automated checks listed below pass.

The final Submit for Review action is manual and requires explicit confirmation
from the account owner after watching the uploaded video once from the portal.

## Runtime configuration

Store these values only in the API/worker secret store:

```text
TOKEN_ENCRYPTION_KEY=<fernet-key>
TIKTOK_CLIENT_KEY=<sandbox-client-key-for-review>
TIKTOK_CLIENT_SECRET=<sandbox-client-secret-for-review>
TIKTOK_REDIRECT_URI=https://nova-video.fly.dev/tiktok/oauth/callback
TIKTOK_WEB_APP_URL=https://www.usekria.com/library
TIKTOK_MEDIA_BASE_URL=https://nova-video.fly.dev/tiktok/media
TIKTOK_PUBLISHING_ENABLED=true
TIKTOK_DRAFT_UPLOAD_ENABLED=true
TIKTOK_CONTENT_POSTING_AUDITED=false
TIKTOK_PERFORMANCE_SYNC_ENABLED=false
TIKTOK_PUBLISHING_BETA_USER_IDS=<review-user-nova-uuid>
```

`TIKTOK_CONTENT_POSTING_AUDITED=false` is mandatory during review: it makes the
backend reject every Direct Post privacy value except `SELF_ONLY`, regardless
of creator-info options. `TIKTOK_PERFORMANCE_SYNC_ENABLED=false` must remain off
because this review does not request `video.list` or statistics scopes.

Deploy with `TIKTOK_DRAFT_UPLOAD_ENABLED=false` first. After the API and every
worker are running this release, set it to `true` and restart both process
groups. This prevents an old worker in a rolling deploy from interpreting a
new draft-only consent record as a Direct Post.

## API surface

Browser calls use the authenticated Next.js plan proxy. TikTok calls the
callback, webhook, and media URLs directly on FastAPI.

| Method | FastAPI path | Purpose |
| --- | --- | --- |
| `GET` | `/tiktok/connection` | Account, granted scopes, and direct/draft capabilities |
| `POST` | `/tiktok/oauth/start` | One-use OAuth state and TikTok authorization URL |
| `GET` | `/tiktok/oauth/callback` | Exchange credentials and return to the originating Kria surface |
| `DELETE` | `/tiktok/connection` | Revoke best-effort and erase credentials |
| `GET` | `/tiktok/publish-options` | Resolve the exact render and fresh creator capabilities |
| `POST` | `/tiktok/publications` | Create an idempotent direct or draft delivery |
| `GET` | `/tiktok/publications` | List the creator's recent delivery records, optionally filtered to one render |
| `GET` | `/tiktok/publications/receipt` | Read the newest receipt for one render, or `null` before its first delivery |
| `GET` | `/tiktok/publications/{id}` | Read delivery lifecycle |
| `POST` | `/tiktok/sync` | Queue official metrics sync; disabled for this review because it requires deferred analytics scopes |
| `GET`, `HEAD` | `/tiktok/media/{id}/{token}.mp4` | Serve the immutable snapshot to TikTok |
| `POST` | `/tiktok/webhook` | Verify and reconcile TikTok lifecycle events |

Direct Post uses TikTok's `/v2/post/publish/video/init/` endpoint with
`PULL_FROM_URL`. Draft handoff uses `/v2/post/publish/inbox/video/init/` with
the same source mode. A draft completion is stored as visibility `draft`, not
`private` or `public`.

An ambiguous submission timeout becomes `submission_unknown` and is never
automatically retried because retrying could duplicate a delivery. The creator
must inspect TikTok before starting another attempt.

## Verification

Run:

```bash
cd src/apps/api
pytest tests/test_tiktok_direct_publishing.py -q
ruff check app/routes/tiktok.py app/services/tiktok_client.py app/tasks/tiktok.py tests/test_tiktok_direct_publishing.py

cd ../web
npx tsc --noEmit
npm test -- --runInBand src/__tests__/tiktok src/__tests__/lib/tiktok-api.test.ts src/__tests__/tiktok-product-workspace.test.tsx
```

The automated suite mocks TikTok. The sandbox recording is the required final
integration check and must exercise TikTok's actual consent, Direct Post,
inbox notification, and draft composer.
