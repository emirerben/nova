import XCTest

final class FeaturedEditPreviewUITests: XCTestCase {
    @MainActor func testSignupPlaysAllFiveEditsTogether() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "signin"
        app.launch()
        let edits = app.otherElements["signin.featured-edits"]
        let playing = NSPredicate(format: "value == %@", "Playing 5 of 5 previews")
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: playing, object: edits)], timeout: 10), .completed)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Five signup edits playing together"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.buttons["signin.email"].tap()
        XCTAssertTrue(app.textFields["signin.email.field"].waitForExistence(timeout: 5))
        app.buttons["signin.email.cancel"].tap()
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: playing, object: edits)], timeout: 10), .completed)
    }

    @MainActor func testLeadingFormatOwnsPlayback() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "format"
        app.launch()
        let montage = app.buttons["format-montage"]
        let talking = app.buttons["format-talking_to_camera"]
        let playing = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Playing preview"), object: montage)
        XCTAssertEqual(XCTWaiter.wait(for: [playing], timeout: 10), .completed)
        XCTAssertEqual(app.buttons["format-narrated"].label, "Voiceover")
        XCTAssertEqual(talking.label, "Talk to camera")
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertEqual(montage.frame.minX, carousel.frame.minX, accuracy: 2)
        alignLeading(app.buttons["format-narrated"], in: carousel)
        XCTAssertEqual(talking.value as? String, "Paused preview")
        alignLeading(talking, in: carousel)
        let talkingPlaying = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Playing preview"), object: talking)
        XCTAssertEqual(XCTWaiter.wait(for: [talkingPlaying], timeout: 10), .completed)
        XCTAssertEqual(montage.value as? String, "Paused preview")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Left-aligned talking preview"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.buttons["format-preview-playback-talking_to_camera"].tap()
        XCTAssertEqual(talking.value as? String, "Paused preview")
    }
    @MainActor func testSliderAdvancesWhenLeadingAndPauses() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "format"
        app.launch()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 10))
        let slider = app.buttons["format-slides"]
        XCTAssertEqual(slider.value as? String, "Slide 1 of 7")
        alignLeading(slider, in: carousel)
        let moves = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value != %@", "Slide 1 of 7"), object: slider)
        XCTAssertEqual(XCTWaiter.wait(for: [moves], timeout: 2.5), .completed)
        app.buttons["slide-format-cover-playback"].tap()
        let still = slider.value as? String
        let changed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value != %@", still ?? ""), object: slider)
        changed.isInverted = true
        XCTAssertEqual(XCTWaiter.wait(for: [changed], timeout: 2.5), .completed)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Left-aligned Slider paused"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor private func alignLeading(_ card: XCUIElement, in carousel: XCUIElement) {
        for _ in 0..<4 {
            let delta = card.frame.minX - carousel.frame.minX
            if abs(delta) < 10 { return }
            let limit = carousel.frame.width * 0.4
            let distance = min(max(delta, -limit), limit)
            let start = carousel.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: -distance, dy: 0)))
        }
        XCTAssertEqual(card.frame.minX, carousel.frame.minX, accuracy: 10)
    }

}
