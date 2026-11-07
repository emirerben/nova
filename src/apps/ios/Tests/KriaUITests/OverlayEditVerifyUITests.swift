import XCTest

/// Every overlay kind is tapped, moved and resized the same way on the editor preview: tap selects (a repeat tap at the
/// same spot cycles stacked items), a body drag moves, a corner drag or a pinch resizes. One test per kind runs the same
/// script on the fixture `Fixtures/OverlayEditVerifyDraft.json` (two stacked items per kind, all visible at 0 s) and writes
/// a screenshot before/after every action plus one result row per kind x action to `.overlay-edit-verify/` (see
/// `Verify.record`). The fixture reaches the app through the DEBUG-only `-ui-testing-editor-captured-creation` bridge.
@MainActor
final class OverlayEditVerifyUITests: XCTestCase {
    // MARK: kinds

    struct Kind {
        let name: String
        let tag: String          // identifier prefix `native-editor-preview-<tag>-`
        let idA: String
        let idB: String
        let drag: CGVector       // body drag as a fraction of the canvas (chosen to stay off the centre snap lines)
        var device = false
        /// Drawings carry no position/size in their accessibility value: measure the transform layer's frame instead.
        var measuresTransformFrame = false
    }

    static let text = Kind(name: "text", tag: "text", idA: "stack-a", idB: "stack-b", drag: CGVector(dx: 0.2, dy: 0.15))
    static let caption = Kind(name: "caption", tag: "caption_cue", idA: "cue-a", idB: "cue-b", drag: CGVector(dx: 0.12, dy: -0.15))
    static let pipArgs = ["-ui-testing-editor", "-ui-testing-editor-captured-creation", "-ui-testing-editor-overlays"]
    static let pip = Kind(name: "pip card", tag: "media_overlay", idA: "card-a", idB: "card-b", drag: CGVector(dx: 0.15, dy: 0.12))
    static let visual = Kind(name: "visual block", tag: "visual_block", idA: "paper-media", idB: "paper-media", drag: CGVector(dx: -0.15, dy: 0.2))
    static let drawing = Kind(name: "drawing", tag: "drawing", idA: "draw-a", idB: "draw-b", drag: CGVector(dx: 0.15, dy: 0.08), measuresTransformFrame: true)

    // MARK: per kind

    func testText() { run(Self.text) }
    func testCaption() { run(Self.caption) }
    /// The pip card draft goes through the `-ui-testing-editor-overlays` bridge (it installs the source resolver, so a card with no
    /// source in the UI-test pool is skipped instead of failing the whole preview; the card has no picture but is a real, movable
    /// preview object). That bridge also marks the variant device-rendered, so the y<=62% clamp is in force.
    func testPipCard() { run(Self.pip, pipFixture: true) }

    func testVisualBlock() { run(Self.visual, visualFixture: true) }
    func testDrawing() { run(Self.drawing) }

    /// A device-rendered variant pulls a pip card up to y <= 62%: a drag low stops there, not at the finger.
    func testPipCardOnDeviceVariantClampsAt62() {
        let kind = Self.pip
        var h = Harness(self, kind)
        h.fixtureFile = "OverlayEditVerifyPipDraft.json"
        h.customArgs = Self.pipArgs
        h.launch()
        guard let point = h.pointOf(kind.idA, kind: kind) else {
            Verify.record("pip card (device variant)", "drag low clamps at y<=62", false, "card not found"); return
        }
        point.tap()
        h.shot("pip-clamp-1-selected")
        let c = h.canvas.frame
        point.press(forDuration: 0.15, thenDragTo: point.withOffset(CGVector(dx: 0, dy: 0.45 * c.height)))
        h.shot("pip-clamp-2-dragged-to-90-percent")
        let after = h.selectedID().flatMap { h.read($0, kind: kind) }
        let ok = after != nil && after!.y == 62
        Verify.record("pip card (device variant)", "drag low (finger to y=90%) clamps at y<=62", ok, "card y after drag: \(after.map { String($0.y) } ?? "?")% (expected 62)")
        XCTAssertTrue(ok, "device pip card clamps at 62%: \(String(describing: after))")
    }

