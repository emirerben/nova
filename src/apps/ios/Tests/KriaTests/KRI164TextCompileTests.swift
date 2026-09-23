import XCTest
import AVFoundation
import KriaMediaEngine
@testable import Kria

/// KRI-164: on-device diagnostics (2026-09-23) showed the editor's live
/// preview fails to build for job `d33dca56…` / item `02eb2bf1…` with
/// `RecipeError.invalidTimeline` (code 2), 100% reproducible, every launch —
/// BEFORE any playback starts. The values below are the item's two real
/// generative-intro text elements verbatim from `guided_edit_revision.
/// text_elements` (admin job-debug dump). This isolates whether `compile()`
/// throws on this exact text data against a single clip spanning the whole
/// timeline (real crossfade/track shape already cleared separately at the
/// KriaMediaEngine layer — see `EditorMontageLivePlaybackTests`).
@MainActor final class KRI164TextCompileTests: XCTestCase {
    private static let guidedTitleJSON = """
    {"z": null, "id": "guided-title", "role": "generative_intro", "text": "Emir Olympics London Edition",
     "color": "#FFF8F0", "end_s": 3.2, "effect": "fade-in", "motion": null, "x_frac": 0.5, "y_frac": 0.16,
     "removed": false, "size_px": 104.0, "start_s": 0.0, "position": "custom", "reveal_s": null,
     "alignment": "center", "text_case": null, "glow_color": null, "segment_id": "chapter-1:m1",
     "size_class": null, "fade_out_ms": null, "font_family": "Fraunces", "line_spacing": 1.0,
     "rotation_deg": null, "shadow_color": null, "shadow_style": "standard", "stroke_color": null,
     "stroke_width": 0.0, "word_timings": null, "editor_preset": null, "glow_strength": null,
     "source_params": null, "behind_subject": false, "letter_spacing": -0.025, "max_width_frac": 0.8,
     "shadow_enabled": true, "shadow_opacity": null, "highlight_color": "#D9FF70", "visual_block_id": null,
     "animation_phases": null, "background_color": null, "theme_transition": null}
    """
    private static let thoughtJSON = """
    {"z": null, "id": "guided-thought-chapter-9", "role": "generative_intro", "text": "post match pub",
     "color": "#FFF8F0", "end_s": 40.433333, "effect": "fade-in", "motion": null, "x_frac": 0.5, "y_frac": 0.8,
     "removed": false, "size_px": 60.0, "start_s": 34.4, "position": "custom", "reveal_s": null,
     "alignment": "center", "text_case": null, "glow_color": null, "segment_id": "chapter-9:m1",
     "size_class": null, "fade_out_ms": null, "font_family": "DM Sans", "line_spacing": 1.08,
     "rotation_deg": null, "shadow_color": null, "shadow_style": "standard", "stroke_color": null,
     "stroke_width": 0.0, "word_timings": null, "editor_preset": null, "glow_strength": null,
     "source_params": null, "behind_subject": false, "letter_spacing": null, "max_width_frac": 0.76,
     "shadow_enabled": true, "shadow_opacity": null, "highlight_color": "#D9FF70", "visual_block_id": null,
     "animation_phases": null, "background_color": null, "theme_transition": null}
    """

    private func decodeElement(_ json: String) throws -> EditorTextElement {
        let object = try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: Data(json.utf8)).objectValue)
        return EditorTextElement(id: try XCTUnwrap(object["id"]?.stringValue),
            text: try XCTUnwrap(object["text"]?.stringValue),
            startS: try XCTUnwrap(object["start_s"]?.numberValue),
            endS: try XCTUnwrap(object["end_s"]?.numberValue),
            role: object["role"]?.stringValue, raw: object)
    }

    func testRealJobTextElementsAgainstDirectlyMatchedItems() throws {
        let title = try decodeElement(Self.guidedTitleJSON)
        let thought = try decodeElement(Self.thoughtJSON)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 42.033333),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 42.033333,
            trimIn: 0, trimOut: 42.033333, sourceDuration: 42.033333, slotID: "slot")
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 42.033333)],
            textElements: [title, thought])
        // `items` matched directly by ID at the text elements' own start_s/end_s
        // — i.e. what a bug-free `NativeEditorSession.timelineItems` projection
        // should produce for an unedited, freshly-opened document (identity
        // mapping, no overlap/base-vs-current clock drift).
        let items = [title, thought].map {
            NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: $0.id), start: $0.startS, end: $0.endS)
        }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.count, 2, "both real text elements should compile into layers")
        try recipe.validate()
    }
}
