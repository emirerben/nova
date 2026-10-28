import XCTest

/// KRI-1007: the caption panel's Animation tab, for all captions and for the line last tapped in Edit captions,
/// on cue captions (a Talking edit) and on text-lane captions (lip-sync lyrics).
@MainActor
final class NativeCaptionAnimationUITests: XCTestCase {
    private func launch(_ arguments: [String], environment: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"] + arguments
        app.launchEnvironment.merge(environment) { _, new in new }
        app.launch()
        let captions = app.buttons["native-editor-tool-captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 20))
        captions.tap()
        return app
    }

    /// Simulator evidence for review: kept on the result bundle, and written to `KRIA_SHOT_DIR` when set.
    private func shot(_ app: XCUIApplication, _ name: String, element: XCUIElement? = nil) {
        let screenshot = element?.screenshot() ?? app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let directory = ProcessInfo.processInfo.environment["KRIA_SHOT_DIR"] {
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name + ".png"))
        }
    }

    private func selected(_ element: XCUIElement) -> Bool { element.isSelected }

    private func pickLine(_ app: XCUIApplication, row: String) {
        app.buttons["native-editor-captions-tab-Edit captions"].tap()
        let line = app.descendants(matching: .any)[row].firstMatch
        XCTAssertTrue(line.waitForExistence(timeout: 5))
        line.tap()
        let done = app.buttons["native-editor-caption-edit-done"]
        if done.waitForExistence(timeout: 3) { done.tap() }
        app.buttons["native-editor-captions-tab-Animation"].tap()
    }

    private func scrollTo(_ app: XCUIApplication, _ element: XCUIElement) {
        let scroll = app.scrollViews["native-editor-captions-scroll"]
        for _ in 0..<4 where !element.isHittable { scroll.swipeUp() }
    }

    private func exerciseScopes(_ app: XCUIApplication, row: String, line: String, prefix: String) {
        let tab = app.buttons["native-editor-captions-tab-Animation"]
        XCTAssertTrue(tab.waitForExistence(timeout: 5))
        tab.tap()
        let all = app.buttons["native-editor-caption-animation-scope-all"]
        let one = app.buttons["native-editor-caption-animation-scope-line"]
        XCTAssertTrue(all.waitForExistence(timeout: 3))
        XCTAssertTrue(selected(all), "All captions is the default scope")
        XCTAssertFalse(one.isEnabled, "no line is picked yet")
        XCTAssertTrue(app.staticTexts["native-editor-caption-animation-line-reason"].exists)
        shot(app, "\(prefix)-1-all-unset")

        app.buttons["In animation Fade"].tap()
        XCTAssertTrue(selected(app.buttons["In animation Fade"]))
        let card = app.buttons["In animation Pop"]
        shot(app, "\(prefix)-2-card-frame-a", element: card)
        Thread.sleep(forTimeInterval: 0.35)
        shot(app, "\(prefix)-3-card-frame-b", element: card)

        pickLine(app, row: row)
        // The playhead sits at the picked line's start: two frames of its default fade-in while it plays.
        app.buttons["native-editor-play-pause"].firstMatch.tap()
        Thread.sleep(forTimeInterval: 0.05)
        shot(app, "\(prefix)-3a-playing-frame-a")
        Thread.sleep(forTimeInterval: 0.25)
        shot(app, "\(prefix)-3b-playing-frame-b")
        app.buttons["native-editor-play-pause"].firstMatch.tap()
        XCTAssertTrue(one.waitForExistence(timeout: 3))
        XCTAssertTrue(one.isEnabled)
        XCTAssertEqual(one.label, line)
        one.tap()
        XCTAssertTrue(selected(one))
        XCTAssertTrue(selected(app.buttons["In animation Fade"]), "a line without its own animation shows the default")
        app.buttons["Loop"].tap()
        app.buttons["Loop animation Bounce"].tap()
        XCTAssertTrue(selected(app.buttons["Loop animation Bounce"]))
        shot(app, "\(prefix)-4-line-bounce")

        all.tap()
        XCTAssertTrue(selected(app.buttons["Loop animation None"]), "the default has no loop")
        XCTAssertTrue(app.staticTexts["native-editor-caption-animation-overrides"].exists)
        shot(app, "\(prefix)-5-all-with-override")
        app.buttons["Loop animation Pulse"].tap()
        XCTAssertFalse(app.staticTexts["native-editor-caption-animation-overrides"].exists, "All captions clears the line's own")
        one.tap()
        XCTAssertTrue(selected(app.buttons["Loop animation Pulse"]), "the line now matches all captions")
        shot(app, "\(prefix)-6-line-matches-all")
    }

    func testCueCaptionsAnimateAllThenOneLine() {
        let app = launch(["-ui-testing-editor-talking-captions", "-ui-testing-editor-caption-animation", "-ui-testing-editor-live-preview"])
        exerciseScopes(app, row: "native-editor-caption-row-native-caption-1", line: "Line 2", prefix: "cue")
    }

    func testLyricCaptionsAnimateAllThenOneLine() {
        let app = launch(["-ui-testing-editor-song-captions", "-ui-testing-editor-caption-animation", "-ui-testing-editor-live-preview"])
        exerciseScopes(app, row: "native-editor-caption-row-song-caption-1-1", line: "Line 5", prefix: "lyrics")
    }

    /// Style > Text motion folded into Animation: its stored entrance shows as the All captions choice, and the
    /// designed edges stay one tap away there.
    func testTextMotionPresetShowsInAllCaptions() {
        for (fixture, prefix) in [("-ui-testing-editor-talking-captions", "preset-cue"), ("-ui-testing-editor-song-captions", "preset-lyrics")] {
            let app = launch([fixture, "-ui-testing-editor-caption-animation", "-ui-testing-editor-caption-text-motion",
                              "-ui-testing-editor-live-preview"])
            app.buttons["native-editor-captions-tab-Style"].tap()
            XCTAssertFalse(app.descendants(matching: .any)["native-editor-caption-text-motion"].exists, "Text motion lives in Animation now")
            app.buttons["native-editor-captions-tab-Animation"].tap()
            XCTAssertTrue(selected(app.buttons["native-editor-caption-animation-scope-all"]))
            let silk = app.buttons["In animation Silk Reveal"]
            scrollTo(app, silk)
            XCTAssertTrue(silk.isSelected, "the Text motion entrance is the all-captions default")
            shot(app, "\(prefix)-all-silk-reveal")
            let echo = app.buttons["In animation Echo Stack"]
            scrollTo(app, echo)
            echo.tap()
            XCTAssertTrue(echo.isSelected)
            XCTAssertFalse(silk.isSelected)
            app.terminate()
        }
    }

    func testWithoutTheCapabilityThereIsNoAnimationTab() {
        let app = launch(["-ui-testing-editor-talking-captions"])
        XCTAssertTrue(app.buttons["native-editor-captions-tab-Style"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-captions-tab-Animation"].exists)
    }

    func testReduceMotionKeepsPreviewsStillUntilPlayed() {
        let app = launch(["-ui-testing-editor-talking-captions", "-ui-testing-editor-caption-animation"],
                         environment: ["UI_TEST_REDUCE_MOTION": "1"])
        app.buttons["native-editor-captions-tab-Animation"].tap()
        let toggle = app.buttons["native-editor-caption-animation-preview-toggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 3))
        scrollTo(app, toggle)
        XCTAssertEqual(toggle.label, "Play previews")
        toggle.tap()
        XCTAssertEqual(toggle.label, "Pause previews")
    }
}
