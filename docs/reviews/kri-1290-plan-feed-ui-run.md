# KRI-1290 plan feed wording: simulator UI run

Run on iPhone 17 Pro simulator (iOS 26.5), local, at commit 52fcf4c47, 2026-11-08:

    xcodebuild -project Kria.xcodeproj -scheme Kria -destination "platform=iOS Simulator,id=<iPhone 17 Pro>" \
      -only-testing:KriaUITests/CreationUITests/testLivePlanFeedFullSequence \
      -only-testing:KriaUITests/CreationUITests/testLivePlanFeedRendersStructuredPayloadsAndOffersReview test

Result: `** TEST SUCCEEDED **`
- testLivePlanFeedFullSequence passed (55.6 s): "Coming up" heading and waiting chips shown, then sections decide in order.
- testLivePlanFeedRendersStructuredPayloadsAndOffersReview passed (39.7 s): header reads "Making your video" while rendering.

Scope: asserts the new header and heading text on the fixture feed. It does not cover the 11-07 production event log, a real device, or telling asked-for from not-asked-for sound effects (the Sound effects chip is hidden for every video).
