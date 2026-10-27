import CoreText
import XCTest
import KriaMediaEngine
@testable import Kria

/// Changing a text's alignment must not move the block on screen, and the Position buttons must put the
/// block flush against the platform safe area. Both are asserted on what the real compiler draws (the
/// preview and the export read the same recipe), not on the stored fracs.
@MainActor final class NativeEditorTextPlacementTests: XCTestCase {
    private static let canvas = CGSize(width: 1080, height: 1920)
    private static let pixel = 1.0 / 1080

    private static let freeJSON = """
    {"id": "free", "text": "Short\\nA second line", "start_s": 0.0, "end_s": 3.0,
     "position": "custom", "x_frac": 0.35, "y_frac": 0.55, "size_px": 64.0, "font_family": "Inter",
     "alignment": "center", "wrap_lines": false, "effect": "none"}
    """
    /// Wide enough that its line breaks depend on where it sits (the authored-text width rule: room to the
    /// frame margins), so moving or re-aligning it can re-wrap it.
    private static let reflowJSON = """
    {"id": "reflow", "text": "Short\\nA much longer second line here", "start_s": 0.0, "end_s": 3.0,
     "position": "custom", "x_frac": 0.35, "y_frac": 0.55, "size_px": 64.0, "font_family": "Inter",
     "alignment": "center", "wrap_lines": false, "effect": "none"}
    """
    private static let presetJSON = """
    {"id": "preset", "text": "Match Day tonight at the stadium", "start_s": 0.0, "end_s": 3.0,
     "position": "top", "x_frac": 0.1, "y_frac": 0.9, "size_class": "large", "alignment": "center",
     "effect": "none"}
    """

