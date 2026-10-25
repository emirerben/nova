import XCTest

/// Saved uploads is the Photos gallery's grid: pick tiles (tap or slide), then Done adds them in pick order.
final class SavedUploadsUITests: XCTestCase {
    private static let morning = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    private static let evening = "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"
    private static let horse = "DDDDDDDD-0000-4000-8000-000000000822"

    func testReuseAcrossTwoPostsWithoutAnotherUpload() {
        let app = launch()
        app.buttons["fixture-open-saved"].tap()
        let tile = self.tile(app, Self.morning)
        XCTAssertTrue(tile.waitForExistence(timeout: 10))
        attach(app, "Saved uploads grid")
        tile.tap()
        XCTAssertEqual(value(tile).hasPrefix("Selected, 1"), true)
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 8))
        app.buttons["fixture-second-post"].tap()
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(tile.waitForExistence(timeout: 8))
        tile.tap()
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 2: 1 saved attachment"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].exists)
        XCTAssertTrue(app.staticTexts["Reuse used no upload requests"].exists)
        attach(app, "Saved upload reused in independent posts")
    }
    // Two picks are numbered in the order they were tapped, and Done attaches both.
    func testSelectTwoThenDoneAddsBoth() {
        let app = launch()
        app.buttons["fixture-open-saved"].tap()
        let morning = tile(app, Self.morning), evening = tile(app, Self.evening)
        // The second page loads by itself as the grid reaches its end.
        XCTAssertTrue(evening.waitForExistence(timeout: 10))
        evening.tap(); morning.tap()
        XCTAssertTrue(value(evening).hasPrefix("Selected, 1"))
        XCTAssertTrue(value(morning).hasPrefix("Selected, 2"))
        XCTAssertTrue(app.staticTexts["saved-uploads-count"].label.hasPrefix("2 of 2 selected"))
        attach(app, "Two saved uploads picked")
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 2 saved attachment"].waitForExistence(timeout: 10))
        // Reopened: both are now part of the post, ticked and not pickable again.
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(morning.waitForExistence(timeout: 8))
        XCTAssertTrue(value(morning).hasPrefix("In this post"))
        morning.tap()
        XCTAssertEqual(app.staticTexts["saved-uploads-count"].label, "Already in this post.")
    }
    func testPhotoReuseAcrossPosts() {
        let app = launch()
        app.buttons["fixture-photo-posts"].tap()
        for post in 1...2 {
            if post == 2 { app.buttons["fixture-second-post"].tap() }
            app.buttons["fixture-open-saved"].tap()
            let tile = self.tile(app, Self.morning)
            XCTAssertTrue(tile.waitForExistence(timeout: 8))
            tile.tap()
            app.buttons["saved-uploads-done"].tap()
            XCTAssertTrue(app.staticTexts["Post \(post): 1 saved attachment"].waitForExistence(timeout: 8))
        }
    }
    func testSearchPaginationAndRetry() {
        let app = launch(failFirst: true)
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(app.staticTexts["saved-uploads-error"].waitForExistence(timeout: 8))
        app.buttons["saved-uploads-retry"].tap()
        // No "Load more" button: the next page arrives as the grid reaches its end.
        XCTAssertTrue(tile(app, Self.evening).waitForExistence(timeout: 10))
        XCTAssertTrue(tile(app, Self.evening).label.contains("Evening song.mp3"))
        let search = app.searchFields.firstMatch
        search.tap(); search.typeText("missing")
        XCTAssertTrue(app.otherElements["saved-uploads-empty"].waitForExistence(timeout: 8) || app.staticTexts["No matching uploads"].waitForExistence(timeout: 3))
        attach(app, "Saved uploads search has an explicit empty state")
    }
    // KRI-822 F1/F8: an owned device-only video (proxy in the cloud, original on this iPhone)
    // says "Original on this iPhone", adds to Visuals by uploading that verified original in full,
    // and stays added when the sheet is reopened.
    func testDeviceOnlyVideoAddsToVisualsFromLocalOriginal() {
        let app = launch(mode: "device-only-visual")
        app.buttons["fixture-open-saved"].tap()
        let horse = tile(app, Self.horse)
        XCTAssertTrue(horse.waitForExistence(timeout: 10))
        // The original check runs off the main thread after the page lands.
        XCTAssertTrue(waitFor(horse, contains: "Original on this iPhone"))
        attach(app, "Visuals tile: Original on this iPhone")
        horse.tap()
        XCTAssertTrue(value(horse).hasPrefix("Selected, 1"))
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
        attach(app, "Added to Visuals")
        app.buttons["fixture-open-saved"].tap()
        XCTAssertTrue(horse.waitForExistence(timeout: 10))
        XCTAssertTrue(value(horse).hasPrefix("In this post"))
        attach(app, "Reopened")
        app.buttons["saved-uploads-cancel"].tap()
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
    }
    // KRI-822 F2: with no local original, tapping the tile asks for it in Files; the verified relink
    // picks the tile and Done finishes the same Visuals add (it does not re-upload another proxy).
    func testRelinkedOriginalFinishesTheVisualsAdd() {
        let app = launch(mode: "device-only-relink")
        app.buttons["fixture-open-saved"].tap()
        let horse = tile(app, Self.horse)
        XCTAssertTrue(horse.waitForExistence(timeout: 10))
        XCTAssertTrue(waitFor(horse, contains: "Full-quality original needed on this iPhone"))
        horse.tap()
        XCTAssertTrue(waitFor(horse, contains: "Selected, 1"))
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Uploaded original 1x, Visuals source EEEEEEEE"].waitForExistence(timeout: 5))
        attach(app, "Relinked original added to Visuals")
    }
    // KRI-822 F13: photo-only phone Visuals keep videos blocked and say where they go instead.
    func testPhotoOnlyVisualsSendVideosToFootage() {
        let app = launch(mode: "images-only")
        app.buttons["fixture-open-saved"].tap()
        let horse = tile(app, Self.horse)
        XCTAssertTrue(horse.waitForExistence(timeout: 10))
        XCTAssertTrue(value(horse).contains("Add videos to this post in Footage."))
        horse.tap()
        XCTAssertEqual(app.staticTexts["saved-uploads-count"].label, "Add videos to this post in Footage.")
        XCTAssertTrue(value(horse).hasPrefix("Not selected"))
    }
    // KRI-822 F9: an asset with both sources still attaches its proxy to phone Footage, with no upload.
    func testPhoneFootageAttachesProxyWithoutUploadingTheOriginal() {
        let app = launch(mode: "footage-both")
        app.buttons["fixture-open-saved"].tap()
        let horse = tile(app, Self.horse)
        XCTAssertTrue(horse.waitForExistence(timeout: 10))
        XCTAssertTrue(waitFor(horse, contains: "Original on this iPhone"))
        horse.tap()
        app.buttons["saved-uploads-done"].tap()
        XCTAssertTrue(app.staticTexts["Post 1: 1 saved attachment"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Uploaded original 0x, Footage source DDDDDDDD"].waitForExistence(timeout: 5))
    }
    private func tile(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.buttons["saved-upload-tile-\(id)"] }
    private func value(_ element: XCUIElement) -> String { element.value as? String ?? "" }
    private func waitFor(_ element: XCUIElement, contains text: String, timeout: TimeInterval = 10) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if value(element).contains(text) { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return false
    }
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
