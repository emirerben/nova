import XCTest

/// KRI-589: the creator's whole drawing journey on the editor fixture whose variant opens `drawings.*`:
/// draw with a finger -> Done -> the block sits on the DRAW row -> trim it -> Save (the real commit request goes
/// to a stub transport) -> reopen the editor -> the drawing is still there.
@MainActor
final class DrawingJourneyUITests: XCTestCase {
    private func launch(reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-drawing"] + (reset ? ["-ui-testing-editor-drawing-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    private func drawingBlock(in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH %@", "native-editor-timeline-drawing-")).firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testDrawDoneTrimSaveReopenPersists() throws {
        let app = launch(reset: true)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        XCTAssertFalse(drawingBlock(in: app).exists, "a fresh cut has no drawings")

        // The Draw tool is offered because the variant opens drawings.add.
        let draw = app.buttons["native-editor-tool-draw"]
        XCTAssertTrue(draw.waitForExistence(timeout: 5))
        draw.tap()

        let canvas = app.descendants(matching: .any)["native-editor-draw-canvas"].firstMatch
        XCTAssertTrue(canvas.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-draw-texture-neon"].exists, "six textures are offered")
        XCTAssertFalse(app.buttons["native-editor-draw-done"].isEnabled, "nothing drawn yet")
        app.buttons["native-editor-draw-texture-neon"].tap()
        app.buttons["native-editor-draw-swatch-32ADE6"].tap()

        func stroke(from a: CGVector, to b: CGVector) {
            canvas.coordinate(withNormalizedOffset: a).press(forDuration: 0.05, thenDragTo: canvas.coordinate(withNormalizedOffset: b))
        }
        stroke(from: CGVector(dx: 0.2, dy: 0.35), to: CGVector(dx: 0.8, dy: 0.45))
        stroke(from: CGVector(dx: 0.3, dy: 0.7), to: CGVector(dx: 0.7, dy: 0.6))
        XCTAssertTrue(app.buttons["native-editor-draw-done"].isEnabled, "two strokes are ink")
        attach(app, "Draw mode with two neon strokes")
        app.buttons["native-editor-draw-done"].tap()

        // One undo step later the drawing is selected, its panel is open and the block is on the timeline.
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-drawing-panel"].firstMatch.waitForExistence(timeout: 5))
        attach(app, "Drawing panel after Done")
        app.buttons["native-editor-drawing-done"].tap()
        let block = drawingBlock(in: app)
        XCTAssertTrue(block.waitForExistence(timeout: 5), "the DRAW row shows the new block")
        XCTAssertTrue(app.buttons["native-editor-save"].isEnabled, "a new drawing is an unsaved edit")

        // Trim: pull the selected block's trailing handle in by a quarter of the block.
        let handle = app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5), "the selected drawing block shows its trim handles")
        let before = block.frame.width
        handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.1, thenDragTo: handle.coordinate(withNormalizedOffset: CGVector(dx: -4, dy: 0.5)))
        XCTAssertLessThan(block.frame.width, before - 4, "trimming shortens the block")
        attach(app, "DRAW block trimmed")

        // Save commits the lane; the stub transport keeps it for the next launch.
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.isEnabled)
        save.tap()
        let saved = NSPredicate(format: "isEnabled == false")
        expectation(for: saved, evaluatedWith: save)
        waitForExpectations(timeout: 10)

        // Reopen: a new launch loads the saved drawings from the stub's store, and the block is there.
        app.terminate()
        let reopened = launch(reset: false)
        XCTAssertTrue(reopened.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        XCTAssertTrue(drawingBlock(in: reopened).waitForExistence(timeout: 5), "the saved drawing survives a reopen")
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
        attach(reopened, "Reopened with the saved drawing")
    }

    func testCancelLeavesNoDrawingAndTheToolIsHiddenWithoutTheCapability() {
        let app = launch(reset: true)
        XCTAssertTrue(app.buttons["native-editor-tool-draw"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-draw"].tap()
        let canvas = app.descendants(matching: .any)["native-editor-draw-canvas"].firstMatch
        XCTAssertTrue(canvas.waitForExistence(timeout: 5))
        canvas.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.5))
            .press(forDuration: 0.05, thenDragTo: canvas.coordinate(withNormalizedOffset: CGVector(dx: 0.6, dy: 0.5)))
        app.buttons["native-editor-draw-cancel"].tap()
        XCTAssertFalse(drawingBlock(in: app).exists)
        XCTAssertFalse(app.buttons["native-editor-save"].isEnabled, "cancelling is not an edit")

        // The same editor on a variant that does not open the lane never shows the tool or the menu row.
        app.terminate()
        let plain = XCUIApplication()
        plain.launchArguments = ["-ui-testing-editor"]
        plain.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        plain.launch()
        XCTAssertTrue(plain.buttons["native-editor-tool-text"].waitForExistence(timeout: 20))
        XCTAssertFalse(plain.buttons["native-editor-tool-draw"].exists)
        plain.buttons["native-editor-add-clip"].tap()
        XCTAssertTrue(plain.descendants(matching: .any)["native-editor-add-menu-text"].waitForExistence(timeout: 3))
        XCTAssertFalse(plain.descendants(matching: .any)["native-editor-add-menu-drawing"].exists)
    }
}
