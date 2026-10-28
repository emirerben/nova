import XCTest

final class FeaturedEditPreviewUITests: XCTestCase {
    @MainActor func testOnboardingPlaysFourEditsInOrder() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "signin"
        app.launch()
        let edits = app.otherElements["signin.featured-edits"]
        for number in 1...4 {
            let expected = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value == %@", "Playing edit \(number) of 4"), object: edits
            )
            XCTAssertEqual(XCTWaiter.wait(for: [expected], timeout: 28), .completed)
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "Onboarding edit \(number)"
            screenshot.lifetime = .keepAlways
            add(screenshot)
        }
    }

    @MainActor func testCenteredFormatOwnsPlayback() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "format"
        app.launch()
        let montage = app.buttons["format-montage"]
        let talking = app.buttons["format-talking_to_camera"]
        let playing = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Playing preview"), object: montage)
        XCTAssertEqual(XCTWaiter.wait(for: [playing], timeout: 10), .completed)
        let carousel = app.scrollViews["format-carousel"]
        center(talking, in: carousel)
        let talkingPlaying = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Playing preview"), object: talking)
        XCTAssertEqual(XCTWaiter.wait(for: [talkingPlaying], timeout: 10), .completed)
        XCTAssertEqual(montage.value as? String, "Paused preview")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Centered talking preview"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.buttons["format-preview-playback-talking_to_camera"].tap()
        XCTAssertEqual(talking.value as? String, "Paused preview")
    }
    @MainActor func testSliderAdvancesWhenCenteredAndPauses() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "format"
        app.launch()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 10))
        let slider = app.buttons["format-slides"]
        XCTAssertEqual(slider.value as? String, "Slide 1 of 7")
        center(slider, in: carousel)
        let moves = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value != %@", "Slide 1 of 7"), object: slider)
        XCTAssertEqual(XCTWaiter.wait(for: [moves], timeout: 2.5), .completed)
        app.buttons["slide-format-cover-playback"].tap()
        let still = slider.value as? String
        let changed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value != %@", still ?? ""), object: slider)
        changed.isInverted = true
        XCTAssertEqual(XCTWaiter.wait(for: [changed], timeout: 2.5), .completed)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Centered Slider paused"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor private func center(_ card: XCUIElement, in carousel: XCUIElement) {
        for _ in 0..<4 {
            let delta = card.frame.midX - carousel.frame.midX
            if abs(delta) < 10 { return }
            let limit = carousel.frame.width * 0.4
            let distance = min(max(delta, -limit), limit)
            let start = carousel.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -distance, dy: 0)))
        }
        XCTAssertEqual(card.frame.midX, carousel.frame.midX, accuracy: 10)
    }

}