    /// The handle is a 44x44pt zone centred on the visible dot. The text element's accessibility frame includes the 22pt handle
    /// circle, so the dot centre is the frame's bottom-right minus 11pt on each axis. Probed along x at 0 (on the dot), 21 (just inside the zone edge),
    /// 32, 42 and 52pt from the dot centre (= 10, 20, 30pt beyond the 22pt zone edge): a grab is expected up to 22pt, none beyond.
    func testHandleGrabbedTwentyPointsOutsideTheCorner() {
        for distance in [0.0, 21.0, 32.0, 42.0, 52.0] {
            let kind = Self.text
            let h = Harness(self, kind)
            h.launch()
            guard let point = h.pointOf(kind.idA) else { continue }
            point.tap()
            guard let id = h.selectedID(), let frameCorner = h.cornerPoint(id) else {
                Verify.record(kind.name, "handle probe \(Int(distance))pt from dot centre", false, "nothing selected / no frame"); continue
            }
            let dot = frameCorner.withOffset(CGVector(dx: -11, dy: -11))
            let before = h.sizeSignature(id)
            h.shot("text-handle-\(Int(distance))pt-1-selected")
            let start = dot.withOffset(CGVector(dx: distance, dy: 0))
            start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: 30, dy: 30)))
            h.shot("text-handle-\(Int(distance))pt-2-after-drag")
            let after = h.sizeSignature(id)
            let grabbed = after != before
            let expected = distance <= 22
            let ok = grabbed == expected
            Verify.record(kind.name, "handle grabbed \(Int(distance))pt from the dot centre: \(expected ? "resizes" : "does not resize")", ok,
                          "\(grabbed ? "resized" : "no resize"): \(before) -> \(after)")
            XCTAssertEqual(grabbed, expected, "text handle probe at \(distance)pt")
        }
    }

    // MARK: the script

    private func run(_ kind: Kind, visualFixture: Bool = false, pipFixture: Bool = false) {
        var h = Harness(self, kind)
        if visualFixture {
            h.fixtureFile = "OverlayEditVerifyVisualDraft.json"
            h.customArgs = ["-ui-testing-editor", "-ui-testing-editor-captured-creation", "-ui-testing-editor-caption-visuals"]
        }
        if pipFixture {
            h.fixtureFile = "OverlayEditVerifyPipDraft.json"
            h.customArgs = Self.pipArgs
        }
        h.launch()
        let n = kind.tag
        guard let a0 = h.read(kind.idA, kind: kind), let b0 = h.read(kind.idB, kind: kind) else {
            Verify.record(kind.name, "fixture present", false, "preview objects \(kind.idA) / \(kind.idB) not found")
            XCTFail("\(kind.name) fixture items missing"); return
        }
        h.shot("\(n)-0-start")
        guard let point = h.pointOf(kind.idA, kind: kind) else { return }

        // (1) tap selects
        point.tap()
        let first = h.selectedID()
        h.shot("\(n)-1-after-tap")
        Verify.record(kind.name, "tap selects", first != nil, "A \(fmt(a0)) B \(fmt(b0)); selected: \(first ?? "none")")
        XCTAssertNotNil(first, "\(kind.name): a tap selects an item")

        // (4) stacked items: a repeat tap at the same spot reaches the one underneath
        var second: String? = first
        if kind.idA == kind.idB {
            Verify.record(kind.name, "repeat tap reaches the stacked item underneath", false,
                          "NOT TESTED: no fixture can draw a second visual block (a block's preview source is registered only for id paper-media)")
        } else {
            Thread.sleep(forTimeInterval: 0.8)
            point.tap()
            second = h.selectedID()
            h.shot("\(n)-2-after-second-tap")
            let cycled = first != nil && second != nil && first != second
            Verify.record(kind.name, "repeat tap reaches the stacked item underneath", cycled, "first tap: \(first ?? "none"), second tap: \(second ?? "none")")
            XCTAssertTrue(cycled, "\(kind.name): second tap cycles to the other stacked item")
        }
        guard let working = second ?? first else { return }
        // A stray choice sheet (captions) must never block the next gesture.
        h.dismissCaptionChoice()

        // (2) drag moves
        guard let m0 = h.read(working, kind: kind) else { return }
        let c = h.canvas.frame
        let dragPts = CGVector(dx: kind.drag.dx * c.width, dy: kind.drag.dy * c.height)
        point.press(forDuration: 0.15, thenDragTo: point.withOffset(dragPts))
        h.dismissCaptionChoice()
        h.shot("\(n)-3-after-drag")
        if let m1 = h.read(working, kind: kind) {
            let expectDX = Double(kind.drag.dx) * 100, expectDY = Double(kind.drag.dy) * 100
            let gotDX = Double(m1.x - m0.x), gotDY = Double(m1.y - m0.y)
            let ok = abs(gotDX - expectDX) <= 3 && abs(gotDY - expectDY) <= 3
            Verify.record(kind.name, "drag moves by about the drag distance", ok,
                          "drag (\(Int(dragPts.dx)), \(Int(dragPts.dy)))pt on a \(Int(c.width))x\(Int(c.height))pt canvas = expected (\(String(format: "%.1f", expectDX)), \(String(format: "%.1f", expectDY)))%; measured (\(String(format: "%.1f", gotDX)), \(String(format: "%.1f", gotDY)))%; \(fmt(m0)) -> \(fmt(m1))")
            XCTAssertTrue(ok, "\(kind.name) moved by \(gotDX), \(gotDY) % vs expected \(expectDX), \(expectDY)")
        } else { Verify.record(kind.name, "drag moves by about the drag distance", false, "value unreadable after drag") }

        // (3a) pinch resizes (the pinch goes onto the item itself for a drawing, whose layer owns the gesture)
        let pinchTarget = kind.measuresTransformFrame ? h.app.descendants(matching: .any)["native-editor-drawing-transform"].firstMatch : h.canvas
        let beforePinch = h.sizeSignature(working, kind: kind)
        pinchTarget.pinch(withScale: 0.7, velocity: -1)
        h.dismissCaptionChoice()
        h.shot("\(n)-4-after-pinch")
        let afterPinch = h.sizeSignature(working, kind: kind)
        let pinchOK = afterPinch != beforePinch
        Verify.record(kind.name, "pinch changes size", pinchOK, "pinch scale 0.7: \(beforePinch) -> \(afterPinch)")
        XCTAssertTrue(pinchOK, "\(kind.name): pinch changed size")

        // (3b) corner drag resizes
        let beforeCorner = h.sizeSignature(working, kind: kind)
        var cornerPoint = h.cornerPoint(working, kind: kind)
        if kind.tag == "caption_cue", let p = h.read(working, kind: kind) {
            // A caption's accessibility frame is only its text; its drawn box is about 81% x 23% of the canvas around its centre
            // (measured on the screenshot), with the handle clamped to the canvas edge.
            cornerPoint = h.canvas.coordinate(withNormalizedOffset: CGVector(dx: min(Double(p.x) / 100 + 0.405, 0.98), dy: Double(p.y) / 100 + 0.113))
        }
        guard let corner = cornerPoint else {
            Verify.record(kind.name, "corner drag changes size", false, "no corner frame"); return
        }
        corner.press(forDuration: 0.15, thenDragTo: corner.withOffset(CGVector(dx: 25, dy: 25)))
        h.dismissCaptionChoice()
        h.shot("\(n)-5-after-corner-drag")
        let afterCorner = h.sizeSignature(working, kind: kind)
        let cornerOK = afterCorner != beforeCorner
        Verify.record(kind.name, "corner drag changes size", cornerOK, "\(beforeCorner) -> \(afterCorner)")
        XCTAssertTrue(cornerOK, "\(kind.name): corner drag changed size")
    }

    private func fmt(_ p: Placement?) -> String {
        guard let p else { return "?" }
        return "pos \(p.x)%,\(p.y)% size \(p.size)%"
    }
}

