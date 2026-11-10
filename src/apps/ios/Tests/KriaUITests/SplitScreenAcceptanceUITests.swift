import XCTest
import UIKit

@MainActor
final class SplitScreenAcceptanceUITests: XCTestCase {
    func testCustomThreePanelLayoutSaveReopenAndFullscreenBoundary() throws {
        let app = launch(reset: true)
        let first = clip(1, in: app)
        XCTAssertTrue(first.waitForExistence(timeout: 30))
        first.tap()
        createSplit(panels: 3, in: app)
        fillEmptyPanel("Middle", withFootage: "Clip 2", in: app) // the blue clip
        fillEmptyPanel("Bottom", withFootage: "Clip 1", in: app) // red again
        let layout = app.buttons["native-editor-split-layout-toggle"]
        XCTAssertTrue(layout.waitForExistence(timeout: 10))
        layout.tap()
        reveal(app.sliders["native-editor-split-gutter"], in: app)
            .adjust(toNormalizedSliderPosition: 0.5)
        app.buttons["native-editor-split-done"].tap()

        let authored = try sections(app)
        let section = try XCTUnwrap((authored["split_screen_sections"] as? [[String: Any]])?.first)
        let weights = try XCTUnwrap(section["panel_weights"] as? [Double])
        XCTAssertEqual(weights.count, 3)
        XCTAssertEqual(try XCTUnwrap(section["duration_s"] as? Double), 4, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(section["gutter"] as? Double), 0.05, accuracy: 0.006)
        XCTAssertEqual((authored["timeline_slots"] as? [Any])?.count, 4)
        try assertAuthoredDuration(authored)
        assertPanelPicture(app)
        attach(app, name: "Customized three panels before Save")
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled)
        save.tap()
        expectation(for: NSPredicate(format: "label == %@ AND isEnabled == false", "Saved"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
        app.terminate()

        let reopened = launch(reset: false)
        XCTAssertTrue(clip(1, in: reopened).waitForExistence(timeout: 30))
        let persisted = try sections(reopened)
        try assertAuthoredDuration(persisted)
        for key in ["timeline_slots", "split_screen_sections"] {
            XCTAssertEqual(try canonical(authored[key]), try canonical(persisted[key]), key)
        }
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled)
        assertPanelPicture(reopened)
        attach(reopened, name: "Customized three panels after relaunch")

        selectClip(4, in: reopened)
        assertPicture(reopened, redAt: [], blueAt: [0.1, 0.3, 0.5, 0.7, 0.9], blackAt: [])
        XCTAssertEqual(reopened.buttons["native-editor-play-pause"].label, "Play preview")
        attach(reopened, name: "Full-screen following clip at section end")
        selectClip(1, in: reopened)
        assertPanelPicture(reopened)
        XCTAssertEqual(try canonical(persisted), try canonical(sections(reopened)),
                       "Seeking across the boundary must not change persisted media or layout")
    }

    // Failure modes: canvas layers become panel-local; seek mutates timing/ownership;
    // preview serves the old side of the cut; Save/reopen drops a layer definition.
    // Known input colors/positions are the pixel oracle, not the compiler's own output.
    func testGlobalLayersStayOnCanvasWhileScrubbingAcrossSplitBoundary() throws {
        let app = launch(reset: true, globalLayers: true)
        XCTAssertTrue(clip(1, in: app).waitForExistence(timeout: 30))
        clip(1, in: app).tap()
        createSplit(panels: 2, in: app)
        fillEmptyPanel("Bottom", withFootage: "Clip 1", in: app)
        app.buttons["native-editor-split-done"].tap()
        let baseline = try sections(app)
        try checkGlobalBoundary(app, baseline: baseline)
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.isEnabled)
        save.tap()
        expectation(for: NSPredicate(format: "label == %@ AND isEnabled == false", "Saved"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
        app.terminate()
        let reopened = launch(reset: false, globalLayers: true)
        XCTAssertTrue(clip(1, in: reopened).waitForExistence(timeout: 30))
        let persisted = try sections(reopened)
        for key in ["timeline_slots", "split_screen_sections", "text_elements", "visual_blocks", "drawings"] {
            XCTAssertEqual(try canonical(baseline[key]), try canonical(persisted[key]), key)
        }
        try checkGlobalBoundary(reopened, baseline: persisted)
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled)
    }

