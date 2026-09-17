# Kria external TestFlight release

The `TestFlight` workflow releases the exact `main` commit whose native `iOS`
workflow passed. It creates an IPA only for an iOS-relevant commit, rejects a
stale test result, and skips a build number that App Store Connect already has.

## One-time account setup

The Apple account holder must register `com.emirerben.kria`, create the Kria app
record, and enable **Sign in with Apple** for that App ID. Create the external
group that owns Kria's public TestFlight link before the first upload. The
workflow adds approved builds to that existing group; it intentionally never
widens distribution to an arbitrary group.

Create a dedicated iOS Google OAuth client for `com.emirerben.kria`. Its client ID
and custom callback scheme become the Release values for
`KRIA_GOOGLE_CLIENT_ID` and `KRIA_GOOGLE_REDIRECT_SCHEME`. The production API
must accept both values before the first TestFlight upload:

- `MOBILE_APPLE_CLIENT_IDS` includes `com.emirerben.kria`.
- `MOBILE_GOOGLE_CLIENT_IDS` includes the dedicated iOS client ID.
- `MOBILE_JWT_SECRET` is present and remains independent from web proxy auth.

Deploy that API configuration before releasing the app. The workflow waits for
the matching Fly deploy whenever either mobile-auth configuration source changes.

### Account deletion and privacy prerequisites

Before external testing, configure the API and worker with `APPLE_TEAM_ID`,
`APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY` (the Sign in with Apple P-256 `.p8` key,
not an App Store Connect upload key). The key must authorize the App ID accepted
by `MOBILE_APPLE_CLIENT_IDS`. `TOKEN_ENCRYPTION_KEY` must remain available to both
process groups: Apple refresh credentials are encrypted in a durable revocation
outbox until Apple confirms revocation. Migration `0106` creates that outbox;
Celery Beat dispatches pending revocations every five minutes. Monitor pending rows and
worker failures after exercising deletion with a dedicated test account.

Configure `RESEND_API_KEY` and the account-deletion email sender. Register and
verify that sender/domain with Apple's private email relay so Apple users who
hide their email can receive the confirmation code. Test delivery to a real relay
address before submission. Deletion requests return an unavailable error when
required email or Apple configuration is absent.

In App Store Connect, review the app privacy answers against the bundled
`PrivacyInfo.xcprivacy` and the public privacy policy. The app collects account
identity, selected media/audio, conversation content, and project interactions
for app functionality, and discloses Google Gemini and OpenAI before AI use.
Confirm the policy, support details, content rights, and reviewer notes with the
account holder. Repository declarations do not update App Store Connect.

KRI-55's 2026-09-15 production secret-name check found the mobile-auth client IDs,
JWT secret, and token encryption key present, but the three Apple revocation
settings and `RESEND_API_KEY` absent. Resolve and verify these release blockers
before sending the repaired build to external testers. See
[`docs/reviews/kri-55-apple-readiness.md`](../reviews/kri-55-apple-readiness.md)
for validation and remaining review checks.

## GitHub environment

Create the protected `testflight-production` environment. Store the signed
distribution certificate, matching App Store profile, its import passphrase,
keychain passphrase, App Store Connect upload authority, and the two beta-review
demo-account secrets (`KRIA_BETA_REVIEW_DEMO_USER`, `KRIA_BETA_REVIEW_DEMO_PASSWORD`
— see "Apple Beta App Review demo account" below) as environment secrets. Keep
the Apple team ID, provisioning-profile name, Google iOS client configuration,
TestFlight group name, beta-review contact, feedback address, description, and
reviewer notes as environment variables.

## Apple Beta App Review demo account

Apple rejected build 0.1.0 (46) on 2026-09-16/17 under Guideline 2.1(a): a
sign-in-gated app must give reviewers a working demo account rather than relying
on Sign in with Apple/Google. KRI-111 fixed this by adding a flag-gated reviewer
login and wiring it into the release.

**API (Fly secrets, `nova-video`):**
- `REVIEWER_LOGIN_ENABLED` — default `false`; gates `POST
  /auth/mobile/reviewer-login` (404 when off), rate-limited 5/min per IP.
- `REVIEWER_LOGIN_EMAIL` — the demo account's email.
- `REVIEWER_LOGIN_PASSWORD_HASH` — scrypt hash, generated with:
  ```sh
  cd src/apps/api && python -m app.cli.reviewer_login hash
  ```
  (reads the password interactively, or via `--stdin`).

**GitHub environment secrets (`testflight-production`):**
- `KRIA_BETA_REVIEW_DEMO_USER`, `KRIA_BETA_REVIEW_DEMO_PASSWORD` — set with:
  ```sh
  gh secret set KRIA_BETA_REVIEW_DEMO_USER --env testflight-production
  gh secret set KRIA_BETA_REVIEW_DEMO_PASSWORD --env testflight-production
  ```

`demo_account_required` is now `true` in the Fastfile's `upload_to_testflight`
call, so every upload writes `demo_account_name`/`demo_account_password` (from
those two secrets) into App Store Connect's Beta App Review Information — Apple
shows them to the reviewer automatically; they are never bundled into the app or
committed to the repo.

Seed the reviewer account with finished videos before submitting (clones a
source user's finished videos onto it):

```sh
python scripts/admin.py --prod POST admin/reviewer-account/seed \
  --json '{"source_user_email":"<email>","limit":6}'
```

Verify the login end-to-end:

```sh
# 200 with correct credentials
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://nova-video.fly.dev/auth/mobile/reviewer-login \
  -H 'content-type: application/json' \
  -d '{"email":"<REVIEWER_LOGIN_EMAIL>","password":"<the real password>"}'
# 401 with a wrong password; 404 when REVIEWER_LOGIN_ENABLED=false
```

**Rotation:** pick a new password → hash it with the CLI above → `fly secrets
set REVIEWER_LOGIN_PASSWORD_HASH=... --app nova-video` → `gh secret set
KRIA_BETA_REVIEW_DEMO_PASSWORD --env testflight-production` → the next
TestFlight upload carries the new credentials to App Store Connect.

**Caveat:** if a reviewer deletes the demo account from within the app, the next
`reviewer-login` recreates an empty user with no videos — re-run the seed
command above before the next review pass.

**Rollback:** `fly secrets set REVIEWER_LOGIN_ENABLED=false --app nova-video` +
`fly machine restart <id>`. Credentials live only in Fly secrets and the GitHub
environment — never in the repo or app bundle.

Recommended `KRIA_BETA_REVIEW_NOTES` (GitHub environment variable):

> Sign in with email using the demo credentials below (tap "Sign in with email"
> under the Google button). Accept the AI data-sharing screen. The Gallery
> already contains finished videos: open one to play, edit in the native
> editor, and Save to Photos. To exercise creation, start a new project and add
> any short clip from Photos or Files; Kria uploads it and shows the generated
> edit when processing completes (2–5 min). Sign in with Apple and Google also
> work with your own accounts. No purchase is required.

## Release behavior

The workflow uses the GitHub run number as the TestFlight build number under
marketing version `0.1.0`. Re-running a job cannot replace a processed build:
Fastlane reads the latest App Store Connect build first and skips an equal or
newer number. Apple still controls external beta-review timing; after approval,
the build is automatically available through the configured public group.

For a local signed archive, supply the same release inputs and run:

```sh
bash scripts/ios/archive-testflight.sh
```

The script fails before archiving if signing, Google OAuth, version, or build
inputs are missing or malformed. It never reads account credentials from source.
