# Media clarification

## Sub-features

Video and Visuals photo candidates, thumbnails, selection and submitted answers.
Source is `Kria/Features/CanonicalChatComponents.swift` and
`Tests/KriaUITests/CreationUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Ask Kria to use named footage or a photo. Open its clarification card and pick
the requested media. Previously settled instructions must survive the answer.

## Driving it with XCUITest

Run `drive media`. The helper selects
`CreationUITests/testPickerShowsAVisualsPhotoWithItsThumbnailAndAPhotoName`.
The photo must be named as a photo and display its own thumbnail. Backend
continuation is separately checked by `test_kri950_picker_inventory.py`.

## Gotchas

A fixture candidate does not prove correct matching of real footage. A single
take needs a moment time rather than another identical tap. Wrong-source and
changed-generation controls must remain red in the backend verification.
