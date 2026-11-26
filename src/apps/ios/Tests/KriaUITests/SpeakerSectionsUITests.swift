import XCTest

/// KRI-1558 (failure modes 38, 46): the speaker controls with the section gate open. The fixture seeds a colour fill with four
/// sections (own take 0-1.5 s, bottom-right corner 1.5-3.5 s, picture alone 3.5-4.5 s, bottom-left corner 4.5-6 s) and no
/// whole-video frame; the playhead starts at 0 s, inside the own-take section. Save goes through the stub commit transport,
/// which keeps the lane it was sent across relaunches.
@MainActor
final class SpeakerSectionsUITests: XCTestCase {
    private func launch(reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speaker-background", "-ui-testing-editor-speaker-sections"]
            + (reset ? ["-ui-testing-editor-speaker-background-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        return app
    }

    private func openBackgroundTool(_ app: XCUIApplication) {
        let tool = app.buttons["native-editor-tool-background"]
        XCTAssertTrue(tool.waitForExistence(timeout: 5))
        tool.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-panel"].firstMatch.waitForExistence(timeout: 5))
    }

    func testACornerPickedInsideASectionChangesThatSectionAndSurvivesSave() {
        let app = launch(reset: true)
        openBackgroundTool(app)
        let scope = app.segmentedControls["native-editor-speaker-scope"]
        XCTAssertTrue(scope.waitForExistence(timeout: 5), "the playhead is inside a section, so the choice shows")
        XCTAssertTrue(scope.buttons["This part"].isSelected, "a section edit is the default")
        XCTAssertFalse(app.buttons["native-editor-background-speaker-full"].isSelected, "an own-take section lights no spot")

        let corner = app.buttons["native-editor-background-speaker-small_top_left"]
        corner.tap()
        XCTAssertTrue(corner.isSelected, "the section under the playhead now draws the speaker top-left")
        XCTAssertTrue(scope.exists, "still inside the section")

        app.buttons["native-editor-background-done"].tap()
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled, "there is something to save")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
        app.terminate()

        let reopened = launch(reset: false)
        let reopenedSave = reopened.buttons["native-editor-save"]
        XCTAssertTrue(reopenedSave.waitForExistence(timeout: 5))
        XCTAssertFalse(reopenedSave.isEnabled, "nothing unsaved after reopening")
        openBackgroundTool(reopened)
        let saved = reopened.buttons["native-editor-background-speaker-small_top_left"]
        XCTAssertTrue(saved.waitForExistence(timeout: 5))
        XCTAssertTrue(saved.isSelected, "the saved section keeps its corner")
        XCTAssertTrue(reopened.segmentedControls["native-editor-speaker-scope"].exists, "the other sections came back too")
    }
}
