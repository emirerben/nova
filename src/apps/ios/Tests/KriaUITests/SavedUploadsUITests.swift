import XCTest

final class SavedUploadsUITests: XCTestCase {
    func testReuseAcrossTwoPostsWithoutAnotherUpload() {
        let app = launch()
        app.buttons["fixture-open-saved"].tap()
        let add = app.buttons["saved-upload-add-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        let pickerShot = XCTAttachment(screenshot: app.screenshot())
        pickerShot.name = "Saved uploads picker"
        pickerShot.lifetime = .keepAlways
        self.add(pickerShot)
        add.tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 8))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 5))
        app.buttons["fixture-second-post"].tap()
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(add.waitForExistence(timeout: 8))
        add.tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 8))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Post 2: 1 saved attachment"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].exists)
        XCTAssertTrue(app.staticTexts["Reuse used no upload requests"].exists)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Saved upload reused in independent posts"
        screenshot.lifetime = .keepAlways
        self.add(screenshot)
    }
    func testPhotoReuseAcrossPosts() {
        let app = launch()
        app.buttons["fixture-photo-posts"].tap()
        for post in 1...2 {
            if post == 2 { app.buttons["fixture-second-post"].tap() }
            app.buttons["fixture-open-saved"].tap()
            let add = app.buttons["saved-upload-add-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"]
            XCTAssertTrue(add.waitForExistence(timeout: 8))
            add.tap()
            XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 8))
            app.buttons["Done"].tap()
            XCTAssertTrue(app.staticTexts["Post \(post): 1 saved attachment"].waitForExistence(timeout: 5))
        }
    }
    func testSearchPaginationAndRetry() {
        let app = launch(failFirst: true)
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(app.staticTexts["saved-uploads-error"].waitForExistence(timeout: 8))
        app.buttons["saved-uploads-retry"].tap()
        XCTAssertTrue(app.buttons["saved-uploads-more"].waitForExistence(timeout: 8))
        app.buttons["saved-uploads-more"].tap()
        XCTAssertTrue(app.staticTexts["Evening song.mp3"].waitForExistence(timeout: 8))
        let search = app.searchFields.firstMatch
        search.tap(); search.typeText("missing")
        XCTAssertTrue(app.otherElements["saved-uploads-empty"].waitForExistence(timeout: 8) || app.staticTexts["No matching uploads"].waitForExistence(timeout: 3))
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Saved uploads search has an explicit empty state"
        screenshot.lifetime = .keepAlways
        self.add(screenshot)
    }
    // KRI-822 F1/F8: an owned device-only video (proxy in the cloud, original on this iPhone)
    // says "Original on this iPhone", adds to Visuals by uploading that verified original in full,
    // and stays added when the sheet is reopened.
    func testDeviceOnlyVideoAddsToVisualsFromLocalOriginal() {
        let app = launch(mode: "device-only-visual")
        app.buttons["fixture-open-saved"].tap()
        let add = app.buttons["saved-upload-add-\(Self.horse)"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["saved-upload-availability-\(Self.horse)"].label, "Original on this iPhone")
        XCTAssertTrue(add.isEnabled, "Original on this iPhone must be addable to Visuals")
        attach(app, "Visuals row: Original on this iPhone + Add enabled")
        add.tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 15))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
        attach(app, "Added to Visuals")
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 10))
        attach(app, "Reopened")
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
    }
    // KRI-822 F2: with no local original, Add asks for it in Files and the relink finishes the
    // same Visuals add (it does not re-upload another proxy).
    func testRelinkedOriginalFinishesTheVisualsAdd() {
        let app = launch(mode: "device-only-relink")
        app.buttons["fixture-open-saved"].tap()
        let add = app.buttons["saved-upload-add-\(Self.horse)"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["saved-upload-availability-\(Self.horse)"].label, "Full-quality original needed on this iPhone")
        XCTAssertTrue(add.isEnabled)
        add.tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 15))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
        attach(app, "Relinked original added to Visuals")
    }
    // KRI-822 F13: photo-only phone Visuals keep videos disabled and say where they go instead.
    func testPhotoOnlyVisualsSendVideosToFootage() {
        let app = launch(mode: "images-only")
        app.buttons["fixture-open-saved"].tap()
        let add = app.buttons["saved-upload-add-\(Self.horse)"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["saved-upload-availability-\(Self.horse)"].label, "Add videos to this post in Footage.")
        XCTAssertFalse(add.isEnabled)
    }
    // KRI-822 F9: an asset with both sources still attaches its proxy to phone Footage, with no upload.
    func testPhoneFootageAttachesProxyWithoutUploadingTheOriginal() {
        let app = launch(mode: "footage-both")
        app.buttons["fixture-open-saved"].tap()
        let add = app.buttons["saved-upload-add-\(Self.horse)"]
        XCTAssertTrue(add.waitForExistence(timeout: 10))
        add.tap()
        XCTAssertTrue(app.staticTexts["Added to this post"].waitForExistence(timeout: 15))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Uploaded original 0x, Footage source DDDDDDDD"].waitForExistence(timeout: 5))
    }
    private static let horse = "DDDDDDDD-0000-4000-8000-000000000822"
    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
    private func launch(failFirst: Bool = false, mode: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-saved-uploads"]
        if failFirst { app.launchEnvironment["KRIA_SAVED_UPLOADS_FAIL_FIRST"] = "1" }
        if let mode { app.launchEnvironment["KRIA_SAVED_UPLOADS_MODE"] = mode }
        app.launch()
        XCTAssertTrue(app.buttons["fixture-open-saved"].waitForExistence(timeout: 10))
        return app
    }
}