    private static func row(_ json: String) throws -> [String: JSONValue] {
        try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: Data(json.utf8)).objectValue)
    }

    private func session(_ json: String) throws -> NativeEditorSession {
        let rows: JSONValue = .array([.object(try Self.row(json))])
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"), "sections": .object(["text_elements": rows]),
        ])]
        return NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
    }

    /// The drawn block in canvas fractions: leftmost line start to rightmost line end, first ascent to last descent.
    private func drawnBlock(_ session: NativeEditorSession, id: String) throws -> CGRect {
        let elements = session.document.textElements
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 5,
            trimIn: 0, trimOut: 5, sourceDuration: 5, slotID: "slot")
        let items = elements.map { NativeEditorTimelineItem(selection: .init(kind: .text, id: $0.id), start: $0.startS, end: $0.endS) }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: EditorDocument(textElements: elements), clips: [clip], items: items, sources: [0: source])
        let layer = try XCTUnwrap(program.recipe.textLayers.first { $0.id == id })
        let first = try XCTUnwrap(layer.runs.first)
        XCTAssertTrue(first.fontVariations.isEmpty, "the oracle measures plain faces only")
        let url = try XCTUnwrap(program.assetURLs[first.fontAssetID])
        let graphics = try XCTUnwrap(CGDataProvider(url: url as CFURL).flatMap { CGFont($0) })
        var left = Double.infinity, right = -Double.infinity
        var font = CTFontCreateWithGraphicsFont(graphics, first.fontSize, nil, nil)
        for run in layer.runs {
            font = CTFontCreateWithGraphicsFont(graphics, run.fontSize, nil, nil)
            let attributed = NSAttributedString(string: run.text, attributes: [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTKernAttributeName as String): run.letterSpacing])
            let width = CTLineGetTypographicBounds(CTLineCreateWithAttributedString(attributed), nil, nil, nil)
            left = min(left, run.x); right = max(right, run.x + width)
        }
        let last = try XCTUnwrap(layer.runs.last)
        let top = first.baselineY - CTFontGetAscent(font)
        let bottom = last.baselineY + CTFontGetDescent(font)
        return CGRect(x: left / Self.canvas.width, y: top / Self.canvas.height,
                      width: (right - left) / Self.canvas.width, height: (bottom - top) / Self.canvas.height)
    }

    private func assertSameBlock(_ actual: CGRect, _ expected: CGRect, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        let tolerance = Self.pixel
        XCTAssertEqual(actual.minX, expected.minX, accuracy: tolerance, "left edge: " + message, file: file, line: line)
        XCTAssertEqual(actual.maxX, expected.maxX, accuracy: tolerance, "right edge: " + message, file: file, line: line)
        XCTAssertEqual(actual.minY, expected.minY, accuracy: tolerance, "top edge: " + message, file: file, line: line)
        XCTAssertEqual(actual.maxY, expected.maxY, accuracy: tolerance, "bottom edge: " + message, file: file, line: line)
    }

    private func assertAlignmentKeepsBlock(_ session: NativeEditorSession, id: String) throws {
        let original = try drawnBlock(session, id: id)
        let before = session.document
        for alignment in ["left", "right", "center", "right", "left"] {
            session.setTextAlignment(id: id, alignment: alignment)
            XCTAssertEqual(session.document.textElements.first?.raw["alignment"], .string(alignment))
            assertSameBlock(try drawnBlock(session, id: id), original, "aligned \(alignment)")
        }
        session.setTextAlignment(id: id, alignment: "right")
        let afterOne = session.document
        session.setTextAlignment(id: id, alignment: "center")
        session.undo()
        XCTAssertEqual(session.document, afterOne, "one alignment change is one undo step")
        while session.canUndo { session.undo() }
        XCTAssertEqual(session.document, before)
    }

    func testAlignmentOnFreeRowKeepsBlockStill() throws {
        let session = try session(Self.freeJSON)
        try assertAlignmentKeepsBlock(session, id: "free")
    }

    func testAlignmentOnPresetPositionRowKeepsBlockStillAndMakesItCustom() throws {
        let session = try session(Self.presetJSON)
        let original = try drawnBlock(session, id: "preset")
        session.setTextAlignment(id: "preset", alignment: "left")
        let row = try XCTUnwrap(session.document.textElements.first)
        XCTAssertEqual(row.raw["position"], .string("custom"))
        XCTAssertEqual(row.raw["y_frac"]?.numberValue ?? 0, EditorTextElement.presetY["top"] ?? -1, accuracy: 0.0001)
        assertSameBlock(try drawnBlock(session, id: "preset"), original, "preset row aligned left")
        try assertAlignmentKeepsBlock(try self.session(Self.presetJSON), id: "preset")
    }

    func testAlignmentOnTextThatReflowsKeepsTheEdgeItIsAnchoredTo() throws {
        let session = try session(Self.reflowJSON)
        let original = try drawnBlock(session, id: "reflow")
        session.setTextAlignment(id: "reflow", alignment: "left")
        XCTAssertEqual(try drawnBlock(session, id: "reflow").minX, original.minX, accuracy: Self.pixel)
        session.setTextAlignment(id: "reflow", alignment: "center")
        session.setTextAlignment(id: "reflow", alignment: "right")
        let right = try drawnBlock(session, id: "reflow")
        session.setTextAlignment(id: "reflow", alignment: "center")
        XCTAssertEqual(try drawnBlock(session, id: "reflow").midX, right.midX, accuracy: Self.pixel)
    }

    private func safe() -> CGRect { NativePreviewDangerZones.safeRect(in: CGSize(width: 1, height: 1)) }

    func testPlacementButtonsLandTheBlockFlushOnTheSafeRect() throws {
        for json in [Self.freeJSON, Self.presetJSON, Self.reflowJSON] {
            let session = try session(json)
            let id = try XCTUnwrap(session.document.textElements.first?.id)
            let alignment = session.document.textElements.first?.raw["alignment"]
            let start = try drawnBlock(session, id: id)

            session.placeTextBlock(id: id, .left)
            var block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.minX, safe().minX, accuracy: Self.pixel, "left lands on the safe rect")
            XCTAssertEqual(block.midY, start.midY, accuracy: Self.pixel, "a horizontal placement leaves the vertical position alone")

            session.placeTextBlock(id: id, .right)
            block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.maxX, safe().maxX, accuracy: Self.pixel, "right lands on the safe rect")

            session.placeTextBlock(id: id, .center)
            block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.midX, 0.5, accuracy: Self.pixel)

            session.placeTextBlock(id: id, .top)
            block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.minY, safe().minY, accuracy: Self.pixel, "top lands on the safe rect")
            XCTAssertEqual(block.midX, 0.5, accuracy: Self.pixel, "a vertical placement leaves the horizontal position alone")

            session.placeTextBlock(id: id, .bottom)
            block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.maxY, safe().maxY, accuracy: Self.pixel, "bottom lands on the safe rect")

            session.placeTextBlock(id: id, .middle)
            block = try drawnBlock(session, id: id)
            XCTAssertEqual(block.midY, 0.5, accuracy: Self.pixel)

            XCTAssertEqual(session.document.textElements.first?.raw["alignment"], alignment, "placement keeps the alignment")
            XCTAssertEqual(session.document.textElements.first?.raw["position"], .string("custom"))
        }
    }

    func testPlacementIsOneUndoStepAndReportsItsSelection() throws {
        let session = try session(Self.freeJSON)
        let before = session.document
        session.placeTextBlock(id: "free", .left)
        session.placeTextBlock(id: "free", .bottom)
        XCTAssertEqual(session.textBlockPlacements(id: "free"), [.left, .bottom])
        session.undo()
        XCTAssertEqual(session.textBlockPlacements(id: "free"), [.left])
        session.undo()
        XCTAssertEqual(session.document, before)
        XCTAssertEqual(session.textBlockPlacements(id: "free"), [])
    }
}
