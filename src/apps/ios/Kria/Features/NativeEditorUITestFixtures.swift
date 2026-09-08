#if DEBUG
import Foundation

/// Account-free editor fixtures used by UI tests and local visual inspection.
/// They deliberately use the compatibility `EditorDraft` projection while
/// retaining future lane data in `serverSnapshot` for characterization tests.
enum NativeEditorUITestFixtures {
    enum Shape: String, CaseIterable, Sendable {
        case twoText = "two-text"
        case boundary = "boundary"
        case allLanes = "all-lanes"
        case stress = "stress-71"
        case unknown = "unknown-sections"
    }

    struct Fixture: Sendable {
        let shape: Shape
        let draft: EditorDraft
    }

    static var current: Fixture {
        fixture(arguments: ProcessInfo.processInfo.arguments)
    }

    static func fixture(arguments: [String]) -> Fixture {
        let shape = Shape.allCases.first { arguments.contains("-ui-testing-editor-\($0.rawValue)") } ?? .twoText
        return Fixture(shape: shape, draft: draft(for: shape))
    }

    static func draft(for shape: Shape) -> EditorDraft {
        switch shape {
        case .twoText: twoText
        case .boundary: boundary
        case .allLanes: allLanes
        case .stress: stress
        case .unknown: unknownSections
        }
    }

