# Delivery

## Sub-features

Device-render status, Stop, playback, Save and Share availability. Source is
`Kria/Features/DeviceRenderPanel.swift`, `Kria/Core/DeviceRenderSessions.swift`
and `Tests/KriaUITests/DeviceRenderUITests.swift` under `src/apps/ios/`.

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
An actual acknowledged export is separate from a ready job and from fulfillment.
