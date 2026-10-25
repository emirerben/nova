import XCTest

final class CameraRollCollectionUITests: XCTestCase {
    func testCompletedTimingReviewStillOffersExplicitReadyVideoChoice() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-unusable-timing"]
        app.launch()
        XCTAssertTrue(app.buttons["collection-find-stories"].waitForExistence(timeout: 10))
        app.buttons["collection-find-stories"].tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10), "One timing-review clip cannot block choosing the other ready video")
        XCTAssertTrue(app.staticTexts["1 video needs a usable source window. You can use the ready videos."].exists)
        XCTAssertFalse(app.staticTexts["Boat journey"].exists, "Partial inclusion still requires a separate creator choice")
        ready.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["1 video needs source or analysis review before inclusion."].exists)
        let evidence = XCTAttachment(screenshot: app.screenshot())
        evidence.name = "Explicit partial choice retains ready footage while timing-review source stays visible"
        evidence.lifetime = .keepAlways
        add(evidence)
    }
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
        XCTAssertTrue(app.buttons["collection-options"].waitForExistence(timeout: 10))
        app.buttons["collection-options"].tap()
        app.buttons["Edit groups"].tap()
        XCTAssertTrue(app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.isHittable)
        XCTAssertTrue((app.textFields.matching(identifier: "collection-group-fixture-video").firstMatch.value as? String ?? "").contains("Evening"))
        app.buttons["collection-groups-done"].tap()
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
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset"]
        app.launch()
        XCTAssertTrue(app.buttons["collection-find-stories"].waitForExistence(timeout: 10))
        app.buttons["collection-find-stories"].tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Boat journey"].exists, "Full-collection ideas must wait for missing analysis")
        ready.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["1 video needs source or analysis review before inclusion."].exists)
    }

    func testSavedIdeasSurviveRelaunchWithoutReselectingFootage() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset"]
        app.launch()
        let find = app.buttons["collection-find-stories"]
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Boat journey"].exists)
        ready.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        let ideasNavigation = app.navigationBars["Story ideas"]
        ideasNavigation.buttons["Done"].tap()
        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: ideasNavigation)
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 5), .completed)
        XCTAssertTrue(app.buttons["View saved ideas"].waitForExistence(timeout: 10))
        app.terminate()
        app.launchArguments = ["-camera-roll-collection-ready"]
        app.launch()
        let saved = app.buttons["View saved ideas"]
        XCTAssertTrue(saved.waitForExistence(timeout: 10))
        XCTAssertFalse(app.textFields["collection-story-query"].exists)
        saved.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "KRI-853 saved ideas reopened after process termination"
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testSavedPartialIdeasRequireNewChoiceWhenCoverageChanges() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset"]
        app.launch()
        let find = app.buttons["collection-find-stories"]
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10))
        ready.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        app.terminate()
        // Same number of missing clips, but a different excluded source: old
        // consent cannot silently authorize the changed coverage.
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-coverage-changed"]
        app.launch()
        let saved = app.buttons["View saved ideas"]
        XCTAssertTrue(saved.waitForExistence(timeout: 10))
        saved.tap()
        // Reading the frozen card grants no consent to generate new partial ideas.
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["collection-use-ready-videos"].exists)
        app.navigationBars["Story ideas"].buttons["Done"].tap()
        let more = app.buttons["Find more ideas"]
        XCTAssertTrue(more.waitForExistence(timeout: 10)); more.tap()
        let fresh = app.buttons["Find story ideas"]
        XCTAssertTrue(fresh.waitForExistence(timeout: 10)); fresh.tap()
        XCTAssertTrue(app.buttons["collection-use-ready-videos"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Boat journey"].exists)
    }

    func testSavedIdeasOpenWithoutIndexGenerationOrExposureWrites() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-saved-read-only"]
        app.launch()
        let saved = app.buttons["View saved ideas"]
        XCTAssertTrue(saved.waitForExistence(timeout: 10))
        saved.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Write attempted while reading saved idea"].exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Frozen saved card opened without index, generation or exposure writes"
        shot.lifetime = .keepAlways; add(shot)
    }

    func testMissingSavedReadEndpointDoesNotFallbackToGeneration() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-saved-read-only", "-collection-saved-old-server"]
        app.launch()
        let saved = app.buttons["View saved ideas"]
        XCTAssertTrue(saved.waitForExistence(timeout: 10)); saved.tap()
        XCTAssertTrue(app.staticTexts["Saved ideas cannot be opened on this server. Your collection is saved."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Boat journey"].exists)
    }

    func testSavedCardReplaysHoldThenRequiresExplicitRecovery() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-saved-read-only", "-collection-saved-recovery"]
        app.launch()
        // Generate/link the real synthetic original before timing product screens.
        XCTAssertTrue(app.staticTexts["saved-recovery-fixture-ready"].waitForExistence(timeout: 60))
        let saved = app.buttons["View saved ideas"]
        XCTAssertTrue(saved.waitForExistence(timeout: 10)); saved.tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        let choose = app.buttons["story-choose-11111111-1111-1111-1111-111111111111"]
        if !choose.isHittable { app.swipeUp() }
        XCTAssertTrue(choose.waitForExistence(timeout: 10)); choose.tap()
        let recover = app.buttons["story-recover-held-selection"]
        XCTAssertTrue(recover.waitForExistence(timeout: 15))
        XCTAssertFalse(app.staticTexts["Saved idea reopened"].exists)
        if !recover.isHittable { app.swipeUp() }; recover.tap()
        XCTAssertTrue(app.staticTexts["Saved idea reopened"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Write attempted while reading saved idea"].exists)
    }

    func testUnavailableAnalysisIsNotAnEditConflict() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-cache-miss"]
        app.launch()
        let find = app.buttons["collection-find-stories"]
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        let ready = app.buttons["collection-use-ready-videos"]
        XCTAssertTrue(ready.waitForExistence(timeout: 10))
        ready.tap()
        XCTAssertTrue(app.staticTexts["This footage needs analysis. Your collection is saved; new analysis is unavailable right now."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'edit changed elsewhere'")).firstMatch.exists)
        XCTAssertTrue(app.textFields["collection-name"].exists)
    }

    func testRealConflictCanOpenLatestWithoutLosingLocalCollection() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-collection-ready", "-collection-ready-reset", "-collection-conflict"]
        app.launch()
        let find = app.buttons["collection-find-stories"]
        XCTAssertTrue(find.waitForExistence(timeout: 10))
        find.tap()
        let latest = app.buttons["collection-review-latest"]
        XCTAssertTrue(latest.waitForExistence(timeout: 10))
        latest.tap()
        app.buttons["Open latest collection"].tap()
        let updated = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Updated outing"), object: app.textFields["collection-name"])
        XCTAssertEqual(XCTWaiter.wait(for: [updated], timeout: 10), .completed)
        app.buttons["collection-options"].tap()
        XCTAssertTrue(app.buttons["Weekend outing (local changes)"].waitForExistence(timeout: 5))
    }

}
