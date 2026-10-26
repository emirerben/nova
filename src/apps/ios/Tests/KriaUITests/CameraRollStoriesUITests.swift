import XCTest

final class CameraRollStoriesUITests: XCTestCase {
    @MainActor func testWholeSourceContextRemainsLabelledAsUnlocalized() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review", "-camera-roll-whole-source-context"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        app.buttons["See clips and details"].tap()
        let scope = app.staticTexts["Whole clip · no timed highlight detected"]
        for _ in 0..<4 where !scope.isHittable { app.swipeUp() }
        XCTAssertTrue(scope.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "0.0–8.2s")).firstMatch.exists)
        XCTAssertFalse(app.staticTexts["Rendering"].exists)
        let evidence = XCTAttachment(screenshot: app.screenshot())
        evidence.name = "Whole-source scope reaches actual native review with verified bounds"
        evidence.lifetime = .keepAlways
        add(evidence)
    }
    @MainActor func testThreeIdeasRemainIndependentWhenOneIsRejected() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review", "-camera-roll-multiple-ideas"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        let third = app.staticTexts["An evening crossing"]
        for _ in 0..<12 where !third.isHittable { app.swipeUp() }
        XCTAssertTrue(third.isHittable, "The third idea must be reachable in the real review screen")
        XCTAssertTrue(app.staticTexts["The light at the end"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Third distinct content idea is available before project selection"
        proof.lifetime = .keepAlways
        add(proof)
        for _ in 0..<12 where !app.staticTexts["Boat journey"].isHittable { app.swipeDown() }
        app.buttons["See clips and details"].firstMatch.tap()
        for _ in 0..<12 where !app.buttons["Not this story"].firstMatch.isHittable { app.swipeUp() }
        app.buttons["Not this story"].firstMatch.tap()
        let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.staticTexts["Boat journey"])
        XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 5), .completed)
        XCTAssertTrue(app.staticTexts["The light at the end"].exists)
        XCTAssertTrue(app.staticTexts["An evening crossing"].exists)
        XCTAssertFalse(app.staticTexts["No story to suggest"].exists)
        XCTAssertFalse(app.staticTexts["Rendering"].exists)
    }

    @MainActor func testReviewReplaceAndRejectWithoutRendering() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Whole clip · no timed highlight detected"].exists)
        app.buttons["See clips and details"].tap()
        for _ in 0..<3 where !app.buttons["story-replace-0"].isHittable { app.swipeUp() }
        app.buttons["story-replace-0"].tap()
        app.buttons["story-alternative-33333333-3333-3333-3333-333333333333"].tap()
        XCTAssertTrue(app.staticTexts["The final arrival"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["A ferry crossing"].exists, "The unrelated second choice must remain")
        for _ in 0..<3 where !app.switches["story-sound"].isHittable { app.swipeDown() }
        app.switches["story-sound"].switches.firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Muted footage"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["The final arrival"].exists)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Corrected recommendation preserves other footage and separate approval"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        for _ in 0..<4 where !app.buttons["Not this story"].exists { app.swipeUp() }
        XCTAssertTrue(app.buttons["story-choose-11111111-1111-1111-1111-111111111111"].exists)
        app.buttons["Not this story"].tap()
        XCTAssertTrue(app.staticTexts["No story to suggest"].waitForExistence(timeout: 5))
    }
}

extension CameraRollStoriesUITests {
    @MainActor func testResearchFactsAndSourcesStayBehindTheIdea() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review", "-camera-roll-story-research"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        let research = app.buttons["story-research-sources"]
        XCTAssertTrue(research.waitForExistence(timeout: 5))
        research.tap()
        XCTAssertTrue(app.staticTexts["Recorded research: the terminal was rebuilt."].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["Recorded public archive"].firstMatch.exists)
        XCTAssertFalse(app.staticTexts["Rendering"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Idea research with provider source, before project approval"
        proof.lifetime = .keepAlways
        add(proof)
    }

    @MainActor func testConfirmExamplesRateIdeaAndForget() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review"]
        app.launch()
        let preferences = app.buttons["editorial-preferences"]
        XCTAssertTrue(preferences.waitForExistence(timeout: 10))
        preferences.tap()
        let example = app.buttons["taste-example-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"]
        XCTAssertTrue(example.waitForExistence(timeout: 5))
        example.tap()
        app.buttons["taste-save"].tap()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 5))
        app.buttons["idea-feedback"].tap()
        app.buttons["Would post"].tap()
        app.buttons["idea-feedback-save"].tap()
        let saved = app.buttons["idea-feedback"]
        let confirmed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "Feedback saved"), object: saved)
        XCTAssertEqual(XCTWaiter.wait(for: [confirmed], timeout: 5), .completed)
        XCTAssertFalse(app.staticTexts["Rendering"].exists)
        preferences.tap()
        let forget = app.buttons["taste-forget"]
        XCTAssertTrue(forget.waitForExistence(timeout: 5))
        forget.tap()
        app.alerts.buttons["Forget"].tap()
        XCTAssertTrue(app.staticTexts["Posts you like"].waitForExistence(timeout: 5))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Editorial taste is explicit and can be forgotten"
        proof.lifetime = .keepAlways
        add(proof)
    }
}
