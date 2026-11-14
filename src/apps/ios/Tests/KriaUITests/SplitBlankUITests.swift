import XCTest
import UIKit

// KRI-1421 blank block in a split panel. Failure modes these journeys exist to catch (written before the journeys):
// - "Fill with blank" is missing without the server's `split_blank_panel` capability, or present while it is off.
// - Fill writes a source clip, a clip index, or no colour, or lands in a different panel than the row that was tapped.
// - The block does not start as the section background, so filling an empty panel looks like nothing happened.
// - A swatch recolours the section background instead of the block, or the change is more than one undo step.
// - The saved document keeps the old colour after undo.
// Every assertion reads the persisted document, not only what is on screen, and the screenshot is the evidence.
@MainActor
final class SplitBlankUITests: XCTestCase {
    private struct Doc {
        let rows: [[String: Any]]
        let sections: [[String: Any]]
        var blanks: [[String: Any]] { rows.filter { $0["media_kind"] as? String == "blank" } }
    }

    func testFillAnEmptyPanelWithBlankAndRecolourIt() throws {
        let app = launch(["-ui-testing-editor-split-screen", "-ui-testing-editor-split-blank"])
        clip(1, in: app).tap()
        createSplit(in: app)

        let fillBottom = app.buttons["native-editor-split-blank-1"]
        XCTAssertTrue(fillBottom.waitForExistence(timeout: 10), "Fill with blank is offered on the empty Bottom panel")
        XCTAssertEqual(fillBottom.label, "Fill Bottom with blank")
        fillBottom.tap()

        let filled = try snapshot(app)
        let block = try XCTUnwrap(filled.blanks.first, "The block is saved as a slot of kind blank")
        XCTAssertEqual(filled.blanks.count, 1)
        XCTAssertNil(block["clip_index"], "A blank has no source")
        XCTAssertEqual(block["split_panel_index"] as? Int, 1)
        XCTAssertEqual(block["split_offset_s"] as? Double ?? -1, 0, accuracy: 0.001)
        let section = try XCTUnwrap(filled.sections.first)
        XCTAssertEqual(block["duration_s"] as? Double ?? -1, section["duration_s"] as? Double ?? -2, accuracy: 0.001, "It fills the panel's free time")
        XCTAssertEqual(block["color"] as? String, section["background_color"] as? String, "A new block starts as the section background")
        attach(app, "Bottom panel filled with a blank block")

        let green = app.buttons["native-editor-split-blank-color-green"]
        XCTAssertTrue(green.waitForExistence(timeout: 10), "The selected block shows its colour swatches")
        green.tap()
        let recoloured = try snapshot(app)
        XCTAssertEqual(recoloured.blanks.first?["color"] as? String, "#00FF00")
        XCTAssertEqual(recoloured.sections.first?["background_color"] as? String, section["background_color"] as? String, "The section background is untouched")
        attach(app, "Blank block recoloured green")

        let done = app.buttons["native-editor-split-done"]
        if done.exists { done.tap() }
        app.buttons["native-editor-undo"].tap()
        XCTAssertEqual(try snapshot(app).blanks.first?["color"] as? String, block["color"] as? String, "One undo restores the colour")
    }

    func testFillWithBlankIsAbsentUntilTheServerOffersIt() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        clip(1, in: app).tap()
        createSplit(in: app)
        XCTAssertTrue(app.staticTexts["Add to Bottom"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["native-editor-split-blank-1"].exists)
        XCTAssertTrue(try snapshot(app).blanks.isEmpty)
    }

    // MARK: Helpers

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-live-preview",
                               "-ui-testing-editor-document-snapshot"] + extra
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(clip(1, in: app).waitForExistence(timeout: 30))
        return app
    }

    private func clip(_ number: Int, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-clip-\(number)"].firstMatch
    }

    private func createSplit(in app: XCUIApplication) {
        let tool = app.buttons["native-editor-tool-split"]
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        for _ in 0..<3 where !tool.isHittable { rail.swipeLeft() }
        XCTAssertTrue(tool.isHittable, "Split is the last tool on the rail")
        tool.tap()
        let scope = app.buttons["native-editor-split-scope-clip"]
        if scope.waitForExistence(timeout: 10) { scope.tap() }
        let card = app.buttons["native-editor-split-create-2"]
        XCTAssertTrue(card.waitForExistence(timeout: 10))
        card.tap()
    }

    private func snapshot(_ app: XCUIApplication) throws -> Doc {
        let probe = app.descendants(matching: .any)["native-editor-test-document"].firstMatch
        XCTAssertTrue(probe.waitForExistence(timeout: 5))
        let value = try XCTUnwrap(probe.value as? String)
        let attachment = XCTAttachment(string: value)
        attachment.name = "Blank block persisted document"
        attachment.lifetime = .keepAlways
        add(attachment)
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any])
        let payload = try XCTUnwrap(root["editor_payload"] as? [String: Any])
        let sections = try XCTUnwrap(payload["sections"] as? [String: Any])
        return Doc(rows: try XCTUnwrap(sections["timeline_slots"] as? [[String: Any]]),
                   sections: sections["split_screen_sections"] as? [[String: Any]] ?? [])
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
