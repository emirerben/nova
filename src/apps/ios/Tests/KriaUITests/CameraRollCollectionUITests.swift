import XCTest

final class CameraRollCollectionUITests: XCTestCase {
    func testReviewCorrectionRemovalAndResumeDoNotUpload() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection", "-collection-fixture-reset"]
        app.launch()
        XCTAssertTrue(app.textFields["collection-name"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.switches.matching(NSPredicate(format: "identifier BEGINSWITH 'collection-analysis-'")).firstMatch.exists)
        let group = app.textFields["collection-group-fixture-video"]
        group.tap()
        group.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        group.typeText("Evening")
        app.buttons["collection-save"].tap()
        app.buttons["collection-remove-fixture-photo"].tap()
        let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["collection-remove-fixture-photo"])
        XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 5), .completed)
        app.terminate()
        app.launchArguments = ["-camera-roll-collection"]
        app.launch()
        XCTAssertTrue(app.textFields["collection-group-fixture-video"].waitForExistence(timeout: 10))
        XCTAssertTrue((app.textFields["collection-group-fixture-video"].value as? String ?? "").contains("Evening"))
        XCTAssertFalse(app.buttons["collection-remove-fixture-photo"].exists)
        XCTAssertTrue(app.staticTexts["No media has been uploaded for this collection."].exists)
        let evidence = XCTAttachment(screenshot: app.screenshot())
        evidence.name = "Corrected collection resumed without upload"
        evidence.lifetime = .keepAlways
        add(evidence)
    }
}
