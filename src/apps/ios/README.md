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

After sign-in, `ChatWorkspaceView` is the app shell: the active creation thread
fills the screen, the left drawer switches projects and opens Gallery, and the
safe-area composer remains available throughout the workflow.
Format cards use the server-owned `select_format` action. New chats select runtime
v2 only when advertised by the server; existing conversations retain their runtime.
Messages, approvals, render state, and results are projected from that thread.
If an account has no thread, the shell
creates one and restores that active project on the next launch.

`Kria/Core` owns API, auth, secure token storage, SwiftData cache models, upload
consent, background upload recovery, and the lossless native editor document.
Ready projects open `NativeEditorView` from the production workspace. The
editor loads the active plan item and selected render variant, gates each lane
from server capabilities, batches local mutations into one undoable document,
and submits one atomic renderer commit when the user taps Save. It keeps the
saved state while the replacement preview renders, then reloads the new
generation without discarding unrelated server-owned fields.
`Kria/DesignSystem` owns the Sunlit semantic color roles, approved Kria wordmark,
and accessible controls. `Kria/Features` owns the adaptive phone-first
shells and flow surfaces. `Packages/KriaMediaEngine` is a local package seam;
the app does not duplicate its timeline or render rules.

The API adapter follows the existing contracts: `/auth/mobile/exchange` and
`/auth/mobile/refresh` and `/auth/mobile/revoke` for native sessions, `/me/jobs` and its playback URL for
the library, and `/creation-threads` for creation. Runtime v1 uses messages/actions;
v2 uses turns/deltas/approvals. Format choices and runtime support come from
`/creation-threads/capabilities`; unavailable formats explain why they cannot be selected.
Native editor entry resolves an existing plan item directly or promotes a
library job through `/me/jobs/{id}/open-in-editor`; variant state comes from
`/generative-jobs/{id}/status`, and Save posts the full changed-section batch to
`/plan-items/{item_id}/variants/{variant_id}/editor-commit`. Footage uses the creation thread's reservation and
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
is generated from the server's Pydantic models; `make ios-verify` rejects
contract drift before XcodeGen builds the OpenAPI plugin output.

For a developer-specific Debug configuration, create the git-ignored
`Config/Local.xcconfig`. `Development.xcconfig` includes it when present, so it
can override `API_BASE_URL`, `KRIA_GOOGLE_CLIENT_ID`, and
`KRIA_GOOGLE_REDIRECT_SCHEME` without changing tracked configuration.

The app has no production credentials in source. Set environment-specific API
values in the xcconfig files or in the Xcode scheme, and keep real secrets in
the developer's keychain/CI secret store.
