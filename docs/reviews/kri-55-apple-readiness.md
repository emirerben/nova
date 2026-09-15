# KRI-55: Apple readiness review

Initial source audit: 2026-09-15, base `6b0b89d4e`. Skills: Apple Design
(local project skill) and `github/awesome-copilot`'s `apple-appstore-reviewer`
(installed with a pinned entry in `skills-lock.json`). This first pass preceded
application changes. Verification and remediation status follow at the end.

## 1. Executive summary

- Kria is an account-based SwiftUI video editor: sign in, create with footage and AI, edit/export a finished video.
- The target is external TestFlight on iOS 18+, including the supported iPad family. XcodeGen, SwiftData, AVFoundation, and Apple's OpenAPI packages supply the native foundation.
- P0: no app privacy manifest despite app-local defaults and disk-capacity API use.
- P1: Account exposes sign-out but no account deletion. The API already offers email-confirmed erasure; Sign in with Apple revocation is missing.
- P1: AI upload consent describes analysis without naming processors, and chat can reach AI before media consent.
- Fast wins: accessible Terms/Privacy links, clear AI disclosure before first use, and a truthful privacy manifest.
- Design findings focus on reachable editor controls and gallery layout at accessibility text sizes. The unused onboarding screen is not a release-path defect.
- App Store Connect declarations, signing authority, Apple private-email relay delivery, and a live full render remain external verification requirements. Static source review cannot prove them.

## 2. Risk register

| Priority | Area | Finding | Review risk | Evidence at initial revision | Recommendation | Effort | Confidence |
|---|---|---|---|---|---|---|---|
| P0 | Privacy | Missing required-reason manifest | Upload rejection | No `PrivacyInfo.xcprivacy`; `AppState`/`BackgroundUploads` use UserDefaults, `DeviceRenderSessions.decision` reads free capacity | Add accurate app manifest and built-bundle guard | S | High |
| P1 | Account | No in-app deletion; Apple authorization not revoked | Account-deletion requirement unmet | `Views.swift:AccountView`; `me.py:confirm_account_deletion`; `AppleAuthProvider` discards authorization code | Reuse verified erasure endpoint, add native confirmation and durable Apple token revocation | L | High |
| P1 | Privacy | Third-party AI disclosure incomplete | User permission does not explain recipients | `AnalysisUploadConsentView`, `CloudUploadConsentView`, root routes directly to chat | Disclose Google Gemini/OpenAI and processed data before AI access, with explicit consent and policy link | M | High |
| P1 | Privacy | Local account state survives sign-out | Another sign-in can see stale account content | `AuthStore.signOut` clears credentials only; `AppModel.init` loads unscoped SwiftData cache | Coordinate account teardown; invalidate in-flight work and clear app-owned account data | M | High |
| P1 | Privacy/UX | Legal copy has no links | Privacy policy inaccessible in app | `SignInView` plain Text; Account has no policy/support links | Add links to existing public pages and support address | S | High |
| P2 | UX | Text inspector fixed dimensions | Large-text controls may clip | `NativeEditorTextPanel.styleControls`, multiline editor's fixed height | Adaptive stacks/minimum sizes; verify at accessibility text sizes | M | Medium (runtime needed) |
| P2 | UX | Gallery fixed two-column grid | Large text truncates titles | `GalleryView`, `GalleryProjectCard` | One column at accessibility sizes; wrapping titles | S | High |
| P2 | UX | Timeline lane text uses fixed 9pt font | Essential labels do not follow preferred text size | `NativeEditorMediaViews.laneLabels` | Scale labels while preserving timeline geometry and accessibility actions | M | High |

## 3. Detailed findings

### Privacy & data handling

Required-reason APIs are used in the app, so their reasons belong in its bundled
manifest. App-local preferences and capacity checks before rendering must be declared according to their actual use.
Verify the manifest in the compiled app, not just the source tree. Data categories
must reflect identity, footage/audio, and AI conversation content; do not claim
that no data is collected. App Store Connect's declarations still need to match.