    static let twoText: EditorDraft = {
        var clips = [clip(0, start: 0, duration: 3), clip(1, start: 3, duration: 3)]
        // Keep the rendered timeline at three seconds while leaving source
        // headroom for the leading-trim extension UI test.
        clips[0].trimIn = 1
        clips[0].trimOut = 4
        let first = text(100, content: "Opening question", x: 0.5, y: 0.28)
        let second = text(101, content: "Your story ritual", x: 0.5, y: 0.72)
        return draft(clips: clips, text: [first, second], captions: true, music: true,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "text_elements": .array([
                            textRecord(first, start: 0, end: 1.5, z: 1),
                            textRecord(second, start: 1.5, end: 3, z: 1),
                        ]),
                     ])
    }()

    static let boundary: EditorDraft = {
        let clips = [clip(2, start: 0, duration: 4)]
        let first = text(200, content: "At zero", x: 0.5, y: 0.25)
        let second = text(201, content: "At the boundary", x: 0.5, y: 0.75)
        return draft(clips: clips, text: [first, second], captions: true, music: false,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "text_elements": .array([
                            textRecord(first, start: 0, end: 2, z: 2),
                            textRecord(second, start: 2, end: 4, z: 1),
                            .object(["id": .string("zero-duration"), "text": .string("Ignore me"), "start_s": .number(4), "end_s": .number(4)]),
                        ]),
                        "caption_cues": .array([
                            .object(["id": .string("cue-a"), "start_s": .number(0), "end_s": .number(2), "text": .string("first")]),
                            .object(["id": .string("cue-b"), "start_s": .number(2), "end_s": .number(4), "text": .string("second")]),
                        ]),
                     ])
    }()

    static let allLanes: EditorDraft = {
        let clips = [clip(3, start: 0, duration: 2), clip(4, start: 2, duration: 2), clip(5, start: 4, duration: 2)]
        let layer = text(300, content: "All lanes", x: 0.5, y: 0.5)
        return draft(clips: clips, text: [layer], captions: true, music: true,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "text_elements": .array([textRecord(layer, start: 0, end: 2, z: 3)]),
                        "caption_cues": .array([.object(["id": .string("cue-all"), "start_s": .number(0), "end_s": .number(1), "text": .string("caption")])]),
                        "music_track_id": .string(id(350).uuidString),
                        "music_window": .object(["start_s": .number(0), "end_s": .number(6)]),
                        "audio_mix": .object(["music_level": .number(0.72), "original_level": .number(1)]),
                        "sound_effects": .array([.object(["id": .string("sfx-1"), "start_s": .number(0.5), "end_s": .number(0.8), "volume": .number(0.8)])]),
                        "media_overlays": .array([.object(["id": .string("overlay-1"), "start_s": .number(1), "end_s": .number(2), "z": .number(4), "src_gcs_path": .string("fixture/overlay.png")])]),
                        "visual_blocks": .array([.object(["id": .string("visual-1"), "start_s": .number(2), "end_s": .number(3), "preset": .string("cards")])]),
                        "motion_scenes": .array([.object(["id": .string("motion-1"), "start_s": .number(3), "end_s": .number(4), "preset": .string("zoom")])]),
                        "camera_effects": .array([.object(["id": .string("camera-1"), "start_s": .number(4), "end_s": .number(5), "kind": .string("push_in")])]),
                        "carousel_moment": .object(["id": .string("carousel-1"), "start_s": .number(1), "end_s": .number(3)]),
                     ])
    }()

    static let stress: EditorDraft = {
        let clips = (0..<71).map { index in clip(index, start: Double(index) * 0.1, duration: 0.1) }
        let textLayers = (0..<8).map { index in text(400 + index, content: "Text \(index + 1)", x: 0.5, y: 0.18 + Double(index % 4) * 0.2) }
        var sections: [String: JSONValue] = [
            "timeline_slots": slots(for: clips),
            "text_elements": .array(textLayers.enumerated().map { index, layer in textRecord(layer, start: Double(index) * 0.8, end: Double(index + 1) * 0.8, z: index) }),
            "caption_cues": .array((0..<40).map { index in .object(["id": .string("stress-cue-\(index)"), "start_s": .number(Double(index) * 0.17), "end_s": .number(Double(index) * 0.17 + 0.16), "text": .string("Cue \(index + 1)")]) }),
            "sound_effects": .array((0..<12).map { index in .object(["id": .string("stress-sfx-\(index)"), "start_s": .number(Double(index) * 0.55), "end_s": .number(Double(index) * 0.55 + 0.12)]) }),
            "media_overlays": .array((0..<6).map { index in .object(["id": .string("stress-overlay-\(index)"), "start_s": .number(Double(index)), "end_s": .number(Double(index) + 0.7), "z": .number(Double(index))]) }),
        ]
        sections["fixture_metadata"] = .object(["source_asset_count": .number(43), "slot_count": .number(71)])
        return draft(clips: clips, text: textLayers, captions: true, music: true, sections: sections)
    }()

    static let unknownSections: EditorDraft = {
        let clips = [clip(600, start: 0, duration: 2)]
        let layer = text(601, content: "Forward compatible", x: 0.5, y: 0.5)
        return draft(clips: clips, text: [layer], captions: false, music: false,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "text_elements": .array([textRecord(layer, start: 0, end: 2, z: 1, extra: ["future_item_field": .string("keep")])]),
                        "future_section": .object(["opaque": .array([.number(1), .bool(true), .null])]),
                     ], rootExtras: ["future_root_key": .string("keep")])
    }()

    private static func draft(clips: [EditorClip], text: [TextLayer], captions: Bool, music: Bool,
                              sections: [String: JSONValue], rootExtras: [String: JSONValue] = [:]) -> EditorDraft {
        var root = rootExtras
        root["schema_version"] = .number(2)
        root["kind"] = .string("editor")
        root["editor_payload"] = .object([
            "base_generation": .string("fixture-generation"),
            "sections": .object(sections),
        ])
        return EditorDraft(
            projectID: id(999), clips: clips, text: text,
            captions: CaptionStyle(enabled: captions, style: "sentence"),
            music: music ? MusicSelection(trackID: id(350), title: "Fixture music", start: 0, volume: 0.72) : nil,
            revision: 3, etag: "fixture-etag", serverSnapshot: root
        )
    }

    private static func clip(_ index: Int, start: Double, duration: Double) -> EditorClip {
        EditorClip(id: id(index), assetID: id(1_000 + index % 43), sourceClipIndex: index % 43,
                   start: start, end: start + duration, trimIn: 0, trimOut: duration,
                   sourceDuration: max(duration, 7.2), slotID: "slot-\(index)")
    }

    private static func text(_ index: Int, content: String, x: Double, y: Double) -> TextLayer {
        TextLayer(id: id(index), content: content, position: CGPoint(x: x, y: y), style: "Fraunces")
    }

    private static func slots(for clips: [EditorClip]) -> JSONValue {
        .array(clips.map { clip in
            .object([
                "slot_id": .string(clip.slotID ?? clip.id.uuidString),
                "clip_index": .number(Double(clip.sourceClipIndex ?? 0)),
                "in_s": .number(clip.trimIn),
                "duration_s": .number(clip.end - clip.start),
                "source_duration_s": .number(clip.sourceDuration ?? clip.trimOut),
                "removed": .bool(false),
            ])
        })
    }

    private static func textRecord(_ layer: TextLayer, start: Double, end: Double, z: Int,
                                   extra: [String: JSONValue] = [:]) -> JSONValue {
        var value: [String: JSONValue] = [
            "id": .string(layer.id.uuidString), "text": .string(layer.content),
            "start_s": .number(start), "end_s": .number(end), "x_frac": .number(layer.position.x),
            "y_frac": .number(layer.position.y), "font_family": .string(layer.style),
            "z": .number(Double(z)),
        ]
        value.merge(extra) { _, new in new }
        return .object(value)
    }

    private static func id(_ index: Int) -> UUID {
        UUID(uuidString: String(format: "00000000-0000-4000-8000-%012d", index))!
    }
}
#endif
