import XCTest

final class ActivityUITests: XCTestCase {
    func testReadyActivityOpensItsCurrentProject() {
        let app = launchInbox(stale: false)
        app.buttons["activity-item-11111111-1111-4111-8111-111111111111"].tap()
        XCTAssertTrue(app.staticTexts["workspace-project-title"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.navigationBars["Activity"].exists)
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, "Activity destination")
    }

    func testStaleActivityCannotNavigateToCachedProject() {
        let app = launchInbox(stale: true)
        app.buttons["activity-item-11111111-1111-4111-8111-111111111111"].tap()
        XCTAssertTrue(app.staticTexts["activity-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.navigationBars["Activity"].exists)
        capture(app, name: "Stale activity remains in inbox")
    }

    func testFailedReadAcknowledgementDoesNotNavigate() {
        let app = launchInbox(stale: false, readFailure: true)
        app.buttons["activity-item-11111111-1111-4111-8111-111111111111"].tap()
        XCTAssertTrue(app.staticTexts["activity-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.navigationBars["Activity"].exists)
    }

    private func capture(_ app: XCUIApplication, name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func launchInbox(stale: Bool, readFailure: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "montage"
        app.launchEnvironment["KRIA_NATIVE_ACTIVITY_FIXTURE"] = stale ? "stale" : readFailure ? "read_failure" : "ready"
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        for expectedTitle in ["Activity destination", "Current project"] {
            app.buttons["Open projects"].tap()
            let newChat = app.buttons["drawer-new-chat"]
            XCTAssertTrue(newChat.waitForExistence(timeout: 5))
            newChat.tap()
            let title = app.staticTexts["workspace-project-title"]
            XCTAssertTrue(title.waitForExistence(timeout: 10))
            let changed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", expectedTitle), object: title)
            XCTAssertEqual(XCTWaiter.wait(for: [changed], timeout: 10), .completed)
            let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
            XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 5), .completed)
        }
        app.buttons["Open projects"].tap()
        XCTAssertTrue(app.buttons["activity-open"].waitForExistence(timeout: 5))
        app.buttons["activity-open"].tap()
        XCTAssertTrue(app.staticTexts["Export available"].waitForExistence(timeout: 10))
        capture(app, name: "Native activity inbox")
        return app
    }
}
