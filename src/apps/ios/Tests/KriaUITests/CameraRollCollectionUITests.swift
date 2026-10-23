import XCTest

final class CameraRollCollectionUITests: XCTestCase {
    func testReviewCorrectionRemovalAndResumeDoNotUpload() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection", "-collection-fixture-reset"]
        app.launch()
        XCTAssertTrue(app.textFields["collection-name"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.switches.matching(NSPredicate(format: "identifier BEGINSWITH 'collection-analysis-'")).firstMatch.exists)
        app.buttons["collection-options"].tap()
        app.buttons["Edit groups"].tap()
        let group = app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch
        group.tap()
        group.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        group.typeText("Evening")
        app.buttons["collection-groups-done"].tap()
        app.buttons["collection-remove-fixture-photo"].tap()
        let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["collection-remove-fixture-photo"])
        XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 5), .completed)
        app.terminate()
        app.launchArguments = ["-camera-roll-collection"]
        app.launch()
        XCTAssertTrue(app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.isHittable)
        XCTAssertTrue((app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.value as? String ?? "").contains("Evening"))
        XCTAssertFalse(app.buttons["collection-remove-fixture-photo"].exists)
        XCTAssertTrue(app.buttons["collection-find-stories"].exists)
        XCTAssertFalse(app.staticTexts["No media has been uploaded for this collection."].exists)
        XCTAssertFalse(app.buttons["collection-analyze"].exists)
        let evidence = XCTAttachment(screenshot: app.screenshot())
        evidence.name = "Corrected collection resumed without upload"
        evidence.lifetime = .keepAlways
        add(evidence)
    }
    func testPartiallyIndexedCollectionWaitsForExplicitPartialChoice() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready"]
        app.launch()
        XCTAssertTrue(app.buttons["collection-find-stories"].waitForExistence(timeout: 10))
        app.buttons["collection-find-stories"].tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Boat journey"].exists, "Full-collection ideas must wait for missing analysis")
        ready.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["1 video wasn’t included because analysis isn’t ready."].exists)
    }

}
