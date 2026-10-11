import XCTest

@MainActor
final class EditorUITests: XCTestCase {
    /// KRI-524: the DEBUG host reads the server-produced, retimed creation
    /// draft from disk. This proves the native editor can open all twelve
    /// word bars, play the source preview, reopen the same saved draft, and
    /// send the rendered file to the dedicated simulator Photos library.
    func testCapturedCreationWordsPlayReopenAndExport() {
        let fixture = ProcessInfo.processInfo.environment["KRIA_UI_FIXTURE_DRAFT"]
            ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Fixtures/KRI524CreationDraft.json").path
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-captured-creation", "-ui-testing-editor-color-cuts"]
        app.launchEnvironment["KRIA_UI_FIXTURE_DRAFT"] = fixture
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-text"].tap()
        let list = app.descendants(matching: .any)["native-editor-text-list"].firstMatch
        XCTAssertTrue(list.waitForExistence(timeout: 5))
        for index in 1...12 {
            let row = app.descendants(matching: .any)["native-editor-text-row-guided-title::sequence-\(index)"].firstMatch
            XCTAssertTrue(row.exists, "server-produced word bar \(index) is available after retime")
        }
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "KRI-524 captured twelve word bars"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.buttons["native-editor-text-cancel"].tap()

        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.waitForExistence(timeout: 5))
        let clock = app.staticTexts["native-editor-current-time"]
        XCTAssertEqual(clock.value as? String, "0:00.0")
        play.tap()
        let advanced = expectation(for: NSPredicate(format: "value != %@", "0:00.0"), evaluatedWith: clock)
        wait(for: [advanced], timeout: 5)

