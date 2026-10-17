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
    private func launch(failFirst: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-saved-uploads"]
        if failFirst { app.launchEnvironment["KRIA_SAVED_UPLOADS_FAIL_FIRST"] = "1" }
        app.launch()
        XCTAssertTrue(app.buttons["fixture-open-saved"].waitForExistence(timeout: 10))
        return app
    }
}
