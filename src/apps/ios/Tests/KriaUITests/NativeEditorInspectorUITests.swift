import XCTest
import UIKit

@MainActor
final class NativeEditorInspectorUITests: XCTestCase {
    func testSlowTimelineScrubPausesPlaybackAndDisplaysMatchingClip() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-color-cuts"]
        app.launch()
        let timeline = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        XCTAssertTrue(timeline.waitForExistence(timeout: 20))
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 12))
        let play = app.buttons["native-editor-play-pause"]
        play.tap()
        let first = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.23))
        first.press(forDuration: 0.05, thenDragTo: first.withOffset(CGVector(dx: -20, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.1)
        XCTAssertEqual(play.label, "Play preview", "Scrubbing must stop the playback clock")
        func assertDisplayedClip() {
            let screenshot = app.screenshot()
            let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch.frame
            let image = screenshot.image.cgImage!
            let scale = Double(image.width) / app.frame.width
            let sample = image.cropping(to: CGRect(x: preview.midX * scale, y: (preview.minY + preview.height * 0.2) * scale, width: 1, height: 1))!
            var rgba = [UInt8](repeating: 0, count: 4)
            let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(sample, in: CGRect(x: 0, y: 0, width: 1, height: 1))
            let label = app.descendants(matching: .any)["native-editor-current-time"].firstMatch.value as? String ?? ""
            let time = Double(label.split(separator: ":").last ?? "0") ?? 0
            // The accessibility time rounds to a tenth; skip the ambiguous
            // sample exactly on the cut and verify the frames on either side.
            if abs(time - 2) > 0.1 {
                XCTAssertGreaterThan(rgba[time < 2 ? 0 : 2], 220, "Wrong clip displayed at \(label): \(rgba)")
                XCTAssertLessThan(rgba[time < 2 ? 2 : 0], 40, "Stale clip displayed at \(label): \(rgba)")
            }
        }
        for index in 0..<8 {
            assertDisplayedClip()
            let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.23))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: index < 4 ? -45 : 45, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
        }
        app.descendants(matching: .any)["native-editor-clip-2"].firstMatch.tap()
        let nearCut = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.23))
        nearCut.press(forDuration: 0.05, thenDragTo: nearCut.withOffset(CGVector(dx: -15, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
        for index in 0..<6 {
            let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.65, dy: 0.23))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: index.isMultiple(of: 2) ? 30 : -30, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
            assertDisplayedClip()
        }
        app.descendants(matching: .any)["native-editor-clip-1"].firstMatch.tap()
        play.tap()
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let advanced = NSPredicate { _, _ in
            let label = time.value as? String ?? ""
            return (Double(label.split(separator: ":").last ?? "0") ?? 0) >= 2.4
        }
        expectation(for: advanced, evaluatedWith: time)
        waitForExpectations(timeout: 5)
        assertDisplayedClip()
    }

    func testTimelineCanZoomBeyondPreviousLimit() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 8))
        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        timeline.pinch(withScale: 2.5, velocity: 2)
        timeline.pinch(withScale: 2.5, velocity: 2)
        let value = timeline.value as? String ?? ""
        let percentage = value.components(separatedBy: "Zoom ").last?.components(separatedBy: " percent").first ?? "0"
        XCTAssertGreaterThan(Double(percentage) ?? 0, 350)
        clip.tap()
        let before = Double(clip.value as? String ?? "0") ?? 0
        let handle = app.descendants(matching: .any)["native-editor-trim-leading"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 2))
        let point = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        point.press(forDuration: 0.1, thenDragTo: point.withOffset(CGVector(dx: 24, dy: 0)))
        XCTAssertLessThan(Double(clip.value as? String ?? "0") ?? 0, before)
    }

    func testTappingClipAlignsItsBeginningUnderPlayhead() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        let playhead = app.descendants(matching: .any)["native-editor-playhead"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 8))
        let timeline = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.23))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -60, dy: 0)))
        XCTAssertGreaterThan(abs(clip.frame.minX - playhead.frame.midX), 30)
        clip.tap()
        let centered = NSPredicate { _, _ in abs(clip.frame.minX - playhead.frame.midX) < 3 }
        expectation(for: centered, evaluatedWith: clip)
        waitForExpectations(timeout: 3)
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        XCTAssertEqual(time.value as? String, "0:00.0")
    }

    func testTimelineSwipesSeekUnderFixedPlayhead() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()
        let timeline = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        XCTAssertTrue(timeline.waitForExistence(timeout: 8))
        let initialTime = time.value as? String
        let playhead = app.descendants(matching: .any)["native-editor-playhead"].firstMatch
        XCTAssertTrue(playhead.exists)
        let originalX = playhead.frame.midX
        for row in [0.23, 0.5, 0.85] {
        let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: row))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -110, dy: 0)))
        XCTAssertNotEqual(time.value as? String, initialTime)
        XCTAssertEqual(playhead.frame.midX, originalX, accuracy: 1)
        let reverse = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: row))
        reverse.press(forDuration: 0.05, thenDragTo: reverse.withOffset(CGVector(dx: 110, dy: 0)))
        XCTAssertEqual(time.value as? String, initialTime)
        XCTAssertEqual(playhead.frame.midX, originalX, accuracy: 1)
        }
    }

    func testSourceVideoScrubbingKeepsLatestPosition() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launch()
        let timeline = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        XCTAssertTrue(timeline.waitForExistence(timeout: 8))
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 12))
        let initialTime = time.value as? String
        let playhead = app.descendants(matching: .any)["native-editor-playhead"].firstMatch
        XCTAssertTrue(playhead.exists)
        let originalX = playhead.frame.midX
        for row in [0.23, 0.5, 0.85] {
        let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: row))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -110, dy: 0)))
        XCTAssertNotEqual(time.value as? String, initialTime)
        XCTAssertEqual(playhead.frame.midX, originalX, accuracy: 1)
        let reverse = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: row))
        reverse.press(forDuration: 0.05, thenDragTo: reverse.withOffset(CGVector(dx: 110, dy: 0)))
        XCTAssertEqual(time.value as? String, initialTime)
        XCTAssertEqual(playhead.frame.midX, originalX, accuracy: 1)
        }
    }

    func testTimelineResizeShrinksPreviewAndRestoresIt() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()
        let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 8))
        let originalHeight = preview.frame.height
        let originalHandleY = handle.frame.midY
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -140)))
        XCTAssertLessThan(preview.frame.height, originalHeight - 80)
        XCTAssertLessThan(handle.frame.midY, originalHandleY - 80)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch.isHittable)
        let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: 300)))
        XCTAssertEqual(preview.frame.height, originalHeight, accuracy: 2)
    }

    func testPreviewPreparationSurvivesLoadedViewTransition() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-delayed-source"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 12))
        XCTAssertFalse(app.staticTexts["Preparing preview"].exists)
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
    }

    func testTextReturnAndDeleteKeepCanvasLinesAligned() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 12))
        app.buttons["native-editor-tool-text"].tap()
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap()
        input.typeText("First\nSecond")
        XCTAssertEqual(input.value as? String, "First\nSecond")
        app.buttons["native-editor-text-done"].tap()
        let multiline = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Text: First\nSecond")).firstMatch
        XCTAssertTrue(multiline.waitForExistence(timeout: 3))
        let multilineHeight = multiline.frame.height
        app.buttons["Edit text"].tap()
        let edit = app.textViews["native-editor-text-content"]
        XCTAssertTrue(edit.waitForExistence(timeout: 3))
        edit.tap()
        edit.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 7) + " Second")
        XCTAssertEqual(edit.value as? String, "First Second")
        app.buttons["native-editor-text-inspector-done"].tap()
        let single = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Text: First Second")).firstMatch
        XCTAssertTrue(single.waitForExistence(timeout: 3))
        XCTAssertLessThan(single.frame.height, multilineHeight)
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
    }

    func testSourcePreviewTextCreationAndAnimations() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launch()
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 12))
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        let initial = XCTAttachment(screenshot: app.screenshot())
        initial.name = "Source compositor editor"; initial.lifetime = .keepAlways; add(initial)
        app.buttons["native-editor-tool-text"].tap()
        let input = app.descendants(matching: .any)["native-editor-new-text-input"].firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap(); input.typeText("Live text")
        XCTAssertGreaterThan(preview.frame.height, 100)
        XCTAssertLessThan(preview.frame.maxY, app.keyboards.firstMatch.frame.minY)
        let keyboard = XCTAttachment(screenshot: app.screenshot())
        keyboard.name = "Live text keyboard"; keyboard.lifetime = .keepAlways; add(keyboard)
        app.buttons["native-editor-text-done"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-panel"].waitForExistence(timeout: 3))
        Thread.sleep(forTimeInterval: 1)
        let textObject = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Text: Live text")).firstMatch
        XCTAssertTrue(textObject.waitForExistence(timeout: 3))
        let initialCenter = CGPoint(x: textObject.frame.midX, y: textObject.frame.midY)
        let center = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: initialCenter.x, dy: initialCenter.y))
        center.press(forDuration: 0.1, thenDragTo: center.withOffset(CGVector(dx: -24, dy: -30)), withVelocity: 20, thenHoldForDuration: 0.1)
        let moveSamples = (preview.value as? String)?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
        XCTAssertGreaterThan(Int(moveSamples) ?? 0, 4, "Moving must use the immediate text layer")
        Thread.sleep(forTimeInterval: 0.5)
        XCTAssertLessThan(textObject.frame.midX, initialCenter.x - 15)
        XCTAssertLessThan(textObject.frame.midY, initialCenter.y - 20)
        let corner = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: textObject.frame.maxX, dy: textObject.frame.maxY))
        corner.press(forDuration: 0.1, thenDragTo: corner.withOffset(CGVector(dx: 24, dy: 18)), withVelocity: 20, thenHoldForDuration: 0.1)
        let cornerSamples = (preview.value as? String)?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
        XCTAssertGreaterThan(Int(cornerSamples) ?? 0, 4, "Corner resizing must use the immediate text layer")
        let resizedSize = app.textFields["native-editor-text-size"].value as? String
        let movedCenter = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: textObject.frame.midX, dy: textObject.frame.midY))
        movedCenter.press(forDuration: 0.05, thenDragTo: movedCenter.withOffset(CGVector(dx: 12, dy: -15)), withVelocity: 60, thenHoldForDuration: 0.05)
        XCTAssertEqual(app.textFields["native-editor-text-size"].value as? String, resizedSize, "Moving immediately after resizing must preserve size")
        let size = app.textFields["native-editor-text-size"]
        XCTAssertTrue(size.waitForExistence(timeout: 3))
        size.doubleTap()
        if app.menuItems["Select All"].waitForExistence(timeout: 1) { app.menuItems["Select All"].tap() }
        size.typeText("600")
        app.buttons["Increase text size"].tap()
        XCTAssertEqual(size.value as? String, "604")
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        let largeText = XCTAttachment(screenshot: app.screenshot())
        largeText.name = "Large text size control"; largeText.lifetime = .keepAlways; add(largeText)
        // The source renderer has finished preparing the isolated text layer.
        // Finger samples must use that layer, not mutate/recompile the document.
        Thread.sleep(forTimeInterval: 1)
        preview.pinch(withScale: 1.4, velocity: 0.4)
        Thread.sleep(forTimeInterval: 0.5)
        preview.pinch(withScale: 0.75, velocity: -0.4)
        let samples = (preview.value as? String)?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
        XCTAssertGreaterThan(Int(samples) ?? 0, 4)
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        app.buttons["Animation"].tap()
        for phase in ["In", "Out", "Loop"] {
            app.buttons[phase].tap()
            for effect in phase == "Loop" ? ["None", "Pulse", "Bounce", "Float"] : ["None", "Fade", "Pop", "Slide", "Typewriter"] {
                let choice = app.buttons["\(phase) animation \(effect)"]
                XCTAssertTrue(choice.exists); choice.tap()
            }
        }
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        let animation = XCTAttachment(screenshot: app.screenshot())
        animation.name = "Live text animation controls"; animation.lifetime = .keepAlways; add(animation)
        app.buttons["native-editor-text-inspector-done"].tap()
        app.buttons["native-editor-tool-text"].tap()
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap(); input.typeText("Cancel this")
        app.buttons["native-editor-text-cancel"].tap()
        XCTAssertFalse(input.exists)
    }

    func testEditorChromeFitsViewportAndEveryToolRemainsReachable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let timeline = app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch
        let toolRail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertTrue(timeline.exists)
        XCTAssertTrue(toolRail.exists)

        XCTAssertLessThanOrEqual(preview.frame.height, 338)
        XCTAssertLessThanOrEqual(timeline.frame.maxY, toolRail.frame.minY + 1)
        XCTAssertLessThanOrEqual(toolRail.frame.maxY, app.frame.maxY + 1)

        let styles = app.buttons["native-editor-tool-styles"]
        if !styles.isHittable { toolRail.swipeLeft() }
        XCTAssertTrue(styles.waitForExistence(timeout: 2))
        XCTAssertTrue(styles.isHittable)
    }

    func testProjectedCaptionsAppearInCaptionLaneOnly() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-projected-captions"]
        app.launch()
        let captions = app.descendants(matching: .any)["native-editor-lane-captions"].firstMatch
        XCTAssertTrue(captions.waitForExistence(timeout: 8))
        let captionID = "native-editor-timeline-text-00000000-0000-4000-8000-000000000301"
        XCTAssertTrue(captions.buttons[captionID].exists)
        let text = app.descendants(matching: .any)["native-editor-lane-text"].firstMatch
        XCTAssertFalse(text.buttons[captionID].exists)
        XCTAssertTrue(text.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000300"].exists)
        XCTAssertEqual(captions.buttons[captionID].label, "Spoken words")
    }

    func testMovingSelectedTextDoesNotOpenStylePanel() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()
        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        text.tap()
        let before = text.value as? String
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.6, thenDragTo: start.withOffset(CGVector(dx: 20, dy: 0)))
        XCTAssertNotEqual(text.value as? String, before)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch.exists)
        // A subsequent deliberate tap still opens the editor.
        text.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-panel"].waitForExistence(timeout: 3))
    }

    func testQuickSwipeStartingOnTextScrubsWithoutMovingBlock() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()
        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let before = time.value as? String
        let timing = text.value as? String
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -45, dy: 0)))
        XCTAssertNotEqual(time.value as? String, before)
        XCTAssertEqual(text.value as? String, timing)
        let reverse = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        reverse.press(forDuration: 0.05, thenDragTo: reverse.withOffset(CGVector(dx: 45, dy: 0)))
        XCTAssertEqual(time.value as? String, before)
        XCTAssertEqual(text.value as? String, timing)
    }

    func testLongPressMovesTextTimingWithoutOpeningInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()
        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        let second = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000101"].firstMatch
        XCTAssertEqual(text.frame.midY, second.frame.midY, accuracy: 1)
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.6, thenDragTo: start.withOffset(CGVector(dx: 35, dy: 0)))
        XCTAssertGreaterThan(abs(text.frame.midY - second.frame.midY), 30)
        let timing = text.value as? String ?? ""
        XCTAssertFalse(timing.hasPrefix("00:00.0 to"), timing)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch.exists)
        let timeline = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let beforeSwipe = time.value as? String
        let swipe = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.25))
        swipe.press(forDuration: 0.05, thenDragTo: swipe.withOffset(CGVector(dx: -70, dy: 0)))
        XCTAssertNotEqual(time.value as? String, beforeSwipe)
        let reverse = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.25))
        reverse.press(forDuration: 0.05, thenDragTo: reverse.withOffset(CGVector(dx: 70, dy: 0)))
        XCTAssertEqual(time.value as? String, beforeSwipe)
        let moveBack = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        moveBack.press(forDuration: 0.6, thenDragTo: moveBack.withOffset(CGVector(dx: -40, dy: 0)))
        XCTAssertEqual(text.frame.midY, second.frame.midY, accuracy: 1)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
    }

    func testTextFirstTapShowsTrimsAndSecondTapOpensInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()

        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        text.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-context"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
        let handle = app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch
        XCTAssertTrue(handle.exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-trim-leading"].firstMatch.exists)
        let before = text.value as? String
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: -20, dy: 0)))
        XCTAssertNotEqual(text.value as? String, before)
        XCTAssertTrue(handle.exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
        text.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-panel"].waitForExistence(timeout: 3))
        app.buttons["Edit text"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-content"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["native-editor-text-time-start"].exists)
        XCTAssertTrue(app.textFields["native-editor-text-time-end"].exists)
        XCTAssertTrue(app.buttons["native-editor-text-inspector-done"].exists)
    }

    func testCaptionSelectionOpensCaptionInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let captions = app.buttons["native-editor-timeline-caption_cue-cue-all"]
        XCTAssertTrue(captions.waitForExistence(timeout: 8))
        captions.tap()

        XCTAssertTrue(app.textFields["native-editor-selected-caption-input"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Caption settings"].exists)
    }

    func testAllPersistedLanesExposeStableTimelineIdentityAndInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let cases: [(timelineID: String, inspectorID: String)] = [
            ("native-editor-timeline-music-00000000-0000-4000-8000-000000000350", "native-editor-selected-music-track"),
            ("native-editor-timeline-sound_effect-sfx-1", "native-editor-selected-sfx-placement"),
            ("native-editor-timeline-media_overlay-overlay-1", "native-editor-selected-overlay-display-mode"),
            ("native-editor-timeline-carousel-carousel-1", "native-editor-selected-carousel-position"),
            ("native-editor-timeline-visual_block-visual-1", "native-editor-selected-visual-preset"),
            ("native-editor-timeline-motion_scene-motion-1", "native-editor-capability-reason"),
            ("native-editor-timeline-camera_effect-camera-1", "native-editor-selected-camera-intensity"),
        ]

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        for value in cases {
            tapTimelineElement(value.timelineID, in: app)
            let inspectorElement = app.descendants(matching: .any)[value.inspectorID].firstMatch
            for _ in 0..<4 {
                if inspectorElement.exists { break }
                app.swipeUp()
            }
            XCTAssertTrue(
                inspectorElement.waitForExistence(timeout: 3),
                "Inspector \(value.inspectorID) did not follow \(value.timelineID)"
            )
            let done = app.buttons["native-editor-inspector-done"]
            XCTAssertTrue(done.exists)
            done.tap()
        }
    }

    func testPreviewDragIsOneUndoableTextEdit() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()

        let text = app.descendants(matching: .any)["native-editor-preview-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        let originalPosition = "position 50%, 28%"
        XCTAssertTrue((text.value as? String)?.contains(originalPosition) == true)
        // SwiftUI exposes the preview object as the canvas-sized accessibility
        // element. Address its authored 50%/28% canvas position; the canvas
        // gesture router resolves that point against the real object rect.
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.28))
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: 34, dy: 24)))

        expectation(for: NSPredicate(format: "NOT value CONTAINS %@", originalPosition), evaluatedWith: text)
        waitForExpectations(timeout: 3)
        if app.buttons["native-editor-text-inspector-done"].exists {
            app.buttons["native-editor-text-inspector-done"].tap()
        }
        let undo = app.buttons["native-editor-undo"]
        XCTAssertTrue(undo.isEnabled)
        undo.tap()
        expectation(for: NSPredicate(format: "value CONTAINS %@", originalPosition), evaluatedWith: text)
        waitForExpectations(timeout: 3)
    }

    func testStressFixtureRemainsReachableAtAccessibilityTypeWithReduceMotion() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-stress-71"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility3"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let fixture = app.descendants(matching: .any)["native-editor-fixture-stress-71"].firstMatch
        XCTAssertTrue(fixture.waitForExistence(timeout: 8))
        XCTAssertEqual(fixture.value as? String, "Dynamic type accessibility3; reduce motion on")
        XCTAssertTrue(app.buttons["native-editor-tool-text"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch.exists)
    }

    private func tapTimelineElement(_ identifier: String, in app: XCUIApplication) {
        let element = app.descendants(matching: .any)[identifier].firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 3), "Missing timeline item \(identifier)")
        // XCUITest scrolls a descendant of SwiftUI's lane ScrollView into view
        // as part of tap(). The inspector assertion at the call site verifies
        // that the auto-scroll reached the intended item.
        element.tap()
    }
}