        // Reopening must again use the captured server draft, not a static
        // Swift word fixture.
        app.terminate(); app.launch()
        XCTAssertTrue(preview.waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-text"].tap()
        XCTAssertTrue(list.waitForExistence(timeout: 5))
        for index in 1...12 {
            XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-row-guided-title::sequence-\(index)"].firstMatch.exists)
        }
        app.buttons["native-editor-text-cancel"].tap()
        app.buttons["native-editor-export"].tap()
        let save = app.buttons["Save to Photos"]
        XCTAssertTrue(save.waitForExistence(timeout: 15))
        save.tap()
        let exported = app.descendants(matching: .any)["native-editor-export-state"].firstMatch
        XCTAssertTrue(exported.waitForExistence(timeout: 30))
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let deadline = Date().addingTimeInterval(45)
        var saved = false
        while Date() < deadline {
            let alert = springboard.alerts.firstMatch
            if alert.exists, alert.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] %@", "Photos")).count > 0,
               alert.buttons["Allow"].exists {
                alert.buttons["Allow"].tap()
            }
            if exported.exists, exported.label.contains("Saved to Photos") {
                saved = true
                let receipt = XCTAttachment(screenshot: app.screenshot())
                receipt.name = "KRI-524 native export saved to simulator Photos"
                receipt.lifetime = .keepAlways
                add(receipt)
                break
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        XCTAssertTrue(saved, "Export never reached Saved to Photos: \(exported.debugDescription)")
    }

    func testDeletingFinalClipShowsEmptyCanvasAndUndoRestoresPlayback() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        for index in 0..<2 {
            clip.tap()
            XCTAssertFalse(app.buttons["native-editor-clip-back"].exists)
            XCTAssertFalse(app.buttons["native-editor-clip-more-actions"].exists)
            XCTAssertFalse(app.buttons["native-editor-clip-earlier-actions"].exists)
            let delete = app.buttons["native-editor-delete"]
            XCTAssertTrue(delete.waitForExistence(timeout: 5))
            if index == 0 {
                let screenshot = XCTAttachment(screenshot: app.screenshot())
                screenshot.name = "Clip actions with passive scroll fade"
                screenshot.lifetime = .keepAlways
                add(screenshot)
            }
            let context = app.scrollViews.containing(.button, identifier: "native-editor-delete").firstMatch
            context.swipeLeft()
            XCTAssertTrue(delete.isHittable, "Swiping the action bar must reveal Delete without dismissing it")
            XCTAssertTrue(app.windows.firstMatch.frame.contains(delete.frame), "The full Delete button must fit on screen")
            if index == 0 {
                let screenshot = XCTAttachment(screenshot: app.screenshot())
                screenshot.name = "Clip actions after swiping"
                screenshot.lifetime = .keepAlways
                add(screenshot)
            }
            XCTAssertTrue(delete.isEnabled)
            delete.tap()
        }

        let addClip = app.buttons["native-editor-empty-add-clip"]
        XCTAssertTrue(addClip.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-play-pause"].isEnabled)
        XCTAssertFalse(app.buttons["native-editor-export"].isEnabled)
        XCTAssertFalse(clip.exists)

        app.buttons["native-editor-undo"].tap()
        XCTAssertTrue(clip.waitForExistence(timeout: 5))
        XCTAssertFalse(addClip.exists)
        XCTAssertTrue(app.buttons["native-editor-play-pause"].isEnabled)

        app.buttons["native-editor-redo"].tap()
        XCTAssertTrue(addClip.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-play-pause"].isEnabled)
        addClip.tap()
        XCTAssertTrue(app.buttons["native-editor-add-clip-files"].waitForExistence(timeout: 5))
    }

    func testClipActionsDismissOnOutsideTapAndKeepClipSelectionWorking() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        let secondClip = app.descendants(matching: .any)["native-editor-clip-2"].firstMatch
        let adjust = app.buttons["native-editor-adjust"]
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let fullscreen = app.descendants(matching: .any)["native-editor-preview-fullscreen"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        XCTAssertTrue(adjust.waitForExistence(timeout: 5))
        secondClip.tap()
        XCTAssertTrue(adjust.exists, "Selecting another clip keeps its actions available")

        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.06, dy: 0.06)).tap()
        XCTAssertFalse(adjust.exists, "An empty preview tap dismisses the selected clip actions")
        XCTAssertFalse(fullscreen.exists, "The dismissal tap must not also open fullscreen")

        clip.tap()
        XCTAssertTrue(adjust.waitForExistence(timeout: 5))
        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.05, dy: 0.25)).tap()
        XCTAssertFalse(adjust.exists, "An empty timeline tap dismisses the selected clip actions")

        clip.tap()
        XCTAssertTrue(adjust.waitForExistence(timeout: 5))
        app.coordinate(withNormalizedOffset: .zero)
            .withOffset(CGVector(dx: 8, dy: preview.frame.midY - app.frame.minY)).tap()
        XCTAssertFalse(adjust.exists, "Tapping the margin beside the preview dismisses the actions")

        clip.tap()
        XCTAssertTrue(adjust.waitForExistence(timeout: 5))
        adjust.tap()
        XCTAssertTrue(app.navigationBars["Adjust"].waitForExistence(timeout: 5), "Toolbar actions still open their inspector")
    }

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
        // The outro placeholder still appears after the last clip (merged from the QuickAdd suite).
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-outro-placeholder"].waitForExistence(timeout: 3))
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
        for shape in ["boundary", "unknown-sections"] {
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

    /// KRI-281: a clip slowed to fill a window longer than its source span must
    /// still be drawn across the whole window, so adjacent video blocks abut.
    func testRetimedClipBlocksAbutWithoutGap() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-retimed-clips"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let first = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        let second = app.descendants(matching: .any)["native-editor-clip-2"].firstMatch
        XCTAssertTrue(first.waitForExistence(timeout: 20))
        XCTAssertTrue(second.waitForExistence(timeout: 5))
        // Clip 1 is a 2.6s window over a 1.45s source span; its block must span 2.6s
        // (blocks are 2.6s and 3s, so widths keep that ratio) and meet clip 2.
        XCTAssertEqual(first.frame.maxX, second.frame.minX, accuracy: 1.0,
                       "Gap between clip blocks: first ends \(first.frame.maxX), second starts \(second.frame.minX)")
        XCTAssertEqual(first.frame.width / second.frame.width, 2.6 / 3.0, accuracy: 0.02)
    }
}