The two media-consent sheets omit AI recipients and first-use chat has no separate
AI consent. Add a clear, explicit first-use permission screen and retain the
per-upload choices. Test decline, restart, accept, sign-out, and the first network
request; declining must not submit prompts or footage.

Account caches and upload recovery are app-global. Teardown must prevent a late
request or background upload from repopulating state after logout/deletion.
Test with an in-flight project load and pending upload, then a second sign-in.

### Permissions & entitlements

Photos save and narration requests have purpose strings. Footage uses the system
Photos picker and Files picker; no camera, location, tracking, or advertising SDK
was found in the native app. Sign in with Apple entitlement is present. Keep
permission requests at point of use; test refusal and recovery. No new permission
is warranted by this audit.

### Monetization (IAP/subscriptions)

No StoreKit purchase, subscription, paywall, or external checkout is present in the
native source inspected. Purchase/restore checks are not applicable to this build.

### Account & authentication

Both Google and Apple sign-in are present; a DEBUG-only local account does not
ship in Release. Account-backed cloud projects explain why authentication is used.
Reuse the existing email-token verification and ownership checks for deletion.
Apple revocation must survive database commit and worker/network failure, and must
never accept a credential belonging to a different account. Test incorrect or
expired email codes, failed reauthentication, retries, transaction rollback, and
revocation retries. Do not exercise deletion on a real user account during QA.

### Content / UGC / external links

The inspected Gallery is the signed-in user's own output, not a public social feed.
No in-app messaging between users was found; do not invent moderation controls for
that absent feature. Add accessible policy, terms, and support links. Confirm the
published pages load and that the account holder approves their current content
and rights declarations before submission; repository legal documentation still
records pending review and music-rights questions.

### Technical stability & performance

Existing recoverable network errors, upload retries, native renderer cancellation,
and exact native CI/TestFlight workflow are reusable. Build and run the full native
gate after changes; test offline account operations and slow requests. App Store
review access and a live render need the deployed API and appropriate test account.

### UX & reviewability

Shared custom fonts already scale with Dynamic Type. Several fixed inspector
frames and the 9pt system timeline label do not provide enough room. Verify actual
screens before changing geometry. Preserve content timing/canvas geometry and all
existing editor accessibility actions. Reduce Motion is already consulted by chat
and editor; fixed-duration drawer settling is a polish observation, not a reason
to redesign the drawer for TestFlight.

## 4. Reviewer experience checklist

| Step | Initial result |
|---|---|
| Install and launch | Build/run verification pending |
| First-run purpose and sign-in | Source clear; live provider auth unverified |
| Explicit AI data-sharing permission | Gap found |
| Photos/Files import and microphone denial | Source routes present; runtime pending |
| Create, edit, play, export | Existing tests; full gate and live render pending |
| Purchase/restore | Not applicable |
| Privacy/Terms/support links | Gap found |
| Account deletion | Native gap and Apple revocation gap found |
| Offline/empty/error recovery | Existing UI; regression checks pending |

## 5. Suggested reviewer notes (draft)

Kria turns footage into short videos. Sign in with Apple or Google. The account
keeps your private projects and finished videos connected. No purchase or paid
subscription is required in this build.

Review the AI data-sharing screen, accept if you wish to use AI editing, then
create a project, choose an edit format, and add your own short footage from
Photos or Files. Review the proposed direction before generation. Open the ready
video to edit and use Save to Photos to export. Photos permission is requested for
saving; microphone permission is requested only if you record narration.

Account includes privacy, terms, support, sign-out, and email-confirmed deletion.
For Apple-linked accounts, deletion also asks you to authorize with the same Apple
account so Kria can revoke its access. Reviewer contact: use the protected
`KRIA_BETA_REVIEW_CONTACT_*` configuration. Do not put real credentials in this file.

