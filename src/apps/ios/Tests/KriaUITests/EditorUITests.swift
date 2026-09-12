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

    private static func clipDuration(_ clip: XCUIElement) -> Double {
        Double(clip.value as? String ?? "") ?? 0
    }
}
