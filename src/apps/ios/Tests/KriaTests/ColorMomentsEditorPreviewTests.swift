import XCTest
@testable import Kria
import KriaMediaEngine

/// KRI-1350: the editor preview reads the server-owned colour-moments lane and clamps it to the edit's length
/// exactly like the server's `compile_color_moments`, so the preview never refuses a recipe the export accepts.
@MainActor
final class ColorMomentsEditorPreviewTests: XCTestCase {
    private func lane(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    func testTheStoredLaneReachesThePreviewRecipe() throws {
        let raw = try lane("""
        {"moments":[{"id":"cm1","start":1.0,"end":2.5,"tone":"mono","exit":{"style":"light_leak","duration":0.3333}}],
         "end_fade":{"duration":1.6}}
        """)
        let (moments, fade) = NativeEditorRenderCompiler.colorMoments(raw, duration: 10)
        XCTAssertEqual(moments.map(\.id), ["cm1"])
        XCTAssertEqual(moments.first?.exit.style, .lightLeak)
        XCTAssertEqual(moments.first?.enter.style, .cut)
        XCTAssertEqual(fade?.duration, 1.6)
    }

    func testAShorterCutShortensOrDropsLikeTheServer() throws {
        let raw = try lane("""
        {"moments":[{"id":"a","start":1,"end":2,"tone":"mono"},{"id":"b","start":3,"end":9,"tone":"warm"},
                    {"id":"c","start":3.8,"end":6,"tone":"faded"}],"end_fade":{"duration":5}}
        """)
        let (moments, fade) = NativeEditorRenderCompiler.colorMoments(raw, duration: 4)
        XCTAssertEqual(moments.map(\.id), ["a", "b"])
        XCTAssertEqual(moments.last?.end, 4)
        XCTAssertNil(fade, "an end fade longer than the video is dropped")
    }

    func testAnUnreadableLanePreviewsWithoutMoments() throws {
        let (moments, fade) = NativeEditorRenderCompiler.colorMoments(try lane(#"{"moments":[{"id":"x","tone":"sepia"}]}"#), duration: 10)
        XCTAssertTrue(moments.isEmpty); XCTAssertNil(fade)
        XCTAssertTrue(NativeEditorRenderCompiler.colorMoments(nil, duration: 10).0.isEmpty)
    }

    func testTheDocumentReadsTheLaneButNeverSavesIt() throws {
        let document = EditorDocument(snapshot: [
            "color_moments": try lane(#"{"moments":[{"id":"cm1","start":1,"end":2,"tone":"mono"}]}"#),
        ])
        XCTAssertNotNil(document.colorMoments)
        XCTAssertNil(EditorSection(rawValue: "color_moments"), "no Save section: the server owns the lane")
        XCTAssertFalse(EditorSection.allCases.map(\.rawValue).contains { $0.contains("color") })
    }
}
