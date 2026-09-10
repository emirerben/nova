import XCTest

@MainActor
final class KriaUITests: XCTestCase {
    func testChatBubblesPreserveShapeAndWrappingAtAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat-bubbles"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        app.launch()

        let shortBubble = app.descendants(matching: .any)["chat-message-short"].firstMatch
        let longBubble = app.descendants(matching: .any)["chat-message-long"].firstMatch
        XCTAssertTrue(shortBubble.waitForExistence(timeout: 8))
        XCTAssertTrue(longBubble.waitForExistence(timeout: 3))

        let viewport = app.windows.firstMatch.frame
        XCTAssertGreaterThan(longBubble.frame.height, shortBubble.frame.height)
        XCTAssertGreaterThanOrEqual(shortBubble.frame.minX, viewport.minX + 54)
        XCTAssertGreaterThanOrEqual(longBubble.frame.minX, viewport.minX + 54)
        XCTAssertLessThanOrEqual(shortBubble.frame.maxX, viewport.maxX - 12)
        XCTAssertLessThanOrEqual(longBubble.frame.maxX, viewport.maxX - 12)
        XCTAssertEqual(shortBubble.frame.maxX, longBubble.frame.maxX, accuracy: 1)
        XCTAssertTrue(shortBubble.label.contains("Montage works."))
        XCTAssertTrue(longBubble.label.contains("ends on the wide sunset shot."))
    }

    func testLaunchEntersChatFirstWorkspace() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        // Exercise creation on every run, even when a prior project restores.
        createFreshChat(in: app)
        let prompt = app.staticTexts["What kind of video are we making?"]
        XCTAssertTrue(prompt.waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Attach footage"].exists)
        XCTAssertFalse(app.buttons["Attach footage"].isEnabled)
        XCTAssertTrue(app.staticTexts["Montage"].waitForExistence(timeout: 3))
        // The offline UI fixture cannot fetch server capabilities, so the
        // workspace must stay on its conservative Montage-only fallback.
        XCTAssertFalse(app.staticTexts["Narrated"].exists)

        let toggle = app.buttons["workspace-menu-toggle"]
        let closedMenuX = toggle.frame.minX
        let viewport = app.windows.firstMatch.frame
        XCTAssertEqual(app.staticTexts["workspace-project-title"].frame.midX, viewport.midX, accuracy: 2)
        XCTAssertFalse(app.buttons["header-new-chat"].exists)
        toggle.tap()
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertEqual(toggle.label, "Close projects")
        XCTAssertTrue(toggle.isHittable)
        XCTAssertGreaterThan(toggle.frame.minX, closedMenuX + 200)
        XCTAssertLessThanOrEqual(toggle.frame.maxX, viewport.maxX)
        XCTAssertEqual(app.buttons.matching(identifier: "Close projects").count, 1)
        toggle.tap()
        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == 'Open projects'"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 3), .completed)
        XCTAssertEqual(toggle.frame.minX, closedMenuX, accuracy: 2)
        let swipeStart = app.coordinate(withNormalizedOffset: CGVector(dx: 0.15, dy: 0.55))
        let swipeEnd = app.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.55))
        swipeStart.press(forDuration: 0.05, thenDragTo: swipeEnd)
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 3))
        XCTAssertEqual(toggle.label, "Close projects")
        let drawerScreenshot = XCTAttachment(screenshot: app.screenshot())
        drawerScreenshot.name = "Swipe-open tinted workspace"
        drawerScreenshot.lifetime = .keepAlways
        add(drawerScreenshot)
        swipeEnd.press(forDuration: 0.05, thenDragTo: swipeStart)
        let swipeClosed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == 'Open projects'"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [swipeClosed], timeout: 3), .completed)
        XCTAssertEqual(toggle.frame.minX, closedMenuX, accuracy: 2)
        toggle.tap()
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.buttons["drawer-new-chat"].exists)

        let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'" )).firstMatch
        XCTAssertTrue(gallery.exists)
        gallery.tap()
        XCTAssertTrue(app.staticTexts["Your finished videos"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["All"].exists)
        XCTAssertTrue(app.buttons["Ready"].exists)
    }

    func testProjectActionsCanBeCancelledWithoutChangingProject() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        createFreshChat(in: app)
        let menu = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Project actions for'")).firstMatch
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        let originalTitle = app.staticTexts["workspace-project-title"].label
        menu.tap()
        app.buttons["Rename project"].tap()
        XCTAssertTrue(app.textFields["rename-project-title"].waitForExistence(timeout: 3))
        app.buttons["Cancel"].tap()
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, originalTitle)
        menu.tap()
        let delete = app.buttons["Delete project"]
        XCTAssertTrue(delete.waitForExistence(timeout: 3))
        XCTAssertTrue(delete.isEnabled)
        delete.tap()
        let confirmation = app.alerts["Delete this project?"]
        XCTAssertTrue(confirmation.waitForExistence(timeout: 3))
        confirmation.buttons["Cancel"].tap()
        XCTAssertTrue(app.staticTexts["workspace-project-title"].waitForExistence(timeout: 3))
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, originalTitle)
    }

    private func createFreshChat(in app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What kind of video are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
    }

    func testNativeEditorStagesLocalEditsAndGatesUnavailableTools() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["native-editor-back"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-project-title"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-workspace-switcher"].exists)
        XCTAssertTrue(app.staticTexts["Local preview"].exists)
        XCTAssertTrue(app.buttons["native-editor-tool-text"].exists)

        app.buttons["native-editor-tool-text"].tap()
        let input = app.textFields["native-editor-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap()
        input.typeText(" ritual")
        app.buttons["native-editor-add-text"].tap()
        app.buttons["native-editor-inspector-done"].tap()

        XCTAssertTrue(app.buttons["native-editor-undo"].isEnabled)
        app.buttons["native-editor-undo"].tap()
        XCTAssertTrue(app.buttons["native-editor-redo"].isEnabled)
        app.buttons["native-editor-redo"].tap()

        app.buttons["native-editor-tool-captions"].tap()
        let captions = app.switches["native-editor-captions-toggle"]
        XCTAssertTrue(captions.waitForExistence(timeout: 3))
        captions.tap()
        app.buttons["native-editor-inspector-done"].tap()

        app.buttons["native-editor-tool-visuals"].tap()
        XCTAssertTrue(app.staticTexts["Select an existing lane to edit its timing and renderer-backed properties."].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["No visual lanes are present in this render."].exists)
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
        expectation(for: NSPredicate(format: "label == %@", "Pause preview"), evaluatedWith: play)
        waitForExpectations(timeout: 2)
        let advanced = NSPredicate(format: "value != %@", "0:00.0")
        expectation(for: advanced, evaluatedWith: clock)
        waitForExpectations(timeout: 4)

        play.tap()
        expectation(for: NSPredicate(format: "label == %@", "Play preview"), evaluatedWith: play)
        waitForExpectations(timeout: 2)
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
        expectation(for: NSPredicate(format: "label == %@", "Pause preview"), evaluatedWith: play)
        waitForExpectations(timeout: 2)
        expectation(for: NSPredicate(format: "value != %@", "0:04.7"), evaluatedWith: clock)
        waitForExpectations(timeout: 2)
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

        let input = app.textFields["native-editor-selected-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        input.tap()
        input.typeText(" that becomes a much longer multi-line title without changing the cut")
        app.buttons["native-editor-selected-text-apply"].tap()
        app.buttons["native-editor-inspector-done"].tap()

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

    private static func clipDuration(_ clip: XCUIElement) -> Double {
        Double(clip.value as? String ?? "") ?? 0
    }
}
