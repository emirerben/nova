import XCTest

/// Item 24: a screen recording that sits in Footage next to a talking take gets one notice, "Move to Overlays".
/// The fixture server (`KRIA_CHAT_PLACE_HINT_FIXTURE`) attaches the footage and really takes the re-upload.
///
/// Rows are looked up inside their own step page: the paged sheet can keep the neighbouring page in the tree, so an
/// app-wide query would find a moved recording on the Overlays page and read it as still in Footage. A row's name and
/// status line are one accessibility element ("Talking take.mov, Video · Ready"), hence the prefix match.
@MainActor final class UploadPlaceHintUITests: XCTestCase {
    private let recordingName = "Screen Recording 2026-11-04 at 10.12.33.mov"
    private let takeName = "Talking take.mov"

    func testLandscapeRecordingNextToTalkingTakeShowsHintAndMovesToOverlays() {
        let app = launchAddMedia(fixture: "recording", flagOn: true)
        let footage = step(app, "footage")
        waitForRows(footage, [takeName, recordingName])
        let hint = footage.descendants(matching: .any)["footage-place-hint"].firstMatch
        XCTAssertTrue(hint.waitForExistence(timeout: 5), "the hint is missing next to a screen recording")
        XCTAssertTrue(row(footage, recordingName).label.contains("Screen recording · Ready"), "the flagged row says which clip is meant")
        XCTAssertTrue(row(footage, takeName).label.contains("Video · Ready"), "the talking take keeps its usual caption")
        attach(app, "Upload place hint")
        let move = footage.buttons["Move to Overlays"]
        XCTAssertTrue(move.waitForExistence(timeout: 3))
        XCTAssertLessThan(move.frame.maxY, app.buttons["attachment-next"].frame.minY,
                          "the hint is seen without scrolling, not under the clip rows behind Next")
        move.tap()
        XCTAssertTrue(hint.waitForNonExistence(timeout: 15), "the hint stays after Move to Overlays")
        XCTAssertTrue(row(footage, recordingName).waitForNonExistence(timeout: 15), "the recording is still in Footage")
        XCTAssertTrue(row(footage, takeName).exists, "the talking take must stay in Footage")
        app.buttons["attachment-next"].tap()
        let overlays = step(app, "overlays")
        XCTAssertTrue(overlays.waitForExistence(timeout: 5), "Footage is followed by the Overlays step")
        XCTAssertTrue(row(overlays, recordingName).waitForExistence(timeout: 20), "the recording is not in the Overlays step")
        attach(app, "After move")
    }

    func testPortraitScreenRecordingByNameShowsHint() {
        let app = launchAddMedia(fixture: "portrait-recording", flagOn: true)
        let footage = step(app, "footage")
        waitForRows(footage, [takeName, "RPReplay_Final1730000000.mp4"])
        XCTAssertTrue(footage.descendants(matching: .any)["footage-place-hint"].firstMatch.waitForExistence(timeout: 5),
                      "a portrait RPReplay recording is detected by its name")
    }

    func testSingleTalkingTakeShowsNoHint() {
        let app = launchAddMedia(fixture: "single", flagOn: true)
        let footage = step(app, "footage")
        waitForRows(footage, [takeName])
        XCTAssertFalse(app.descendants(matching: .any)["footage-place-hint"].firstMatch.waitForExistence(timeout: 3))
    }

    func testSeveralTalkingTakesShowNoHint() {
        let app = launchAddMedia(fixture: "takes", flagOn: true)
        let footage = step(app, "footage")
        waitForRows(footage, [takeName, "Talking take 2.mov"])
        XCTAssertFalse(app.descendants(matching: .any)["footage-place-hint"].firstMatch.waitForExistence(timeout: 3))
    }

    func testSwitchOffLeavesFootageUnchanged() {
        let app = launchAddMedia(fixture: "recording", flagOn: false)
        let footage = step(app, "footage")
        waitForRows(footage, [takeName, recordingName])
        XCTAssertFalse(app.descendants(matching: .any)["footage-place-hint"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["Move to Overlays"].exists)
        XCTAssertTrue(row(footage, recordingName).label.contains("Video · Ready"), "the recording keeps today's caption")
        XCTAssertTrue(row(footage, takeName).label.contains("Video · Ready"))
        XCTAssertEqual(footage.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Screen recording")).count, 0)
    }

    private func step(_ app: XCUIApplication, _ name: String) -> XCUIElement { app.scrollViews["attachment-step-\(name)"] }

    /// The row whose combined label starts with `name` (the Remove button reads "Remove <name>", so it never matches).
    private func row(_ container: XCUIElement, _ name: String) -> XCUIElement {
        container.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", name)).firstMatch
    }

    private func waitForRows(_ container: XCUIElement, _ names: [String]) {
        for name in names {
            XCTAssertTrue(row(container, name).waitForExistence(timeout: 15), "\(name) is not in the Footage step")
        }
    }

    /// Talking project with footage already attached, Add media open on its Footage step.
    private func launchAddMedia(fixture: String, flagOn: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
        app.launchEnvironment["KRIA_CHAT_PLACE_HINT_FIXTURE"] = fixture
        if flagOn { app.launchEnvironment["KRIA_UPLOAD_PLACE_HINT"] = "1" }
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
        // The footage stage's Upload opens Add media with the "Upload from" list over it; close the list to reach the steps.
        let upload = app.buttons["choose-videos"]
        if upload.waitForExistence(timeout: 10) {
            upload.tap()
        } else {
            XCTAssertTrue(app.buttons["Attach footage"].waitForExistence(timeout: 5), "no way into Add media with footage attached")
            app.buttons["Attach footage"].tap()
        }
        if app.buttons["upload-source-photos"].waitForExistence(timeout: 5) {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)).tap()
            XCTAssertTrue(app.buttons["upload-source-photos"].waitForNonExistence(timeout: 5))
        }
        XCTAssertTrue(step(app, "footage").waitForExistence(timeout: 5), "Add media did not open on Footage")
        return app
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
