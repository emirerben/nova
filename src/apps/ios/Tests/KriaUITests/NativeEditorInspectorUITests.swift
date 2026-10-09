import XCTest
import UIKit

@MainActor
final class NativeEditorInspectorUITests: XCTestCase {
    func testConnectedPanelsKeepRailAndPreviewStableAndCollapseActiveTool() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_EDITOR_WIDTH"] = "390"
        app.launch()
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(rail.waitForExistence(timeout: 20))
        let railBottom = rail.frame.maxY
        let previewHeight = preview.frame.height
        var panelFrame: CGRect?
        for tool in ["captions", "visuals", "sounds", "text", "captions"] {
            app.buttons["native-editor-tool-" + tool].tap()
            let content = tool == "text" ? app.textViews["native-editor-new-text-input"] : app.scrollViews["native-editor-" + tool + "-scroll"]
            XCTAssertTrue(content.waitForExistence(timeout: 5))
            XCTAssertFalse(app.keyboards.firstMatch.exists, "Opening a tab must not take keyboard focus")
            let frame = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch.frame
            if let panelFrame {
                XCTAssertEqual(frame.minY, panelFrame.minY, accuracy: 2, tool)
                XCTAssertEqual(frame.height, panelFrame.height, accuracy: 2, tool)
            } else { panelFrame = frame }
            XCTAssertTrue(app.buttons["native-editor-tool-" + tool].isSelected)
            XCTAssertEqual(rail.frame.maxY, railBottom, accuracy: 2)
            XCTAssertEqual(preview.frame.height, previewHeight, accuracy: 2)
            // UIKit retains the backing scroll container in its inspection
            // tree. Covered editing controls must be absent or disabled.
            for id in ["native-editor-timeline-caption_cue-cue-paper", "native-editor-timeline-visual_block-paper-media"] {
                let covered = app.buttons[id]
                if covered.exists { XCTAssertFalse(covered.isEnabled) }
            }
            XCTAssertTrue(app.buttons["native-editor-play-pause"].isHittable)
            XCTAssertFalse(app.buttons["native-editor-undo"].exists)
            for name in ["text", "captions", "visuals", "sounds"] {
                XCTAssertTrue(app.buttons["native-editor-tool-" + name].isHittable)
            }
            let capture = XCTAttachment(screenshot: app.screenshot())
            capture.name = "connected-panel-" + tool
            capture.lifetime = .keepAlways
            add(capture)
        }
        let play = app.buttons["native-editor-play-pause"]
        play.tap()
        XCTAssertEqual(play.label, "Pause preview")
        play.tap()
        XCTAssertEqual(play.label, "Play preview")
        app.buttons["native-editor-tool-captions"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-tool-captions"].isSelected)
        app.buttons["native-editor-tool-text"].tap()
        XCTAssertTrue(app.textViews["native-editor-new-text-input"].waitForExistence(timeout: 5))
        app.buttons["native-editor-tool-text"].tap()
        XCTAssertFalse(app.textViews["native-editor-new-text-input"].exists)
        XCTAssertFalse(app.buttons["native-editor-tool-text"].isSelected)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch.exists)
        app.buttons["native-editor-tool-text"].tap()
        app.buttons["native-editor-text-done"].tap()
        XCTAssertFalse(app.textViews["native-editor-new-text-input"].exists)
        XCTAssertFalse(app.buttons["native-editor-tool-text"].isSelected)
        app.buttons["native-editor-tool-text"].tap()
        app.buttons["native-editor-text-cancel"].tap()
        XCTAssertFalse(app.textViews["native-editor-new-text-input"].exists)
        XCTAssertFalse(app.buttons["native-editor-tool-text"].isSelected)
    }

    func testVisiblePanelHandleExpandsAndRestoresEveryTool() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-captions"].waitForExistence(timeout: 20))
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let panel = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch
        for tool in ["captions", "visuals", "sounds", "text"] {
            app.buttons["native-editor-tool-" + tool].tap()
            let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
            XCTAssertTrue(handle.waitForExistence(timeout: 5), tool)
            XCTAssertTrue(handle.isHittable, tool)
            XCTAssertGreaterThanOrEqual(handle.frame.height, 44, tool)
            let initialPanel = panel.frame
            let initialPreview = preview.frame
            let railBottom = rail.frame.maxY
            let headerY = app.buttons["native-editor-back"].frame.minY
            // Start on the visible capsule near the panel's top edge, not
            // merely somewhere inside its larger accessibility target.
            // Keep the original minimum expansion, then rise 40pt into the
            // preview even when a taller device leaves a larger transport gap.
            let expansionDistance = max(160, initialPanel.minY - initialPreview.maxY + 40)
            let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0))
                .withOffset(CGVector(dx: 0, dy: 10))
            start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -expansionDistance)))
            XCTAssertGreaterThan(panel.frame.height, initialPanel.height + 80, tool)
            XCTAssertLessThan(panel.frame.minY, initialPanel.minY - 80, tool)
            // KRI-170: the panel handle no longer shrinks the preview; the panel
            // rises over it, and its handle stays reachable.
            XCTAssertEqual(preview.frame.height, initialPreview.height, accuracy: 2, tool)
            XCTAssertLessThan(panel.frame.minY, preview.frame.maxY, tool)
            XCTAssertTrue(handle.isHittable, tool)
            // The transport stays put (it's covered, not dragged along) and the
            // panel never reaches the header.
            XCTAssertGreaterThanOrEqual(panel.frame.minY, app.buttons["native-editor-back"].frame.maxY, tool)
            XCTAssertEqual(rail.frame.maxY, railBottom, accuracy: 2, tool)
            XCTAssertEqual(app.buttons["native-editor-back"].frame.minY, headerY, accuracy: 2, tool)
            let capture = XCTAttachment(screenshot: app.screenshot())
            capture.name = "expanded-visible-handle-" + tool
            capture.lifetime = .keepAlways
            add(capture)
            // Collapse with a small overshoot: a long pull past the bottom
            // closes the panel (KRI-253).
            let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0))
                .withOffset(CGVector(dx: 0, dy: 10))
            // Return to the minimum from the panel's actual raised position,
            // then add 40pt: enough to settle the resize without reaching the
            // 64pt below-minimum dismissal threshold (KRI-253).
            let restoreDistance = panel.frame.height - initialPanel.height + 40
            raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: restoreDistance)),
                         withVelocity: .slow, thenHoldForDuration: 0.2)
            XCTAssertEqual(panel.frame.height, initialPanel.height, accuracy: 2, tool)
            XCTAssertEqual(preview.frame.height, initialPreview.height, accuracy: 2, tool)
        }
    }

    func testConnectedTextPresetAndAnimationPreviewControls() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launchEnvironment["UI_TEST_REDUCE_TRANSPARENCY"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-text"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-text"].tap()
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Connected text")
        XCTAssertFalse(app.buttons["native-editor-tool-sounds"].exists)
        app.buttons["native-editor-text-done"].tap()
        let preset = app.buttons["native-editor-text-preset"]
        XCTAssertTrue(preset.waitForExistence(timeout: 5))
        XCTAssertLessThan(preset.frame.width, 200)
        XCTAssertTrue(app.buttons["native-editor-tool-sounds"].isHittable)
        preset.tap()
        app.buttons["Bold"].tap()
        let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -180)))
        app.buttons["Animation"].tap()
        let scroll = app.scrollViews["native-editor-text-inspector-scroll"]
        let toggle = app.buttons["native-editor-text-animation-preview-toggle"]
        for _ in 0..<3 {
            if toggle.isHittable { break }
            scroll.swipeUp()
        }
        XCTAssertTrue(toggle.isHittable)
        XCTAssertEqual(toggle.label, "Play previews")
        toggle.tap()
        XCTAssertEqual(toggle.label, "Pause previews")
        toggle.tap()
        XCTAssertEqual(toggle.label, "Play previews")
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = "connected-text-animation-reduced-motion"
        capture.lifetime = .keepAlways
        add(capture)
        app.buttons["native-editor-tool-sounds"].tap()
        XCTAssertTrue(app.scrollViews["native-editor-sounds-scroll"].waitForExistence(timeout: 5))
        app.buttons["native-editor-tool-sounds"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch.waitForExistence(timeout: 5))
    }

    func testKeyboardKeepsSourcePreviewVisibleAboveConnectedPanel() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-color-cuts"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-text"].tap()
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Visible preview")
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch.frame
        XCTAssertGreaterThanOrEqual(preview.height, 80)
        XCTAssertLessThan(preview.maxY, input.frame.minY)
        XCTAssertTrue(app.buttons["native-editor-text-done"].isHittable)
        // Geometry alone missed the retained timeline painting over this
        // frame. The first source clip is red; sample its actual visible pixel.
        let screenshot = app.screenshot()
        let image = screenshot.image.cgImage!
        let scale = Double(image.width) / app.frame.width
        let sample = image.cropping(to: CGRect(x: preview.midX * scale,
            y: (preview.minY + preview.height * 0.2) * scale, width: 1, height: 1))!
        var rgba = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.draw(sample, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        // The preview's light gradient lifts green/blue slightly at this
        // compact size; red dominance still distinguishes it from the lanes.
        XCTAssertGreaterThan(rgba[0], 220, "Source preview was covered: \(rgba)")
        XCTAssertGreaterThan(Int(rgba[0]) - Int(rgba[1]), 150, "Source preview was covered: \(rgba)")
        XCTAssertGreaterThan(Int(rgba[0]) - Int(rgba[2]), 150, "Source preview was covered: \(rgba)")
        let capture = XCTAttachment(screenshot: screenshot)
        capture.name = "keyboard-source-preview-visible"
        capture.lifetime = .keepAlways
        add(capture)
    }

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
        var sampledFirstClip = false
        var sampledSecondClip = false
        var sampledBrandOutro = false
        var sampledContentAfterOutro = false
        let previewElement = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let timeElement = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        func samplePreview() -> (rgba: [UInt8], label: String, time: Double, diagnostic: String) {
            let screenshot = app.screenshot()
            let preview = previewElement.frame
            let image = screenshot.image.cgImage!
            let scale = Double(image.width) / app.frame.width
            let sample = image.cropping(to: CGRect(x: preview.midX * scale, y: (preview.minY + preview.height * 0.2) * scale, width: 1, height: 1))!
            var rgba = [UInt8](repeating: 0, count: 4)
            let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(sample, in: CGRect(x: 0, y: 0, width: 1, height: 1))
            let label = timeElement.value as? String ?? ""
            let time = Double(label.split(separator: ":").last ?? "0") ?? 0
            return (rgba, label, time, previewElement.value as? String ?? "")
        }
        /// Whether the sampled pixel is the clip the playhead is on (transition boundaries match anything).
        func showsExpectedClip(_ rgba: [UInt8], at time: Double) -> Bool {
            if abs(time - 2) <= 0.1 || abs(time - 4) <= 0.1 { return true }
            if time < 2 { return rgba[0] > 220 && rgba[2] < 40 }
            if time < 4 { return rgba[2] > 220 && rgba[0] < 40 }
            return rgba[0] > 220 && rgba[1] > 220 && rgba[2] > 220
        }
        func assertDisplayedPreview() {
            // The still frame is requested asynchronously after each scrub, so the screenshot can still show the
            // previous position. Wait for the diagnostic to report the frame, then re-sample (bounded) until the
            // pixel matches; a preview that is genuinely stuck on the wrong clip still fails after the deadline.
            let frameReady = NSPredicate { _, _ in (previewElement.value as? String ?? "").contains("stillFrameReady:true") }
            _ = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: frameReady, object: previewElement)], timeout: 5)
            var (rgba, label, time, previewDiagnostic) = samplePreview()
            let deadline = Date().addingTimeInterval(3)
            while !showsExpectedClip(rgba, at: time), Date() < deadline {
                Thread.sleep(forTimeInterval: 0.15)
                (rgba, label, time, previewDiagnostic) = samplePreview()
            }
            // The accessible source preview now includes the 1.6-second Kria
            // outro after its two editable two-second clips. Skip the two
            // transition boundaries, then sample the red, blue, and branded
            // white sections independently.
            if abs(time - 2) > 0.1 && abs(time - 4) > 0.1 {
                if time < 2 {
                    sampledFirstClip = true
                    if sampledBrandOutro { sampledContentAfterOutro = true }
                    XCTAssertGreaterThan(rgba[0], 220, "Wrong first clip at \(label): \(rgba), preview: \(previewDiagnostic)")
                    XCTAssertLessThan(rgba[2], 40, "Stale second clip at \(label): \(rgba), preview: \(previewDiagnostic)")
                } else if time < 4 {
                    sampledSecondClip = true
                    if sampledBrandOutro { sampledContentAfterOutro = true }
                    XCTAssertGreaterThan(rgba[2], 220, "Wrong second clip at \(label): \(rgba), preview: \(previewDiagnostic)")
                    XCTAssertLessThan(rgba[0], 40, "Stale first clip at \(label): \(rgba), preview: \(previewDiagnostic)")
                } else {
                    sampledBrandOutro = true
                    XCTAssertGreaterThan(rgba[0], 220, "Brand outro was not displayed at \(label): \(rgba), preview: \(previewDiagnostic)")
                    XCTAssertGreaterThan(rgba[1], 220, "Brand outro was not displayed at \(label): \(rgba), preview: \(previewDiagnostic)")
                    XCTAssertGreaterThan(rgba[2], 220, "Brand outro was not displayed at \(label): \(rgba), preview: \(previewDiagnostic)")
                }
            }
        }
        for index in 0..<8 {
            assertDisplayedPreview()
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
            assertDisplayedPreview()
        }
        XCTAssertTrue(sampledFirstClip, "Scrubbing must display a frame from the first editable clip")
        XCTAssertTrue(sampledSecondClip, "Scrubbing must display a frame from the second editable clip")
        XCTAssertTrue(sampledBrandOutro, "Scrubbing must reach the branded transport outro")
        XCTAssertTrue(sampledContentAfterOutro, "Reverse scrubbing must return from the outro to content")
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
            time.value as? String == "0:05.6" && play.label == "Play preview"
                && (previewState.value as? String ?? "").contains("stillFrameReady:true")
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: finalFrame, object: previewState)], timeout: 15), .completed)
        assertDisplayedPreview()
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

    // KRI-165: dragging a clip's trailing trim handle to the timeline edge
    // and holding there must keep extending the clip — the timeline
    // auto-scrolls so the handle stays reachable past what was on screen at
    // gesture start. The fixture's clip 1 carries far more source-duration
    // headroom (20s) than a single translation from the handle's actual
    // on-screen position to the edge could reach (well under a second here),
    // so any extension beyond a few seconds only happens if the HOLD kept
    // advancing the clock — proven independently via native-editor-current-time,
    // which nothing else touches during a clip-trim gesture.
    func testDraggingTrailingClipHandleToRightEdgeAutoScrollsPastVisibleWindow() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-autoscroll-extend"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 8))
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let initialTime = time.value as? String
        let initialDuration = Double(clip.value as? String ?? "0") ?? 0

        clip.tap()
        let handle = app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 2))
        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        let edge = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: edge, withVelocity: 40, thenHoldForDuration: 1.5)

        XCTAssertNotEqual(time.value as? String, initialTime,
            "auto-scroll must advance the clock while the finger holds at the edge")
        let finalDuration = Double(clip.value as? String ?? "0") ?? 0
        XCTAssertGreaterThan(finalDuration - initialDuration, 3,
            "a one-shot drag from the handle's actual position could not reach this far without auto-scroll")
    }

    // KRI-165: same drag-and-hold pattern, on a text block. A long press
    // (not a quick swipe — see testQuickSwipeStartingOnTextScrubsWithoutMovingBlock)
    // picks the block up, then holding at the edge must move it well past
    // the visible window at gesture start.
    func testLongPressDraggingTextBlockToRightEdgeAutoScrollsPastVisibleWindow() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-autoscroll-extend"]
        app.launch()
        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000850"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let initialTime = time.value as? String
        let initialTiming = text.value as? String

        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        let edge = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5))
        start.press(forDuration: 0.6, thenDragTo: edge, withVelocity: 40, thenHoldForDuration: 1.5)

        XCTAssertNotEqual(time.value as? String, initialTime,
            "auto-scroll must advance the clock while the finger holds at the edge")
        XCTAssertNotEqual(text.value as? String, initialTiming,
            "the block must have moved well past its original position")
    }

    // KRI-165 regression guard: a clip's leading trim handle never tracks
    // the finger (the clip's timeline start is fixed by slot order — see
    // NativeMiniStrip's design note), so it must never auto-scroll even
    // while the finger holds inside an edge zone. If this regressed, a
    // leading trim would keep un-trimming itself with no further finger
    // movement.
    func testDraggingLeadingClipHandleNearEdgeDoesNotAutoScroll() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-autoscroll-extend"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 8))
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let initialTime = time.value as? String

        clip.tap()
        let handle = app.descendants(matching: .any)["native-editor-trim-leading"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 2))
        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        let edge = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: edge, withVelocity: 40, thenHoldForDuration: 1.5)

        XCTAssertEqual(time.value as? String, initialTime,
            "a leading clip trim must never auto-scroll the clock, even while holding near an edge")
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
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let visibleHeight = min(timeline.frame.maxY, rail.frame.minY - 8) - timeline.frame.minY
        XCTAssertGreaterThan(visibleHeight, 44)
        for row in [0.23, 0.5, 0.85] {
        let origin = timeline.coordinate(withNormalizedOffset: .zero)
        let start = origin.withOffset(CGVector(dx: timeline.frame.width * 0.75, dy: visibleHeight * row))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -110, dy: 0)))
        XCTAssertNotEqual(time.value as? String, initialTime)
        XCTAssertEqual(playhead.frame.midX, originalX, accuracy: 1)
        let reverse = origin.withOffset(CGVector(dx: timeline.frame.width * 0.3, dy: visibleHeight * row))
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
        // KRI-131: this fixture's short timeline leaves the tool island
        // floating over roughly the lower half of the lane scroll's own
        // frame (unlike `-ui-testing-editor-all-lanes`, whose tall,
        // scrollable content fills that same region with real lanes
        // instead). A touch landing there reaches the island's buttons,
        // not the timeline's pan gesture — the intended overlay behavior,
        // not a regression — so the sampled rows stay in the upper portion
        // that's never covered by the island.
        for row in [0.15, 0.3, 0.45] {
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
        // Dragging down past the default now grows the preview (KRI-170), so
        // restore by exactly the amount it shrank.
        let shrunkBy = originalHeight - preview.frame.height
        let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: shrunkBy)))
        XCTAssertEqual(preview.frame.height, originalHeight, accuracy: 3)
    }

    func testTimelineResizeGrowsPreviewBeyondDefault() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()
        let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let strip = app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 8))
        let originalHeight = preview.frame.height
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 200)))
        XCTAssertGreaterThan(preview.frame.height, originalHeight + 60, "dragging down must grow the preview past its default")
        XCTAssertTrue(strip.exists, "a strip of timeline must survive")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch.isHittable)
        let back = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        back.press(forDuration: 0.1, thenDragTo: back.withOffset(CGVector(dx: 0, dy: -400)))
        XCTAssertLessThan(preview.frame.height, originalHeight - 40, "dragging up still shrinks it")
    }

    /// The preview handle resizes from anywhere in its row, not only the
    /// 80 pt around the grabber line.
    func testTimelineResizeWorksFromTheBandBesideTheGrabber() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()
        let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 8))
        let window = app.windows.firstMatch
        XCTAssertGreaterThan(handle.frame.width, window.frame.width - 40, "the handle spans its row")
        let originalHeight = preview.frame.height
        // Well left of the line, where the old 80 pt target never reached.
        let start = window.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: 36, dy: handle.frame.midY))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -140)))
        XCTAssertLessThan(preview.frame.height, originalHeight - 80, "dragging the band beside the grabber shrinks the preview")
    }

    func testPanelExpansionResetsWhenPanelCloses() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let captions = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 20))
        captions.tap()
        let panel = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch
        let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5))
        let initial = panel.frame.height
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -160)))
        XCTAssertGreaterThan(panel.frame.height, initial + 80)
        captions.tap() // close the panel
        XCTAssertFalse(panel.waitForExistence(timeout: 2))
        captions.tap() // reopen
        XCTAssertTrue(panel.waitForExistence(timeout: 5))
        XCTAssertEqual(panel.frame.height, initial, accuracy: 2, "a reopened panel starts at its default height")
    }

    /// KRI-235: the panel's whole header resizes it, not only the grabber line.
    func testPanelHeaderDragsResizePanel() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let captions = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 20))
        captions.tap()
        let panel = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch
        let done = app.buttons["native-editor-captions-done"]
        XCTAssertTrue(done.waitForExistence(timeout: 5))
        let initial = panel.frame.height
        // Blank header space between the title and Done, well clear of the grabber.
        let start = done.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0.5)).withOffset(CGVector(dx: -40, dy: 0))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -160)))
        XCTAssertGreaterThan(panel.frame.height, initial + 80, "dragging up from the header must raise the panel")
        captions.tap() // close; the next panel starts at its default height
        XCTAssertFalse(panel.waitForExistence(timeout: 2))

        let text = app.buttons["native-editor-tool-text"]
        text.tap()
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Header drag")
        app.buttons["native-editor-text-done"].tap()
        let editTab = app.buttons["Edit text"]
        let animationTab = app.buttons["Animation"]
        let styleTab = app.buttons["Style"]
        XCTAssertTrue(editTab.waitForExistence(timeout: 5))
        animationTab.tap()
        XCTAssertTrue(animationTab.isSelected, "header buttons must stay tappable")
        styleTab.tap()
        XCTAssertTrue(styleTab.isSelected, "header buttons must stay tappable")
        let before = panel.frame.height
        let tabStart = editTab.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        tabStart.press(forDuration: 0.1, thenDragTo: tabStart.withOffset(CGVector(dx: 0, dy: -160)))
        XCTAssertGreaterThan(panel.frame.height, before + 80, "dragging up from the tab strip must raise the panel")
        XCTAssertTrue(styleTab.isSelected, "a drag from a tab must not select it")
    }

    /// KRI-253: pulling the panel down past its smallest size does what Done does.
    func testPanelDragDownClosesLikeDone() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let captions = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 20))
        captions.tap()
        let panel = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch
        let done = app.buttons["native-editor-captions-done"]
        XCTAssertTrue(done.waitForExistence(timeout: 5))
        let initial = panel.frame
        let header = { done.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0.5)).withOffset(CGVector(dx: -40, dy: 0)) }

        // A short, slow pull springs back.
        var start = header()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 40)),
                    withVelocity: .slow, thenHoldForDuration: 0.3)
        XCTAssertTrue(panel.exists, "a short pull must not close the panel")
        XCTAssertEqual(panel.frame.minY, initial.minY, accuracy: 2, "the panel springs back")
        XCTAssertEqual(panel.frame.height, initial.height, accuracy: 2)

        // Collapsing a raised panel stops at its smallest size.
        start = header()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -160)))
        XCTAssertGreaterThan(panel.frame.height, initial.height + 80)
        start = header()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 200)),
                    withVelocity: .slow, thenHoldForDuration: 0.3)
        XCTAssertTrue(panel.exists, "collapsing a raised panel must not close it")
        XCTAssertEqual(panel.frame.height, initial.height, accuracy: 2)

        // A long pull from the header closes it.
        start = header()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 140)))
        XCTAssertTrue(panel.waitForNonExistence(timeout: 3), "pulling the header down must close the panel")
        XCTAssertTrue(captions.isHittable, "the tool rail stays")

        // On Add text, a pull from the grabber keeps the words, like Done.
        app.buttons["native-editor-tool-text"].tap()
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Pulled closed")
        let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5))
        let grab = { handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10)) }
        start = grab()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 140)))
        let styleTab = app.buttons["Style"]
        XCTAssertTrue(styleTab.waitForExistence(timeout: 5), "the new text opens for styling, as Done does")
        XCTAssertTrue(app.buttons["native-editor-text-inspector-done"].exists)
        start = grab()
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 140)))
        XCTAssertTrue(panel.waitForNonExistence(timeout: 3), "pulling the text panel down closes it")
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Pulled closed")).firstMatch.exists,
                      "the pulled-closed text is kept")
    }

    func testPreviewResizeIsAvailableAcrossEditorPanels() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text", "-ui-testing-editor-analyzing-gallery"]
        for tool in ["visuals", "captions", "text", "text-style", "text-animation"] {
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
            // KRI-508: with the keyboard on the text box the compact bar owns the split (no handle);
            // testSecondTapOpensEditTextWithKeyboardAndGrowingBox covers that state.
            let originalHeight = preview.frame.height
            let originalHeaderY = header.frame.minY
            let originalPanelHeight = panel.frame.height
            let originalBottom = panel.frame.maxY
            let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -100)))
            XCTAssertLessThan(preview.frame.height, originalHeight - 40, tool)
            XCTAssertEqual(header.frame.minY, originalHeaderY, accuracy: 2, tool)
            // KRI-170: the panel is independent of the preview — it never
            // shrinks, and its bottom edge stays put.
            XCTAssertGreaterThanOrEqual(panel.frame.height, originalPanelHeight - 1, tool)
            XCTAssertEqual(panel.frame.maxY, originalBottom, accuracy: 2, tool)
            let shrunkBy = originalHeight - preview.frame.height
            let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: shrunkBy)))
            XCTAssertEqual(preview.frame.height, originalHeight, accuracy: 3, tool)
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
        // "Done" leaves the text panel open on its Style tab. All three tab buttons share the
        // container's identifier, so pick the Edit one by identifier AND label rather than leaning on
        // a bare "Edit text" label that the context strip's button shares whenever it is on screen (KRI-213).
        let editTab = app.buttons.matching(identifier: "native-editor-text-tabs")
            .matching(NSPredicate(format: "label == %@", "Edit text")).firstMatch
        XCTAssertTrue(editTab.waitForExistence(timeout: 3))
        editTab.tap()
        let edit = app.textViews["native-editor-text-content"]
        XCTAssertTrue(edit.waitForExistence(timeout: 3))
        // Selecting the tab focuses the field with the caret at the end of the text. Wait for the
        // keyboard instead of tapping: typeText on an unfocused field taps its centre, which can
        // land the caret inside the second line, and a coordinate near the corner was not a stable
        // target either (the field resizes as the keyboard rises).
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
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
        reveal(size, in: app.scrollViews["native-editor-text-inspector-scroll"])
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
        // KRI-131: the tool rail now floats as a glass island over the
        // timeline, which keeps running behind it, instead of sitting
        // beneath it in its own opaque row.
        XCTAssertGreaterThanOrEqual(timeline.frame.maxY, toolRail.frame.minY, "the timeline must extend behind the floating island")
        XCTAssertLessThan(toolRail.frame.maxY, app.frame.maxY, "the island must float above the physical bottom edge")
        XCTAssertGreaterThanOrEqual(toolRail.frame.minX, 12, "the island is inset, not edge-to-edge")
        XCTAssertLessThanOrEqual(toolRail.frame.maxX, app.frame.maxX - 12, "the island is inset, not edge-to-edge")
        XCTAssertGreaterThanOrEqual(timeline.frame.height, 120)

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

    func testToolIslandFloatsOverTimelineAndLastLaneStaysReachable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let timeline = app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch
        let toolRail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let laneScroll = app.descendants(matching: .any)["native-editor-lane-scroll"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertTrue(timeline.exists)
        XCTAssertTrue(toolRail.exists)
        let previewHeightBeforeSelection = preview.frame.height

        // (a) the rail floats above the physical bottom edge, and the
        // timeline extends behind it rather than stopping above it.
        XCTAssertLessThan(toolRail.frame.maxY, app.frame.maxY, "the island must float above the physical bottom edge")
        // The island must sit close to the safe-area inset (6pt above it,
        // per spec), not stranded high above it. A regression here (e.g.
        // double-counting the safe-area inset in the island's own bottom
        // padding) previously floated it ~34pt too high on a Face ID
        // device; on a home-indicator device this gap should land well
        // under 60pt.
        XCTAssertGreaterThanOrEqual(app.frame.maxY - toolRail.frame.maxY, 20, "the island must not float detached from the safe area")
        XCTAssertLessThanOrEqual(app.frame.maxY - toolRail.frame.maxY, 60, "the island must sit close to the safe-area inset, not far above it")
        XCTAssertGreaterThanOrEqual(timeline.frame.maxY, toolRail.frame.minY, "the timeline must run behind the island")

        // (b) the last lane must be able to clear the island; nothing may
        // stay permanently hidden behind it. `tap()` on an off-screen
        // descendant of a SwiftUI ScrollView makes XCUITest auto-scroll it
        // into view first (the established pattern in this file — see
        // `tapTimelineElement` below); a raw swipe/drag gesture on this
        // ScrollView is unreliable because its bottom edge sits directly
        // under the floating, horizontally-centered island, which consumes
        // the touch before the ScrollView's own pan recognizer sees it.
        let lastLabel = app.staticTexts["native-editor-lane-label-last"].firstMatch
        XCTAssertTrue(lastLabel.waitForExistence(timeout: 4), "fixture must expose an identifiable last lane label")
        lastLabel.tap()
        // `tap()` only guarantees XCUITest found a non-obscured hit point
        // (near the element's center) once scrolled into view, not that the
        // element's entire bounding box cleared the island — so this checks
        // the element's midpoint, not its trailing edge, against the
        // island's top.
        XCTAssertLessThanOrEqual(lastLabel.frame.midY, toolRail.frame.minY + 1, "the last lane must be reachable above the island")

        // (c) selecting a clip shows the context capsule without shrinking
        // the preview, and every tool button stays hittable.
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 4))
        clip.tap()
        let adjust = app.buttons["native-editor-adjust"]
        XCTAssertTrue(adjust.waitForExistence(timeout: 4))
        XCTAssertTrue(adjust.isHittable)
        for tool in ["text", "captions", "visuals", "sounds"] {
            XCTAssertTrue(app.buttons["native-editor-tool-\(tool)"].isHittable, "\(tool) must stay tappable once a clip is selected")
        }
        XCTAssertEqual(preview.frame.height, previewHeightBeforeSelection, accuracy: 1, "selecting a clip must not resize the preview")
    }

    /// KRI-374: a montage built on the creator's own song shows it, connected and read-only, in Sounds.
    func testSoundsTabShowsCreatorsOwnSongWithoutCatalogControls() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-user-song"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))

        app.buttons["native-editor-tool-sounds"].tap()
        let row = app.descendants(matching: .any)["native-editor-your-song"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 4))
        XCTAssertEqual(row.label, "Midnight Drive, Plays 1:48 – 1:53, Background")
        XCTAssertTrue(app.staticTexts["Midnight Drive"].exists)
        XCTAssertTrue(app.staticTexts["Plays 1:48 – 1:53"].exists)
        XCTAssertTrue(app.staticTexts["Background"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-note"].firstMatch.exists)
        XCTAssertGreaterThanOrEqual(row.frame.height, 44)

        XCTAssertFalse(app.textFields["native-editor-music-track-input"].exists, "the song is a project asset, not a track ID")
        XCTAssertFalse(app.buttons["native-editor-add-music"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-music-volume"].firstMatch.exists)
    }

    /// KRI-428: a background song carries a volume slider, a start-point bar and Remove; removing it is an
    /// unsaved, undoable edit that falls back to the camera audio without offering any catalog controls.
    func testSoundsTabShowsSongVolumeStartAndRemove() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-user-song"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))

        app.buttons["native-editor-tool-sounds"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song"].firstMatch.waitForExistence(timeout: 4))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-volume"].firstMatch.exists)
        XCTAssertTrue(app.staticTexts["80%"].exists, "the saved song level is shown")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-start"].firstMatch.exists)
        XCTAssertTrue(app.staticTexts["Starts at 1:48"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-your-song-start-locked"].firstMatch.exists)
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled, "opening the controls is not an edit")

        // Sliding the start window is one unsaved edit and moves the label.
        let bar = app.descendants(matching: .any)["native-editor-your-song-start-bar"].firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 4))
        XCTAssertTrue(bar.isEnabled)
        // The Sounds panel starts short: raise it so the whole bar sits inside, where a drag reaches it.
        let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
        let grab = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10))
        grab.press(forDuration: 0.1, thenDragTo: grab.withOffset(CGVector(dx: 0, dy: -300)))
        let scroll = app.scrollViews["native-editor-sounds-scroll"]
        XCTAssertTrue(scroll.frame.contains(CGPoint(x: bar.frame.midX, y: bar.frame.midY)), "the start bar must sit inside the raised Sounds panel")
        bar.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.1, thenDragTo: bar.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)),
                   withVelocity: 100, thenHoldForDuration: 0.2)
        XCTAssertFalse(app.staticTexts["Starts at 1:48"].exists, "the label follows the drag")
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled)

        let remove = app.buttons["native-editor-your-song-remove"]
        XCTAssertTrue(remove.exists)
        remove.tap()
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-your-song"].firstMatch.exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-removed"].firstMatch.waitForExistence(timeout: 4))
        XCTAssertFalse(app.textFields["native-editor-music-track-input"].exists, "no catalog controls after removing the song")
        XCTAssertFalse(app.buttons["native-editor-add-music"].exists)
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled)

        // Undo lives on the timeline strip, behind the open Sounds panel.
        app.buttons["native-editor-sounds-done"].tap()
        app.buttons["native-editor-undo"].tap()
        app.buttons["native-editor-tool-sounds"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song"].firstMatch.waitForExistence(timeout: 4))
    }

    /// KRI-428: a lip-sync song keeps its start where the takes were filmed; volume and Remove still work.
    func testLipSyncSongLocksStartButKeepsVolumeAndRemove() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-user-song-lipsync"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))

        app.buttons["native-editor-tool-sounds"].tap()
        let row = app.descendants(matching: .any)["native-editor-your-song"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 4))
        XCTAssertEqual(row.label, "Midnight Drive, Plays 1:48 – 1:53, Lip-sync · master audio")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-volume"].firstMatch.exists)
        XCTAssertTrue(app.buttons["native-editor-your-song-remove"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-your-song-start-locked"].firstMatch.exists)
        XCTAssertTrue(app.staticTexts["Lip-sync keeps the song where you filmed it."].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-your-song-start"].firstMatch.exists, "no start bar to drag")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-your-song-start-bar"].firstMatch.exists)
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled)
    }

    /// The founder's debugging need: on a creator-song (lip-sync) video, Sounds carries an Original audio
    /// volume (0% by default, because the song plays alone) and the clip's Audio button really toggles that
    /// clip's sound. Both are unsaved, undoable edits.
    func testOriginalAudioVolumeAndPerClipAudioButtonWorkOnALipSyncVideo() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-user-song-lipsync"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled)

        app.buttons["native-editor-tool-sounds"].tap()
        let control = app.descendants(matching: .any)["native-editor-original-audio-volume"].firstMatch
        XCTAssertTrue(control.waitForExistence(timeout: 4), "Sounds carries an Original audio control")
        let percent = app.staticTexts["native-editor-original-audio-percent"]
        XCTAssertTrue(percent.exists)
        XCTAssertEqual(percent.label, "0%", "the song plays alone until the creator turns the camera up")
        let slider = app.sliders["Original audio volume"]
        XCTAssertTrue(slider.waitForExistence(timeout: 4))
        XCTAssertGreaterThanOrEqual(control.frame.height, 44, "the control keeps a 44pt touch target")
        // Scroll the control into the panel's reachable area if the song controls pushed it down.
        let scroll = app.scrollViews["native-editor-sounds-scroll"]
        if !slider.isHittable { scroll.swipeUp() }
        slider.adjust(toNormalizedSliderPosition: 0.5)
        XCTAssertNotEqual(percent.label, "0%", "moving the slider changes the level")
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled, "a level change is an unsaved edit")
        app.buttons["native-editor-sounds-done"].tap()

        // The per-clip Audio button in the clip's context strip now flips that clip's sound.
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 4))
        clip.tap()
        let audio = app.buttons["native-editor-clip-audio"]
        XCTAssertTrue(audio.waitForExistence(timeout: 4))
        XCTAssertTrue(audio.isEnabled)
        XCTAssertTrue(audio.isHittable)
        XCTAssertEqual(audio.frame.height, app.buttons["native-editor-adjust"].frame.height, accuracy: 1, "same size as its neighbours")
        XCTAssertEqual(audio.value as? String, "On", "the clip is audible once the level is up")
        audio.tap()
        XCTAssertEqual(audio.value as? String, "Off", "tapping Audio mutes this clip")
        audio.tap()
        XCTAssertEqual(audio.value as? String, "On", "and tapping again brings it back")
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
            // KRI-131: the tool rail floats over the timeline, which now runs
            // behind it, instead of the timeline stopping above the rail.
            XCTAssertGreaterThanOrEqual(timeline.frame.maxY, toolRail.frame.minY, "\(state): the timeline must extend behind the floating island")
            XCTAssertLessThan(toolRail.frame.maxY, app.frame.maxY, "\(state): the tool rail must float above the physical bottom edge")
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
        XCTAssertTrue(app.staticTexts["Showing your last finished video"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["native-editor-retry-source-preview"].exists)
        XCTAssertTrue(app.buttons["native-editor-retry-source-preview"].isHittable, "Retry must not sit under the canvas")
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "Text:")).firstMatch.exists,
                       "Fallback video is playback-only; canvas objects cannot be selected or manipulated")
    }

    /// KRI-281: a permanent failure (no video track) rebuilds identically, so Try again must not loop.
    func testPermanentSourceFailureHidesTryAgain() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-failure-permanent"]
        app.launch()

        XCTAssertTrue(app.staticTexts["Showing your last finished video"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons["native-editor-retry-source-preview"].exists)
    }

    /// KRI-211: a project made on another device has no originals here. Retry cannot fix that, so
    /// the editor says so in plain words and offers the one thing that can: finding the files.
    func testMissingOriginalsOffersFindingFilesInsteadOfRetry() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-missing-originals"]
        app.launch()

        XCTAssertTrue(app.staticTexts["Showing your last finished video"].waitForExistence(timeout: 8))
        let copy = app.staticTexts["native-editor-originals-unavailable"]
        XCTAssertTrue(copy.waitForExistence(timeout: 3))
        XCTAssertEqual(copy.label, "The original clips for this edit are on another device. Find the files to edit here.")
        XCTAssertFalse(app.buttons["native-editor-retry-source-preview"].exists, "Retry can never make missing originals appear")
        XCTAssertFalse(app.buttons["Retry"].exists)

        let find = app.buttons["native-editor-find-originals"]
        XCTAssertTrue(find.isHittable)
        find.tap()
        // The sheet lists the file the preview could not find (from the editor's own source pool),
        // and never claims the originals are already here while the preview says they are not.
        XCTAssertTrue(app.staticTexts["Find your originals"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Find original 1"].waitForExistence(timeout: 5), "A real relink target row is listed")
        XCTAssertFalse(app.staticTexts["The original files are available on this iPhone."].exists)
    }

    func testHardSourcePreviewFailureShowsUnavailableStateWithoutFallbackPlayer() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-source-failure"]
        app.launch()

        XCTAssertTrue(app.staticTexts["Preview unavailable"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["Retry"].firstMatch.isHittable, "Retry must not sit under the canvas")
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
        let originalTiming = text.value as? String ?? ""
        let original = timingSeconds(originalTiming)
        let pointsPerSecond = text.frame.width / max(0.1, (original?.end ?? 1.5) - (original?.start ?? 0))
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.6, thenDragTo: start.withOffset(CGVector(dx: 35, dy: 0)))
        waitForStableRowRelation(text, second) { abs($0) > 30 }
        XCTAssertGreaterThan(abs(text.frame.midY - second.frame.midY), 30)
        let timing = text.value as? String ?? ""
        XCTAssertFalse(timing.hasPrefix("00:00.0 to"), timing)
        // The block follows the finger's whole travel. The move used to drop
        // the travel before the drag's first sample; on a busy runner this
        // 70ms drag arrived as one sample and the block never moved.
        XCTAssertEqual(timingSeconds(timing)?.start ?? -1, 35 / pointsPerSecond, accuracy: 0.1, timing)
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
        // Overshoot: the move clamps at 0, so the block lands exactly on its
        // original timing instead of depending on the forward move's distance.
        moveBack.press(forDuration: 0.6, thenDragTo: moveBack.withOffset(CGVector(dx: -120, dy: 0)))
        waitForStableRowRelation(text, second) { abs($0) <= 1 }
        XCTAssertEqual(
            text.frame.midY, second.frame.midY, accuracy: 1,
            "text.value=\(text.value ?? "nil") second.value=\(second.value ?? "nil")"
        )
        // The value now carries a ", selected" suffix; compare the timing prefix.
        XCTAssertEqual((text.value as? String)?.hasPrefix(originalTiming), true, "text.value=\(text.value ?? "nil") originalTiming=\(originalTiming)")
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
        // KRI-508: the second tap opens Edit text with the keyboard up; Start/End wait behind Timing.
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-content"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.textFields["native-editor-text-time-start"].exists)
        app.buttons["native-editor-text-timing"].tap()
        XCTAssertTrue(app.textFields["native-editor-text-time-start"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["native-editor-text-time-end"].exists)
        XCTAssertTrue(app.buttons["native-editor-text-inspector-done"].exists)
    }

    /// KRI-508: tapping a selected text opens Edit text with the keyboard. The box above the keyboard
    /// is one line, grows with long words and with Return, and never reaches under the keyboard. The
    /// header steps away while it is open and comes back with Done.
    func testSecondTapOpensEditTextWithKeyboardAndGrowingBox() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let back = app.buttons["native-editor-back"]
        XCTAssertTrue(back.waitForExistence(timeout: 8))
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let browseHeight = preview.frame.height
        let text = app.descendants(matching: .any)["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"]
        XCTAssertTrue(text.waitForExistence(timeout: 3))
        text.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-context"].waitForExistence(timeout: 3))
        text.tap()

        let field = app.textViews["native-editor-text-content"]
        XCTAssertTrue(field.waitForExistence(timeout: 3))
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 5), "the keyboard is up as the panel opens")
        let editTab = app.buttons.matching(identifier: "native-editor-text-tabs")
            .matching(NSPredicate(format: "label == %@", "Edit text")).firstMatch
        XCTAssertTrue(editTab.isSelected, "an existing text opens on Edit text, not Style")
        XCTAssertTrue(back.waitForNonExistence(timeout: 3), "the header steps away so the video gets the screen")
        XCTAssertGreaterThan(preview.frame.height, browseHeight, "the video grows into the freed room")

        func waitFor(_ message: String, _ condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(5)
            while !condition(), Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
            XCTAssertTrue(condition(), message)
        }
        func assertClearOfKeyboard(_ message: String) {
            XCTAssertGreaterThanOrEqual(keyboard.frame.minY - field.frame.maxY, 12, message)
        }
        func shot(_ name: String) {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
        shot("text-edit-one-line")
        let oneLine = field.frame.height
        let previewOneLine = preview.frame.height
        XCTAssertLessThan(oneLine, 60, "the box starts as one line")
        assertClearOfKeyboard("one line")

        field.typeText("\nSecond line")
        waitFor("Return grows the box") { field.frame.height > oneLine + 10 }
        let twoLines = field.frame.height
        assertClearOfKeyboard("Return grows the box")

        field.typeText(String(repeating: " and a much longer line", count: 8))
        waitFor("long words grow the box") { field.frame.height > twoLines + 10 }
        shot("text-edit-grown")
        assertClearOfKeyboard("long words grow the box")
        waitFor("the preview gives the room to the box") { preview.frame.height < previewOneLine - 10 }

        app.buttons["native-editor-text-inspector-done"].tap()
        XCTAssertTrue(back.waitForExistence(timeout: 3), "the header returns with Done")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-panel"].waitForNonExistence(timeout: 3))
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
        // KRI-240: ONE tap opens the edit bar with the keyboard up and the caret
        // at the end of the line. No second tap on the field.
        row.tap()

        let field = app.textViews["native-editor-caption-field"]
        XCTAssertTrue(field.waitForExistence(timeout: 3), "the caption edit bar never appeared after one row tap")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 3), "one row tap must bring up the keyboard")
        XCTAssertEqual(app.staticTexts["native-editor-caption-position"].label, "Line 1 of 1")
        field.typeText(" edited")
        app.buttons["native-editor-caption-edit-done"].tap()

        // Done returns to the list; close and reopen to prove the edit reached the
        // document and survived a full close/reopen cycle.
        let panelDone = app.buttons["native-editor-captions-done"]
        XCTAssertTrue(panelDone.waitForExistence(timeout: 3))
        panelDone.tap()
        captions.tap()
        let reopenedRow = app.descendants(matching: .any)["native-editor-caption-row-cue-all"].firstMatch
        XCTAssertTrue(reopenedRow.waitForExistence(timeout: 3))
        XCTAssertTrue(reopenedRow.label.contains("caption edited"), "typed edit did not persist: \(reopenedRow.label)")
    }

    // Value: protects=the first opened line keeps its own transaction, so emptying it removes it with one Undo; fails_when=swapping the list for the bar ends the line's transaction (the Group lifecycle bug) or the field sits under the keyboard; why_new=the edit test never empties a line or checks the field frame; seam=none
    func testEmptyingATalkingCaptionLineRemovesItAndUndoRestoresIt() {
        let app = XCUIApplication()
        let field = openTalkingCaptionLine(app, row: "native-caption-0")
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 3))
        XCTAssertLessThanOrEqual(field.frame.maxY, keyboard.frame.minY + 1, "the line field must sit above the keyboard")
        XCTAssertEqual(app.staticTexts["native-editor-caption-position"].label, "Line 1 of 2")

        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 24))
        app.buttons["native-editor-caption-edit-done"].tap()

        let undo = app.buttons["native-editor-caption-undo-removal"]
        XCTAssertTrue(undo.waitForExistence(timeout: 3), "an emptied line is removed with an Undo")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-caption-row-native-caption-0"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-caption-row-native-caption-1"].exists)
        undo.tap()

        let restored = app.descendants(matching: .any)["native-editor-caption-row-native-caption-0"].firstMatch
        XCTAssertTrue(restored.waitForExistence(timeout: 3))
        XCTAssertTrue(restored.label.contains("Bu alan var mı?"), "one Undo restores the original line: \(restored.label)")
        XCTAssertTrue(restored.label.hasSuffix("1 second to 4 seconds"), "spoken times read in English: \(restored.label)")
    }

    private func openTalkingCaptionLine(_ app: XCUIApplication, row id: String) -> XCUIElement {
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-talking-captions"]
        app.launch()
        let tool = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(tool.waitForExistence(timeout: 8))
        tool.tap()
        let row = app.descendants(matching: .any)["native-editor-caption-row-\(id)"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        row.tap()
        let field = app.textViews["native-editor-caption-field"]
        XCTAssertTrue(field.waitForExistence(timeout: 3))
        return field
    }

    private func waitForLabel(_ element: XCUIElement, _ label: String) {
        let changed = expectation(for: NSPredicate(format: "label == %@", label), evaluatedWith: element)
        wait(for: [changed], timeout: 3)
    }

    // Value: protects=Previous/Next move between lines with the right text and ends disabled; fails_when=neighbour lookup or the bar's enablement regresses; why_new=no test left line 1; seam=none
    func testNextAndPreviousMoveBetweenCaptionLines() {
        let app = XCUIApplication()
        let field = openTalkingCaptionLine(app, row: "native-caption-0")
        let previous = app.buttons["native-editor-caption-previous"]
        let next = app.buttons["native-editor-caption-next"]
        let position = app.staticTexts["native-editor-caption-position"]
        XCTAssertFalse(previous.isEnabled)
        XCTAssertTrue(next.isEnabled)
        next.tap()
        waitForLabel(position, "Line 2 of 2")
        XCTAssertEqual(field.value as? String, "Evet, boş.")
        XCTAssertFalse(next.isEnabled, "the last line has no Next")
        previous.tap()
        waitForLabel(position, "Line 1 of 2")
        XCTAssertEqual(field.value as? String, "Bu alan var mı?")
    }

    // Value: protects=Save tapped mid-edit commits the open line and closes the bar; fails_when=beforeSave is dropped and the bar and keyboard stay up over a saved draft; why_new=no test saves with a line open; seam=none
    func testSavingWithACaptionLineOpenCommitsItAndClosesTheBar() {
        let app = XCUIApplication()
        let field = openTalkingCaptionLine(app, row: "native-caption-0")
        field.typeText(" tamam")
        app.buttons["native-editor-save"].tap()
        let closed = expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: field)
        wait(for: [closed], timeout: 3)
        let row = app.descendants(matching: .any)["native-editor-caption-row-native-caption-0"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        XCTAssertTrue(row.label.contains("Bu alan var mı? tamam"), "the open line's edit was committed: \(row.label)")
    }

    /// KRI-306: the header's video-shape button opens Vertical / Landscape and Black bars / Crop, enabled
    /// from the server capability; picking Landscape hides the fit row and reshapes the preview.
    func testVideoShapePickerIsEnabledFromTheCapabilityAndReshapesThePreview() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-video-shape"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 20))
        app.buttons["native-editor-video-shape-button"].tap()
        let section = app.descendants(matching: .any)["native-editor-video-shape-section"].firstMatch
        XCTAssertTrue(section.waitForExistence(timeout: 5))

        let vertical = app.buttons["native-editor-video-shape-orientation-portrait"]
        let landscape = app.buttons["native-editor-video-shape-orientation-landscape"]
        let blackBars = app.buttons["native-editor-video-shape-fit-fit"]
        let crop = app.buttons["native-editor-video-shape-fit-fill"]
        XCTAssertTrue(vertical.isEnabled && landscape.isEnabled && blackBars.isEnabled && crop.isEnabled)
        XCTAssertTrue(vertical.isSelected && blackBars.isSelected, "current values come from the capability")
        // The sheet reports its content at ~0.96 of layout size (every row measures 42.25 for a 44pt
        // minimum), so allow that scale here; the unscaled 44pt check runs on the confirm screen.
        for control in [vertical, landscape, blackBars, crop] {
            XCTAssertGreaterThanOrEqual(control.frame.height, 42, "touch target \(control.identifier): \(control.frame)")
        }
        XCTAssertFalse(app.staticTexts["native-editor-video-shape-locked-reason"].exists)

        crop.tap()
        XCTAssertTrue(crop.isSelected)
        XCTAssertFalse(blackBars.isSelected)
        landscape.tap()
        XCTAssertTrue(landscape.isSelected)
        XCTAssertFalse(crop.exists, "Landscape always crops, so the fit row goes away")
        vertical.tap()
        XCTAssertTrue(app.buttons["native-editor-video-shape-fit-fill"].waitForExistence(timeout: 3), "back to Vertical shows the fit row again")
        XCTAssertTrue(app.buttons["native-editor-video-shape-fit-fill"].isSelected, "the Crop pick survived the round trip")
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = "video-shape-editor-open"
        capture.lifetime = .keepAlways
        add(capture)

        // The pick reshapes the preview behind the sheet: the frame goes wide.
        landscape.tap()
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled, "the pick is an unsaved edit")
        let ratio = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in preview.frame.width > preview.frame.height }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [ratio], timeout: 5), .completed, "preview is landscape: \(preview.frame)")
    }

    /// KRI-306: a cloud editor closes both axes (reason `cloud_unsupported` on every map), so the
    /// header carries no video-shape button at all.
    func testVideoShapeButtonIsHiddenWhenBothCapabilitiesAreClosed() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-video-shape-closed"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["native-editor-export"].exists)
        XCTAssertFalse(app.buttons["native-editor-video-shape-button"].exists)
    }

    /// KRI-306: a format that can re-fit but not re-shape (voiceover montage) keeps the button; the
    /// closed orientation row is disabled and says why while bars/crop stays usable.
    func testVideoShapeShowsTheServerReasonWhenOnlyOrientationIsClosed() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-video-shape-fit-only"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        app.buttons["native-editor-video-shape-button"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-video-shape-section"].firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-video-shape-orientation-portrait"].isEnabled)
        XCTAssertFalse(app.buttons["native-editor-video-shape-orientation-landscape"].isEnabled)
        XCTAssertTrue(app.buttons["native-editor-video-shape-fit-fill"].isEnabled)
        XCTAssertTrue(app.buttons["native-editor-video-shape-fit-fit"].isSelected, "the capability value seeds the picker")
        let reason = app.descendants(matching: .any)["native-editor-video-shape-locked-reason"].firstMatch
        XCTAssertTrue(reason.waitForExistence(timeout: 3))
        XCTAssertTrue(reason.label.contains("can’t change shape"), reason.label)
        app.buttons["native-editor-video-shape-fit-fill"].tap()
        XCTAssertTrue(app.buttons["native-editor-video-shape-fit-fill"].isSelected)
        app.buttons["native-editor-video-shape-fit-fit"].tap()
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled, "toggling back to the loaded value is not an edit")
    }

    /// KRI-306: at accessibility text sizes the picker stacks its options and keeps 44pt targets.
    func testVideoShapePickerStacksAtAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-video-shape"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility3"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        app.buttons["native-editor-video-shape-button"].tap()
        let vertical = app.buttons["native-editor-video-shape-orientation-portrait"]
        let landscape = app.buttons["native-editor-video-shape-orientation-landscape"]
        XCTAssertTrue(vertical.waitForExistence(timeout: 5))
        XCTAssertGreaterThan(landscape.frame.minY, vertical.frame.maxY - 1, "options stack vertically")
        XCTAssertEqual(landscape.frame.minX, vertical.frame.minX, accuracy: 2)
        for control in [vertical, landscape] {
            XCTAssertGreaterThanOrEqual(control.frame.height, 42, "\(control.identifier): \(control.frame)")
        }
        let blackBars = app.buttons["native-editor-video-shape-fit-fit"]
        XCTAssertTrue(blackBars.exists)
        XCTAssertGreaterThanOrEqual(blackBars.frame.height, 42)
    }

    func testAllPersistedLanesExposeStableTimelineIdentityAndInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let cases: [(timelineID: String, inspectorID: String)] = [
            ("native-editor-timeline-music-00000000-0000-4000-8000-000000000350", "native-editor-selected-music-track"),
            ("native-editor-timeline-sound_effect-sfx-1", "native-editor-sfx-volume"),
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
            let doneID: String
            switch value.inspectorID {
            case "native-editor-visuals-panel": doneID = "native-editor-visuals-done"
            case "native-editor-sfx-volume": doneID = "native-editor-sounds-done"
            default: doneID = "native-editor-inspector-done"
            }
            let done = app.buttons[doneID]
            XCTAssertTrue(done.exists)
            done.tap()
        }
    }

    /// KRI-288: Effects home → library → add → edit sound → trim → use full sound → remove → home.
    func testSoundEffectsHomeLibraryEditTrimFlow() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))

        app.buttons["native-editor-tool-sounds"].tap()
        app.buttons["native-editor-sounds-tab-Effects"].tap()
        let home = app.descendants(matching: .any)["native-editor-sfx-home"].firstMatch
        XCTAssertTrue(home.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-sfx-row-sfx-1"].exists, "the fixture sound is listed under In this edit")
        attachShot("sfx-1-home", app)

        app.buttons["native-editor-sfx-add-sound"].tap()
        XCTAssertTrue(app.buttons["native-editor-sfx-library-back"].waitForExistence(timeout: 3))
        attachShot("sfx-2-library", app)
        let search = app.textFields["native-editor-sfx-search"]
        search.tap()
        search.typeText("zzzz\n")
        XCTAssertTrue(app.buttons["native-editor-sfx-clear-search"].waitForExistence(timeout: 3))
        app.buttons["native-editor-sfx-clear-search"].tap()
        app.buttons["native-editor-sfx-add-fx-pop"].tap()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-sfx-volume"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["native-editor-remove-sfx"].exists)
        attachShot("sfx-3-edit", app)
        app.buttons["native-editor-sfx-trim-row"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-sfx-trim-bar"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["native-editor-sfx-trim-reset"].isEnabled, "untrimmed sound is already full")
        let handle = app.descendants(matching: .any)["Trim end"].firstMatch
        XCTAssertTrue(handle.exists)
        let grip = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        grip.press(forDuration: 0.1, thenDragTo: grip.withOffset(CGVector(dx: -80, dy: 0)))
        XCTAssertTrue(app.buttons["native-editor-sfx-trim-reset"].waitForExistence(timeout: 2))
        attachShot("sfx-4-trim", app)
        app.buttons["native-editor-sfx-trim-reset"].tap()
        app.buttons["native-editor-sfx-trim-back"].tap()

        app.buttons["native-editor-remove-sfx"].tap()
        XCTAssertTrue(home.waitForExistence(timeout: 3), "removing the sound returns to the list")
    }

    private func attachShot(_ name: String, _ app: XCUIApplication) {
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = name
        capture.lifetime = .keepAlways
        add(capture)
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

    /// KRI-281: a DragGesture on the caption rows fought the ScrollView, so a long list (narrated /
    /// voiceover captions) could not be scrolled by hand to its last blocks.
    func testLongCaptionListScrollsByHandToLastLines() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-stress-71"]
        app.launch()

        let captions = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 8))
        captions.tap()
        let scroll = app.scrollViews["native-editor-captions-scroll"]
        XCTAssertTrue(scroll.waitForExistence(timeout: 5))
        let last = app.descendants(matching: .any)["native-editor-caption-row-stress-cue-30"].firstMatch
        // The panel is short on a phone; a hand swipe moves roughly one viewport.
        for _ in 0..<40 where !last.isHittable {
            scroll.swipeUp(velocity: .fast)
        }
        XCTAssertTrue(last.isHittable, "caption list did not scroll to line 31")
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

    /// Coordinate-based numeric editing needs the whole field above the rail;
    /// unlike element.tap(), a coordinate tap does not reveal an offscreen row.
    private func reveal(_ element: XCUIElement, in scroll: XCUIElement) {
        for _ in 0..<16 {
            let frame = element.frame
            let viewport = scroll.frame
            if element.isHittable, frame.minY >= viewport.minY, frame.maxY <= viewport.maxY { return }
            let moveUp = frame.midY > viewport.midY
            let start = scroll.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.7 : 0.4))
            let end = scroll.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.4 : 0.7))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: 40, thenHoldForDuration: 0.1)
        }
        XCTFail("Could not reveal \(element.identifier) above the tool rail")
    }

    /// Start and end seconds of a timeline bar's value, e.g.
    /// "00:00.7 to 00:02.2, selected".
    private func timingSeconds(_ value: String) -> (start: Double, end: Double)? {
        let stamps = (value.split(separator: ",").first ?? "").components(separatedBy: " to ").compactMap { stamp -> Double? in
            let parts = stamp.split(separator: ":")
            guard parts.count == 2, let minutes = Double(parts[0]), let seconds = Double(parts[1]) else { return nil }
            return minutes * 60 + seconds
        }
        return stamps.count == 2 ? (stamps[0], stamps[1]) : nil
    }

    /// Lanes repack synchronously when a timing drag ends, so this only gives
    /// XCUITest time to observe the new layout: the row relation must satisfy
    /// `holds` on two consecutive polls. A timeout means the items really do
    /// still overlap in time (test geometry), not a stale layout.
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