// MARK: - Helpers

struct Placement: Equatable { var x: Int; var y: Int; var size: Int }

enum Verify {
    static let outDir: URL = {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".overlay-edit-verify")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }()

    /// Appends one result row (kind x action, PASS/FAIL, measured numbers) to `results.jsonl`.
    static func record(_ kind: String, _ action: String, _ pass: Bool, _ detail: String) {
        let row: [String: Any] = ["kind": kind, "action": action, "status": pass ? "PASS" : "FAIL", "detail": detail]
        guard let data = try? JSONSerialization.data(withJSONObject: row), var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let file = outDir.appendingPathComponent("results.jsonl")
        if let handle = try? FileHandle(forWritingTo: file) {
            handle.seekToEndOfFile(); handle.write(Data(line.utf8)); try? handle.close()
        } else { try? line.write(to: file, atomically: true, encoding: .utf8) }
    }
}

@MainActor
struct Harness {
    let test: XCTestCase
    let kind: OverlayEditVerifyUITests.Kind
    let app = XCUIApplication()
    var customArgs: [String]?
    var fixtureFile = "OverlayEditVerifyDraft.json"

    init(_ test: XCTestCase, _ kind: OverlayEditVerifyUITests.Kind) { self.test = test; self.kind = kind }

    /// Several elements share the preview identifier; the canvas is the largest of them.
    var canvas: XCUIElement {
        let all = app.descendants(matching: .any).matching(identifier: "native-editor-preview").allElementsBoundByIndex
        return all.max { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height } ?? app.descendants(matching: .any)["native-editor-preview"].firstMatch
    }

