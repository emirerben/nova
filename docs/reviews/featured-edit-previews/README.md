# Featured edit previews

This change implements the creator-supplied preview refresh in KRI-638 and
KRI-643. The acceptance record comes from the user requests in this chat.

Onboarding plays Bosphorus, Lisbon, sunset, then office, one at a time.
Finished slots return to their opening posters. Landscape frames preserve their
aspect ratio without black bars. New Chat plays only the centered video card.
Slider holds for 1.2 seconds and swipes for 0.35 seconds. Narrated keeps its
existing image because the user deferred a replacement.

The final user instruction retains production email sign-in so the KRI-111 Apple
reviewer demo form stays reachable. Local-account access remains development-only.

## Evidence

The report documents retain unedited `xcresulttool get test-results summary` JSON.
`acceptance.md` records the independently supplied expectations.

- `drawer-gesture.md` records the reproduced CI failure and three passing runs after correcting the swipe surface.
- `native-integrated.md` records five focused native checks after merging current main, plus the test-inventory validation.
- `native-debug-final.md` covers all ten focused Debug UI checks, including
  four-edit sequencing, centered playback, Slider timing/pause, sign-in and tile sizes.
- `production-signin.md` verifies the final Release app shows email sign-in and hides local-account access.
- `native-unit.md` records 2,087 tests, including 26 explicit skips, with no failures.

The original Debug report predates the final email-entry restoration.
The current integration report includes that restoration. The Release report and
screenshot verify the final production sign-in state.

The native evidence is from an iPhone 17 Pro simulator on iOS 26.5. It does not
prove a physical-device build, an App Store review, or a production rollout.
The review and documentation audit used the current agent because the user
explicitly disabled delegation. No independent-agent review is claimed.

## Media

The five `featured-*` movies are intentional, muted app preview resources
re-encoded from the user-selected examples. Original attachments, signed URLs,
and private job output locations are excluded. They add about 12 MB to the app.
The app loads them from its bundle, so sign-in previews work without networking.

## Reproduce

Run the repository native unit gate with `KRIA_IOS_TEST_MODE=unit make ios-verify`.
Build the Debug UI test bundle, then run `FeaturedEditPreviewUITests`,
`SignInUITests`, and `ProductionSignInUITests`. Run
`SlidePostUITests/testSliderFormatTileMatchesTheOtherTilesPictureSize` for the
shared preview dimensions.

For the production sign-in check, use the `Kria Production SignIn Check` scheme.
It uses Release and includes only the production sign-in UI check, so Debug-only
unit-test helpers are excluded. Neither `DEBUG` nor `DEV_AUTH` is added. The check
launches the normal Release app and asserts that email sign-in is present and local-account access is absent.

The Release UI verification used code-coverage instrumentation and testable module
exports to reuse the simulator build. These settings do not define `DEBUG` or
`DEV_AUTH` and do not change the sign-in conditions.
