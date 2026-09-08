# Kria iOS

The native iOS foundation targets iOS 18 and is generated from `project.yml` with
XcodeGen. The app is intentionally credential-free: Development uses the local
auth affordance and fixture-backed projects, while Release uses the Apple and
Google auth provider seams and the typed `KriaAPIClient` contract.

## Generate and build

```sh
xcodegen generate --spec project.yml
xcodebuild -project Kria.xcodeproj -scheme Kria \
  -destination 'platform=iOS Simulator,name=iPhone 16 Pro' test
```

`Kria/Core` owns API, auth, secure token storage, SwiftData cache models, upload
consent, background upload recovery, and an internal editor diagnostic. The
diagnostic persists preview state only and is intentionally not exposed in the
production navigation until native edits can create an exact server approval
and renderer commit. `Kria/DesignSystem` owns the paper/ink/lime
tokens and accessible controls. `Kria/Features` owns the adaptive phone-first
shells and flow surfaces. `Packages/KriaMediaEngine` is a local package seam;
the app does not duplicate its timeline or render rules.

The API adapter follows the existing contracts: `/auth/mobile/exchange` and
`/auth/mobile/refresh` and `/auth/mobile/revoke` for native sessions, `/me/jobs` and its playback URL for
the library, and `/creation-threads` plus runtime-v2 turns/delta/draft/approval
operations for creation. The production app does not advertise a native
"Save and render" action: rendering remains approval-driven through the Kria
conversation until that handoff is wired end to end. Footage uses the creation thread's reservation and
attachment endpoints, so a successful background PUT becomes part of the
server-owned project instead of an unattached temporary blob. Access tokens are refreshed once on a 401; refresh
token rotation remains server-authoritative and concurrent 401s share one
rotation. Sign-out clears local credentials immediately, then revokes the
refresh-token family on a best-effort basis. Google
sign-in uses an authorization-code flow with state validation and PKCE; the
server validates the returned provider token and nonce. Render UI uses an
indeterminate state until a real server event is supplied, so it never invents
percentages. Server timestamps are decoded with fractional-second and offset
ISO-8601 compatibility.

`Config/Development.xcconfig`, `Config/Staging.xcconfig`, and
`Config/Production.xcconfig` are the only checked-in environment defaults.
They provide `API_BASE_URL` and `KRIA_GOOGLE_*` build settings; put real client
configuration in a local override or CI secret store. `Kria/Generated/openapi.yaml`
and the XcodeGen OpenAPI plugin keep the native endpoint surface checked
against the server contract during builds.

For a developer-specific Debug configuration, create the git-ignored
`Config/Local.xcconfig`. `Development.xcconfig` includes it when present, so it
can override `API_BASE_URL`, `KRIA_GOOGLE_CLIENT_ID`, and
`KRIA_GOOGLE_REDIRECT_SCHEME` without changing tracked configuration.

The app has no production credentials in source. Set environment-specific API
values in the xcconfig files or in the Xcode scheme, and keep real secrets in
the developer's keychain/CI secret store.
