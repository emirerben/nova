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
        // The ruler is clear of trim handles while no clip is selected.
        let first = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.05))
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
            let previewDiagnostic = app.descendants(matching: .any)["native-editor-preview"].firstMatch.value as? String ?? ""
            // The accessibility time rounds to a tenth; skip the ambiguous
            // sample exactly on the cut and verify the frames on either side.
            if abs(time - 2) > 0.1 {
                XCTAssertGreaterThan(rgba[time < 2 ? 0 : 2], 220, "Wrong clip displayed at \(label): \(rgba), preview: \(previewDiagnostic)")
                XCTAssertLessThan(rgba[time < 2 ? 2 : 0], 40, "Stale clip displayed at \(label): \(rgba), preview: \(previewDiagnostic)")
            }
        }
        for index in 0..<8 {
            assertDisplayedClip()
            let start = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.78, dy: 0.05))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: index < 4 ? -45 : 45, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
        }
        app.descendants(matching: .any)["native-editor-clip-2"].firstMatch.tap()
        // Selection animates the action tray and moves the timeline. Wait for
        // the audio row to settle, then target its visible area well below
        // the selected clip's trim handles.
        let audioRow = app.staticTexts["native-editor-original-audio"]
        var previousY: CGFloat?
        let settled = NSPredicate { _, _ in
            let y = audioRow.frame.minY
            defer { previousY = y }
            return previousY == y
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: settled, object: audioRow)], timeout: 5), .completed)
        func audioScrubPoint() -> XCUICoordinate {
            let visible = audioRow.frame.intersection(timeline.frame)
            XCTAssertFalse(visible.isNull)
            XCTAssertGreaterThan(visible.height, 10)
            return app.coordinate(withNormalizedOffset: .zero).withOffset(
                CGVector(dx: visible.midX, dy: visible.midY)
            )
        }
        let nearCut = audioScrubPoint()
        nearCut.press(forDuration: 0.05, thenDragTo: nearCut.withOffset(CGVector(dx: -15, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
        for index in 0..<6 {
            let start = audioScrubPoint()
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: index.isMultiple(of: 2) ? 30 : -30, dy: 0)), withVelocity: 20, thenHoldForDuration: 0.3)
            assertDisplayedClip()
        }
        app.descendants(matching: .any)["native-editor-clip-1"].firstMatch.tap()
        XCTAssertEqual(app.descendants(matching: .any)["native-editor-clip-1"].firstMatch.value as? String, "2.000")
        XCTAssertEqual(app.descendants(matching: .any)["native-editor-clip-2"].firstMatch.value as? String, "2.000")
        play.tap()
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let advanced = NSPredicate { _, _ in
            let label = time.value as? String ?? ""
            return (Double(label.split(separator: ":").last ?? "0") ?? 0) >= 2.4
        }
        expectation(for: advanced, evaluatedWith: time)
        // Hosted simulator snapshots can take several seconds; the target
        // remains observable at the end, so wait for progress rather than
        // requiring a second snapshot inside a five-second window.
        waitForExpectations(timeout: 15)
        let previewState = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let finalFrame = NSPredicate { _, _ in
            time.value as? String == "0:04.0" && play.label == "Play preview"
                && (previewState.value as? String ?? "").contains("stillFrameReady:true")
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: finalFrame, object: previewState)], timeout: 15), .completed)
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

    func testPreviewResizeIsAvailableAcrossEditorPanels() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text", "-ui-testing-editor-analyzing-gallery"]
        for tool in ["visuals", "captions", "text", "text-style", "text-animation", "text-edit"] {
            app.launch()
            let button = app.buttons["native-editor-tool-\(tool.hasPrefix("text") ? "text" : tool)"]
            XCTAssertTrue(button.waitForExistence(timeout: 20))
            button.tap()
            if tool.hasPrefix("text-") {
                let input = app.textViews["native-editor-new-text-input"]
                XCTAssertTrue(input.waitForExistence(timeout: 5))
                input.tap()
                input.typeText("Resize test")
                app.buttons["native-editor-text-done"].tap()
                if tool == "text-animation" { app.buttons["Animation"].tap() }
                if tool == "text-edit" { app.buttons["Edit text"].tap() }
            }
            let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
            let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
            let header = app.buttons["native-editor-back"]
            let panel = tool == "text"
                ? app.textViews["native-editor-new-text-input"]
                : app.scrollViews[tool.hasPrefix("text-") ? "native-editor-text-inspector-scroll" : "native-editor-\(tool)-scroll"]
            XCTAssertTrue(handle.waitForExistence(timeout: 5), tool)
            XCTAssertTrue(panel.waitForExistence(timeout: 5), tool)
            XCTAssertTrue(handle.isHittable, tool)
            let originalHeight = preview.frame.height
            let originalHeaderY = header.frame.minY
            let originalPanelHeight = panel.frame.height
            let originalBottom = panel.frame.maxY
            let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -100)))
            XCTAssertLessThan(preview.frame.height, originalHeight - 40, tool)
            XCTAssertEqual(header.frame.minY, originalHeaderY, accuracy: 2, tool)
            XCTAssertGreaterThan(panel.frame.height, originalPanelHeight + 40, tool)
            XCTAssertEqual(panel.frame.maxY, originalBottom, accuracy: 2, tool)
            let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: 300)))
            XCTAssertEqual(preview.frame.height, originalHeight, accuracy: 2, tool)
            XCTAssertEqual(header.frame.minY, originalHeaderY, accuracy: 2, tool)
            app.terminate()
        }
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
        // typeText can return before the last keystroke reaches the text view
        // on a busy simulator, so wait for the value instead of sampling it.
        expectation(for: NSPredicate(format: "value == %@", "First\nSecond"), evaluatedWith: input)
        waitForExpectations(timeout: 5)
        app.buttons["native-editor-text-done"].tap()
        let multiline = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Text: First\nSecond")).firstMatch
        XCTAssertTrue(multiline.waitForExistence(timeout: 3))
        let multilineHeight = multiline.frame.height
        app.buttons["Edit text"].tap()
        let edit = app.textViews["native-editor-text-content"]
        XCTAssertTrue(edit.waitForExistence(timeout: 3))
        // A center tap can place the caret inside the second line. Tap beyond
        // its trailing text so deletion starts at the end on every screen size.
        edit.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.95)).tap()
        edit.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 7))
        expectation(for: NSPredicate(format: "value == %@", "First"), evaluatedWith: edit)
        waitForExpectations(timeout: 5)
        edit.typeText(" Second")
        expectation(for: NSPredicate(format: "value == %@", "First Second"), evaluatedWith: edit)
        waitForExpectations(timeout: 5)
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
        // typeText can return before the last keystroke reaches the text view
        // on a busy simulator, so wait for the value instead of sampling it.
        expectation(for: NSPredicate(format: "value == %@", "Live text"), evaluatedWith: input)
        waitForExpectations(timeout: 5)
        XCTAssertGreaterThan(preview.frame.height, 100)
        XCTAssertLessThan(preview.frame.maxY, app.keyboards.firstMatch.frame.minY)
        let keyboard = XCTAttachment(screenshot: app.screenshot())
        keyboard.name = "Live text keyboard"; keyboard.lifetime = .keepAlways; add(keyboard)
        app.buttons["native-editor-text-done"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-panel"].waitForExistence(timeout: 3))
        let textReady = NSPredicate { _, _ in
            (preview.value as? String ?? "").contains("liveTextReady:true")
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: textReady, object: preview)], timeout: 15), .completed)
        let textObject = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Text: Live text")).firstMatch
        XCTAssertTrue(textObject.waitForExistence(timeout: 3))
        let initialCenter = CGPoint(x: textObject.frame.midX, y: textObject.frame.midY)
        let center = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: initialCenter.x, dy: initialCenter.y))
        center.press(forDuration: 0.1, thenDragTo: center.withOffset(CGVector(dx: -24, dy: -30)), withVelocity: 20, thenHoldForDuration: 0.1)
        let moveSamples = (preview.value as? String)?.components(separatedBy: ";").first?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
        XCTAssertGreaterThan(Int(moveSamples) ?? 0, 4, "Moving must use the immediate text layer")
        Thread.sleep(forTimeInterval: 0.5)
        XCTAssertLessThan(textObject.frame.midX, initialCenter.x - 15)
        XCTAssertLessThan(textObject.frame.midY, initialCenter.y - 20)
        let corner = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: textObject.frame.maxX, dy: textObject.frame.maxY))
        corner.press(forDuration: 0.1, thenDragTo: corner.withOffset(CGVector(dx: 24, dy: 18)), withVelocity: 20, thenHoldForDuration: 0.1)
        let cornerSamples = (preview.value as? String)?.components(separatedBy: ";").first?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
        XCTAssertGreaterThan(Int(cornerSamples) ?? 0, 4, "Corner resizing must use the immediate text layer")
        let resizedSize = app.textFields["native-editor-text-size"].value as? String
        let movedCenter = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: textObject.frame.midX, dy: textObject.frame.midY))
        movedCenter.press(forDuration: 0.05, thenDragTo: movedCenter.withOffset(CGVector(dx: 12, dy: -15)), withVelocity: 60, thenHoldForDuration: 0.05)
        XCTAssertEqual(app.textFields["native-editor-text-size"].value as? String, resizedSize, "Moving immediately after resizing must preserve size")
        let size = app.textFields["native-editor-text-size"]
        XCTAssertTrue(size.waitForExistence(timeout: 3))
        // Focus at the end and clear explicitly: double-tap does not
        // reliably select the whole decimal value on every iOS version.
        size.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5)).tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let previousSize = size.value as? String ?? ""
        size.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: previousSize.count + 1))
        XCTAssertTrue(["", "Size"].contains(size.value as? String ?? ""), "The prior numeric value must be cleared")
        size.typeText("600")
        XCTAssertEqual(size.value as? String, "600")
        app.buttons["Increase text size"].tap()
        XCTAssertEqual(size.value as? String, "604")
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        let largeText = XCTAttachment(screenshot: app.screenshot())
        largeText.name = "Large text size control"; largeText.lifetime = .keepAlways; add(largeText)
        // Finger samples must use the prepared layer, not mutate/recompile
        // the document. Preparation is asynchronous on hosted simulators.
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: textReady, object: preview)], timeout: 15), .completed)
        preview.pinch(withScale: 1.4, velocity: 0.4)
        Thread.sleep(forTimeInterval: 0.5)
        preview.pinch(withScale: 0.75, velocity: -0.4)
        let samples = (preview.value as? String)?.components(separatedBy: ";").first?.replacingOccurrences(of: "liveTextSamples:", with: "") ?? ""
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

    func testEditorChromeFitsViewportAndCurrentToolsRemainReachable() {
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

        XCTAssertFalse(app.buttons["native-editor-tool-styles"].exists)
        XCTAssertFalse(app.buttons["native-editor-tool-overlays"].exists)
        let tools = ["text", "captions", "visuals", "sounds"]
        let buttons = tools.map { app.buttons["native-editor-tool-\($0)"] }
        for button in buttons {
            XCTAssertTrue(button.waitForExistence(timeout: 2), "Every tool must remain in the tool rail")
            XCTAssertTrue(button.isHittable, "Every tool must remain reachable without horizontal scrolling")
        }
        let widths = buttons.map(\.frame.width)
        for width in widths.dropFirst() { XCTAssertEqual(width, widths[0], accuracy: 2) }
        XCTAssertEqual(buttons[0].frame.minX, toolRail.frame.minX + 8, accuracy: 2)
        XCTAssertEqual(buttons[3].frame.maxX, toolRail.frame.maxX - 8, accuracy: 2)
        for index in 1..<buttons.count {
            XCTAssertEqual(buttons[index].frame.minX - buttons[index - 1].frame.maxX, 2, accuracy: 2)
        }

        XCTAssertTrue(app.buttons["native-editor-save"].exists)
        XCTAssertTrue(app.buttons["native-editor-export"].exists)
    }

    func testSongReferenceBarKeepsTimelineAndToolsOnScreen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-song-reference"]
        app.launch()

        // Reference-only music adds the posting-song bar above the preview.
        // Collapsed or expanded, its height must come out of the preview, not
        // push the timeline and tool rail past the bottom edge.
        let card = app.descendants(matching: .any)["native-song-reference"].firstMatch
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let timeline = app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch
        let toolRail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 8))
        XCTAssertTrue(preview.exists)
        XCTAssertTrue(timeline.exists)
        XCTAssertTrue(toolRail.exists)

        func assertTimelineAndToolsOnScreen(_ state: String) {
            XCTAssertLessThanOrEqual(card.frame.maxY, preview.frame.minY + 1, "\(state): the song bar sits above the preview")
            XCTAssertGreaterThanOrEqual(timeline.frame.height, 120, "\(state): the timeline must not be squeezed")
            XCTAssertLessThanOrEqual(timeline.frame.maxY, toolRail.frame.minY + 1)
            XCTAssertLessThanOrEqual(toolRail.frame.maxY, app.frame.maxY + 1, "\(state): the tool rail must stay on screen")
            for tool in ["text", "captions", "visuals", "sounds"] {
                XCTAssertTrue(app.buttons["native-editor-tool-\(tool)"].isHittable, "\(state): \(tool) must stay tappable")
            }
        }

        assertTimelineAndToolsOnScreen("Collapsed")
        XCTAssertGreaterThanOrEqual(preview.frame.height, app.frame.height * 0.25, "The song bar must not shrink the preview to a thumbnail")
        XCTAssertLessThanOrEqual(card.frame.height, 56, "The song card starts as a thin bar")
        let toggle = app.buttons["native-song-reference-toggle"]
        XCTAssertTrue(toggle.isHittable)
        XCTAssertTrue(toggle.label.hasPrefix("Add the song when posting"), toggle.label)
        let copy = app.buttons["native-song-reference-copy"]
        XCTAssertFalse(copy.exists, "Song details stay hidden until the bar is tapped")

        toggle.tap()
        XCTAssertTrue(copy.waitForExistence(timeout: 2))
        XCTAssertTrue(copy.isHittable, "Song details can still be copied from the editor")
        assertTimelineAndToolsOnScreen("Expanded")

        toggle.tap()
        XCTAssertTrue(copy.waitForNonExistence(timeout: 2))
    }

    func testFinishedRenderFallbackRemainsPlayableAndDisablesCanvasManipulation() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-failure"]
        app.launch()

        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 8))
        XCTAssertTrue(app.staticTexts["Showing finished render"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["native-editor-retry-source-preview"].exists)
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "Text:")).firstMatch.exists,
                       "Fallback video is playback-only; canvas objects cannot be selected or manipulated")
    }

    func testHardSourcePreviewFailureShowsUnavailableStateWithoutFallbackPlayer() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-source-failure"]
        app.launch()

        XCTAssertTrue(app.staticTexts["Preview unavailable"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-preview-fallback"].exists)
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "Text:")).firstMatch.exists)
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

    // KRI-110: tapping a caption-tagged text bar (guided_story's persisted
    // shape) must open the Captions panel, not the Text inspector — the
    // routing gap that made these captions unreachable as captions.
    func testProjectedCaptionTapOpensCaptionsPanelNotTextInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-projected-captions"]
        app.launch()
        let captionID = "native-editor-timeline-text-00000000-0000-4000-8000-000000000301"
        let captionBar = app.descendants(matching: .any)["native-editor-lane-captions"].firstMatch.buttons[captionID]
        XCTAssertTrue(captionBar.waitForExistence(timeout: 8))
        captionBar.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-captions-panel"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-text-panel"].exists)
        XCTAssertTrue(app.staticTexts["Spoken words"].exists)
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
        waitForStableRowRelation(text, second) { abs($0) > 30 }
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
        // The async lane repack's tail latency is longer than it first looks:
        // local looping saw occasional stalls past 10s and 15s (still landing
        // on the pre-fix 651 vs 701 one-row-off reading), but 20/20 passed at
        // 25s. Match that observed budget rather than the file's usual 15s.
        waitForStableRowRelation(text, second, timeout: 25) { abs($0) <= 1 }
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

        XCTAssertTrue(app.buttons["native-editor-captions-tab-Style"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-caption-row-cue-all"].firstMatch.exists)
    }

    // KRI-110: tapping a caption row must actually enter text-edit mode.
    // Pre-existing on origin/main for native caption_cues captions too
    // (confirmed against an unmodified checkout before this fix) — a
    // conditional TextField driven by @FocusState never committed when set
    // from the row's tap gesture in the same turn as an @ObservedObject
    // (session) mutation (session.select). Fixed by moving the Text/
    // TextField swap onto a plain @State; the field's own
    // `.accessibilityIdentifier` is masked by the row's (a separate,
    // pre-existing SwiftUI identifier-inheritance quirk that doesn't affect
    // real usage — VoiceOver reads label/value, not identifier), so this
    // locates it by type within the row instead.
    func testTappingCaptionRowEntersEditModeAndPersistsTypedText() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let captions = app.buttons["native-editor-timeline-caption_cue-cue-all"]
        XCTAssertTrue(captions.waitForExistence(timeout: 8))
        captions.tap()

        let row = app.descendants(matching: .any)["native-editor-caption-row-cue-all"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        row.tap()

        let field = app.textFields.matching(identifier: "native-editor-caption-row-cue-all").firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 3), "TextField never appeared after tapping the row")
        field.tap()
        field.typeText(" edited")
        app.buttons["native-editor-captions-done"].tap()

        // Reopen and confirm the edit persisted in the document, not just
        // transiently in the now-dismissed TextField.
        captions.tap()
        let reopenedRow = app.descendants(matching: .any)["native-editor-caption-row-cue-all"].firstMatch
        XCTAssertTrue(reopenedRow.waitForExistence(timeout: 3))
        // Cursor placement on a freshly-focused multi-line TextField isn't
        // guaranteed to be at the end, so the typed text may land before or
        // after the original "caption" — either order proves the edit
        // reached the document and survived a full close/reopen cycle.
        let editedRow = app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'edited' AND label CONTAINS 'caption'")).firstMatch
        XCTAssertTrue(editedRow.waitForExistence(timeout: 3), "Typed edit did not persist across close/reopen")
    }

    func testAllPersistedLanesExposeStableTimelineIdentityAndInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let cases: [(timelineID: String, inspectorID: String)] = [
            ("native-editor-timeline-music-00000000-0000-4000-8000-000000000350", "native-editor-selected-music-track"),
            ("native-editor-timeline-sound_effect-sfx-1", "native-editor-selected-sfx-placement"),
            ("native-editor-timeline-media_overlay-overlay-1", "native-editor-visuals-panel"),
            ("native-editor-timeline-carousel-carousel-1", "native-editor-selected-carousel-position"),
            ("native-editor-timeline-visual_block-visual-1", "native-editor-visuals-panel"),
            ("native-editor-timeline-motion_scene-motion-1", "native-editor-visuals-panel"),
            ("native-editor-timeline-camera_effect-camera-1", "native-editor-visuals-panel"),
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
            let done = app.buttons[value.inspectorID == "native-editor-visuals-panel" ? "native-editor-visuals-done" : "native-editor-inspector-done"]
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

    /// Lane repacking after a timing-drag gesture is suppressed during the
    /// gesture itself and lands on a later async refresh, not synchronously
    /// with its completion. Wait for the vertical relation between the two
    /// rows to both satisfy `holds` and hold steady across two consecutive
    /// polls before trusting a frame snapshot of them.
    private func waitForStableRowRelation(_ text: XCUIElement, _ second: XCUIElement, timeout: TimeInterval = 5, holds: @escaping (CGFloat) -> Bool) {
        var previousDelta: CGFloat?
        let stable = NSPredicate { _, _ in
            let delta = text.frame.midY - second.frame.midY
            defer { previousDelta = delta }
            return holds(delta) && previousDelta == delta
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: stable, object: text)], timeout: timeout), .completed)
    }
}