    private func checkGlobalBoundary(_ app: XCUIApplication, baseline: [String: Any]) throws {
        selectClip(3, in: app) // Serial full-screen clip starts at 4 seconds.
        let serial = clip(3, in: app)
        let pixelsPerSecond = serial.frame.width / 2
        func scrub(_ seconds: Double) {
            // The playhead maps rightward drag to later time; background panning uses the opposite sign.
            let playhead = app.descendants(matching: .any)["native-editor-playhead"].firstMatch
            XCTAssertTrue(playhead.isHittable)
            let start = playhead.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(
                CGVector(dx: seconds * pixelsPerSecond, dy: 0)), withVelocity: 100, thenHoldForDuration: 0.2)
        }
        func time() throws -> Double {
            let raw = try XCTUnwrap(app.descendants(matching: .any)["native-editor-current-time"].firstMatch.value as? String)
            let parts = raw.split(separator: ":").compactMap { Double($0) }
            XCTAssertEqual(parts.count, 2)
            return (parts.first ?? 0) * 60 + (parts.last ?? 0)
        }
        // A fill covers its panel for 3 s (the default added-clip length), so stay inside that window.
        scrub(-1.5)
        XCTAssertLessThan(try time(), 3)
        assertGlobalPixels(app, redBackground: true)
        attach(app, name: "Global layers before split boundary")
        scrub(2)
        XCTAssertGreaterThan(try time(), 4)
        XCTAssertLessThan(try time(), 6)
        assertGlobalPixels(app, redBackground: false)
        attach(app, name: "Global layers after split boundary")
        XCTAssertEqual(try canonical(baseline), try canonical(sections(app)),
                       "Seeking must leave the complete document unchanged")
    }

    private func assertGlobalPixels(_ app: XCUIApplication, redBackground: Bool) {
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let predicate = NSPredicate { _, _ in
            guard let image = app.screenshot().image.cgImage else { return false }
            let frame = preview.frame
            let scale = CGFloat(image.width) / app.frame.width
            func count(_ box: CGRect, matching: (UInt8, UInt8, UInt8) -> Bool) -> Int {
                let rect = CGRect(x: (frame.minX + box.minX * frame.width) * scale,
                    y: (frame.minY + box.minY * frame.height) * scale,
                    width: box.width * frame.width * scale, height: box.height * frame.height * scale)
                guard let crop = image.cropping(to: rect) else { return 0 }
                var bytes = [UInt8](repeating: 0, count: crop.width * crop.height * 4)
                guard let context = CGContext(data: &bytes, width: crop.width, height: crop.height,
                    bitsPerComponent: 8, bytesPerRow: crop.width * 4, space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return 0 }
                context.draw(crop, in: CGRect(x: 0, y: 0, width: crop.width, height: crop.height))
                return stride(from: 0, to: bytes.count, by: 4).filter {
                    matching(bytes[$0], bytes[$0 + 1], bytes[$0 + 2])
                }.count
            }
            let green = count(CGRect(x: 0.15, y: 0.47, width: 0.1, height: 0.06)) { $0 < 50 && $1 > 200 && $2 < 50 }
            let magenta = count(CGRect(x: 0.73, y: 0.48, width: 0.14, height: 0.04)) { $0 > 200 && $1 < 50 && $2 > 200 }
            let text = count(CGRect(x: 0.25, y: 0.15, width: 0.5, height: 0.1)) { $0 > 220 && $1 > 220 && $2 > 220 }
            let background = count(CGRect(x: 0.45, y: 0.65, width: 0.1, height: 0.05)) {
                redBackground ? ($0 > 200 && $1 < 50 && $2 < 50) : ($0 < 50 && $1 < 50 && $2 > 200)
            }
            return green > 30 && magenta > 30 && text > 30 && background > 30
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: nil)], timeout: 15), .completed,
                       "Global text, green overlay and magenta drawing keep independently authored canvas positions")
    }

    private func launch(reset: Bool, globalLayers: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-split-screen",
            "-ui-testing-editor-split-persistence", "-ui-testing-editor-document-snapshot",
            "-ui-testing-editor-live-preview", "-ui-testing-editor-color-cuts"]
        if globalLayers { app.launchArguments.append("-ui-testing-editor-split-global-layers") }
        if reset { app.launchArguments.append("-ui-testing-editor-split-persistence-reset") }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    /// The Split tool is the only way in: tool rail, then a layout card. Leaves the panel open on the first empty panel.
    private func createSplit(panels: Int, in app: XCUIApplication) {
        let tool = app.buttons["native-editor-tool-split"]
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        for _ in 0..<3 where !tool.isHittable { rail.swipeLeft() }
        XCTAssertTrue(tool.isHittable, "Split is the last tool on the rail")
        tool.tap()
        let card = app.buttons["native-editor-split-create-\(panels)"]
        XCTAssertTrue(card.waitForExistence(timeout: 10))
        card.tap()
    }

    /// Fills the panel the Split panel is asking for from footage already in the edit (the real pickers need Photos).
    private func fillEmptyPanel(_ panel: String, withFootage label: String, in app: XCUIApplication) {
        XCTAssertTrue(app.staticTexts["Add to \(panel)"].waitForExistence(timeout: 10), "The Split panel must ask for the \(panel) panel")
        let source = app.buttons.matching(NSPredicate(
            format: "identifier BEGINSWITH 'native-editor-split-source-' AND label == %@", label)).firstMatch
        XCTAssertTrue(source.waitForExistence(timeout: 10), "The Split panel must offer \(label) for the empty panel")
        source.tap()
    }

    private func clip(_ index: Int, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-clip-\(index)"].firstMatch
    }

    private func reveal(_ element: XCUIElement, in app: XCUIApplication) -> XCUIElement {
        for _ in 0..<8 where !element.isHittable { app.swipeUp() }
        XCTAssertTrue(element.isHittable)
        return element
    }

    private func selectClip(_ index: Int, in app: XCUIApplication) {
        let target = clip(index, in: app)
        let timeline = app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch
        for _ in 0..<6 where !target.exists {
            let ruler = timeline.coordinate(withNormalizedOffset: CGVector(dx: 0.7, dy: 0.03))
            ruler.press(forDuration: 0.05, thenDragTo: ruler.withOffset(
                CGVector(dx: index == 1 ? 120 : -120, dy: 0)), withVelocity: 100, thenHoldForDuration: 0.2)
        }
        XCTAssertTrue(target.waitForExistence(timeout: 3), "Reveal the clip in the horizontal timeline before selecting it")
        let lanes = app.scrollViews["native-editor-lane-scroll"]
        let toolbar = app.descendants(matching: .any)["native-editor-clip-context"].firstMatch
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let bottom = toolbar.exists ? toolbar.frame.minY : rail.frame.minY
        for _ in 0..<6 where target.frame.maxY >= bottom || target.frame.minY < lanes.frame.minY {
            let direction: CGFloat = target.frame.minY < lanes.frame.minY ? 90 : -90
            let start = app.coordinate(withNormalizedOffset: .zero)
                .withOffset(CGVector(dx: lanes.frame.minX + 24, dy: min(bottom - 16, lanes.frame.maxY - 16)))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 0, dy: direction)))
        }
        XCTAssertTrue(target.isHittable)
        target.tap()
        XCTAssertTrue(target.isSelected)
    }

    private func sections(_ app: XCUIApplication) throws -> [String: Any] {
        let probe = app.descendants(matching: .any)["native-editor-test-document"].firstMatch
        XCTAssertTrue(probe.waitForExistence(timeout: 5))
        let value = try XCTUnwrap(probe.value as? String)
        let attachment = XCTAttachment(string: value)
        attachment.name = "Split-screen persisted document"
        attachment.lifetime = .keepAlways
        add(attachment)
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any])
        let payload = try XCTUnwrap(document["editor_payload"] as? [String: Any])
        return try XCTUnwrap(payload["sections"] as? [String: Any])
    }

    private func canonical(_ value: Any?) throws -> Data {
        try JSONSerialization.data(withJSONObject: XCTUnwrap(value), options: [.sortedKeys, .fragmentsAllowed])
    }

    private func assertAuthoredDuration(_ sections: [String: Any]) throws {
        let layouts = try XCTUnwrap(sections["split_screen_sections"] as? [[String: Any]])
        let rows = try XCTUnwrap(sections["timeline_slots"] as? [[String: Any]])
        let sectionDuration = try layouts.reduce(0.0) { try $0 + XCTUnwrap($1["duration_s"] as? Double) }
        let serialDuration = try rows.filter { $0["split_section_id"] == nil }.reduce(0.0) {
            try $0 + XCTUnwrap($1["duration_s"] as? Double)
        }
        XCTAssertEqual(sectionDuration + serialDuration, 6, accuracy: 0.001,
                       "The branded playback outro must not extend authored footage")
    }

    // Equal panel heights with a 5.5% gutter: panels span 0-0.297, 0.352-0.648 and 0.703-1. A 3pt white divider line
    // (with its swap handle) sits on each gutter's centre, so black is sampled beside it, not on it.
    private func assertPanelPicture(_ app: XCUIApplication) {
        assertPicture(app, redAt: [0.15, 0.85], blueAt: [0.5], blackAt: [0.31, 0.34, 0.66, 0.69])
    }

    private func assertPicture(_ app: XCUIApplication, redAt: [CGFloat], blueAt: [CGFloat], blackAt: [CGFloat]) {
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let predicate = NSPredicate { _, _ in
            guard let image = app.screenshot().image.cgImage else { return false }
            let scale = CGFloat(image.width) / app.frame.width
            let frame = preview.frame
            func matches(_ y: CGFloat, _ color: (UInt8, UInt8, UInt8) -> Bool) -> Bool {
                let rect = CGRect(x: (frame.minX + frame.width * 0.25 - 2) * scale, y: (frame.minY + frame.height * y - 2) * scale,
                                  width: 4 * scale, height: 4 * scale)
                guard let crop = image.cropping(to: rect) else { return false }
                var bytes = [UInt8](repeating: 0, count: crop.width * crop.height * 4)
                guard let context = CGContext(data: &bytes, width: crop.width, height: crop.height,
                    bitsPerComponent: 8, bytesPerRow: crop.width * 4,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
                context.draw(crop, in: CGRect(x: 0, y: 0, width: crop.width, height: crop.height))
                let matching = stride(from: 0, to: bytes.count, by: 4).filter {
                    color(bytes[$0], bytes[$0 + 1], bytes[$0 + 2])
                }.count
                return matching * 4 > bytes.count * 3 / 4
            }
            return redAt.allSatisfy { matches($0) { r, g, b in r > 190 && g < 120 && b < 130 && Int(r) > Int(b) + 60 } } // the selected panel wears a light wash
                && blueAt.allSatisfy { matches($0) { r, g, b in b > 200 && r < 50 && g < 50 } }
                && blackAt.allSatisfy { matches($0) { r, g, b in r < 35 && g < 35 && b < 35 } }
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: nil)], timeout: 15), .completed,
                       "Rendered panel colors and dividers must match the customized layout")
    }

    private func attach(_ app: XCUIApplication, name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
