import XCTest

/// KRI-1136: the Tilt sheet on a device-rendered montage. Opens a clip, turns it 15 degrees, picks a blurred
/// background, saves through the stub commit transport and reopens to find the tilt where it was left.
@MainActor
final class NativeTiltUITests: XCTestCase {
    private func launch(reset: Bool, capability: Bool = true, media: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        if capability { app.launchArguments += ["-ui-testing-editor-tilt"] }
        if media { app.launchArguments += ["-ui-testing-editor-tilt-media"] }
        if reset && capability { app.launchArguments += ["-ui-testing-editor-tilt-reset"] }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    private func openTilt(_ app: XCUIApplication) {
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        let tilt = app.buttons["native-editor-clip-tilt"]
        XCTAssertTrue(tilt.waitForExistence(timeout: 5), "Tilt sits in the clip's context strip")
        XCTAssertTrue(tilt.isHittable)
        tilt.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-tilt-sheet"].firstMatch.waitForExistence(timeout: 5))
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = name
        capture.lifetime = .keepAlways
        add(capture)
    }

    func testTiltFifteenDegreesWithABlurredBackgroundSavesAndSurvivesReopen() {
        let app = launch(reset: true)
        openTilt(app)
        let value = app.staticTexts["native-editor-tilt-value"]
        XCTAssertEqual(value.label, "0°")
        XCTAssertFalse(app.buttons["native-editor-tilt-reset"].isEnabled, "an upright clip has nothing to reset")

        let right = app.buttons["native-editor-tilt-nudge-right"]
        for _ in 0..<15 { right.tap() }
        XCTAssertEqual(value.label, "15°")
        app.buttons["native-editor-tilt-backdrop-blur"].tap()
        XCTAssertTrue(app.buttons["native-editor-tilt-backdrop-blur"].isSelected)
        attach(app, "Tilt sheet at 15 degrees with a blurred background")
        app.buttons["native-editor-inspector-done"].tap()

        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.isEnabled, "a tilt is an unsaved edit")
        attach(app, "Preview tilted 15 degrees over the blurred background")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)

        app.terminate()
        let reopened = launch(reset: false)
        openTilt(reopened)
        XCTAssertEqual(reopened.staticTexts["native-editor-tilt-value"].label, "15°")
        XCTAssertTrue(reopened.buttons["native-editor-tilt-backdrop-blur"].isSelected)
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
        attach(reopened, "Reopened with the saved tilt")
    }

    func testTurnNinetyAndResetStraightenTheClipWithNothingLeftToSave() {
        let app = launch(reset: true)
        openTilt(app)
        let value = app.staticTexts["native-editor-tilt-value"]
        app.buttons["native-editor-tilt-turn-90"].tap()
        XCTAssertEqual(value.label, "90°")
        app.buttons["native-editor-tilt-turn-90"].tap()
        XCTAssertEqual(value.label, "180°")
        app.buttons["native-editor-tilt-turn-90"].tap()
        XCTAssertEqual(value.label, "-90°", "a quarter turn past 180 wraps")
        app.buttons["native-editor-tilt-reset"].tap()
        XCTAssertEqual(value.label, "0°")
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled, "Reset restores the loaded clip exactly")
    }

    func testNudgeMovesTheDialClockwiseAndCounterClockwiseByOneDegree() {
        let app = launch(reset: true)
        openTilt(app)
        let value = app.staticTexts["native-editor-tilt-value"]
        app.buttons["native-editor-tilt-nudge-left"].tap()
        XCTAssertEqual(value.label, "-1°")
        app.buttons["native-editor-tilt-nudge-right"].tap()
        app.buttons["native-editor-tilt-nudge-right"].tap()
        XCTAssertEqual(value.label, "1°")
        let dial = app.descendants(matching: .any)["native-editor-tilt-dial"].firstMatch
        XCTAssertTrue(dial.exists)
        XCTAssertEqual(dial.value as? String, "1°")
    }

    /// KRI-1139: the fourth Background option opens the editor's shared pool picker; the chosen photo shows as a
    /// thumbnail, and Reset returns to the blank background.
    func testPhotoOrVideoBackgroundIsPickedShownAndResetToBlank() {
        let app = launch(reset: true, media: true)
        openTilt(app)
        let option = app.buttons["native-editor-tilt-backdrop-media"]
        XCTAssertTrue(option.waitForExistence(timeout: 5), "the server opened clips.tilt_backdrop_media")
        XCTAssertTrue(option.isHittable)
        XCTAssertEqual(option.frame.height, app.buttons["native-editor-tilt-backdrop-blur"].frame.height, accuracy: 0.5,
                       "the same capsule target as the other options")
        for _ in 0..<10 { app.buttons["native-editor-tilt-nudge-right"].tap() }
        option.tap()

        let photo = app.buttons["native-editor-tilt-media-visual-fixture-bg-photo"]
        XCTAssertTrue(photo.waitForExistence(timeout: 5), "the item's pool photo is offered")
        XCTAssertTrue(app.buttons["native-editor-tilt-media-visual-fixture-bg-video"].exists, "and its pool video")
        XCTAssertTrue(app.buttons["native-editor-tilt-media-add"].exists)
        XCTAssertTrue(app.buttons["native-editor-tilt-media-none"].isSelected, "nothing chosen yet")
        attach(app, "Pool picker for a tilt background")
        photo.tap()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-tilt-media-thumbnail"].firstMatch.waitForExistence(timeout: 5),
                      "the chosen photo is shown")
        XCTAssertTrue(app.buttons["native-editor-tilt-backdrop-media"].isSelected)
        XCTAssertTrue(app.buttons["native-editor-tilt-media-change"].exists)
        XCTAssertEqual(app.staticTexts["native-editor-tilt-value"].label, "10°", "choosing a background keeps the angle")
        attach(app, "Tilt sheet with a photo behind the clip")

        app.buttons["native-editor-tilt-reset"].tap()
        XCTAssertEqual(app.staticTexts["native-editor-tilt-value"].label, "0°")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-tilt-media-thumbnail"].firstMatch.exists)
        XCTAssertTrue(app.buttons["native-editor-tilt-backdrop-blank"].isSelected, "Reset returns to the blank background")
    }

    func testPhotoOrVideoIsOfferedOnlyWhereTheServerListsIt() {
        let app = launch(reset: true)
        openTilt(app)
        XCTAssertTrue(app.buttons["native-editor-tilt-backdrop-blur"].exists)
        XCTAssertFalse(app.buttons["native-editor-tilt-backdrop-media"].exists)
    }

    func testTiltIsHiddenWhereTheServerDoesNotOfferIt() {
        let app = launch(reset: false, capability: false)
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        XCTAssertTrue(app.buttons["native-editor-clip-range"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-clip-tilt"].exists)
    }
}
