# Delivery

## Sub-features

Device-render status, Stop, playback, Save and Share availability. Source is
`Kria/Features/DeviceRenderPanel.swift`, `Kria/Core/DeviceRenderSessions.swift`,
`Kria/Core/PhotoLibrarySaver.swift` and
`Tests/KriaUITests/DeviceRenderUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Approve a video, wait for rendering, play it, then save it to Photos.

## Driving it with XCUITest

Run `drive delivery`. The helper selects
`DeviceRenderUITests/testPhoneRenderReachesReadyWithPlaySaveAndShare`.
It checks Stop during work, then Play, Save and Share, sync completion and
playback dismissal. No Retry control should remain on the finished result.

## Gotchas

This UI test explicitly replaces the exporter and upload. It does not tap Save
to Photos. Run native export tests and inspect an MP4 separately. Actual Photos
permission denial and success need a physical iPhone and the named candidate.
The shared video saver retries Photos error 3301 once with the same file. A second
interruption remains a failure. Other Photos errors and cancelled tasks do not
retry. `KriaTests/PhotoLibrarySaverTests` exercises that external-boundary matrix;
its injected transactions do not prove a real Photos write. A native QA app with
the same source can exercise a real write without replacing the installed release.
Record the release candidate separately from that QA result.
An actual acknowledged export is separate from a ready job and from fulfillment.
