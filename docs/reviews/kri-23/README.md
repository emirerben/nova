# KRI-23 native design review

Reference: [Paper Mobile Flow](https://app.paper.design/file/01M166BHQ2QZ6EMMBB1D40GV9W/3-0).

The review captures use the real SwiftUI components in deterministic Debug fixtures.
They represent UI states, not a completed cloud render. The editor keeps its existing
native toolbar, capability gates, local preview, and multi-lane timeline. Native
system authentication, keyboard, picker, permission, and share surfaces retain
platform behavior. Rendering progress remains indeterminate when the server does
not supply a percentage; the preview does not fabricate Paper's example progress.

## Visual evidence

- [Rounded drawer top edge](rounded-drawer-top.jpg): final full-height surface with
  continuous corners. Tint and corner radius follow drag progress; touch-down uses
  a 3-point threshold and direct tracking, with one ease-out settling animation.
  Opening or closing triggers a light, low-intensity haptic. Swipes that start
  inside the format carousel stay with its horizontal scrolling.
  This supersedes the rectangular edge in the earlier drawer capture.

- [Updated drawer and centered header](drawer-toggle.jpg): the menu button moves
  with the main screen and toggles the drawer. Horizontal swipes also open and
  close it, and the shifted workspace uses the menu button’s warm background.
  New chat remains in the drawer.
  This follow-up supersedes the header and drawer navigation in the original captures.

- [All eight mobile states](mobile-flow.jpg): format, footage, direction, rendering,
  ready, editor, projects, Gallery. Reviewed against each Paper artboard.
- [Supporting screens](supporting-screens.jpg): sign-in, account, consent, recovery,
  keyboard, text inspector, large-text drawer, and consent after explicit selection.
- [Device layouts](device-layouts.jpg): iPad mini and iPhone 17 Pro Max; compact
  captures use iPhone 17e. All simulators run iOS 26.5.

The compact, large-phone and tablet checks covered spacing, wrapping, semantic
colors, card bounds, readable controls, and reachable navigation. Accessibility3
captures also covered format, direction, sign-in, drawer, and consent. Consent
scrolls while its Continue action stays pinned; the action is disabled until the
switch is selected. The software keyboard leaves the composer visible and supports
multiple lines. The accessibility tree exposes full project titles, descriptive
icon labels, selected project state and disabled controls, and hides the underlying
chat while the drawer is open. Long header titles truncate within their flexible
slot rather than displacing the two 44-point controls.

## Behavior coverage

ProjectActionsTests covers rename success/conflict/failure, revision and idempotency
payloads, stale projection suppression, empty-204 deletion, selection/cache cleanup,
render/upload restrictions, delete conflicts, failure preservation, truly overlapping
project/library responses, and duplicate create requests with retry after failure.
ProjectRestorationTests covers preferred-project restoration, authoritative title
and revision, full transcript history, and missing-project fallback. UI tests cover
new chat, drawer/Gallery navigation, rename/delete cancellation, real bundled-video
playback, editing, undo/redo, save/back protections, inspectors and accessibility
text with Reduce Motion, format-carousel swipe exclusion, and chat-bubble wrapping
at accessibility text sizes. Existing media/runtime tests cover consent and explicit
render approval contracts.

## Verification limits

The final `make ios-verify` run completed 155 unit tests (one existing skip) and
17 UI tests with zero failures. The pre-PR gate also passed.

No authenticated live footage-to-cloud-render journey was run in this session.
Live ready playback, server save and Gallery refresh still need a staging session;
the simulator checks use bundled media and protocol fixtures. VoiceOver labels and
focus isolation were inspected through the accessibility tree; spoken VoiceOver
navigation was not exercised on hardware. Physical-device performance and exports
remain subject to the existing native rollout gates.
