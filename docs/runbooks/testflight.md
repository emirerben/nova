# Kria external TestFlight release

The `TestFlight` workflow releases the exact `main` commit whose native `iOS`
workflow passed. It creates an IPA only for an iOS-relevant commit, rejects a
stale test result, and skips a build number that App Store Connect already has.

## One-time account setup

The Apple account holder must register `com.kria.app`, create the Kria app
record, and enable **Sign in with Apple** for that App ID. Create the external
group that owns Kria's public TestFlight link before the first upload. The
workflow adds approved builds to that existing group; it intentionally never
widens distribution to an arbitrary group.

Create a dedicated iOS Google OAuth client for `com.kria.app`. Its client ID
and custom callback scheme become the Release values for
`KRIA_GOOGLE_CLIENT_ID` and `KRIA_GOOGLE_REDIRECT_SCHEME`. The production API
must accept both values before the first TestFlight upload:

- `MOBILE_APPLE_CLIENT_IDS` includes `com.kria.app`.
- `MOBILE_GOOGLE_CLIENT_IDS` includes the dedicated iOS client ID.
- `MOBILE_JWT_SECRET` is present and remains independent from web proxy auth.

Deploy that API configuration before releasing the app. The workflow waits for
the matching Fly deploy whenever either mobile-auth configuration source changes.

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
