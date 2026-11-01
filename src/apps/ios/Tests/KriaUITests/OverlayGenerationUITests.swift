import XCTest

@MainActor final class OverlayGenerationUITests: XCTestCase {
    private func waitUntilEnabled(_ element: XCUIElement) {
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 10), .completed, "Planning waits for the account gate")
    }

    /// Planning and image generation use allowance; the image prompt is visible before the separate explicit Generate action.
    private func approveVisiblePromptAndGenerate(_ app: XCUIApplication) {
        let submit = app.buttons["generated-media-submit"]
        XCTAssertTrue(submit.waitForExistence(timeout: 15))
        XCTAssertTrue(app.textFields["generated-media-prompt"].exists || app.textViews["generated-media-prompt"].exists,
                      "The prompt to be paid for is shown before generation")
        XCTAssertFalse(app.buttons["generated-media-use"].exists, "No hidden first spend")
        XCTAssertTrue(submit.isEnabled)
        submit.tap()
    }
    func testMixedPlanShowsTheLogoNoticeNextToTheKeptOverlay() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-overlays"]
        app.launchEnvironment = ["KRIA_OVERLAY_MIXED": "1", "KRIA_OVERLAY_RESET": "1", "UI_TEST_REDUCE_MOTION": "1"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        app.buttons["native-editor-generate-media"].tap()
        app.segmentedControls["overlay-generation-mode"].buttons["Let Kria choose"].tap()
        waitUntilEnabled(app.buttons["overlay-generation-plan"])
        app.buttons["overlay-generation-plan"].tap()
        let notice = app.staticTexts["overlay-generation-message"]
        let scroll = app.scrollViews["native-editor-visuals-scroll"]
        for _ in 0..<4 where !notice.exists { scroll.swipeUp() }
        XCTAssertTrue(notice.waitForExistence(timeout: 10))
        XCTAssertTrue(notice.label.contains("Upload the real logo"), "The refused-logo notice is shown with the kept overlay")
        XCTAssertTrue(app.buttons["overlay-generation-use-DCC14763-5702-4AE3-8510-8C72845E6B15"].exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "mixed-plan-logo-notice"
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testAvailabilityCheckFailureCanBeRetriedWithoutLeavingThePanel() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-overlays"]
        app.launchEnvironment = ["KRIA_OVERLAY_CAPABILITY_FAIL_ONCE": "1", "KRIA_OVERLAY_RESET": "1", "UI_TEST_REDUCE_MOTION": "1"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        app.buttons["native-editor-generate-media"].tap()
        // Automatic mode needs no prompt, so only the account gate can keep Plan disabled.
        app.segmentedControls["overlay-generation-mode"].buttons["Let Kria choose"].tap()
        let retry = app.buttons["overlay-generation-availability-retry"]
        let scroll = app.scrollViews["native-editor-visuals-scroll"]
        for _ in 0..<4 where !retry.exists { scroll.swipeUp() }
        XCTAssertTrue(retry.waitForExistence(timeout: 10), "A failed account check offers Try again")
        XCTAssertFalse(app.buttons["overlay-generation-plan"].isEnabled, "Planning stays closed while the gate is unproven")
        let failed = XCTAttachment(screenshot: app.screenshot())
        failed.name = "availability-check-failed"
        failed.lifetime = .keepAlways
        add(failed)
        retry.tap()
        waitUntilEnabled(app.buttons["overlay-generation-plan"])
        XCTAssertFalse(retry.exists)
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'use your account’s allowance'")).firstMatch.exists,
                      "The paid-use disclosure returns once the account check succeeds")
        let recovered = XCTAttachment(screenshot: app.screenshot())
        recovered.name = "availability-check-recovered"
        recovered.lifetime = .keepAlways
        add(recovered)
    }

    func testNewLookCanBeAddedAfterAcceptedSourceRegistrationResponseIsLost() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-overlays"]
        app.launchEnvironment = ["KRIA_OVERLAY_IMAGE": "1", "KRIA_OVERLAY_RESET": "1",
            "KRIA_OVERLAY_REGISTRATION_RECOVERY": "1", "UI_TEST_REDUCE_MOTION": "1"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        app.buttons["native-editor-generate-media"].tap()
        app.segmentedControls["overlay-generation-mode"].buttons["Let Kria choose"].tap()
        waitUntilEnabled(app.buttons["overlay-generation-plan"])
        app.buttons["overlay-generation-plan"].tap()
        approveVisiblePromptAndGenerate(app)
        let use = app.buttons["generated-media-use"]
        XCTAssertTrue(use.waitForExistence(timeout: 15))
        let scroll = app.scrollViews["native-editor-visuals-scroll"]
        for _ in 0..<4 where !use.isHittable { scroll.swipeUp() }
        use.tap()
        XCTAssertTrue(app.staticTexts["generated-media-error"].waitForExistence(timeout: 10))
        let block = app.buttons["native-editor-timeline-visual_block-DCC14763-5702-4AE3-8510-8C72845E6B15"]
        XCTAssertFalse(block.exists)
        app.buttons["Try another look"].tap()
        XCTAssertTrue(use.waitForExistence(timeout: 10))
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: use)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 10), .completed)
        use.tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].waitForExistence(timeout: 15))
        app.buttons["native-editor-visuals-done"].tap()
        XCTAssertTrue(block.waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons.matching(identifier: block.identifier).count, 1)
    }

    func testInlineImageApprovalUsesNormalVisualControlsUndoAndSavedReopen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-overlays"]
        app.launchEnvironment["KRIA_OVERLAY_IMAGE"] = "1"
        app.launchEnvironment["KRIA_OVERLAY_RESET"] = "1"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        app.buttons["native-editor-generate-media"].tap()
        XCTAssertTrue(app.otherElements["native-editor-overlay-generator"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Generate media"].exists)
        let input = app.textFields["overlay-generation-prompt"].exists
            ? app.textFields["overlay-generation-prompt"] : app.textViews["overlay-generation-prompt"]
        let initial = XCTAttachment(screenshot: app.screenshot())
        initial.name = "inline-visuals-prompt-before-keyboard"
        initial.lifetime = .keepAlways
        add(initial)
        input.tap()
        input.typeText("Add a supporting graphic at the spoken moment")
        let scroll = app.scrollViews["native-editor-visuals-scroll"]
        let generate = app.buttons["overlay-generation-plan"]
        for _ in 0..<4 where !generate.isHittable { scroll.swipeUp() }
        let promptShot = XCTAttachment(screenshot: app.screenshot())
        promptShot.name = "inline-visuals-prompt"
        promptShot.lifetime = .keepAlways
        add(promptShot)
        waitUntilEnabled(generate)
        generate.tap()
        approveVisiblePromptAndGenerate(app)
        let addImage = app.buttons["generated-media-use"]
        XCTAssertTrue(addImage.waitForExistence(timeout: 15))
        for _ in 0..<4 where !addImage.isHittable { scroll.swipeUp() }
        let block = app.buttons["native-editor-timeline-visual_block-DCC14763-5702-4AE3-8510-8C72845E6B15"]
        XCTAssertFalse(block.exists, "Preview must not insert a timeline block")
        let previewShot = XCTAttachment(screenshot: app.screenshot())
        previewShot.name = "inline-visuals-image-preview"
        previewShot.lifetime = .keepAlways
        add(previewShot)
        XCTAssertTrue(addImage.isEnabled)
        addImage.doubleTap()
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Edit"].exists)
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        let editShot = XCTAttachment(screenshot: app.screenshot())
        editShot.name = "ordinary-image-edit-controls"
        editShot.lifetime = .keepAlways
        add(editShot)
        app.buttons["native-editor-visuals-done"].tap()
        XCTAssertTrue(block.waitForExistence(timeout: 10))
        XCTAssertEqual(app.buttons.matching(identifier: block.identifier).count, 1)
        app.buttons["native-editor-undo"].tap()
        XCTAssertFalse(block.exists)
        app.buttons["native-editor-redo"].tap()
        XCTAssertTrue(block.exists)
        app.buttons["native-editor-tool-visuals"].tap()
        if app.buttons["native-editor-add-another-visual"].exists {
            app.buttons["native-editor-add-another-visual"].tap()
        }
        app.buttons["native-editor-generate-media"].tap()
        let existing = app.buttons["overlay-generation-use-DCC14763-5702-4AE3-8510-8C72845E6B15"]
        XCTAssertTrue(existing.waitForExistence(timeout: 5))
        XCTAssertEqual(existing.label, "Edit in timeline")
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
        for _ in 0..<4 where !existing.isHittable { scroll.swipeUp() }
        existing.tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].waitForExistence(timeout: 5))
        app.buttons["native-editor-visuals-done"].tap()
        app.buttons["native-editor-save"].tap()
        let saved = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == 'Saved'"), object: app.buttons["native-editor-save"])
        XCTAssertEqual(XCTWaiter.wait(for: [saved], timeout: 15), .completed)
        app.terminate()
        app.launchEnvironment["KRIA_OVERLAY_RESET"] = "0"
        app.launch()
        XCTAssertTrue(block.waitForExistence(timeout: 20))
        XCTAssertFalse(app.staticTexts["Preview unavailable"].exists)
        XCTAssertEqual(app.buttons.matching(identifier: block.identifier).count, 1)
        block.tap()
        if app.buttons["native-editor-visuals-done"].exists { app.buttons["native-editor-visuals-done"].tap() }
        XCTAssertTrue(app.buttons["native-editor-save"].waitForExistence(timeout: 5))
        let reopened = XCTAttachment(screenshot: app.screenshot())
        reopened.name = "ordinary-image-saved-reopen"
        reopened.lifetime = .keepAlways
        add(reopened)
    }

    func testVisualsPlansAndAddsAnEditableNumberOverlay() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text", "-ui-testing-editor-overlays"]
        app.launchEnvironment["KRIA_OVERLAY_RESET"] = "1"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let visuals = app.buttons["native-editor-tool-visuals"]
        XCTAssertTrue(visuals.waitForExistence(timeout: 20))
        visuals.tap()
        let generate = app.buttons["native-editor-generate-media"]
        XCTAssertTrue(generate.waitForExistence(timeout: 5))
        XCTAssertTrue(generate.isEnabled)
        generate.tap()
        XCTAssertTrue(app.otherElements["native-editor-overlay-generator"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Generate media"].exists)
        XCTAssertTrue(app.buttons["native-editor-play-pause"].exists)
        let input = app.textFields["overlay-generation-prompt"].exists
            ? app.textFields["overlay-generation-prompt"] : app.textViews["overlay-generation-prompt"]
        input.tap()
        input.typeText("Show 10 when I say number ten")
        app.segmentedControls["overlay-generation-mode"].buttons["Let Kria choose"].tap()
        XCTAssertFalse(input.exists)
        waitUntilEnabled(app.buttons["overlay-generation-plan"])
        app.buttons["overlay-generation-plan"].tap()
        let use = app.buttons["overlay-generation-use-DCC14763-5702-4AE3-8510-8C72845E6B15"]
        for _ in 0..<5 where !use.isHittable { app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(use.waitForExistence(timeout: 10))
        XCTAssertTrue(use.isEnabled)
        let review = XCTAttachment(screenshot: app.screenshot())
        review.name = "overlay-plan-review-fixture"
        review.lifetime = .keepAlways
        add(review)
        use.tap()
        XCTAssertTrue(app.buttons["native-editor-timeline-text-DCC14763-5702-4AE3-8510-8C72845E6B15"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.otherElements["native-editor-overlay-generator"].exists)
        let applied = XCTAttachment(screenshot: app.screenshot())
        applied.name = "overlay-added-fixture"
        applied.lifetime = .keepAlways
        add(applied)
    }
}
