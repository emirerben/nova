import XCTest

/// Script entry and failure recovery exercise the real native screen, store,
/// and navigation. Simulator camera unavailability is deliberately truthful.
@MainActor final class TeleprompterUITests: XCTestCase {
    func testWindowBackgroundAndDimensionsSurviveReopening() {
        let app = launchTalking()
        openScriptCamera(app)
        let window = windowElement(app)
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        XCTAssertTrue((window.value as? String)?.localizedCaseInsensitiveContains("transparent") == true)
        let initial = window.frame

        openWindowSettings(app)
        app.buttons["teleprompter-background-glass"].tap()
        app.sliders["teleprompter-window-width"].adjust(toNormalizedSliderPosition: 0.35)
        app.sliders["teleprompter-window-height"].adjust(toNormalizedSliderPosition: 0.6)
        let preferredHeight = app.sliders["teleprompter-window-height"].value as? String
        app.buttons["Move right"].tap()
        app.buttons["teleprompter-window-settings-close"].tap()
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        let saved = window.frame
        XCTAssertLessThan(saved.width, initial.width - 10)
        XCTAssertGreaterThan(saved.minX, initial.minX)
        XCTAssertTrue((window.value as? String)?.localizedCaseInsensitiveContains("glass") == true)
        assertWindowStaysClearOfRecord(window, app: app)
        capture(app, "Movable glass script window")

        app.buttons["Close camera"].tap()
        app.buttons["teleprompter-close"].tap()
        app.buttons["teleprompter-open"].tap()
        app.buttons["teleprompter-open-camera"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        XCTAssertTrue((window.value as? String)?.localizedCaseInsensitiveContains("glass") == true)
        XCTAssertEqual(window.frame.width, saved.width, accuracy: 2)
        XCTAssertEqual(window.frame.minX, saved.minX, accuracy: 2)

        openWindowSettings(app)
        XCTAssertEqual(app.sliders["teleprompter-window-height"].value as? String, preferredHeight)
        app.buttons["teleprompter-background-solid"].tap()
        app.buttons["teleprompter-window-reset"].tap()
        app.buttons["teleprompter-window-settings-close"].tap()
        XCTAssertTrue((window.value as? String)?.localizedCaseInsensitiveContains("solid") == true)
        XCTAssertEqual(window.frame.width, initial.width, accuracy: 2)
        XCTAssertEqual(window.frame.minX, initial.minX, accuracy: 2)
        assertWindowStaysClearOfRecord(window, app: app)
        capture(app, "Solid script window reset to default placement")
    }

    func testWindowHandlesDoNotPauseOrScrollTheScript() {
        let app = launchTalking()
        openScriptCamera(app)
        let window = windowElement(app)
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        let original = window.frame
        scrollOptions(app, to: app.buttons["teleprompter-pause-text"])
        app.buttons["teleprompter-pause-text"].tap()
        XCTAssertTrue(app.buttons["Pause rehearsal"].exists)

        let resize = app.buttons["teleprompter-window-resize"]
        XCTAssertTrue(resize.isHittable)
        let resizeStart = resize.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        resizeStart.press(forDuration: 0.15, thenDragTo: resizeStart.withOffset(CGVector(dx: -80, dy: -20)))
        XCTAssertEqual(window.frame.width, original.width - 80, accuracy: 5,
                       "Resizing follows finger travel in screen coordinates.")
        XCTAssertTrue(app.buttons["Pause rehearsal"].exists, "Resizing must not send the script body its pause/scroll gesture.")

        let shrunk = window.frame
        let move = app.buttons["teleprompter-window-move"]
        XCTAssertTrue(move.isHittable)
        let moveStart = move.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        moveStart.press(forDuration: 0.15, thenDragTo: moveStart.withOffset(CGVector(dx: 35, dy: 25)))
        XCTAssertEqual(window.frame.minX, shrunk.minX + 35, accuracy: 5,
                       "Moving follows finger travel even while the grip itself moves.")
        XCTAssertTrue(app.buttons["Pause rehearsal"].exists, "Moving must keep the current rehearsal running.")
        assertWindowStaysClearOfRecord(window, app: app)
        capture(app, "Transparent window moved and resized")

        // SwiftUI exposes the identifier on the first laid-out line, which can already
        // have scrolled above the clip. Drag the visible reading area below the grip.
        let textStart = window.coordinate(withNormalizedOffset: CGVector(dx: 0.4, dy: 0.7))
        textStart.press(forDuration: 0.15, thenDragTo: textStart.withOffset(CGVector(dx: 0, dy: -25)))
        XCTAssertTrue(app.buttons["Rehearse"].exists, "Dragging text still pauses and repositions the script.")
    }

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

    private func openScriptCamera(_ app: XCUIApplication) {
        app.buttons["teleprompter-open"].tap()
        let field = app.textViews["teleprompter-script-editor"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("This is a longer script for reading at a steady pace. We can move the reading window without losing our place. We can resize the window while keeping the camera controls clear. The background can be transparent, glass, or solid. These settings should return when this script is reopened.")
        app.buttons["teleprompter-open-camera"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
    }

    private func windowElement(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: "teleprompter-window").firstMatch
    }

    private func openWindowSettings(_ app: XCUIApplication) {
        let settings = app.buttons["teleprompter-window-settings"]
        scrollOptions(app, to: settings)
        settings.tap()
        XCTAssertTrue(app.buttons["teleprompter-window-settings-close"].waitForExistence(timeout: 5))
    }

    private func assertWindowStaysClearOfRecord(_ window: XCUIElement, app: XCUIApplication) {
        XCTAssertGreaterThan(window.frame.width, 100)
        XCTAssertGreaterThan(window.frame.height, 40)
        XCTAssertTrue(app.frame.contains(window.frame), "The entire reading window must fit on screen.")
        let record = app.buttons["teleprompter-record"]
        XCTAssertTrue(record.isHittable)
        XCTAssertFalse(window.frame.intersects(record.frame), "Moving/resizing must preserve the Record/Stop touch area.")
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
