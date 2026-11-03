import XCTest

/// Record | Upload across the footage surfaces (Paper "3 Record + Upload"): the chat stage, video-type row, the
/// composer tray, the camera, and the review. The simulator has no camera, so the camera shows its fixture states.
@MainActor final class FootageSourcesUITests: XCTestCase {
    func testChatLeadsWithRecordAndUploadAndKeepsTheVideoTypeFirst() {
        let app = launchTalking()
        let typeRow = app.descendants(matching: .any)["video-type-row"].firstMatch
        XCTAssertTrue(typeRow.waitForExistence(timeout: 10))
        let record = app.buttons["record-footage"], upload = app.buttons["choose-videos"]
        XCTAssertTrue(record.waitForExistence(timeout: 5))
        XCTAssertTrue(upload.exists)
        XCTAssertEqual(record.frame.size.width, upload.frame.size.width, accuracy: 1, "Record and Upload are the same size")
        XCTAssertEqual(record.frame.size.height, upload.frame.size.height, accuracy: 1)
        XCTAssertLessThan(typeRow.frame.minY, record.frame.minY, "The video type reads first in the transcript")
        XCTAssertGreaterThanOrEqual(app.buttons["change-video-type"].frame.height, 44)
        capture(app, "S3.02 Chat, Talk to camera, empty")

        app.buttons["change-video-type"].tap()
        XCTAssertTrue(app.buttons["reselect-format-montage"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["reselect-format-talkingToCamera"].isSelected)
        capture(app, "S3.02b Chat, change video type")
        app.buttons["reselect-format-talkingToCamera"].tap()
        XCTAssertTrue(app.buttons["record-footage"].waitForExistence(timeout: 5))
    }

    func testComposerPlusOpensTheTrayAndUploadAsksWhere() {
        let app = launchTalking()
        let plus = app.buttons["chat-attach"]
        XCTAssertTrue(plus.waitForExistence(timeout: 10))
        plus.tap()
        XCTAssertTrue(app.buttons["attach-record"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["attach-upload"].exists)
        XCTAssertTrue(app.buttons["attach-script"].exists, "Talk to camera offers the optional script")
        XCTAssertFalse(app.buttons["upload-source-photos"].exists, "Photos and Files only appear after Upload")
        XCTAssertEqual(plus.label, "Close attach menu", "the + turns into an x")
        capture(app, "S3.05c Composer + menu, open")
        app.buttons["attach-upload"].tap()
        XCTAssertTrue(app.buttons["upload-source-photos"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["upload-source-files"].exists)
        capture(app, "S3.05d Upload sources, after tapping Upload")
        app.buttons["attach-tray-back"].tap()
        XCTAssertTrue(app.buttons["attach-record"].waitForExistence(timeout: 5))
    }

    func testRecordOpensTheCameraWithoutAScript() {
        let app = launchTalking(cameraReady: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let capture = app.buttons["teleprompter-record"]
        XCTAssertTrue(capture.waitForExistence(timeout: 10), "Record opens the camera with no script written")
        XCTAssertTrue(app.buttons["camera-upload"].exists, "a labelled Upload capsule sits beside the capture button")
        XCTAssertGreaterThanOrEqual(app.buttons["camera-upload"].frame.height, 43.9)
        XCTAssertTrue(app.buttons["teleprompter-flip"].exists)
        XCTAssertTrue(app.buttons["teleprompter-script"].exists)
        XCTAssertTrue(eventually { capture.isEnabled })
        self.capture(app, "S3.08 Kria camera, ready")

        app.buttons["teleprompter-script"].tap()
        XCTAssertTrue(app.textViews["teleprompter-script-editor"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["teleprompter-use-script"].isEnabled)
        self.capture(app, "S3.11 Script sheet, optional")
        app.buttons["teleprompter-no-script"].tap()
        XCTAssertTrue(capture.waitForExistence(timeout: 5))

        capture.tap()
        XCTAssertTrue(app.buttons["teleprompter-cancel-countdown"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["camera-upload"].exists, "Upload hides during the countdown")
        self.capture(app, "S3.09a Kria camera, countdown")
        app.buttons["teleprompter-cancel-countdown"].tap()
    }

    func testScriptRowOpensTheCameraWithTheScriptSheetFirst() {
        let app = launchTalking(cameraReady: true, legacyAttach: true)
        app.buttons["Attach footage"].tap()
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.textViews["teleprompter-script-editor"].waitForExistence(timeout: 15))
    }

    func testRefusedCameraOffersSettingsAndUpload() {
        let app = launchTalking()
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["camera-upload"].exists, "a camera that cannot open still leads to Upload")
        capture(app, "S3.10 Kria camera, access off")
    }

    func testReviewTakeIsOfferedWithRecordAgainAndUploadInstead() {
        let app = launchTalking(review: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["teleprompter-retake"].exists)
        XCTAssertTrue(app.buttons["review-upload"].exists)
        XCTAssertTrue(app.buttons["teleprompter-discard"].exists)
        XCTAssertTrue(app.buttons["review-play"].exists)
        capture(app, "S3.12 Review take")
    }

    func testAddMediaSheetLeadsWithRecordAndUploadAndOptionalScript() {
        let app = launchTalking(legacyAttach: true)
        app.buttons["Attach footage"].tap()
        XCTAssertTrue(app.buttons["source-record"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["source-record"].frame.width, app.buttons["source-upload"].frame.width, accuracy: 1)
        XCTAssertTrue(app.buttons["teleprompter-open"].exists, "the script is an optional row, not a requirement")
        capture(app, "S3.06 Add media, Footage step")
        app.buttons["source-upload"].tap()
        XCTAssertTrue(app.buttons["upload-source-photos"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["upload-source-files"].exists)
        capture(app, "Upload from sheet over Add media")
    }

    private func launchTalking(review: Bool = false, cameraReady: Bool = false, legacyAttach: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = legacyAttach ? ["-ui-testing-chat", "-ui-testing-legacy-attach"] : ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        if cameraReady { app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_READY"] = "1" }
        else { app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1" }
        if review { app.launchEnvironment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] = "1" }
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
        return app
    }

    private func eventually(timeout: TimeInterval = 5, _ condition: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end { if condition() { return true }; Thread.sleep(forTimeInterval: 0.2) }
        return condition()
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
