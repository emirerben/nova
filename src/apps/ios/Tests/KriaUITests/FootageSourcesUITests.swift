import XCTest

/// Record | Upload across the footage surfaces (Paper "3 Record + Upload"): the chat stage, video-type row, the
/// composer tray, the camera, and the review. The camera itself offers Record only: Upload lives on the chat and the
/// Add media sheet. The simulator has no camera, so the camera shows its fixture states.
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
        assertNoUploadOnTheCamera(app)
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
        self.capture(app, "S3.09a Kria camera, countdown")
        app.buttons["teleprompter-cancel-countdown"].tap()
    }

    func testScriptRowOpensTheCameraWithTheScriptSheetFirst() {
        let app = launchTalking(cameraReady: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-script"].tap()
        XCTAssertTrue(app.textViews["teleprompter-script-editor"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["teleprompter-use-script"].isEnabled, "Use script waits for words")
        XCTAssertTrue(app.buttons["teleprompter-no-script"].isHittable, "recording without a script is one tap away")
    }

    func testRefusedCameraShowsTheReasonWithoutUpload() {
        let app = launchTalking()
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.staticTexts["teleprompter-camera-error"].waitForExistence(timeout: 10))
        assertNoUploadOnTheCamera(app)
        capture(app, "S3.10 Kria camera, access off")
    }

    func testCameraAccessOffOffersOpenSettingsOnly() {
        let app = launchTalking(cameraRefusal: "camera_access")
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.staticTexts["Camera access is off"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["teleprompter-open-settings"].exists)
        assertNoUploadOnTheCamera(app)
        XCTAssertFalse(app.buttons["teleprompter-retry-camera"].exists)
        XCTAssertGreaterThanOrEqual(app.buttons["teleprompter-close"].frame.minX, 15.9, "the close disc sits 16pt in")
        capture(app, "S3.10 Kria camera, Camera access is off")
    }

    func testCameraAccessOffKeepsEveryLabelWholeAtAccessibilitySize() {
        let app = launchTalking(largeText: true, cameraRefusal: "camera_access")
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.staticTexts["Camera access is off"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["teleprompter-open-settings"].isHittable)
        assertNoUploadOnTheCamera(app)
        capture(app, "S3.10 Camera access is off at accessibility5")
    }

    func testLookControlsKeepTheirLabelsWholeAtAccessibilitySize() {
        let app = launchTalking(cameraReady: true, largeText: true, cinematic: "on", speakerBackground: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let record = app.buttons["teleprompter-record"]
        _ = record.waitForExistence(timeout: 10)
        capture(app, "S3.08 Camera at accessibility5")
        XCTAssertTrue(record.exists)
        let background = app.buttons["teleprompter-background"], cinematic = app.buttons["teleprompter-cinematic"]
        XCTAssertTrue(background.isHittable)
        XCTAssertTrue(cinematic.isHittable)
        XCTAssertGreaterThan(background.frame.width, 150, "a labelled capsule, not an icon-only circle")
        XCTAssertGreaterThan(cinematic.frame.width, 150)
        XCTAssertTrue(app.buttons["teleprompter-flip"].isHittable)
        XCTAssertTrue(record.isHittable)
    }

    func testBackgroundAndCinematicShareOneRowAboveRecordAndOffReadsAsOff() {
        let app = launchTalking(cameraReady: true, cinematic: "on", speakerBackground: true, scene: "light")
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let record = app.buttons["teleprompter-record"]
        let background = app.buttons["teleprompter-background"], cinematic = app.buttons["teleprompter-cinematic"]
        XCTAssertTrue(record.waitForExistence(timeout: 10))
        XCTAssertTrue(background.exists)
        XCTAssertTrue(cinematic.exists)
        assertNoUploadOnTheCamera(app)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] 'unedited'")).firstMatch.exists,
                       "the camera makes no promise about the preview")
        XCTAssertEqual(background.frame.midY, cinematic.frame.midY, accuracy: 2, "Background and Cinematic share one row")
        XCTAssertLessThan(background.frame.maxY, record.frame.minY, "the row sits above the record button")
        XCTAssertLessThan(record.frame.minY - cinematic.frame.maxY, 48, "and close to it, not up with the Script control")
        XCTAssertLessThan(background.frame.minX, cinematic.frame.minX)
        XCTAssertGreaterThan(background.frame.minY, app.buttons["teleprompter-script"].frame.maxY, "Script stays on top")
        // The choice persists between launches, so start from off.
        if cinematic.value as? String == "On" { cinematic.tap() }
        XCTAssertTrue(eventually(timeout: 12) { cinematic.value as? String == "Off" }, "off is named, not just dimmer glass")
        XCTAssertEqual(background.value as? String, "None")
        cinematic.tap()
        XCTAssertTrue(eventually(timeout: 12) { cinematic.value as? String == "On" })
        XCTAssertTrue(cinematic.isSelected)
    }

    func testLookControlsOffAndOnOverALightScene() { captureLookControls(scene: "light") }
    func testLookControlsOffAndOnOverADarkScene() { captureLookControls(scene: "dark") }

    private func captureLookControls(scene: String) {
        let app = launchTalking(cameraReady: true, cinematic: "on", speakerBackground: true, scene: scene)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let cinematic = app.buttons["teleprompter-cinematic"]
        XCTAssertTrue(cinematic.waitForExistence(timeout: 10))
        if cinematic.value as? String == "On" { cinematic.tap() }
        XCTAssertTrue(eventually(timeout: 12) { cinematic.value as? String == "Off" })
        capture(app, "Look controls, both off, \(scene) scene")
        cinematic.tap()
        XCTAssertTrue(eventually(timeout: 12) { cinematic.value as? String == "On" })
        capture(app, "Look controls, Cinematic on, \(scene) scene")
        app.buttons["teleprompter-background"].tap()
        app.buttons["teleprompter-background-choose"].tap()
        XCTAssertTrue(eventually { app.buttons["teleprompter-background"].value as? String == "Photo" })
        capture(app, "Look controls, both on, \(scene) scene")
        cinematic.tap()
        XCTAssertTrue(eventually(timeout: 12) { cinematic.value as? String == "Off" })
        capture(app, "Look controls, Background on and Cinematic off, \(scene) scene")
    }

    /// A screen recording beside a talking take used to earn a "Screen recordings go in Overlays" card. The fixture still
    /// advertises the removed capability, as an older server would: the card must not come back.
    func testScreenRecordingInATalkingProjectGetsNoPlacementHint() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_SCREEN_RECORDING"] = "1"
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
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
        let upload = app.buttons["choose-videos"]
        if upload.waitForExistence(timeout: 10) { upload.tap() }
        else {
            XCTAssertTrue(app.buttons["Attach footage"].waitForExistence(timeout: 5), "no way into Add media with footage attached")
            app.buttons["Attach footage"].tap()
        }
        if app.buttons["upload-source-photos"].waitForExistence(timeout: 5) {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)).tap()
            XCTAssertTrue(app.buttons["upload-source-photos"].waitForNonExistence(timeout: 5))
        }
        let footage = app.scrollViews["attachment-step-footage"]
        XCTAssertTrue(footage.waitForExistence(timeout: 5), "Add media did not open on Footage")
        let recording = footage.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH 'Screen Recording'")).firstMatch
        XCTAssertTrue(recording.waitForExistence(timeout: 15), "the screen recording is attached to the talking project")
        XCTAssertTrue(recording.label.contains("Video · Ready"), "it keeps the plain caption")
        XCTAssertFalse(app.descendants(matching: .any)["footage-place-hint"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] 'go in Overlays'")).firstMatch.exists)
        XCTAssertFalse(app.buttons["Move to Overlays"].exists)
        capture(app, "Screen recording in a talking project, no hint")
    }

    private func assertNoUploadOnTheCamera(_ app: XCUIApplication) {
        XCTAssertFalse(app.buttons["camera-upload"].exists, "the camera has no Upload control")
        XCTAssertFalse(app.buttons["review-upload"].exists)
        XCTAssertFalse(app.buttons["Upload"].exists)
        XCTAssertFalse(app.buttons["Upload instead"].exists)
    }

    func testScriptAndSpeedSheetsReachEveryControlAtAccessibilitySize() {
        let app = launchTalking(cameraReady: true, largeText: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let script = app.buttons["teleprompter-script"]
        XCTAssertTrue(script.waitForExistence(timeout: 10))
        script.tap()
        let editor = app.textViews["teleprompter-script-editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        capture(app, "S3.11 Script sheet at accessibility5")
        editor.tap()
        editor.typeText("Read this opening at a steady pace.")
        let noScript = app.buttons["teleprompter-no-script"]
        for _ in 0..<4 where !noScript.isHittable { app.staticTexts["Script"].firstMatch.swipeUp() }
        XCTAssertTrue(noScript.isHittable, "Record without a script stays reachable by scrolling the sheet")
        capture(app, "S3.11 Script sheet at accessibility5, scrolled")
        app.buttons["teleprompter-use-script"].tap()
        let speed = app.buttons["teleprompter-speed"]
        XCTAssertTrue(speed.waitForExistence(timeout: 5))
        speed.tap()
        XCTAssertTrue(app.sliders["teleprompter-pace"].waitForExistence(timeout: 5))
        capture(app, "Speed sheet at accessibility5")
        let start = app.buttons["Start of script"]
        if !start.isHittable { app.sliders["teleprompter-pace"].swipeUp() }
        XCTAssertTrue(start.isHittable, "Start of script is reachable by scrolling the sheet")
    }

    func testCinematicCapsuleShowsWhatTheCameraRecordsNotWhatWasAskedFor() {
        let app = launchTalking(cameraReady: true, cinematic: "fallback")
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let cinematic = app.buttons["teleprompter-cinematic"]
        XCTAssertTrue(cinematic.waitForExistence(timeout: 10))
        let note = app.staticTexts["teleprompter-cinematic-note"]
        if cinematic.isSelected || note.exists { cinematic.tap() }
        XCTAssertTrue(eventually { !cinematic.isSelected && !note.exists }, "off means off: no note, not selected")
        cinematic.tap()
        XCTAssertTrue(note.waitForExistence(timeout: 5), "asked for, but the camera fell back: the camera says so")
        XCTAssertTrue(note.label.contains("standard mode"))
        XCTAssertFalse(cinematic.isSelected, "the capsule does not claim Cinematic while the take is standard")
        capture(app, "Cinematic asked for, camera fell back to standard")
        cinematic.tap()
        XCTAssertTrue(eventually { !note.exists })
    }

    func testCinematicCapsuleIsSelectedWhenTheCameraRecordsCinematic() {
        let app = launchTalking(cameraReady: true, cinematic: "on")
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        let cinematic = app.buttons["teleprompter-cinematic"]
        XCTAssertTrue(cinematic.waitForExistence(timeout: 10))
        if !cinematic.isSelected { cinematic.tap() }
        XCTAssertTrue(eventually { cinematic.isSelected })
        XCTAssertFalse(app.staticTexts["teleprompter-cinematic-note"].exists)
        capture(app, "Cinematic on")
        cinematic.tap()
        XCTAssertTrue(eventually { !cinematic.isSelected })
    }

    func testReviewTakeKeepsEveryLabelWholeAtAccessibilitySize() {
        let app = launchTalking(review: true, largeText: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 10))
        let retake = app.buttons["teleprompter-retake"]
        XCTAssertTrue(retake.exists)
        XCTAssertGreaterThan(retake.frame.width, 200, "Record again is a full-width row, not a clipped half")
        assertNoUploadOnTheCamera(app)
        capture(app, "S3.12 Review take at accessibility5")
    }

    func testReviewPlayerOffersAnAdjustableScrubberAndPauseToVoiceOver() {
        let app = launchTalking(review: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["review-play"].exists)
        let scrubber = app.sliders["review-scrubber"]
        XCTAssertTrue(scrubber.waitForExistence(timeout: 5), "VoiceOver gets an adjustable position, not a drag-only strip")
        XCTAssertTrue((scrubber.value as? String)?.contains(" of ") == true, "the position is spoken as time, not a percentage")
        app.buttons["review-play"].tap()
        XCTAssertTrue(app.buttons["review-pause"].waitForExistence(timeout: 5), "playing offers Pause")
    }

    func testReviewTakeIsOfferedWithRecordAgainAndDiscard() {
        let app = launchTalking(review: true)
        app.buttons["chat-attach"].tap()
        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["teleprompter-retake"].exists)
        assertNoUploadOnTheCamera(app)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] 'unedited'")).firstMatch.exists)
        XCTAssertTrue(app.buttons["teleprompter-discard"].exists)
        XCTAssertTrue(app.buttons["review-play"].exists)
        capture(app, "S3.12 Review take")
    }

    func testAddMediaSheetLeadsWithRecordAndUploadAndOptionalScript() {
        let app = launchTalking()
        // The footage stage's Upload opens Add media with the "Upload from" list over it; close the list to see the sheet.
        app.buttons["choose-videos"].tap()
        XCTAssertTrue(app.buttons["upload-source-photos"].waitForExistence(timeout: 5))
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)).tap()
        XCTAssertTrue(app.buttons["upload-source-photos"].waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.buttons["source-record"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["source-record"].frame.width, app.buttons["source-upload"].frame.width, accuracy: 1)
        XCTAssertTrue(app.buttons["teleprompter-open"].exists, "the script is an optional row, not a requirement")
        capture(app, "S3.06 Add media, Footage step")
        app.buttons["source-upload"].tap()
        XCTAssertTrue(app.buttons["upload-source-photos"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["upload-source-files"].exists)
        capture(app, "Upload from sheet over Add media")
    }

    /// `cameraRefusal` is the camera fixture's refusal: `camera_access`, `microphone_access`, `restricted`, or `1`
    /// for hardware that is not there. `cinematic` is `on` or `fallback` and needs `cameraReady`.
    private func launchTalking(review: Bool = false, cameraReady: Bool = false,
                               largeText: Bool = false, cameraRefusal: String = "1", cinematic: String? = nil,
                               speakerBackground: Bool = false, scene: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        if speakerBackground {
            app.launchArguments.append("-ui-testing-speaker-background")
            app.launchEnvironment["KRIA_TELEPROMPTER_BACKGROUND_FIXTURE"] = "1"
        }
        if let scene { app.launchEnvironment["KRIA_TELEPROMPTER_SCENE_FIXTURE"] = scene }
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        if cameraReady { app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_READY"] = "1" }
        else { app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = cameraRefusal }
        if let cinematic { app.launchEnvironment["KRIA_TELEPROMPTER_CINEMATIC_FIXTURE"] = cinematic }
        if largeText {
            app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
            app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        }
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
