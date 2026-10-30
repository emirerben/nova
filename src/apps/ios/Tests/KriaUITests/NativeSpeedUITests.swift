import XCTest

@MainActor
final class NativeSpeedUITests: XCTestCase {
    // This offline fixture verifies real controls and canonical clip duration.
    // Retimed media playback/export is covered separately by AVFoundation tests.
    func testDeviceClipSpeedAdjustsResetsAndUndoesThroughTheInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speed", "-ui-testing-editor-device"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        app.buttons["native-editor-adjust"].tap()
        let slider = app.sliders["native-editor-playback-rate"]
        XCTAssertTrue(app.navigationBars["Adjust"].exists)
        XCTAssertTrue(app.buttons["Slide source earlier"].exists)
        func revealSpeed() {
            for _ in 0..<3 where !slider.exists || !slider.isHittable {
                app.collectionViews.firstMatch.swipeUp()
            }
            XCTAssertTrue(slider.waitForExistence(timeout: 5), app.debugDescription)
        }
        revealSpeed()
        XCTAssertTrue(slider.isEnabled)
        func verifyRate(_ rate: String, duration: String) {
            XCTAssertEqual(slider.value as? String, rate)
            let label = app.descendants(matching: .any)["native-editor-speed-duration"].firstMatch
            XCTAssertEqual(label.value as? String, duration)
        }
        verifyRate("1.00x", duration: "00:06.0")
        slider.adjust(toNormalizedSliderPosition: 0)
        verifyRate("0.25x", duration: "00:24.0")
        slider.adjust(toNormalizedSliderPosition: 1)
        verifyRate("4.00x", duration: "00:01.5")
        let reset = app.buttons["Reset speed"]
        if !reset.isHittable { app.collectionViews.firstMatch.swipeUp() }
        reset.tap()
        verifyRate("1.00x", duration: "00:06.0")
        app.buttons["native-editor-inspector-done"].tap()
        app.buttons["native-editor-undo"].tap()
        app.buttons["native-editor-adjust"].tap()
        revealSpeed()
        verifyRate("4.00x", duration: "00:01.5")
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = "Phone clip speed after slower, faster, reset and undo"
        capture.lifetime = .keepAlways
        add(capture)
    }

    func testClosingSpeedInspectorCommitsResetForSingleUndo() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speed", "-ui-testing-editor-device"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        app.buttons["native-editor-adjust"].tap()
        let slider = app.sliders["native-editor-playback-rate"]
        let duration = app.descendants(matching: .any)["native-editor-speed-duration"].firstMatch
        func revealSpeed() {
            for _ in 0..<3 where !slider.exists || !slider.isHittable {
                app.collectionViews.firstMatch.swipeUp()
            }
            XCTAssertTrue(slider.waitForExistence(timeout: 5), app.debugDescription)
        }
        revealSpeed()
        slider.adjust(toNormalizedSliderPosition: 0.5)
        let chosen = try settledSpeed(slider, duration: duration)
        let chosenRate = chosen.rate
        let chosenDuration = chosen.duration
        XCTAssertTrue(chosenRate.hasSuffix("x"), "Pre-Reset rate must include its unit: \(chosenRate)")
        let numericRate = try XCTUnwrap(Double(chosenRate.dropLast()))
        XCTAssertGreaterThan(numericRate, 1, "The creator must choose a faster clip before Reset")
        XCTAssertLessThanOrEqual(numericRate, 4, "Pre-Reset rate must stay within the supported range")
        let expectedDuration = String(format: "00:%04.1f", 6 / numericRate)
        XCTAssertEqual(chosenDuration, expectedDuration, "The six-second clip must shorten consistently with the chosen rate \(chosenRate)")
        app.buttons["Reset speed"].tap()
        XCTAssertEqual(slider.value as? String, "1.00x")
        XCTAssertEqual(duration.value as? String, "00:06.0")
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertFalse(app.navigationBars["Adjust"].exists)
        let undo = app.buttons["native-editor-undo"]
        XCTAssertTrue(undo.isEnabled, "Closing the inspector must commit its pending edit")
        undo.tap()
        app.buttons["native-editor-adjust"].tap()
        revealSpeed()
        XCTAssertEqual(slider.value as? String, chosenRate, "One Undo must restore the speed immediately before Reset")
        XCTAssertEqual(duration.value as? String, chosenDuration, "Undo must restore rate and duration together")
        print("KRIA_NATIVE_TEST_PROOF speed-reset-close-undo-v3 requested_normalized=0.5 chosen_rate=\(chosenRate) chosen_duration=\(chosenDuration) expected_duration=\(expectedDuration)")
    }

    private func settledSpeed(_ slider: XCUIElement, duration: XCUIElement) throws -> (rate: String, duration: String) {
        var last: (rate: String, duration: String)?
        var matchingSamples = 0
        let settled = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let rate = slider.value as? String, let duration = duration.value as? String else { return false }
            if let previous = last, previous.rate == rate, previous.duration == duration {
                matchingSamples += 1
            } else {
                matchingSamples = 1
            }
            last = (rate, duration)
            return matchingSamples >= 3
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 5), .completed, "Speed and duration must settle before the next action")
        return try XCTUnwrap(last)
    }

}
