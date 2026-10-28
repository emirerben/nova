# Acceptance

Expectations supplied by the creator before implementation.

```json
{
  "request_provenance": "User instructions in this Codex chat, 2026-10-25 through 2026-10-28; KRI-638 and KRI-643. The final instruction on 2026-10-28 retains production email sign-in for Apple reviewer access.",
  "onboarding_order": [
    "bosphorus",
    "lisbon",
    "sunset",
    "office"
  ],
  "onboarding_behavior": "One muted video at a time; completion advances to the next; fourth wraps to first; inactive slots show opening posters.",
  "new_chat_behavior": "Only the centered card plays. Off-center cards pause. No black bars. Narrated keeps its existing image.",
  "drawer_gestures": "Carousel swipes keep the drawer closed; swipes on the workspace heading open and close the drawer; the header button still toggles it.",
  "slider_hold_seconds": 1.2,
  "slider_transition_seconds": 0.35,
  "production_signin": [
    "Apple",
    "Google",
    "email reviewer form"
  ],
  "development_signin": [
    "Apple",
    "Google",
    "email reviewer form",
    "local account"
  ],
  "media_policy": "Five intentionally curated app preview encodes from creator-supplied examples. Original uploaded files, signed URLs, and job output paths are excluded.",
  "release_check": "ProductionSignInUITests/testBuildControlsDevelopmentSignIn is run in both DEV_AUTH Debug and production Release, without DEV_AUTH."
}
```