These notes describe the intended repaired build. Confirm them against the final
Release build before entering them in App Store Connect. There is no Release demo
login; if review requires credentials, provide a dedicated account through App
Store Connect rather than enabling the DEBUG test harness.

## 6. Next pass

KRI-55 explicitly requests autoship, so the implementation pass is already
authorized: repair the evidenced gaps, run focused regression checks and the full
native gate, then prepare the PR for the autoship approval gate. No release-readiness
claim is made until the verification evidence and external prerequisites are checked.

## Remediation and verification

The implementation adds a bundled privacy manifest, explicit account-scoped AI
permission before chat, named processors in both media-sharing choices, working
privacy/terms/support links, and native email-confirmed account deletion. Apple
reauthorization is requested only at deletion time. The API verifies the complete
linked Apple identity set before exchanging single-use codes, and commits
encrypted revocation work with account erasure. Workers retry provider or broker
failures. Ordinary sign-in does not require the new revocation credentials.

The text inspector now adapts its editing, timing, style, and color controls for
accessibility text sizes; timeline lane labels scale, and the gallery uses one
column with wrapping titles at accessibility sizes. The public privacy copy now
describes Apple sign-in and stored provider identifiers accurately.

### Evidence (2026-09-15)

- Native unit gate: 285 tests executed, zero failures, one expected unsigned
  simulator Keychain-entitlement skip. The built app contains the privacy
  manifest and expected API reasons/data declarations.
- New account UI flows: consent acceptance, decline at accessibility5,
  unavailable deletion email, invalid confirmation/cancel, and confirmed
  deletion returning to sign-in passed against an offline HTTP fixture.
- Account/auth backend regression set: 105 tests passed, including targeted
  route/outbox/lease tests added after review. Real PostgreSQL tests verify
  rollback/commit durability and advisory-lock exclusion, with migration `0106`
  applied only to an isolated temporary test database.
- Release configuration compiled successfully for the iOS simulator. This was
  not a signed device archive or TestFlight upload.
- Web TypeScript check, scoped privacy-page ESLint, and final pre-PR gate passed.
- Public Privacy and Terms URLs returned HTTP 200.
- Full native UI run: 49 tests executed, 47 passed initially. The existing
  long-press timing test passed unchanged in isolation. The new accessibility5
  inspector test passed after replacing overshooting swipes with short directed
  drags and handling lazily exposed color controls. The focused reruns passed;
  the complete suite was not rerun after the test-only correction, so this is
  not a clean post-correction 49-test suite result.

### Open release blockers

1. **Account cache isolation:** automatic sign-out cleanup of project/upload
   metadata is awaiting explicit user approval. Automatic approval review
   rejected both blanket media deletion and the narrower metadata-only cleanup
   because unsynced records could be lost. Local originals and exports have not
   been erased. The existing unscoped account cache remains a privacy gap until
   an approved teardown or account-isolation design is implemented and tested.
2. **Production deletion configuration:** a read-only secret-name inventory
   found `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY`, and
   `RESEND_API_KEY` absent. Mobile client IDs, JWT secret, and token encryption
   key were present. The repaired route fails unavailable rather than claiming
   to send a confirmation email when configuration is missing.
3. **External review evidence:** verify Apple private-relay email delivery,
   real provider login/deletion/revocation, App Store Connect privacy answers,
   signed archive/TestFlight distribution, and a live create/edit/export flow.
   Fixture results do not establish these. Follow the TestFlight runbook and
   resolve the existing legal/content-rights review items before submission.

This is an implemented readiness improvement with outstanding release blockers,
not a claim of App Store approval or successful TestFlight distribution.

## Sources

Official Apple pages read on 2026-09-15 using gstack browse:

- [App Review Guidelines: privacy, accounts, and AI sharing](https://developer.apple.com/app-store/review/guidelines/)
- [Offering account deletion](https://developer.apple.com/support/offering-account-deletion-in-your-app/)
- [Required-reason API declarations](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api)
- [Approved reason codes](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacyaccessedapitypes/nsprivacyaccessedapitypereasons)
