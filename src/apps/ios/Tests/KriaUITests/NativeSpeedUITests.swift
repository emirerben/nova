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

    func testClosingSpeedInspectorCommitsResetForSingleUndo() {
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
        // The SDK's adjustment is approximate. Undo must restore the actual
        // visible choice, independently of the exact-endpoint test above.
        slider.adjust(toNormalizedSliderPosition: 0.8)
        let chosenRate = slider.value as? String
        let chosenDuration = duration.value as? String
        let numericRate = Double((chosenRate ?? "").replacingOccurrences(of: "x", with: "")) ?? 0
        XCTAssertTrue((2...4).contains(numericRate), "The creator must have a faster clip before Reset")
        XCTAssertNotNil(chosenDuration)
        XCTAssertNotEqual(chosenDuration, "00:06.0")
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
        print("KRIA_NATIVE_TEST_PROOF speed-reset-close-undo-v1")
    }
}
