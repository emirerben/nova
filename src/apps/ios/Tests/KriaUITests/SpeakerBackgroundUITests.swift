import XCTest

/// KRI-1118: the Background tool of a Talk-to-camera edit, on the fixture whose variant opens `speaker_background`
/// and whose pool holds one photo and one video. Pixel sampling of an exported frame is not asserted here: it needs
/// the renderer (engine lane) and is checked where the export runs.
@MainActor
final class SpeakerBackgroundUITests: XCTestCase {
    private func launch(reset: Bool, extra: [String] = ["-ui-testing-editor-speaker-background"]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"] + extra + (reset ? ["-ui-testing-editor-speaker-background-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func openBackground(_ app: XCUIApplication) {
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        let tool = app.buttons["native-editor-tool-background"]
        XCTAssertTrue(tool.waitForExistence(timeout: 5), "the server opened speaker_background, so the rail offers Background")
        tool.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-panel"].firstMatch.waitForExistence(timeout: 5))
    }

    private func tile(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.buttons["native-editor-background-" + id] }

    /// The header (and Save with it) steps away while a tool panel is open, so Save is read after Done.
    private func closePanel(_ app: XCUIApplication) {
        app.buttons["native-editor-background-done"].tap()
        XCTAssertTrue(app.buttons["native-editor-save"].waitForExistence(timeout: 5), "closing the panel brings the header back")
    }

    private func saveAndWait(_ app: XCUIApplication) {
        closePanel(app)
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.isEnabled)
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
    }

    func testPickingAPhotoSelectsItAndMakesTheEditSaveable() {
        let app = launch(reset: true)
        openBackground(app)
        let photo = tile(app, "visual-fixture-bg-photo")
        let video = tile(app, "visual-fixture-bg-video")
        XCTAssertTrue(photo.waitForExistence(timeout: 5), "the item's pool photo is offered")
        XCTAssertTrue(video.exists, "and its pool video")
        XCTAssertTrue(app.buttons["native-editor-background-add"].exists, "Add photo or video")
        XCTAssertTrue(tile(app, "none").isSelected, "a fresh Talking edit has no background")

        photo.tap()
        XCTAssertTrue(photo.isSelected)
        XCTAssertFalse(video.isSelected)
        XCTAssertFalse(tile(app, "none").isSelected)
        attach(app, "Photo background selected")

        video.tap()
        XCTAssertTrue(video.isSelected, "swapping moves the selection")
        XCTAssertFalse(photo.isSelected)
        photo.tap()
        closePanel(app)
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled, "a background is an unsaved edit")
    }

    func testNoneRemovesTheBackgroundAndBothChoicesSurviveSaveAndReopen() {
        let app = launch(reset: true)
        openBackground(app)
        tile(app, "visual-fixture-bg-video").tap()
        XCTAssertTrue(tile(app, "visual-fixture-bg-video").isSelected)
        saveAndWait(app)

        // Reopen: the saved lane comes back selected, and nothing is unsaved.
        app.terminate()
        let reopened = launch(reset: false)
        openBackground(reopened)
        XCTAssertTrue(tile(reopened, "visual-fixture-bg-video").waitForExistence(timeout: 5))
        XCTAssertTrue(tile(reopened, "visual-fixture-bg-video").isSelected, "the saved background survives a reopen")
        attach(reopened, "Reopened with the saved video background")
        closePanel(reopened)
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
        reopened.buttons["native-editor-tool-background"].tap()
        XCTAssertTrue(tile(reopened, "none").waitForExistence(timeout: 5))

        tile(reopened, "none").tap()
        XCTAssertTrue(tile(reopened, "none").isSelected)
        XCTAssertFalse(tile(reopened, "visual-fixture-bg-video").isSelected, "None removes the background")
        saveAndWait(reopened)

        reopened.terminate()
        let cleared = launch(reset: false)
        openBackground(cleared)
        XCTAssertTrue(tile(cleared, "none").waitForExistence(timeout: 5))
        XCTAssertTrue(tile(cleared, "none").isSelected, "the cleared lane stays cleared after a reopen")
        XCTAssertFalse(tile(cleared, "visual-fixture-bg-video").isSelected)
    }

    func testPickingNoneOnAnEditWithoutABackgroundIsNotAnEdit() {
        let app = launch(reset: true)
        openBackground(app)
        tile(app, "none").tap()
        closePanel(app)
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled)
    }

    func testToolIsHiddenWhenTheServerDoesNotOpenTheCapability() {
        let app = launch(reset: false, extra: ["-ui-testing-editor-talking-captions"])
        XCTAssertTrue(app.buttons["native-editor-tool-text"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].exists)
        XCTAssertFalse(app.buttons["native-editor-tool-background"].exists)
    }
}
