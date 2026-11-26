import XCTest

/// Dim on a device-rendered montage, through the editor UI: dim the first clip to 50% and watch the live preview get
/// darker, Apply to all, remove the dim from one clip, Save through the stub commit transport and reopen to find every
/// clip's dim where it was left.
@MainActor
final class NativeDimUITests: XCTestCase {
    private func launch(reset: Bool, capability: Bool = true) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        if capability { app.launchArguments += ["-ui-testing-editor-dim"] }
        if reset && capability { app.launchArguments += ["-ui-testing-editor-dim-reset"] }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    private func clip(_ app: XCUIApplication, _ number: Int) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-clip-\(number)"].firstMatch
    }

    private func openDim(_ app: XCUIApplication, clip number: Int) {
        let target = clip(app, number)
        XCTAssertTrue(target.waitForExistence(timeout: 20))
        target.tap()
        let dim = app.buttons["native-editor-clip-dim"]
        XCTAssertTrue(dim.waitForExistence(timeout: 5), "Dim sits in the clip's context strip")
        if !dim.isHittable { app.scrollViews.containing(.button, identifier: "native-editor-clip-dim").firstMatch.swipeLeft() }
        XCTAssertTrue(dim.isHittable)
        dim.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-dim-sheet"].firstMatch.waitForExistence(timeout: 5))
    }

    private func done(_ app: XCUIApplication) {
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-dim-sheet"].firstMatch.waitForNonExistence(timeout: 5))
    }

    private func value(_ app: XCUIApplication) -> String { app.staticTexts["native-editor-dim-value"].label }

    /// The preview canvas: several elements share its identifier, and the canvas is the largest.
    private func preview(_ app: XCUIApplication) -> XCUIElement {
        let all = app.descendants(matching: .any).matching(identifier: "native-editor-preview").allElementsBoundByIndex
        return all.filter(\.exists).max { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }
            ?? app.descendants(matching: .any)["native-editor-preview"].firstMatch
    }

    /// Mean Rec. 709 luma of a text-free patch of the preview (the middle half across, from 60% to 85% down: the
    /// fixture's title sits at 40%), read from a screenshot of it.
    private func previewLuma(_ app: XCUIApplication) throws -> Double {
        let image = try XCTUnwrap(preview(app).screenshot().image.cgImage)
        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        var sum = 0.0, count = 0
        for y in stride(from: height * 6 / 10, to: height * 85 / 100, by: 2) { for x in stride(from: width / 4, to: width * 3 / 4, by: 2) {
            let i = (y * width + x) * 4
            sum += 0.2126 * Double(pixels[i]) + 0.7152 * Double(pixels[i + 1]) + 0.0722 * Double(pixels[i + 2]); count += 1
        } }
        return sum / Double(max(count, 1))
    }

    /// Waits until the preview's luma satisfies `condition`, re-reading it as the composition redraws.
    private func waitForPreview(_ app: XCUIApplication, timeout: TimeInterval = 15, _ condition: (Double) -> Bool) throws -> Double {
        let deadline = Date().addingTimeInterval(timeout)
        var luma = try previewLuma(app)
        while !condition(luma), Date() < deadline {
            Thread.sleep(forTimeInterval: 0.5)
            luma = try previewLuma(app)
        }
        return luma
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = name
        capture.lifetime = .keepAlways
        add(capture)
    }

    func testDimHalfApplyToAllRemoveOneSaveAndReopen() throws {
        let app = launch(reset: true)
        XCTAssertTrue(clip(app, 2).waitForExistence(timeout: 20), "the fixture has more than one clip")
        Thread.sleep(forTimeInterval: 3)
        let undimmed = try previewLuma(app)
        XCTAssertGreaterThan(undimmed, 20, "the undimmed preview shows the clip")
        attach(app, "Preview before the dim")

        openDim(app, clip: 1)
        XCTAssertEqual(value(app), "0%")
        XCTAssertFalse(app.buttons["native-editor-dim-remove"].isEnabled, "an undimmed clip has nothing to remove")
        for _ in 0..<10 { app.buttons["native-editor-dim-more"].tap() }
        XCTAssertEqual(value(app), "50%")
        XCTAssertEqual(app.sliders["native-editor-dim-slider"].value as? String, "50%")
        done(app)

        // An encoded multiply keeps half the luma; a multiply in linear light would keep about 0.73 of it.
        let dimmed = try waitForPreview(app) { (0.45...0.58).contains($0 / undimmed) }
        XCTAssertGreaterThanOrEqual(dimmed / undimmed, 0.45, "50% is not darker than half: \(dimmed) vs \(undimmed)")
        XCTAssertLessThanOrEqual(dimmed / undimmed, 0.58, "50% halves the picture's luma: \(dimmed) vs \(undimmed)")
        attach(app, "Preview with the first clip dimmed 50%")
        let measured = XCTAttachment(string: "preview luma undimmed=\(undimmed) dimmed=\(dimmed) ratio=\(dimmed / undimmed)")
        measured.name = "Preview luma"
        measured.lifetime = .keepAlways
        add(measured)

        openDim(app, clip: 1)
        app.buttons["native-editor-dim-apply-all"].tap()
        let note = app.staticTexts["native-editor-dim-apply-all-note"]
        XCTAssertTrue(note.waitForExistence(timeout: 5))
        XCTAssertTrue(note.label.hasPrefix("Set 50% on"), note.label)
        done(app)

        openDim(app, clip: 2)
        XCTAssertEqual(value(app), "50%", "Apply to all dimmed the second clip too")
        app.buttons["native-editor-dim-remove"].tap()
        XCTAssertEqual(value(app), "0%")
        done(app)
        openDim(app, clip: 1)
        XCTAssertEqual(value(app), "50%", "removing one clip's dim leaves the others")
        done(app)

        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.isEnabled, "a dim is an unsaved edit")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)

        app.terminate()
        let reopened = launch(reset: false)
        for (number, expected) in [(1, "50%"), (2, "0%")] {
            openDim(reopened, clip: number)
            XCTAssertEqual(value(reopened), expected, "clip \(number) after reopening")
            done(reopened)
        }
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
        attach(reopened, "Reopened with the saved dims")
    }

    func testDimIsHiddenWhereTheServerDoesNotOfferIt() {
        let app = launch(reset: false, capability: false)
        XCTAssertTrue(clip(app, 1).waitForExistence(timeout: 20))
        clip(app, 1).tap()
        XCTAssertTrue(app.buttons["native-editor-clip-range"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-clip-dim"].exists)
    }
}
