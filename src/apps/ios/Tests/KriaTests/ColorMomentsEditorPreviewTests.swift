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

    // MARK: Range trims (review of 280701077): the preview moves the moments and their sounds like the server's Save.

    /// The server Range fixture: a 10 s take kept [0,4) and [5,10) (output 4.0 s = source 5.0 s).
    private func talkingDocument(start: Double, end: Double, soundAt: Double) throws -> EditorDocument {
        let slot = { (id: String, inS: Double, duration: Double) -> JSONValue in
            .object(["slot_id": .string(id), "clip_index": .number(0), "in_s": .number(inS), "duration_s": .number(duration)])
        }
        let slots: JSONValue = .array([slot("native-phone-talking-source", 0, 4), slot("native-phone-talking-source-1", 5, 5)])
        return EditorDocument(snapshot: [
            "editor_payload": .object(["sections": .object([
                "timeline_slots": slots,
                "sound_effects": .array([.object(["id": .string("cmsfx-cm1-enter"), "sound_effect_id": .string("pop"),
                                                  "at_s": .number(soundAt), "duration_s": .number(0.5)])]),
            ])]),
            "user_timeline": .object(["slots": slots]),
            "color_moments": try lane("""
            {"moments":[{"id":"cm1","start":\(start),"end":\(end),"tone":"mono","enter":{"style":"light_leak","duration":0.3333}}]}
            """),
        ])
    }

    /// Trim the first second off the first window, as the Range sheet does.
    private func trimmed(_ document: EditorDocument) -> EditorDocument {
        var next = document
        next.clips[0].inS = 1; next.clips[0].durationS = 3
        NativeSpeechTimelineProjection.rebase(&next, from: document)
        return next
    }

    private func previewMoments(_ document: EditorDocument) -> [(Double, Double)] {
        let lane = NativeSpeechTimelineProjection.colorMomentsLane(document.colorMoments, baseSlots: document.colorMomentsBaseSlots, document: document)
        return NativeEditorRenderCompiler.colorMoments(lane, duration: 8).0.map { ($0.start, $0.end) }
    }

    func testARangeTrimMovesThePreviewMomentAndItsSoundTogether() throws {
        let document = try talkingDocument(start: 6, end: 7.5, soundAt: 6)
        XCTAssertEqual(previewMoments(document).map(\.0), [6], "an untouched timeline previews the stored times")
        let next = trimmed(document)
        let moments = previewMoments(next)
        XCTAssertEqual(moments.count, 1)
        XCTAssertEqual(moments[0].0, 5, accuracy: 1e-9); XCTAssertEqual(moments[0].1, 6.5, accuracy: 1e-9)
        XCTAssertEqual(next.soundEffects.map(\.id), ["cmsfx-cm1-enter"])
        XCTAssertEqual(try XCTUnwrap(next.soundEffects.first?.pointS), 5, accuracy: 1e-9)
        XCTAssertEqual(next.soundEffects.first?.raw["at_s"], .number(5))
    }

    func testAMomentWhoseFirstFramesAreTrimmedKeepsItsSoundOnTheNewEdge() throws {
        let next = trimmed(try talkingDocument(start: 0.5, end: 3, soundAt: 0.5))
        let moments = previewMoments(next)
        XCTAssertEqual(moments.count, 1)
        XCTAssertEqual(moments[0].0, 0, accuracy: 1e-9); XCTAssertEqual(moments[0].1, 2, accuracy: 1e-9)
        XCTAssertEqual(next.soundEffects.map(\.id), ["cmsfx-cm1-enter"], "the sound follows its moment instead of vanishing with the footage")
        XCTAssertEqual(try XCTUnwrap(next.soundEffects.first?.pointS), 0, accuracy: 1e-9)
    }

    func testAnEditWithoutMomentsKeepsNoBaseAndAnUntouchedTimelineIsUnchanged() throws {
        let plain = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object([:])])])
        XCTAssertTrue(plain.colorMomentsBaseSlots.isEmpty)
        let document = try talkingDocument(start: 6, end: 7.5, soundAt: 6)
        XCTAssertEqual(NativeSpeechTimelineProjection.colorMomentsLane(document.colorMoments, baseSlots: document.colorMomentsBaseSlots, document: document),
                       document.colorMoments)
    }
}