    func launch() {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(fixtureFile)").path
        app.launchArguments = customArgs ?? (["-ui-testing-editor", "-ui-testing-editor-captured-creation"] + (kind.device ? ["-ui-testing-editor-device"] : []))
        app.launchEnvironment["KRIA_UI_FIXTURE_DRAFT"] = fixture
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(canvas.waitForExistence(timeout: 30), "editor canvas")
        // Give the source preview time to build so the items are directly manipulable.
        Thread.sleep(forTimeInterval: 8)
        shot("launch-settled")
    }

    func element(_ id: String, tag: String? = nil) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-preview-\(tag ?? kind.tag)-\(id)"].firstMatch
    }

    private func value(of id: String) -> String { element(id).value as? String ?? "" }

    /// "value size|drawn width in points": caption values never change their size text, so the drawn width is the second signal.
    func sizeSignature(_ id: String, kind k: OverlayEditVerifyUITests.Kind? = nil) -> String {
        let kk = k ?? kind
        let drawn = kk.measuresTransformFrame ? app.descendants(matching: .any)["native-editor-drawing-transform"].firstMatch : element(id, tag: kk.tag)
        return "value \(read(id, kind: kk)?.size ?? -9)% / drawn \(Int(drawn.frame.width))x\(Int(drawn.frame.height))pt"
    }

    func isSelected(_ id: String) -> Bool { value(of: id).hasSuffix("selected") }

    func selectedID() -> String? {
        [kind.idA, kind.idB].first { isSelected($0) }
    }

    /// Reads "position X%, Y%, size N%" from the value. Drawings fall back to their transform layer's frame.
    func read(_ id: String, kind k: OverlayEditVerifyUITests.Kind? = nil) -> Placement? {
        let v = value(of: id)
        func numbers(after key: String) -> [Int] {
            guard let tail = v.components(separatedBy: key).dropFirst().first else { return [] }
            let upToNextKey = tail.components(separatedBy: ", ").prefix(key == "position " ? 2 : 1).joined(separator: ", ")
            return upToNextKey.components(separatedBy: CharacterSet.decimalDigits.inverted).compactMap { Int($0) }
        }
        let pos = numbers(after: "position "), size = numbers(after: "size ")
        if pos.count >= 2, let s = size.first { return Placement(x: pos[0], y: pos[1], size: s) }
        if (k ?? kind).measuresTransformFrame {
            let t = app.descendants(matching: .any)["native-editor-drawing-transform"].firstMatch
            let c = canvas.frame
            if t.exists, c.width > 0 {
                return Placement(x: Int(((t.frame.midX - c.minX) / c.width * 100).rounded()),
                                 y: Int(((t.frame.midY - c.minY) / c.height * 100).rounded()),
                                 size: Int((t.frame.width / c.width * 100).rounded()))
            }
            // Before selection: the drawing is only on the canvas; no numbers in the value.
            return Placement(x: 50, y: 62, size: 0)
        }
        // A kind without the position or size in its value: report what exists.
        if pos.count >= 2 { return Placement(x: pos[0], y: pos[1], size: -1) }
        return nil
    }

    /// Where the item is drawn: its reported centre on the canvas. Drawings start at the fixture's centre (50%, 62%).
    func pointOf(_ id: String, kind k: OverlayEditVerifyUITests.Kind? = nil) -> XCUICoordinate? {
        guard let p = read(id, kind: k) else { return nil }
        return canvas.coordinate(withNormalizedOffset: CGVector(dx: Double(p.x) / 100, dy: Double(p.y) / 100))
    }

    /// The visible bottom-right corner of the selected item, in screen coordinates.
    func cornerPoint(_ id: String, kind k: OverlayEditVerifyUITests.Kind? = nil) -> XCUICoordinate? {
        let source = (k ?? kind).measuresTransformFrame ? app.descendants(matching: .any)["native-editor-drawing-transform"].firstMatch : element(id, tag: (k ?? kind).tag)
        guard source.exists else { return nil }
        let f = source.frame
        let origin = app.coordinate(withNormalizedOffset: .zero)
        // A drawing's transform frame includes its 22pt grab margin: the drawn corner is that far inside.
        let inset: CGFloat = (k ?? kind).measuresTransformFrame ? 22 : 0
        return origin.withOffset(CGVector(dx: f.maxX - inset, dy: f.maxY - inset))
    }

    func dismissCaptionChoice() {
        let choice = app.buttons["All captions in this video"]
        if choice.waitForExistence(timeout: 1) { choice.tap() }
    }

    func shot(_ name: String) {
        let screenshot = app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name; attachment.lifetime = .keepAlways
        test.add(attachment)
        try? screenshot.pngRepresentation.write(to: Verify.outDir.appendingPathComponent("\(name).png"))
    }
}
