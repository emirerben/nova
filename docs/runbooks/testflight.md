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
keychain passphrase, and App Store Connect upload authority as environment
secrets. Keep the Apple team ID, provisioning-profile name, Google iOS client
configuration, TestFlight group name, beta-review contact, feedback address,
description, and reviewer notes as environment variables.

The beta-review notes should state that the reviewer can use Sign in with Apple
or Google, then create a project with their own footage. Do not place test user
credentials in the app or repository.

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
