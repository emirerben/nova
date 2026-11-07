import XCTest
import UIKit

@MainActor
final class SplitScreenAcceptanceUITests: XCTestCase {
    func testCustomThreePanelLayoutSaveReopenAndFullscreenBoundary() throws {
        let app = launch(reset: true)
        let first = clip(1, in: app)
        XCTAssertTrue(first.waitForExistence(timeout: 30))
        first.tap()
        app.buttons["native-editor-adjust"].tap()
        reveal(app.buttons["native-editor-split-create-3"], in: app).tap()
        let height = reveal(app.sliders["native-editor-split-weight-0"], in: app)
        height.adjust(toNormalizedSliderPosition: 0.7)
        reveal(app.sliders["native-editor-split-gutter"], in: app)
            .adjust(toNormalizedSliderPosition: 0.5)
        app.buttons["native-editor-inspector-done"].tap()

        selectClip(2, in: app)
        app.buttons["native-editor-adjust"].tap()
        reveal(app.buttons["native-editor-split-replace"], in: app).tap()
        XCTAssertTrue(app.buttons["Clip 2"].waitForExistence(timeout: 5))
        app.buttons["Clip 2"].tap()
        app.buttons["native-editor-inspector-done"].tap()
        let authored = try sections(app)
        let layout = try XCTUnwrap((authored["split_screen_sections"] as? [[String: Any]])?.first)
        let weights = try XCTUnwrap(layout["panel_weights"] as? [Double])
        XCTAssertEqual(weights.count, 3)
        XCTAssertGreaterThan(weights[0], weights[1] * 2)
        XCTAssertEqual(try XCTUnwrap(layout["gutter"] as? Double), 0.05, accuracy: 0.006)
        XCTAssertEqual((authored["timeline_slots"] as? [Any])?.count, 4)
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

    private func launch(reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-split-screen",
            "-ui-testing-editor-split-persistence", "-ui-testing-editor-document-snapshot",
            "-ui-testing-editor-live-preview", "-ui-testing-editor-color-cuts"]
        if reset { app.launchArguments.append("-ui-testing-editor-split-persistence-reset") }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
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
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any])
        let payload = try XCTUnwrap(document["editor_payload"] as? [String: Any])
        return try XCTUnwrap(payload["sections"] as? [String: Any])
    }

    private func canonical(_ value: Any?) throws -> Data {
        try JSONSerialization.data(withJSONObject: XCTUnwrap(value), options: [.sortedKeys, .fragmentsAllowed])
    }

    private func assertPanelPicture(_ app: XCUIApplication) {
        assertPicture(app, redAt: [0.2, 0.9], blueAt: [0.7], blackAt: [0.6, 0.8])
    }

    private func assertPicture(_ app: XCUIApplication, redAt: [CGFloat], blueAt: [CGFloat], blackAt: [CGFloat]) {
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let predicate = NSPredicate { _, _ in
            guard let image = app.screenshot().image.cgImage else { return false }
            let scale = CGFloat(image.width) / app.frame.width
            let frame = preview.frame
            func matches(_ y: CGFloat, _ color: (UInt8, UInt8, UInt8) -> Bool) -> Bool {
                let rect = CGRect(x: (frame.midX - 2) * scale, y: (frame.minY + frame.height * y - 2) * scale,
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
            return redAt.allSatisfy { matches($0) { r, g, b in r > 200 && g < 50 && b < 50 } }
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
