# Account access and deletion

Source under `src/apps/ios/`: `Kria/Features/SignInView.swift`,
`Kria/Features/AccountPrivacyViews.swift`, `Kria/Features/AccountDeletionView.swift`
and `Tests/KriaUITests/AccountPrivacyUITests.swift`.

Run `drive account`. Eight tests drive preferences, learning consent, explicit AI
consent, acceptance into the workspace, confirmed deletion back to sign-in, and
invalid-code recovery. The account transport is a fixture. No real account is
deleted. A failed confirmation must leave the account open.

The user route is sign-in, consent, workspace, Manage account, Delete account.
Real Google identity, session refresh, server erasure and the release candidate
require separate account/service checks. Consent entry does not prove Google login.
Keep the result bundle and inspect its screenshots. Never delete the owner's account.
