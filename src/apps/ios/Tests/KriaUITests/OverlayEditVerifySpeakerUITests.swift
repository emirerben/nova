import XCTest

// MARK: - Speaker frame

/// The corner speaker of a Talking green-screen edit (existing seeded fixture) gets the same four actions.
@MainActor
final class OverlayEditVerifySpeakerUITests: XCTestCase {
    func testSpeakerFrame() {
        let kind = OverlayEditVerifyUITests.Kind(name: "speaker frame", tag: "speaker", idA: "speaker", idB: "speaker", drag: CGVector(dx: -0.15, dy: -0.2))
        let h = Harness(self, kind)
        let app = h.app
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speaker-background", "-ui-testing-editor-speaker-background-windows",
                               "-ui-testing-editor-speaker-frame", "-ui-testing-editor-live-preview", "-ui-testing-editor-speaker-background-reset"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(h.canvas.waitForExistence(timeout: 30))
        let speaker = app.descendants(matching: .any)["native-editor-preview-speaker-speaker"].firstMatch
        XCTAssertTrue(speaker.waitForExistence(timeout: 20))
        let ready = NSPredicate { _, _ in (speaker.value as? String ?? "").contains("position") }
        expectation(for: ready, evaluatedWith: nil)
        waitForExpectations(timeout: 30)
        h.shot("speaker-0-start")
        guard let p0 = h.read("speaker") else { return }

        let point = h.canvas.coordinate(withNormalizedOffset: CGVector(dx: Double(p0.x) / 100, dy: Double(p0.y) / 100))
        point.tap()
        let selected = h.isSelected("speaker")
        h.shot("speaker-1-after-tap")
        Verify.record("speaker frame", "tap selects", selected, "start pos \(p0.x)%,\(p0.y)% size \(p0.size)%; panel: \(app.descendants(matching: .any)["native-editor-you-panel"].firstMatch.exists)")
        XCTAssertTrue(selected)

        let cs = h.canvas.frame
        point.press(forDuration: 0.15, thenDragTo: point.withOffset(CGVector(dx: kind.drag.dx * cs.width, dy: kind.drag.dy * cs.height)))
        h.shot("speaker-2-after-drag")
        if let p1 = h.read("speaker") {
            let c = h.canvas.frame
            let ex = Double(kind.drag.dx) * 100, ey = Double(kind.drag.dy) * 100; _ = c
            let gx = Double(p1.x - p0.x), gy = Double(p1.y - p0.y)
            let ok = abs(gx - ex) <= 3 && abs(gy - ey) <= 3
            Verify.record("speaker frame", "drag moves by about the drag distance", ok,
                          "expected (\(String(format: "%.1f", ex)), \(String(format: "%.1f", ey)))%, measured (\(String(format: "%.1f", gx)), \(String(format: "%.1f", gy)))%; pos \(p0.x),\(p0.y) -> \(p1.x),\(p1.y)")
            XCTAssertTrue(ok)
        }

        // The speaker box is square in canvas fractions (width = size%), so its bottom-right handle is centre + size/2.
        if let before = h.read("speaker"),
           case let corner = h.canvas.coordinate(withNormalizedOffset: CGVector(dx: Double(before.x) / 100 + Double(before.size) / 200, dy: Double(before.y) / 100 + Double(before.size) / 200)) {
            corner.press(forDuration: 0.15, thenDragTo: corner.withOffset(CGVector(dx: 40, dy: 40)))
            h.shot("speaker-3-after-corner-drag")
            let after = h.read("speaker")
            let ok = (after?.size ?? before.size) != before.size
            Verify.record("speaker frame", "corner drag changes size", ok, "size \(before.size)% -> \(after.map { String($0.size) } ?? "?")%")
            XCTAssertTrue(ok)
        }
        let beforePinch = h.read("speaker")?.size ?? 0
        h.canvas.pinch(withScale: 0.7, velocity: -1)
        h.shot("speaker-4-after-pinch")
        let afterPinch = h.read("speaker")?.size
        let ok = (afterPinch ?? beforePinch) != beforePinch
        Verify.record("speaker frame", "pinch changes size", ok, "pinch 0.7: size \(beforePinch)% -> \(afterPinch.map(String.init) ?? "?")%")
        XCTAssertTrue(ok)

        Verify.record("speaker frame", "repeat tap reaches the stacked item underneath", false,
                      "NOT TESTED: the seeded speaker fixture has no second item at the speaker's spot")
    }
}
