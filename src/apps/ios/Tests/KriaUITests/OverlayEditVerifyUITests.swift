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

    // MARK: drawing regressions (aspect mismatch, single outline)

    static let wide = Kind(name: "drawing (16:9 on a 9:16 canvas)", tag: "drawing", idA: "draw-wide", idB: "draw-wide", drag: CGVector(dx: 0.15, dy: 0.1), measuresTransformFrame: true)

    private func wideHarness() -> Harness {
        var h = Harness(self, Self.wide)
        h.fixtureFile = "OverlayEditVerifyWideDrawingDraft.json"
        h.customArgs = ["-ui-testing-editor", "-ui-testing-editor-captured-creation"]
        h.launch()
        return h
    }

    private func transform(_ h: Harness) -> XCUIElement { h.app.descendants(matching: .any)["native-editor-drawing-transform"].firstMatch }

    private func drawingIsSelected(_ h: Harness) -> Bool {
        transform(h).exists || h.isSelected("draw-wide")
    }

    /// A 16:9 drawing on the 9:16 canvas: its transform frame sits in the letterboxed band, a tap on the ink selects it, a tap on the
    /// empty top or bottom bar does not, and a drag still moves it by about the drag distance.
    func testDrawingOnAWideCanvasStaysInItsBand() {
        let kind = Self.wide
        let name = kind.name
        let h = wideHarness()
        h.shot("wide-0-start")
        let c0 = h.canvas.frame
        // Empty bars: the fitted band is 0.5625/1.7778 = 31.6% of the canvas height, centred, so 12% and 88% are outside it.
        for (label, y) in [("top bar", 0.12), ("bottom bar", 0.88)] {
            let c = h.canvas.frame
            h.canvas.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y)).tap()
            let selected = drawingIsSelected(h)
            h.shot("wide-1-tap-\(label.replacingOccurrences(of: " ", with: "-"))")
            Verify.record(name, "tap on the empty \(label) does not select", !selected,
                          "tapped (50%, \(Int(y * 100))%) of the \(Int(c.width))x\(Int(c.height))pt canvas; drawing selected: \(selected)")
            XCTAssertFalse(selected, "tap on the \(label) selected the drawing")
        }
        h.canvas.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        let selected = drawingIsSelected(h)
        h.shot("wide-2-tap-on-ink")
        Verify.record(name, "tap on the visible ink selects", selected, "tapped canvas centre (the ink's centre); selected: \(selected)")
        XCTAssertTrue(selected, "tap on the ink selects the drawing")
        guard selected, transform(h).waitForExistence(timeout: 3) else { return }

        // Band check, in the (possibly resized) current canvas.
        let c = h.canvas.frame
        let f = transform(h).frame
        let bandH = c.height * 0.5625 / 1.7778
        let bandTop = c.midY - bandH / 2, bandBottom = c.midY + bandH / 2
        let centreOK = f.midY >= bandTop && f.midY <= bandBottom
        let insideCanvas = f.minX >= c.minX - 22 - 2 && f.maxX <= c.maxX + 22 + 2
        // The frame includes a 22pt grab margin on every side.
        let marginOK = f.minY >= bandTop - 22 - 2 && f.maxY <= bandBottom + 22 + 2
        Verify.record(name, "transform frame sits inside the letterboxed band", centreOK && marginOK && insideCanvas,
                      "canvas y \(Int(c.minY))...\(Int(c.maxY)) (h \(Int(c.height))pt); band y \(Int(bandTop))...\(Int(bandBottom)) (h \(Int(bandH))pt); frame y \(Int(f.minY))...\(Int(f.maxY)) centre \(Int(f.midY)); frame x \(Int(f.minX))...\(Int(f.maxX)) vs canvas x \(Int(c.minX))...\(Int(c.maxX))")
        XCTAssertTrue(centreOK && marginOK && insideCanvas, "wide drawing frame outside its band")

        // Drag moves by about the drag distance.
        guard let before = h.read("draw-wide", kind: kind) else { return }
        let centre = h.app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: f.midX, dy: f.midY))
        centre.press(forDuration: 0.15, thenDragTo: centre.withOffset(CGVector(dx: kind.drag.dx * c.width, dy: kind.drag.dy * c.height)))
        h.shot("wide-3-after-drag")
        guard let after = h.read("draw-wide", kind: kind) else { return }
        let gx = Double(after.x - before.x), gy = Double(after.y - before.y)
        let ok = abs(gx - 15) <= 3 && abs(gy - 10) <= 3
        Verify.record(name, "drag moves by about the drag distance", ok, "expected (15.0, 10.0)%; measured (\(gx), \(gy))%; frame centre \(before.x),\(before.y) -> \(after.x),\(after.y)")
        XCTAssertTrue(ok, "wide drawing moved by \(gx), \(gy)")
        _ = c0
    }

    /// Counts the selection outline of the selected drawing while it is dragged and pinched (the gesture runs on a background queue
    /// and is held, the main thread samples mid-gesture) and after release. The preview object for a drawing draws no outline (the app skips it
    /// for the drawing kind), so the only outline element is native-editor-drawing-transform.
    func testDrawingHasOneOutlineDuringDragAndPinch() {
        let name = "drawing (single outline)"
        let h = wideHarness()
        h.canvas.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        guard transform(h).waitForExistence(timeout: 5) else {
            Verify.record(name, "drawing selected", false, "no transform layer after tap"); XCTFail("not selected"); return
        }
        func outlineCount() -> (transform: Int, previewValue: String) {
            let n = h.app.descendants(matching: .any).matching(identifier: "native-editor-drawing-transform").count
            let v = h.app.descendants(matching: .any)["native-editor-preview-drawing-draw-wide"].firstMatch.value as? String ?? "?"
            return (n, v)
        }
        let rest = outlineCount()
        h.shot("outline-0-selected-at-rest")
        Verify.record(name, "one outline at rest", rest.transform == 1, "transform elements: \(rest.transform); preview drawing value: \(rest.previewValue)")
        XCTAssertEqual(rest.transform, 1)

        // Mid-drag.
        let c = h.canvas.frame, f = transform(h).frame
        let centre = h.app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: f.midX, dy: f.midY))
        let end = centre.withOffset(CGVector(dx: 0.15 * c.width, dy: 0.1 * c.height))
        // The gesture call blocks the test thread while it spins the run loop; a block scheduled on the main queue runs in the middle of it.
        var midDrag: (transform: Int, previewValue: String) = (-1, "not sampled")
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            midDrag = outlineCount()
            h.shot("outline-1-mid-drag")
        }
        centre.press(forDuration: 0.15, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 6)
        let afterDrag = outlineCount()
        h.shot("outline-2-after-drag-release")
        Verify.record(name, "one outline mid-drag", midDrag.transform == 1, "transform elements: \(midDrag.transform); preview drawing value: \(midDrag.previewValue)")
        Verify.record(name, "one outline after drag release", afterDrag.transform == 1, "transform elements: \(afterDrag.transform); preview drawing value: \(afterDrag.previewValue)")
        XCTAssertEqual(midDrag.transform, 1); XCTAssertEqual(afterDrag.transform, 1)

        // Mid-pinch (slow pinch on the drawing).
        var midPinch: (transform: Int, previewValue: String) = (-1, "not sampled")
        let target = transform(h)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            midPinch = outlineCount()
            h.shot("outline-3-mid-pinch")
        }
        target.pinch(withScale: 2.0, velocity: 0.25)
        let afterPinch = outlineCount()
        h.shot("outline-4-after-pinch-release")
        Verify.record(name, "one outline mid-pinch", midPinch.transform == 1, "transform elements: \(midPinch.transform); preview drawing value: \(midPinch.previewValue)")
        Verify.record(name, "one outline after pinch release", afterPinch.transform == 1, "transform elements: \(afterPinch.transform); preview drawing value: \(afterPinch.previewValue)")
        XCTAssertEqual(midPinch.transform, 1); XCTAssertEqual(afterPinch.transform, 1)
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
    /// Evidence lives outside the git tree: the test runner's temp directory (override with OVERLAY_EDIT_VERIFY_DIR).
    static let outDir: URL = {
        let override = ProcessInfo.processInfo.environment["OVERLAY_EDIT_VERIFY_DIR"]
        let url = override.map { URL(fileURLWithPath: $0) }
            ?? URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("overlay-edit-verify")
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
        // The element list can change between enumeration and the frame read (the panel opens and the preview resizes): retry.
        for _ in 0..<5 {
            let all = app.descendants(matching: .any).matching(identifier: "native-editor-preview").allElementsBoundByIndex
            let sized = all.filter { $0.exists }.map { (element: $0, area: $0.frame.width * $0.frame.height) }
            if let best = sized.max(by: { $0.area < $1.area }) { return best.element }
            Thread.sleep(forTimeInterval: 0.3)
        }
        return app.descendants(matching: .any)["native-editor-preview"].firstMatch
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
