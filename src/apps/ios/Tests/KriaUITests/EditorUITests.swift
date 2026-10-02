import XCTest

@MainActor
final class EditorUITests: XCTestCase {
    func testNativeEditorStagesLocalEditsAndGatesUnavailableTools() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["native-editor-back"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-project-title"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-workspace-switcher"].exists)
        XCTAssertTrue(app.buttons["native-editor-tool-text"].exists)

        app.buttons["native-editor-tool-text"].tap()
        let input = app.descendants(matching: .any)["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap()
        input.typeText(" ritual")
        app.buttons["native-editor-text-done"].tap()
        app.buttons["native-editor-text-inspector-done"].tap()

        XCTAssertTrue(app.buttons["native-editor-undo"].isEnabled)
        app.buttons["native-editor-undo"].tap()
        XCTAssertTrue(app.buttons["native-editor-redo"].isEnabled)
        app.buttons["native-editor-redo"].tap()

        app.buttons["native-editor-tool-captions"].tap()
        app.buttons["native-editor-captions-tab-Settings"].tap()
        let captions = app.buttons["native-editor-caption-visible"]
        XCTAssertTrue(captions.waitForExistence(timeout: 3))
        captions.tap()
        app.buttons["Off"].tap()
        app.buttons["native-editor-captions-done"].tap()

        app.buttons["native-editor-tool-visuals"].tap()
        XCTAssertTrue(app.staticTexts["Add visual"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Your added photos and videos"].exists)
    }

    func testNativeEditorBackButtonReturnsToPreviousSurface() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        app.buttons["native-editor-back"].tap()

        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.buttons["drawer-new-chat"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-preview"].firstMatch.exists)
    }

    func testNativeEditorFixturePlaysAndAdvancesTheClock() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.waitForExistence(timeout: 8))
        let clock = app.staticTexts["native-editor-current-time"]
        XCTAssertTrue(clock.exists)
        XCTAssertEqual(clock.value as? String, "0:00.0")

        play.tap()
        // The label flips optimistically, synchronously with the tap, before
        // playback actually starts — but that flip can't reach the screen
        // until togglePlayback() returns, and it activates AVAudioSession
        // (setCategory/setActive) synchronously right after. That system
        // call's completion time is not under app control and can occasionally
        // exceed 2s on a loaded CI simulator, so this waits generously for
        // real app state rather than media readiness.
        expectation(for: NSPredicate(format: "label == %@", "Pause preview"), evaluatedWith: play)
        waitForExpectations(timeout: 5)
        let advanced = NSPredicate(format: "value != %@", "0:00.0")
        expectation(for: advanced, evaluatedWith: clock)
        waitForExpectations(timeout: 4)

        // This fixture lasts only 4.7s. XCTest's idle synchronization can
        // deliver a second toggle after its natural end, starting replay.
        // Pause is covered synchronously by the session transport test;
        // natural completion and replay are covered by the next UI test.
    }

    func testNativeEditorPlaysTheRenderedAssetThroughItsRealEndAndReplays() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let play = app.buttons["native-editor-play-pause"]
        let clock = app.staticTexts["native-editor-current-time"]
        let duration = app.staticTexts["native-editor-duration"]
        XCTAssertTrue(play.waitForExistence(timeout: 8))
        XCTAssertTrue(duration.exists)

        // The fixture timeline deliberately claims 6.0s while the bundled
        // rendered asset is 4.666…s. The transport must reconcile to the
        // playable media instead of stopping before a fictional timeline end.
        expectation(for: NSPredicate(format: "value == %@", "0:04.7"), evaluatedWith: duration)
        waitForExpectations(timeout: 5)

        play.tap()
        expectation(for: NSPredicate(format: "label == %@", "Play preview"), evaluatedWith: play)
        waitForExpectations(timeout: 8)
        XCTAssertEqual(clock.value as? String, "0:04.7")

        play.tap()
        // Same AVAudioSession.setActive latency as the sibling fixture test
        // above; mirror its 5s / 4s waits rather than the tighter 2s here.
        expectation(for: NSPredicate(format: "label == %@", "Pause preview"), evaluatedWith: play)
        waitForExpectations(timeout: 5)
        expectation(for: NSPredicate(format: "value != %@", "0:04.7"), evaluatedWith: clock)
        waitForExpectations(timeout: 4)
    }

    func testFirstSourcePreviewPlaysBrandOutroAndReplays() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launch()

        let play = app.buttons["native-editor-play-pause"]
        let clock = app.staticTexts["native-editor-current-time"]
        let duration = app.staticTexts["native-editor-duration"]
        XCTAssertTrue(play.waitForExistence(timeout: 8))
        // Four editable seconds plus the bundled 1.6-second brand outro,
        // visible on first open without saving or exporting.
        expectation(for: NSPredicate(format: "value == %@", "0:05.6"), evaluatedWith: duration)
        waitForExpectations(timeout: 15)
        let first = XCTAttachment(screenshot: app.screenshot())
        first.name = "Branded first preview"
        first.lifetime = .keepAlways
        add(first)

        play.tap()
        expectation(for: NSPredicate(format: "value == %@", "0:05.6"), evaluatedWith: clock)
        waitForExpectations(timeout: 12)
        XCTAssertEqual(play.label, "Play preview")
        let tail = XCTAttachment(screenshot: app.screenshot())
        tail.name = "Branded outro before export"
        tail.lifetime = .keepAlways
        add(tail)

        play.tap()
        expectation(for: NSPredicate(format: "value != %@", "0:05.6"), evaluatedWith: clock)
        waitForExpectations(timeout: 5)
    }

    func testNativeEditorLongTextEditPreservesDurationAndClipGeometry() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let duration = app.staticTexts["native-editor-duration"]
        XCTAssertTrue(duration.waitForExistence(timeout: 8))
        expectation(for: NSPredicate(format: "value == %@", "0:04.7"), evaluatedWith: duration)
        waitForExpectations(timeout: 5)

        let firstClip = app.descendants(matching: .any)["native-editor-clip-1"]
        let secondClip = app.descendants(matching: .any)["native-editor-clip-2"]
        XCTAssertTrue(firstClip.exists)
        XCTAssertTrue(secondClip.exists)
        let originalFirstClip = firstClip.value as? String
        let originalSecondClip = secondClip.value as? String

        let text = app.descendants(matching: .any)["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"]
        XCTAssertTrue(text.waitForExistence(timeout: 3))
        text.tap()
        app.buttons["Edit text"].tap()
        app.buttons["Edit text"].tap()
        let input = app.descendants(matching: .any)["native-editor-text-content"]
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap()
        input.typeText(" that becomes a much longer multi-line title without changing the cut")
        app.buttons["native-editor-text-inspector-done"].tap()

        let updatedText = app.descendants(matching: .any)["native-editor-preview-text-00000000-0000-4000-8000-000000000100"]
        expectation(
            for: NSPredicate(format: "label CONTAINS %@", "much longer multi-line title"),
            evaluatedWith: updatedText
        )
        waitForExpectations(timeout: 3)
        XCTAssertEqual(duration.value as? String, "0:04.7")
        XCTAssertEqual(firstClip.value as? String, originalFirstClip)
        XCTAssertEqual(secondClip.value as? String, originalSecondClip)
    }

    func testNativeEditorTrimHandleShortensExtendsAndUndoesAsOneGesture() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let clip = app.descendants(matching: .any)["native-editor-clip-1"]
        XCTAssertTrue(clip.waitForExistence(timeout: 8))
        clip.tap()

        let trailing = app.descendants(matching: .any)["native-editor-trim-trailing"]
        XCTAssertTrue(trailing.waitForExistence(timeout: 2))
        let originalValue = try! XCTUnwrap(clip.value as? String)
        let originalDuration = Self.clipDuration(clip)
        let start = trailing.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: -72, dy: 0)))

        expectation(for: NSPredicate(format: "value != %@", originalValue), evaluatedWith: clip)
        waitForExpectations(timeout: 2)
        XCTAssertLessThan(Self.clipDuration(clip), originalDuration)

        let undo = app.buttons["native-editor-undo"]
        XCTAssertTrue(undo.isEnabled)
        undo.tap()
        expectation(for: NSPredicate(format: "value == %@", originalValue), evaluatedWith: clip)
        waitForExpectations(timeout: 2)
        XCTAssertFalse(undo.isEnabled)

        let leading = app.descendants(matching: .any)["native-editor-trim-leading"]
        XCTAssertTrue(leading.waitForExistence(timeout: 2))
        let restoredStart = leading.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        restoredStart.press(forDuration: 0.15, thenDragTo: restoredStart.withOffset(CGVector(dx: -24, dy: 0)))
        expectation(for: NSPredicate(format: "value != %@", originalValue), evaluatedWith: clip)
        waitForExpectations(timeout: 2)
        XCTAssertGreaterThan(Self.clipDuration(clip), originalDuration)
    }

    func testNativeEditorNamedFixturesLaunchWithoutAnAccount() {
        for shape in ["two-text", "boundary", "all-lanes", "stress-71", "unknown-sections"] {
            let app = XCUIApplication()
            app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-\(shape)"]
            app.launch()

            let marker = app.descendants(matching: .any)["native-editor-fixture-\(shape)"]
            XCTAssertTrue(marker.waitForExistence(timeout: 8), "Fixture \(shape) did not launch")
            XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.exists)
            app.terminate()
        }
    }

    /// KRI-170: an empty tap on the preview opens a dimmed ~90% fullscreen
    /// watch surface that plays; tapping it returns to the editor unchanged.
    func testPreviewEmptyTapTogglesFullscreen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let clock = app.staticTexts["native-editor-current-time"]
        let fullscreen = app.descendants(matching: .any)["native-editor-preview-fullscreen"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 20))
        XCTAssertFalse(fullscreen.exists)
        let originalFrame = preview.frame
        let startTime = clock.value as? String

        // Top-leading corner: away from the bottom-trailing conversation button.
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.06, dy: 0.06)).tap()
        XCTAssertTrue(fullscreen.waitForExistence(timeout: 5))
        // `.isModal` wraps the video in an alert container; the video box is its child.
        let box = fullscreen.descendants(matching: .any).firstMatch
        XCTAssertTrue(box.exists)
        // The box grows out of the preview; wait for the animation to settle.
        let settled = NSPredicate { _, _ in abs(box.frame.width - app.frame.width * 0.9) < 4 }
        expectation(for: settled, evaluatedWith: nil)
        waitForExpectations(timeout: 5)
        XCTAssertEqual(box.frame.width, app.frame.width * 0.9, accuracy: 4)
        XCTAssertGreaterThan(box.frame.height, originalFrame.height * 1.5)
        XCTAssertEqual(box.frame.midY, app.frame.midY, accuracy: 4, "centered on the full screen")
        // Entering plays (through the re-hosted layer).
        expectation(for: NSPredicate(format: "value != %@", startTime ?? ""), evaluatedWith: clock)
        waitForExpectations(timeout: 6)

        fullscreen.tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: fullscreen)
        waitForExpectations(timeout: 5)
        XCTAssertEqual(preview.frame.height, originalFrame.height, accuracy: 2)
        // It was paused before entering, so leaving pauses again.
        XCTAssertEqual(app.buttons["native-editor-play-pause"].label, "Play preview")
    }

    /// KRI-170: taps on objects still select them; only empty taps go fullscreen.
    func testPreviewObjectTapStillSelectsNotFullscreen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let text = app.descendants(matching: .any)["native-editor-preview-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 20))
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.40)).tap()
        XCTAssertFalse(
            app.descendants(matching: .any)["native-editor-preview-fullscreen"].firstMatch.waitForExistence(timeout: 1.5),
            "tapping a text object must select it, not open fullscreen"
        )
        XCTAssertTrue(
            app.descendants(matching: .any)["native-editor-text-context"].firstMatch.waitForExistence(timeout: 5),
            "the text object should be selected"
        )
    }

    /// KRI-185: the Text tab lists every on-screen text block, so a title or a
    /// per-clip label is one tap away instead of a hunt along the timeline.
    func testTextTabListsEveryTextBlockAndOpensOneForEditing() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-guided-text"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))

        app.buttons["native-editor-tool-text"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-list"].waitForExistence(timeout: 3))
        func row(_ id: String) -> XCUIElement {
            app.descendants(matching: .any)["native-editor-text-row-" + id].firstMatch
        }
        let title = row("guided-title")
        let first = row("clip-label-unified-cut-1")
        let note = row("creator-note")
        let second = row("clip-label-unified-cut-2")
        for block in [title, first, note, second] { XCTAssertTrue(block.waitForExistence(timeout: 3)) }
        // Captions have their own tab, and removed blocks are not in the video.
        XCTAssertFalse(row("caption-1").exists)
        XCTAssertFalse(row("clip-label-unified-cut-3").exists)
        // Title first, then the clips and the creator's text in time order.
        XCTAssertLessThan(title.frame.minY, first.frame.minY)
        XCTAssertLessThan(first.frame.minY, note.frame.minY)
        XCTAssertLessThan(note.frame.minY, second.frame.minY)
        XCTAssertTrue(second.label.contains("Dolmabahçe Palace"))
        XCTAssertTrue(second.label.contains("Clip 2"))

        // One tap opens the block on its words.
        second.tap()
        let content = app.textViews["native-editor-text-content"]
        XCTAssertTrue(content.waitForExistence(timeout: 3))
        XCTAssertEqual(content.value as? String, "Dolmabahçe Palace")
        // The block being edited must stay visible above the panel, not hidden under it.
        let onCanvas = app.descendants(matching: .any)["native-editor-preview-text-clip-label-unified-cut-2"].firstMatch
        let panelFrame = app.descendants(matching: .any)["native-editor-connected-panel"].firstMatch.frame
        XCTAssertTrue(onCanvas.waitForExistence(timeout: 3))
        XCTAssertLessThanOrEqual(onCanvas.frame.maxY, panelFrame.minY + 1,
                                 "the panel must not cover the text that is being edited")
        content.tap()
        content.typeText(" Gate")
        app.buttons["native-editor-text-inspector-done"].tap()

        // Done returns to the list, which shows the edit.
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-list"].waitForExistence(timeout: 3))
        XCTAssertTrue(second.waitForExistence(timeout: 3))
        XCTAssertTrue(second.label.contains("Gate"))
    }

    private static func clipDuration(_ clip: XCUIElement) -> Double {
        Double(clip.value as? String ?? "") ?? 0
    }
}
