import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1581: the editor redraws a designed row exactly as the server compiled it, so a Save never
/// changes the design's shadow or drops its scrim.
///
/// The row is the A2 fact-0 heading of the Kyoto edit as the server resolved it
/// (`scripts/ios/kri-1581-text-designs-e2e.py prepare`, `cases/A2/rows.json`). The expected
/// values are that run's server-compiled layer (`cases/A2/recipe.json`): blur sigma 14, drop 3,
/// alpha 140/255, and a scrim from y 921.6 (0.48 of 1920) to the bottom, 0 -> 0.6 at 60% then flat.
@MainActor final class KRI1581TextShapesCompileTests: XCTestCase {
    private static let headingJSON = """
    {"id": "kri1585-fact-0-heading", "text": "7,3 km", "start_s": 4.0, "end_s": 9.05, "role": "generative_intro",
     "position": "custom", "x_frac": 0.07, "y_frac": 0.64858, "font_family": "Schibsted Grotesk", "size_px": 66,
     "color": "#FFE7A0", "stroke_width": 0.0, "shadow_color": "#000000", "shadow_opacity": 0.55, "shadow_enabled": true,
     "glow_strength": 0, "alignment": "left", "effect": "static", "line_spacing": 1.0, "max_width_frac": 0.8,
     "removed": false, "behind_subject": false, "shadow_blur": 14.0, "shadow_offset_y": 3.0,
     "scrim": {"top_frac": 0.48, "opacity": 0.6, "color": "#000000"}, "wrap_lines": false}
    """

    private func compile(_ object: [String: JSONValue]) throws -> KriaMediaEngine.EditRecipe {
        let element = EditorTextElement(id: try XCTUnwrap(object["id"]?.stringValue), text: try XCTUnwrap(object["text"]?.stringValue),
            startS: try XCTUnwrap(object["start_s"]?.numberValue), endS: try XCTUnwrap(object["end_s"]?.numberValue),
            role: object["role"]?.stringValue, raw: object)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 60),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 60,
            trimIn: 0, trimOut: 60, sourceDuration: 60, slotID: "slot")
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 60)], textElements: [element])
        let items = [NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: element.id), start: element.startS, end: element.endS)]
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        try recipe.validate()
        return recipe
    }

    private func heading() throws -> [String: JSONValue] {
        try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: Data(Self.headingJSON.utf8)).objectValue)
    }

    func testEditorDrawsTheServersShadowAndScrim() throws {
        let recipe = try compile(heading())
        let layer = try XCTUnwrap(recipe.textLayers.first)
        let blurs = layer.runs.flatMap(\.blurLayers)
        XCTAssertFalse(blurs.isEmpty)
        for blur in blurs {
            XCTAssertEqual(blur.sigma, 14); XCTAssertEqual(blur.dx, 0); XCTAssertEqual(blur.dy, 3)
            XCTAssertEqual(blur.color.alpha, 140.0 / 255, accuracy: 1e-9)
        }
        let scrim = try XCTUnwrap(layer.scrim)
        XCTAssertEqual(scrim.top, 921.6, accuracy: 1e-6)
        XCTAssertEqual(scrim.height, 998.4, accuracy: 1e-6)
        XCTAssertEqual(scrim.stops.map(\.position), [0, 0.6, 1])
        XCTAssertEqual(scrim.stops.map(\.color.alpha), [0, 0.6, 0.6])
        XCTAssertEqual(Set(scrim.stops.map { [$0.color.red, $0.color.green, $0.color.blue] }), [[0, 0, 0]])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.textDesigns))
    }

    /// The same row as a build before KRI-1581 saved it: the standard shadow and no scrim.
    func testARowWithoutTheNewKeysKeepsTheStandardShadow() throws {
        var row = try heading()
        for key in ["shadow_blur", "shadow_offset_y", "scrim"] { row.removeValue(forKey: key) }
        let recipe = try compile(row)
        let layer = try XCTUnwrap(recipe.textLayers.first)
        XCTAssertNil(layer.scrim)
        XCTAssertEqual(Set(layer.runs.flatMap(\.blurLayers).map { [$0.sigma, $0.dy] }), [[12, 6]])
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.textDesigns))
    }
}
