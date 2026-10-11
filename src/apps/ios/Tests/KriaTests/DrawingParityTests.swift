import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-589 wire + compile contract, pinned by the fixtures the Python `DrawingElement` validator and
/// `compile_drawing_layers` are pinned to as well (`tests/fixtures/drawing-parity/`).
@MainActor final class DrawingParityTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        return try Data(contentsOf: url.appendingPathComponent("tests/fixtures/drawing-parity/\(name).json"))
    }

    private struct Wire: Decodable {
        struct Invalid: Decodable { let name: String; let element: [String: JSONValue] }
        let elements: [[String: JSONValue]]
        let invalid: [Invalid]
    }

    /// Decoded with the plain decoder: `convertFromSnakeCase` would also rewrite the keys inside `JSONValue` objects.
    private struct CompileFile: Decodable {
        struct Case: Decodable {
            let name: String
            let timeline_duration_s: Double
            let drawings: [[String: JSONValue]]
            let expected_layers: [JSONValue]
            var timelineDurationS: Double { timeline_duration_s }
            func expectedLayers() throws -> [PortableDrawingLayer] {
                try RecipeJSON.decoder().decode([PortableDrawingLayer].self, from: JSONEncoder().encode(expected_layers))
            }
        }
        let cases: [Case]
    }

    func testEveryWireElementDecodesAndRoundTripsLosslessly() throws {
        let wire = try JSONDecoder().decode(Wire.self, from: fixture("v1"))
        XCTAssertGreaterThanOrEqual(wire.elements.count, 9)
        for element in wire.elements {
            let drawing = try XCTUnwrap(EditorDrawing(json: element), "\(element["id"] ?? .null) should decode")
            XCTAssertEqual(drawing.json, .object(element), "\(drawing.id) must encode back to the wire it came from")
        }
        let textures = Set(wire.elements.flatMap { $0["strokes"]?.arrayValue ?? [] }.compactMap { $0.objectValue?["texture"]?.stringValue })
        XCTAssertEqual(textures, Set(DrawingTexture.allCases.map(\.rawValue)), "the fixture must exercise every texture")
    }

    func testInvalidWireRowsAreRejectedAndKeptVerbatimAsOpaqueRows() throws {
        let wire = try JSONDecoder().decode(Wire.self, from: fixture("v1"))
        XCTAssertGreaterThanOrEqual(wire.invalid.count, 9)
        for item in wire.invalid {
            XCTAssertNil(EditorDrawing(json: item.element), "\(item.name) must be rejected")
            // A row the client cannot type is preserved untouched on save, never rewritten or dropped.
            let document = EditorDocument.decode(snapshot: ["drawings": .array([.object(item.element)])])
            XCTAssertTrue(document.drawings.isEmpty, item.name)
            XCTAssertEqual(document.opaqueRecords[.drawings]?.count, 1, item.name)
        }
    }

    func testCompileFixtureThroughThePureCompileAndTheRenderCompiler() throws {
        let file = try JSONDecoder().decode(CompileFile.self, from: fixture("compile_v1"))
        XCTAssertGreaterThanOrEqual(file.cases.count, 8)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        for item in file.cases {
            let drawings = try item.drawings.map { try XCTUnwrap(EditorDrawing(json: $0), item.name) }
            let expected = try item.expectedLayers()
            XCTAssertEqual(NativeDrawingCompile.layers(drawings, timelineDuration: item.timelineDurationS), expected, item.name)

            // The same drawings through the editor's own compiler, on a timeline of exactly that length.
            let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
                asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: item.timelineDurationS),
                url: URL(fileURLWithPath: "/fixture/original.mp4"))
            let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: item.timelineDurationS,
                                  trimIn: 0, trimOut: item.timelineDurationS, sourceDuration: item.timelineDurationS, slotID: "slot")
            var document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: item.timelineDurationS)])
            document.drawings = drawings
            let recipe = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source]).recipe
            XCTAssertEqual(recipe.drawingLayers, expected, "\(item.name) via NativeEditorRenderCompiler")
            XCTAssertEqual(recipe.effectiveCapabilities.contains(.freehandDrawing), !expected.isEmpty, item.name)
        }
    }

    func testRecipeWithoutDrawingsIsByteIdenticalAndWithDrawingsRoundTrips() throws {
        let file = try JSONDecoder().decode(CompileFile.self, from: fixture("compile_v1"))
        let layers = try XCTUnwrap(file.cases.first { $0.name == "identity-inside-timeline" }).expectedLayers()
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2")
        XCTAssertFalse(String(decoding: try RecipeJSON.encode(recipe), as: UTF8.self).contains("drawing"))
        recipe.drawingLayers = layers
        let wire = try RecipeJSON.encode(recipe)
        XCTAssertTrue(String(decoding: wire, as: UTF8.self).contains("drawing_layers"))
        XCTAssertEqual(try RecipeJSON.decode(wire).drawingLayers, layers)
    }

    /// Failure mode: "Draw more" on a moved / scaled / rotated drawing shifts what was already drawn when the
    /// bounding box (and with it the pivot) grows. Everything already on screen must stay exactly where it is.
    func testDrawMoreKeepsEveryExistingStrokeWhereItWasOnScreen() throws {
        let existing = DrawingStroke(color: "#FF3B30", widthFrac: 0.02, points: [[0.3, 0.4], [0.5, 0.5], [0.4, 0.6]])
        var drawing = EditorDrawing(startS: 0, endS: 3, canvasAspect: 9.0 / 16.0, centerX: 0.55, centerY: 0.45, scale: 1.3, rotationDeg: 20, strokes: [existing])
        let before = existing.points.map { drawing.previewPoint(of: $0) }
        let added = DrawingStroke(color: "#34C759", widthFrac: 0.02, points: [[0.4, 0.3], [0.75, 0.35], [0.7, 0.6]])
        let onScreen = added.points
        drawing.append(previewStrokes: [added])
        XCTAssertEqual(drawing.strokes.count, 2)
        for (index, point) in existing.points.enumerated() {
            let after = drawing.previewPoint(of: point)
            XCTAssertEqual(after[0], before[index][0], accuracy: 0.0006)
            XCTAssertEqual(after[1], before[index][1], accuracy: 0.0006)
        }
        // And the new strokes land where the finger drew them.
        for (index, point) in drawing.strokes[1].points.enumerated() {
            let shown = drawing.previewPoint(of: point)
            XCTAssertEqual(shown[0], onScreen[index][0], accuracy: 0.002)
            XCTAssertEqual(shown[1], onScreen[index][1], accuracy: 0.002)
        }
        XCTAssertTrue(drawing.isValid)
    }

    func testSimplifyKeepsTheShapeWithinEpsilonAndTheEndpoints() {
        let line = (0...200).map { [Double($0) / 200, 0.5 + 0.0004 * sin(Double($0))] }
        let simple = DrawingStrokeTools.simplify(line)
        XCTAssertEqual(simple.count, 2, "a near-straight line collapses to its endpoints")
        let curve = (0...200).map { [Double($0) / 200, 0.5 + 0.2 * sin(Double($0) / 200 * .pi * 2)] }
        let reduced = DrawingStrokeTools.simplify(curve)
        XCTAssertLessThan(reduced.count, curve.count / 3)
        XCTAssertEqual(reduced.first, curve.first); XCTAssertEqual(reduced.last, curve.last)
    }
}
