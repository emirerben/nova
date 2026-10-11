import XCTest

/// Script entry and failure recovery exercise the real native screen, store,
/// and navigation. Simulator camera unavailability is deliberately truthful.
@MainActor final class TeleprompterUITests: XCTestCase {
    func testScriptIsSavedAndCameraFailureKeepsItEditable() {
        let app = launchTalking()
        app.buttons["teleprompter-open"].tap()
        let field = app.textViews["teleprompter-script-editor"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["teleprompter-open-camera"].isEnabled)
        field.tap()
        field.typeText("Today I want to share a simple idea.\nTake your time and enjoy the story.")
        app.buttons["teleprompter-open-camera"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["teleprompter-record"].isEnabled)
        capture(app, "Camera unavailable preserves script")
        app.buttons["teleprompter-edit-script"].tap()
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertTrue((field.value as? String)?.contains("Take your time") == true)
        app.buttons["teleprompter-close"].tap()
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertTrue((field.value as? String)?.contains("Today I want") == true)
        capture(app, "Script restored on reopen")
    }

    func testScriptControlsAreReachableAtAccessibilitySize() {
        let app = launchTalking(largeText: true)
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.textViews["teleprompter-script-editor"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["teleprompter-close"].isHittable)
        XCTAssertTrue(app.buttons["teleprompter-open-camera"].isHittable)
        capture(app, "Script at accessibility size")
        let field = app.textViews["teleprompter-script-editor"]
        field.tap()
        field.typeText("Read at a comfortable pace.")
        app.buttons["teleprompter-open-camera"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Close camera"].isHittable)
        let record = app.buttons["teleprompter-record"]
        XCTAssertTrue(record.isHittable)
        XCTAssertFalse(record.isEnabled)
        XCTAssertTrue(app.buttons["Next line"].exists)
        scrollOptions(app, to: app.buttons["Use steady scrolling"])
        app.buttons["Use steady scrolling"].tap()
        scrollOptions(app, to: app.buttons["teleprompter-pause-text"])
        app.buttons["teleprompter-pause-text"].tap()
        XCTAssertTrue(app.buttons["Pause rehearsal"].exists)
        app.buttons["teleprompter-pause-text"].tap()
        XCTAssertTrue(app.buttons["Rehearse"].exists)
        capture(app, "Camera controls at accessibility size")
    }

    func testPreviousTakeAndFailedIntakeKeepTheRecordingRecoverable() {
        let app = launchTalking(review: true)
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.buttons["Previous take"].waitForExistence(timeout: 10))
        app.buttons["Previous take"].tap()
        XCTAssertFalse(app.buttons["Previous take"].exists)
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled)
        app.buttons["teleprompter-use-take"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-review-error"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled)
        XCTAssertTrue(app.buttons["teleprompter-retake"].isEnabled)
        capture(app, "Failed native proxy intake retains take")
        app.buttons["teleprompter-close"].tap()
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled)
        capture(app, "Previous take restored after failed intake")
    }

    private func launchTalking(largeText: Bool = false, review: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
        if review { app.launchEnvironment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] = "1" }
        if largeText {
            app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
            app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        }
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        let talking = app.buttons["format-talking_to_camera"]
        XCTAssertTrue(talking.waitForExistence(timeout: 10))
        if !talking.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        talking.tap()
        let attach = app.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 5))
        attach.tap()
        XCTAssertTrue(app.buttons["teleprompter-open"].waitForExistence(timeout: 5))
        return app
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func scrollOptions(_ app: XCUIApplication, to button: XCUIElement) {
        let options = app.scrollViews["teleprompter-capture-options"]
        options.swipeDown()
        for _ in 0..<6 {
            if button.isHittable { return }
            options.swipeUp()
        }
        XCTAssertTrue(button.isHittable)
    }
}
