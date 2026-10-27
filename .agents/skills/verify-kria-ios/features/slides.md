# Slides

## Sub-features

Selecting slides, drawing, saving and switching slides. Source is
`Kria/Features/SlidePostWorkspaceView.swift` and
`Tests/KriaUITests/SlidePostUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Create a slide post, open a slide, draw on it, save and switch slides.

## Driving it with XCUITest

Run `drive slides`. The helper selects
`SlidePostUITests/testDrawOnASlideLeavesInkThatSurvivesSaveAndSlideSwitching`.
The ink must remain after saving and returning to its slide.

## Gotchas

The fixture transport proves the native interaction and persistence contract.
It does not prove production storage, publication, all visual styles or album
permissions. Mixed video/photo export needs its own actual output inspection.
