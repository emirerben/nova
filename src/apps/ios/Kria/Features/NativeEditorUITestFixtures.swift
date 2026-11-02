#if DEBUG
import Foundation
import KriaMediaEngine

/// Account-free editor fixtures used by UI tests and local visual inspection.
/// They deliberately use the compatibility `EditorDraft` projection while
/// retaining future lane data in `serverSnapshot` for characterization tests.
enum NativeEditorUITestFixtures {
    enum Shape: String, CaseIterable, Sendable {
        case legacyVisuals = "legacy-visuals"
        case captionVisuals = "caption-visuals"
        case projectedCaptions = "projected-captions"
        case sourceText = "source-text"
        case selectionBoundary = "selection-boundary"
        case gamedayAdd = "gameday-add"
        case twoText = "two-text"
        case boundary = "boundary"
        case allLanes = "all-lanes"
        case stress = "stress-71"
        case unknown = "unknown-sections"
        case autoScrollExtend = "autoscroll-extend"
        case guidedText = "guided-text"
        case talkingCaptions = "talking-captions"
        /// KRI-1007: lip-sync lyrics as caption_cue text rows (one per word, `line_end` closes a line).
        case songCaptions = "song-captions"
        case retimedClips = "retimed-clips"
        /// Four 2.5 s cuts on four takes (KRI-561): a lip-sync song trim visibly drops cuts.
        case songTrim = "song-trim"
        /// KRI-589: a 6 s cut whose variant opens the drawings lane; Save commits through a stub transport
        /// that keeps the saved drawings across relaunches (`-ui-testing-editor-drawing-reset` starts clean).
        case drawing = "drawing"
        /// KRI-1118: a Talking edit whose variant opens the `speaker_background` capability, with two ready pool visuals
        /// (a photo and a video). Save commits through the same stub transport and keeps the saved lane across
        /// relaunches (`-ui-testing-editor-speaker-background-reset` starts with no background).
        case speakerBackground = "speaker-background"
        /// Three clips of different lengths (1.0 / 1.4 / 1.2 s) that sit inside one viewport, so a long-press drag
        /// can reorder them without reaching the auto-scroll edge, and the durations tell the order apart. Each is
        /// at least 1 s so the default crossfades are never capped by a short neighbour and the length is exact.
        case reorderClips = "reorder-clips"
        /// KRI-717 on a phone Talking edit: three kept segments of one take (timeline closed, Range open through
        /// `clips.source_window`). Save commits through the stub transport, which keeps the saved windows across
        /// relaunches (`-ui-testing-editor-talking-range-reset` starts clean; `-ui-testing-editor-talking-range-closed`
        /// closes the leaf with a reason).
        case talkingRange = "talking-range"
    }

    /// Three catalog sounds in two categories; nil preview URLs keep UI tests off the network.
    static let soundEffectCatalog: [NativeEditorSoundEffect] = [
        .init(id: "fx-whoosh", name: "Soft whoosh", durationS: 0.8, previewAudioURL: nil, roleTags: [], category: "transition", searchTerms: ["whoosh"]),
        .init(id: "fx-sweep", name: "Air sweep", durationS: 1.2, previewAudioURL: nil, roleTags: [], category: "transition", searchTerms: ["sweep"]),
        .init(id: "fx-pop", name: "Pop", durationS: 0.3, previewAudioURL: nil, roleTags: [], category: "impact", searchTerms: ["pop"]),
    ]

    struct Fixture: Sendable {
        let shape: Shape
        let draft: EditorDraft
    }

    static var current: Fixture {
        fixture(arguments: ProcessInfo.processInfo.arguments)
    }

    static func fixture(arguments: [String]) -> Fixture {
        if arguments.contains("-ui-testing-editor-speed") {
            return Fixture(shape: .reorderClips, draft: speedControls)
        }
        if arguments.contains("-ui-testing-editor-captured-creation") {
            // External captures are not built-in deterministic fixture shapes:
            // they share source-preview behavior but require a supplied draft.
            return Fixture(shape: .sourceText, draft: capturedCreation)
        }
        if arguments.contains("-ui-testing-editor-gameday-reset") { UserDefaults.standard.removeObject(forKey: gamedaySavedKey) }
        let shape = Shape.allCases.first { arguments.contains("-ui-testing-editor-\($0.rawValue)") } ?? .twoText
        var value = draft(for: shape)
        if arguments.contains("-ui-testing-editor-text-bend") {
            // KRI-646: the server advertises `editor_capabilities.text_bend` to a protocol-9 build on a device-rendered variant.
            var caps = value.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
            caps["text_bend"] = .bool(true)
            value.serverSnapshot["editor_capabilities"] = .object(caps)
        }
        if arguments.contains("-ui-testing-editor-caption-animation") {
            // KRI-1007: the server advertises `editor_capabilities.caption_animation` to a protocol-24 build.
            var caps = value.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
            caps["caption_animation"] = .bool(true)
            caps["caption_editor_style"] = .bool(true)
            value.serverSnapshot["editor_capabilities"] = .object(caps)
        }
        if arguments.contains("-ui-testing-editor-caption-style-scope") {
            var caps = value.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
            caps["caption_style_scope"] = .bool(true)
            caps["caption_editor_style"] = .bool(true)
            caps["caption_meta"] = .object(["editable": .bool(true)])
            caps["caption_cues"] = .object(["editable": .bool(true)])
            value.serverSnapshot["editor_capabilities"] = .object(caps)
        }
        if arguments.contains("-ui-testing-editor-caption-text-motion") {
            // An all-captions entrance stored by Style > Text motion before it folded into Animation (KRI-1007).
            var payload = value.serverSnapshot["editor_payload"]?.objectValue ?? [:]
            var sections = payload["sections"]?.objectValue ?? [:]
            var meta = sections["caption_meta"]?.objectValue ?? [:]
            var appearance = meta["appearance"]?.objectValue ?? [:]
            appearance["animation_phases"] = .object([
                "entrance": .string("silkReveal"), "exit": .string("none"), "loop": .string("none"), "speed": .number(1),
            ])
            appearance["highlight_spoken_word"] = .bool(false)
            meta["appearance"] = .object(appearance)
            meta["style"] = .string("sentence")
            sections["caption_meta"] = .object(meta)
            payload["sections"] = .object(sections)
            value.serverSnapshot["editor_payload"] = .object(payload)
        }
        if arguments.contains("-ui-testing-editor-research-sources") {
            // Offline UI coverage only; live research is verified separately on device.
            var payload = value.serverSnapshot["editor_payload"]?.objectValue ?? [:]
            var sections = payload["sections"]?.objectValue ?? [:]
            var elements = sections["text_elements"]?.arrayValue ?? []
            if var first = elements.first?.objectValue {
                first["source_params"] = .object(["research": .object([
                    "original_text": first["text"] ?? .string(""),
                    "start_s": first["start_s"] ?? .number(0),
                    "end_s": first["end_s"] ?? .number(1.5),
                    "media_ids": .array([]),
                    "timeline_binding": sections["timeline_slots"] ?? .array([]),
                    "sources": .array([.object([
                        "title": .string("Museum fixture source"),
                        "url": .string("https://museum.example.org/collection")
                    ])]),
                    "retrieved_at": .string("2026-10-19T00:00:00Z")
                ])])
                elements[0] = .object(first)
                sections["text_elements"] = .array(elements)
                payload["sections"] = .object(sections)
                value.serverSnapshot["editor_payload"] = .object(payload)
            }
        }
        return Fixture(shape: shape, draft: value)
    }

    static func draft(for shape: Shape) -> EditorDraft {
        switch shape {
        case .legacyVisuals: legacyVisuals
        case .captionVisuals: captionVisuals
        case .projectedCaptions: projectedCaptions
        case .sourceText: sourceText
        case .selectionBoundary: selectionBoundary
        case .gamedayAdd: gamedayAdd
        case .twoText: twoText
        case .boundary: boundary
        case .allLanes: allLanes
        case .stress: stress
        case .unknown: unknownSections
        case .autoScrollExtend: autoScrollExtend
        case .guidedText: guidedText
        case .talkingCaptions: talkingCaptions
        case .songCaptions: songCaptions
        case .retimedClips: retimedClips
        case .songTrim: songTrim
        case .drawing: drawing
        case .speakerBackground: speakerBackground
        case .reorderClips: reorderClips
        case .talkingRange: talkingRange
        }
    }

    /// The Talking Range windows the stub commit transport last saved (UserDefaults, so a relaunch "reopens" them).
    static let savedTalkingRangeKey = "kria.ui-test.saved-talking-range.v1"

    /// Three kept segments of one 4.6 s take (the bundled preview footage is 4.67 s) at source 0-1, 1.5-2.5 and
    /// 3-4 s, output 0-1, 1-2 and 2-3 s, with one caption in each. The server keeps the timeline closed for Talking
    /// and opens only Range. Needs `-ui-testing-editor-live-preview` for the compiled preview the sheet reads.
    static let talkingRange: EditorDraft = {
        let windows: [(id: String, start: Double)] = [
            ("native-phone-talking-source", 0), ("native-phone-talking-source-1", 1.5), ("native-phone-talking-source-2", 3),
        ]
        let clips = windows.enumerated().map { index, window in
            EditorClip(id: id(700 + index), assetID: id(1_700), sourceClipIndex: 0,
                       start: Double(index), end: Double(index + 1), trimIn: window.start, trimOut: window.start + 1,
                       sourceDuration: 4.6, slotID: window.id)
        }
        let closed = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-talking-range-closed")
        let leaf: JSONValue = closed
            ? .object(["editable": .bool(false), "reason": .string("talking_range_multi_clip")])
            : .object(["editable": .bool(true)])
        let words = ["First part", "Second part", "Third part"]
        return draft(clips: clips, text: [], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "caption_cues": .array(words.enumerated().map { index, text in
                .object(["id": .string("talking-range-cue-\(index)"), "text": .string(text),
                         "start_s": .number(Double(index) + 0.1), "end_s": .number(Double(index) + 0.9)])
            }),
            "caption_meta": .object(["enabled": .bool(true), "style": .string("sentence")]),
        ], rootExtras: [
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "editor_capabilities": .object([
                "timeline": .bool(false),
                "caption_cues": .object(["editable": .bool(true)]),
                "caption_meta": .object(["editable": .bool(true)]),
                "text_elements": .object(["editable": .bool(false), "reason": .string("Text isn’t editable for this edit on this iPhone.")]),
                "clips": .object(["source_window": leaf]),
            ]),
        ])
    }()

    /// The drawings the stub commit transport last saved (UserDefaults, so a relaunch "reopens" them).
    static let savedDrawingsKey = "kria.ui-test.saved-drawings.v1"
    /// KRI-1136: the first timeline slot's saved `tilt`, kept across relaunches by the stub commit transport.
    static let savedTiltKey = "kria.ui-test.saved-tilt.v1"

    /// The `speaker_background` value the stub commit transport last saved (UserDefaults; JSON `null` = cleared).
    static let savedSpeakerBackgroundKey = "kria.ui-test.saved-speaker-background.v1"

    /// The two pool visuals the Background fixture offers; no URLs, so the tiles show their placeholders offline.
    static let speakerBackgroundVisuals: [CreationVisual] = [
        CreationVisual(id: "fixture-bg-photo", kind: "image", status: "ready", sourceFilename: "Beach photo", displayURL: nil, previewURL: nil, retryable: nil),
        CreationVisual(id: "fixture-bg-video", kind: "video", status: "ready", sourceFilename: "City video", displayURL: nil, previewURL: nil, retryable: nil),
    ]

    static var speakerBackground: EditorDraft {
        let defaults = UserDefaults.standard
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-speaker-background-reset") {
            defaults.removeObject(forKey: savedSpeakerBackgroundKey)
        }
        var value = talkingCaptions
        var caps = value.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background"] = .object(["editable": .bool(true)])
        value.serverSnapshot["editor_capabilities"] = .object(caps)
        if let data = defaults.data(forKey: savedSpeakerBackgroundKey), let saved = try? JSONDecoder().decode(JSONValue.self, from: data) {
            var payload = value.serverSnapshot["editor_payload"]?.objectValue ?? [:]
            var sections = payload["sections"]?.objectValue ?? [:]
            sections["speaker_background"] = saved
            payload["sections"] = .object(sections)
            value.serverSnapshot["editor_payload"] = .object(payload)
        }
        return value
    }

    static let drawing: EditorDraft = {
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-drawing-reset") {
            UserDefaults.standard.removeObject(forKey: savedDrawingsKey)
        }
        let clips = [clip(0, start: 0, duration: 6)]
        var sections: [String: JSONValue] = ["timeline_slots": slots(for: clips)]
        if let data = UserDefaults.standard.data(forKey: savedDrawingsKey), let saved = try? JSONDecoder().decode(JSONValue.self, from: data) {
            sections["drawings"] = saved
        }
        return draft(clips: clips, text: [], captions: false, music: false, sections: sections, rootExtras: ["editor_capabilities": .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "drawings": .object(["add": .bool(true), "edit": .bool(true), "trim": .bool(true), "delete": .bool(true)]),
        ])])
    }()

    /// Reference-only music, so the editor shows the posting-song bar above
    /// the preview (`-ui-testing-editor-song-reference`).
    static let songReferenceVariant: [String: JSONValue] = [
        "music_playback_mode": .string("reference_only"),
        "song_reference": .object([
            "track_id": .string("fixture-song"), "title": .string("Waka Waka (This Time for Africa)"),
            "artist": .string("Shakira"), "start_s": .number(55.71), "end_s": .number(68.71)
        ])
    ]

    /// A montage built on the creator's own song (KRI-374): reference-only, camera audio
    /// muted, and the additive `user_song` field the Sounds tab reads
    /// (`-ui-testing-editor-user-song`).
    static let userSongVariant: [String: JSONValue] = [
        "music_playback_mode": .string("reference_only"),
        "source_audio_preserved": .bool(false),
        "user_song": .object([
            "title": .string("Midnight Drive"), "mode": .string("background"),
            "duration_s": .number(214), "window_start_s": .number(108), "window_end_s": .number(123),
            "volume": .number(0.8)
        ])
    ]

    /// The same montage with a lip-sync song (`-ui-testing-editor-user-song-lipsync`): the start is locked
    /// (or, with `user_song.trim`, trimmed by cutting the video), volume and remove stay editable.
    static let userSongLipSyncVariant: [String: JSONValue] = {
        var variant = userSongVariant
        var song = variant["user_song"]?.objectValue ?? [:]
        song["mode"] = .string("lipsync")
        variant["user_song"] = .object(song)
        return variant
    }()

    /// A background song over the `song-trim` shape: a short 40 s song so the two trim handles sit well apart on
    /// the bar (10 s of video between 0:10 and 0:20).
    static let userSongBackgroundTrimVariant: [String: JSONValue] = {
        var variant = userSongVariant
        var song = variant["user_song"]?.objectValue ?? [:]
        song["duration_s"] = .number(40)
        song["window_start_s"] = .number(10)
        song["window_end_s"] = .number(20)
        variant["user_song"] = .object(song)
        variant["duration_s"] = .number(10)
        return variant
    }()

    /// A lip-sync song over the `song-trim` shape: the four takes sit in song order, 2.5 s apart, so every cut
    /// votes for the same song start (108 s) and the window is exactly the 10 s video.
    static let userSongLipSyncTrimVariant: [String: JSONValue] = {
        var variant = userSongLipSyncVariant
        var song = variant["user_song"]?.objectValue ?? [:]
        song["window_end_s"] = .number(118)
        song["takes"] = .object(["take-10": .number(108), "take-11": .number(110.5),
                                 "take-12": .number(113), "take-13": .number(115.5)])
        variant["user_song"] = .object(song)
        variant["duration_s"] = .number(10)
        return variant
    }()

    /// The delta of each take by the clip index the `song-trim` shape gives it (the source pool's job on a real load).
    static let songTrimTakeDeltas: [Int: Double] = [10: 108, 11: 110.5, 12: 113, 13: 115.5]

    /// The `user_song.*` capabilities the server advertises for a creator-song variant. `trim` is the
    /// KRI-561 capability; an older server omits it (`trim: false`) and keeps the single-handle bar / lock.
    static func userSongCapabilities(lipSync: Bool, trim: Bool = true) -> [String: EditorCapability] {
        var capabilities: [String: EditorCapability] = [
            "user_song.volume": EditorCapability(editable: true),
            "user_song.window": lipSync
                ? EditorCapability(editable: false, reason: "user_song_lipsync_locked")
                : EditorCapability(editable: true),
            "user_song.remove": EditorCapability(editable: true),
            // The footage's own sound: one level for the video, and a switch per clip.
            "original_audio": EditorCapability(editable: true),
            "clips.audio": EditorCapability(editable: true),
        ]
        if trim { capabilities["user_song.trim"] = EditorCapability(editable: true) }
        return capabilities
    }

    static let songTrim: EditorDraft = {
        let clips = (0..<4).map { clip(10 + $0, start: Double($0) * 2.5, duration: 2.5, sourceDuration: 12) }
        return draft(clips: clips, text: [], captions: false, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "title": .string("Song trim fixture"),
        ])
    }()

    static let legacyVisuals: EditorDraft = {
        var value = captionVisuals
        var caps = value.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps.removeValue(forKey: "visual_editor_style")
        value.serverSnapshot["editor_capabilities"] = .object(caps)
        return value
    }()

    static let captionVisuals: EditorDraft = {
        let clips = [clip(0, start: 0, duration: 4)]
        return draft(clips: clips, text: [], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "caption_cues": .array([.object(["id": .string("cue-paper"), "text": .string("One more game!"), "start_s": .number(0), "end_s": .number(2)])]),
            "caption_meta": .object(["enabled": .bool(true), "style": .string("sentence")]),
            "visual_blocks": .array([.object(["id": .string("paper-media"), "kind": .string("media"),
                "version": .number(1), "start_s": .number(1), "end_s": .number(3), "media_kind": .string("video"),
                "asset_id": .string("fixture"), "src_gcs_path": .string("fixture.mp4"), "source_duration_s": .number(4),
                "display_mode": .string("overlay"), "scale": .number(0.35), "x_frac": .number(0.5), "y_frac": .number(0.5),
                "editor_style": .object(NativeVisualAuthoring.defaultStyle)])])
        ], rootExtras: ["editor_capabilities": .object([
            "text_elements": .bool(true), "timeline": .bool(true), "visual_blocks": .bool(true),
            "camera_effects": .bool(true), "motion_scenes": .bool(true), "caption_editor_style": .bool(true),
            "visual_editor_style": .bool(true)])])
    }()

    static let gamedaySavedKey = "native-editor-gameday-saved"
    static let gamedayPoolKey = "native-editor-gameday-pool"
    static let gamedayRecipeKey = "native-editor-gameday-recipe"
    static var gamedayAdd: EditorDraft {
        if let data = UserDefaults.standard.data(forKey: gamedaySavedKey),
           let saved = try? JSONDecoder().decode(EditorDraft.self, from: data) { return saved }
        let clips = (0..<4).map { clip($0, start: Double($0), duration: 1) }
        let layer = text(100, content: "Gameday", x: 0.2, y: 0.25)
        return draft(clips: clips, text: [layer], captions: false, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array([textRecord(layer, start: 0, end: 4, z: 1,
                extra: ["font_family": .string("Inter"), "size_px": .number(72)])]),
        ])
    }

    static let sourceText: EditorDraft = {
        let clips = [clip(0, start: 0, duration: 2), clip(1, start: 2, duration: 2)]
        let layer = text(100, content: "Your story starts here", x: 0.5, y: 0.4)
        return draft(clips: clips, text: [layer], captions: false, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array([textRecord(layer, start: 0, end: 4, z: 1,
                extra: ["font_family": .string("Inter"), "size_px": .number(72)])]),
        ])
    }()

    static let selectionBoundary: EditorDraft = {
        let clips = [clip(0, start: 0, duration: 2.001), clip(1, start: 2.001, duration: 2)]
        let layer = text(100, content: "Selected text", x: 0.5, y: 0.4)
        return draft(clips: clips, text: [layer], captions: false, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array([textRecord(layer, start: 2.001, end: 4.001, z: 1,
                extra: ["font_family": .string("Inter"), "size_px": .number(72)])]),
        ])
    }()

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

    /// A unified montage's text lane (KRI-185): the title and a label per clip, plus
    /// a caption, a removed label and the creator's own text. The Text tab lists
    /// the title, both labels and the creator's text, in that order.
    static let guidedText: EditorDraft = {
        let clips = [clip(4, start: 0, duration: 2), clip(5, start: 2, duration: 2)]
        func record(_ id: String, _ text: String, _ start: Double, _ end: Double, y: Double,
                    extra: [String: JSONValue] = [:]) -> JSONValue {
            var value: [String: JSONValue] = [
                "id": .string(id), "text": .string(text), "start_s": .number(start), "end_s": .number(end),
                "x_frac": .number(0.5), "y_frac": .number(y), "font_family": .string("Inter"),
                "size_px": .number(58), "z": .number(1),
            ]
            value.merge(extra) { _, new in new }
            return .object(value)
        }
        return draft(clips: clips, text: [], captions: false, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array([
                record("clip-label-unified-cut-2", "Dolmabahçe Palace", 2, 4, y: 0.78),
                record("clip-label-unified-cut-1", "Galata Tower", 0, 2, y: 0.78),
                record("guided-title", "20K Run · Arnavutkoy → Eminonu", 0, 2.2, y: 0.16),
                record("creator-note", "My note", 1, 3, y: 0.5),
                record("caption-1", "Spoken words", 0.5, 1.5, y: 0.85,
                       extra: ["source_params": .object(["source": .string("caption_cue")])]),
                record("clip-label-unified-cut-3", "Removed label", 3, 4, y: 0.78,
                       extra: ["removed": .bool(true)]),
            ]),
        ], rootExtras: ["editor_capabilities": .object(["text_elements": .bool(true), "timeline": .bool(true)])])
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

    static let projectedCaptions: EditorDraft = {
        let clips = [clip(3, start: 0, duration: 4)]
        let title = text(300, content: "Title", x: 0.5, y: 0.3)
        let caption = text(301, content: "Spoken words", x: 0.5, y: 0.8)
        return draft(clips: clips, text: [title, caption], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array([
                textRecord(title, start: 0, end: 2, z: 3),
                .object(["id": .string(id(301).uuidString), "text": .string("Spoken words"),
                         "start_s": .number(0), "end_s": .number(2), "role": .string("generative_intro"),
                         "source_params": .object(["source": .string("caption_cue")])])
            ])
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
                        "background_music": .object(["track_id": .string("fixture-background"), "enabled": .bool(true), "start_s": .number(0), "end_s": .number(6), "gain_db": .number(-6), "muted": .bool(false)]),
                        "title": .string("All lanes fixture"),
                        "orientation": .string("9:16"),
                        "lyrics": .object(["enabled": .bool(true), "text": .string("Fixture lyrics")]),
                        "sound_effects": .array([.object(["id": .string("sfx-1"), "src_gcs_path": .string("sound-effects/fixture.wav"), "at_s": .number(0.5), "trim_start_s": .number(0), "trim_end_s": .number(0.3), "duration_s": .number(1), "gain": .number(0.8), "label": .string("Whoosh")])]),
                        "media_overlays": .array([.object(["id": .string("overlay-1"), "kind": .string("image"), "start_s": .number(1), "end_s": .number(2), "z": .number(4), "src_gcs_path": .string("users/fixture/plan/fixture/overlays/overlay.png"), "display_mode": .string("pip"), "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.5), "scale": .number(0.35)])]),
                        "visual_blocks": .array([.object(["version": .number(1), "id": .string("visual-1"), "kind": .string("text_card"), "start_s": .number(2), "end_s": .number(3), "timing_mode": .string("manual"), "origin": .string("user"), "style_preset_id": .string("text-card-default"), "background": .object(["type": .string("solid"), "color": .string("#111111")])])]),
                        "motion_scenes": .array([.object(["id": .string("motion-1"), "start_frame": .number(90), "end_frame_exclusive": .number(120), "preset_id": .string("route_trace"), "preset_version": .number(1), "palette": .object(["primary": .string("#FFFFFF"), "accent": .string("#84CC16")]), "intensity": .number(0.7)])]),
                        "motion_runtime_hash": .string("fixture-required-hash"),
                        "camera_effects": .array([.object(["id": .string("camera-1"), "token": .string("semantic_crop_pulse"), "start_s": .number(4), "end_s": .number(5), "intensity": .number(0.04), "easing": .string("sine_pulse"), "source": .string("user")])]),
                        "carousel_moment": .object(["id": .string("carousel-1"), "position": .string("middle"), "mode": .string("rolling"), "effect": .string("cover_flow"), "duration_s": .number(2), "transition_in": .string("none"), "transition_out": .string("none"), "timing_model": .string("ripple_v1")]),
                     ], rootExtras: [
                        "editor_capabilities": .object([
                            "sfx": .object(["editable": .bool(true)]),
                            "overlays": .object(["editable": .bool(true)]),
                            "visual_blocks": .object(["editable": .bool(true)]),
                            "motion_scenes": .bool(false),
                            "motion_scenes_reason": .string("motion_runtime_mismatch"),
                            "motion_runtime_hash": .string("fixture-editor-hash"),
                            "motion_required_runtime_hash": .string("fixture-required-hash"),
                            "camera_effects": .object(["editable": .bool(true)]),
                            "carousel": .object(["editable": .bool(true)]),
                            "background_music": .object(["editable": .bool(true)]),
                            "orientation": .object(["editable": .bool(false), "reason": .string("Orientation is fixed by the rendered variant.")]),
                            "lyrics": .object(["editable": .bool(false), "reason": .string("Lyrics are supplied by the rendered variant.")]),
                        ])
                     ])
    }()

    static let visualMedia: EditorDraft = {
        let clips = [clip(700, start: 0, duration: 4)]
        return draft(
            clips: clips,
            text: [],
            captions: false,
            music: false,
            sections: [
                "timeline_slots": slots(for: clips),
                "visual_blocks": .array([.object([
                    "version": .number(1),
                    "id": .string("visual-media-1"),
                    "kind": .string("media"),
                    "start_s": .number(0.5),
                    "end_s": .number(2.5),
                    "timing_mode": .string("manual"),
                    "origin": .string("user"),
                    "asset_id": .string("fixture-asset"),
                    "src_gcs_path": .string("users/fixture/visual.png"),
                    "media_kind": .string("image"),
                    "display_mode": .string("fullscreen"),
                    "transform": .object([
                        "fit_mode": .string("contain"),
                        "focal_x": .number(0.5),
                        "focal_y": .number(0.5),
                        "zoom": .number(1),
                    ]),
                    "x_frac": .number(0.5),
                    "y_frac": .number(0.5),
                    "scale": .number(0.35),
                    "z": .number(0),
                ])]),
            ],
            rootExtras: ["editor_capabilities": .object(["visual_blocks": .bool(true)])]
        )
    }()

    /// KRI-182 step 1: a phone-rendered `subtitled` variant with its editable
    /// sfx + overlay lanes (one catalog-resolved sound effect, one "fade"
    /// image media overlay), `text_elements` closed. Mirrors the server's
    /// `project_phone_subtitled_editor_sections` projection shape.
    static let phoneSubtitledLanes: EditorDraft = {
        let clips = [clip(800, start: 0, duration: 4)]
        return draft(clips: clips, text: [], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "sound_effects": .array([.object([
                "id": .string("phone-sfx-1"), "sound_effect_id": .string("catalog-buzzer"),
                "src_gcs_path": .string("sound-effects/buzzer.wav"), "at_s": .number(1),
                "gain": .number(1), "duration_s": .number(0.6), "label": .string("Buzzer"),
                "source": .string("phone_lane"),
            ])]),
            "media_overlays": .array([.object([
                "id": .string("phone-overlay-1"), "kind": .string("image"),
                "src_gcs_path": .string("users/fixture/plan/fixture/overlays/overlay.png"),
                "display_mode": .string("pip"), "x_frac": .number(0.5), "y_frac": .number(0.72),
                "scale": .number(0.32), "start_s": .number(0.5), "end_s": .number(3), "z": .number(1),
                "entrance_token": .string("fade"), "exit_token": .string("fade"), "source": .string("phone_lane"),
            ])]),
        ], rootExtras: [
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "editor_capabilities": .object([
                "sfx": .object(["editable": .bool(true)]),
                "sound_effects": .object(["editable": .bool(true)]),
                "overlays": .object(["editable": .bool(true)]),
                "media_overlays": .object(["editable": .bool(true)]),
                "text_elements": .object(["editable": .bool(false), "reason": .string("Text isn’t editable for this edit on this iPhone.")]),
            ]),
        ])
    }()

    /// A Talking (subtitled) edit rendered on this iPhone, shaped like the
    /// API's payload: each `caption_cues` row also comes back in
    /// `text_elements` as a `caption_cue`-tagged mirror with its own hex id
    /// and the cue's window (`_base_text_elements_for_variant`, CAPTION path).
    /// The timeline must still show one CAPTIONS row.
    static let talkingCaptions: EditorDraft = {
        let clips = [clip(900, start: 0, duration: 6)]
        let cues: [(text: String, start: Double, end: Double)] = [("Bu alan var mı?", 1.2, 3.7), ("Evet, boş.", 3.7, 5.6)]
        return draft(clips: clips, text: [], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "caption_cues": .array(cues.map { .object(["text": .string($0.text), "start_s": .number($0.start), "end_s": .number($0.end)]) }),
            "caption_meta": .object(["enabled": .bool(true), "style": .string("sentence")]),
            "text_elements": .array(cues.enumerated().map { index, cue in .object([
                "id": .string(String(format: "%032x", 0x901 + index)), "text": .string(cue.text),
                "start_s": .number(cue.start), "end_s": .number(cue.end), "role": .string("generative_sequence"),
                "position": .string("bottom"), "alignment": .string("center"), "effect": .string("static"),
                "source_params": .object(["source": .string("caption_cue"), "key": .string(String(index)),
                                          "source_text": .string(cue.text)]),
            ]) }),
        ], rootExtras: [
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "editor_capabilities": .object([
                "timeline": .bool(true),
                "caption_cues": .object(["editable": .bool(true)]),
                "caption_meta": .object(["editable": .bool(true)]),
                "text_elements": .object(["editable": .bool(false), "reason": .string("Text isn’t editable for this edit on this iPhone.")]),
            ]),
        ])
    }()

    static let songCaptions: EditorDraft = {
        let clips = [clip(910, start: 0, duration: 6)]
        let lines = [["Midnight", "drive", "again"], ["Hold", "on", "tight"]]
        var rows: [JSONValue] = []
        var start = 0.6
        for (lineIndex, words) in lines.enumerated() {
            for (wordIndex, word) in words.enumerated() {
                var params: [String: JSONValue] = ["source": .string("caption_cue")]
                if wordIndex == words.count - 1 { params["line_end"] = .bool(true) }
                rows.append(.object([
                    "id": .string("song-caption-\(lineIndex)-\(wordIndex)"), "text": .string(word),
                    "start_s": .number(start), "end_s": .number(start + 0.8), "role": .string("generative_narration_caption"),
                    "position": .string("bottom"), "alignment": .string("center"), "source_params": .object(params),
                ]))
                start += 0.8
            }
            start += 0.4
        }
        return draft(clips: clips, text: [], captions: true, music: false, sections: [
            "timeline_slots": slots(for: clips),
            "text_elements": .array(rows),
            "caption_meta": .object(["enabled": .bool(true), "style": .string("sentence")]),
        ], rootExtras: [
            "render_destination": .string("device"),
            "editor_capabilities": .object([
                "timeline": .bool(true), "text_elements": .bool(true),
                "caption_meta": .object(["editable": .bool(true)]), "caption_editor_style": .bool(true),
            ]),
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

    /// KRI-165: 15s of timeline (well beyond one 6s viewport width at 1x
    /// zoom) so a text-block move/trim genuinely needs the clock to
    /// auto-scroll to reach past what's visible on launch, rather than a
    /// single one-shot drag translation reaching it unaided. Clip 1 also
    /// carries far more `source_duration_s` headroom (20s) than its 5s
    /// on-timeline duration, so extending its trailing trim past the
    /// visible window likewise requires auto-scroll to actually fire.
    /// KRI-281: a narrated/voiceover edit whose first shot's source (1.45s
    /// usable) is shorter than its 2.6s timeline window (slowed to fill it).
    /// Video-lane blocks must still abut.
    static let retimedClips: EditorDraft = {
        let clips = [
            clip(900, start: 0, duration: 2.6, sourceDuration: 1.5),
            clip(901, start: 2.6, duration: 3, sourceDuration: 10),
        ]
        var slotValues = slots(for: clips)
        if case .array(var items) = slotValues, case .object(var first) = items[0] {
            first["native_source_span_s"] = .number(2.45)
            items[0] = .object(first)
            slotValues = .array(items)
        }
        return draft(clips: clips, text: [], captions: false, music: false,
                     sections: ["timeline_slots": slotValues, "title": .string("Retimed fixture")])
    }()

    /// DEBUG-only bridge for simulator evidence. The path is supplied by the
    /// repeatable fixture generator and must contain an `EditorDraft` whose
    /// serverSnapshot is the persisted, compiler-produced server draft.
    static let capturedCreation: EditorDraft = {
        let supplied = ProcessInfo.processInfo.environment["KRIA_UI_FIXTURE_DRAFT"] ?? ""
        let path: String
        if supplied.hasPrefix("Documents/") {
            path = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent(String(supplied.dropFirst("Documents/".count))).path
        } else { path = supplied }
        guard let data = FileManager.default.contents(atPath: path) else {
            preconditionFailure("KRI-524 captured editor fixture requires KRIA_UI_FIXTURE_DRAFT")
        }
        do { return try JSONDecoder().decode(EditorDraft.self, from: data) }
        catch { preconditionFailure("KRI-524 captured editor fixture is invalid: \(error)") }
    }()

    static let speedControls: EditorDraft = {
        let clips = [clip(960, start: 0, duration: 6, sourceDuration: 20)]
        return draft(clips: clips, text: [], captions: false, music: false,
                     sections: ["timeline_slots": slots(for: clips)], rootExtras: [
                        "render_destination": .string("device"),
                        "editor_timeline_mode": .string("authored"),
                        "editor_capabilities": .object([
                            "timeline": .bool(true),
                            "clips": .object(["playback_rate": .object(["editable": .bool(true)])]),
                        ]),
                     ])
    }()

    static let reorderClips: EditorDraft = {
        let clips = [
            clip(950, start: 0, duration: 1),
            clip(951, start: 1, duration: 1.4),
            clip(952, start: 2.4, duration: 1.2),
        ]
        return draft(clips: clips, text: [], captions: false, music: false,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "title": .string("Reorder fixture"),
                        "orientation": .string("9:16"),
                     ])
    }()

    static let autoScrollExtend: EditorDraft = {
        // Clip 1 is short (2s) so its trailing handle is fully on screen at
        // launch (zoom 1 shows roughly ±3s around the playhead) — its
        // trailing trim reaches the visible edge with room to spare, and
        // must auto-scroll from there to use its 18s of source headroom.
        let clips = [
            clip(800, start: 0, duration: 2, sourceDuration: 20),
            clip(801, start: 2, duration: 7),
            clip(802, start: 9, duration: 6),
        ]
        let layer = text(850, content: "Auto-scroll target", x: 0.5, y: 0.5)
        return draft(clips: clips, text: [layer], captions: false, music: false,
                     sections: [
                        "timeline_slots": slots(for: clips),
                        "text_elements": .array([textRecord(layer, start: 0.2, end: 1, z: 1)]),
                        "title": .string("Auto-scroll fixture"),
                        "orientation": .string("9:16"),
                     ])
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
        clip(index, start: start, duration: duration, sourceDuration: max(duration, 7.2))
    }

    /// Explicit `sourceDuration` override for fixtures that need trailing
    /// trim headroom beyond the default (e.g. KRI-165's auto-scroll tests).
    private static func clip(_ index: Int, start: Double, duration: Double, sourceDuration: Double) -> EditorClip {
        EditorClip(id: id(index), assetID: id(1_000 + index % 43), sourceClipIndex: index % 43,
                   start: start, end: start + duration, trimIn: 0, trimOut: duration,
                   sourceDuration: sourceDuration, slotID: "slot-\(index)")
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

/// KRI-589: answers `editor-commit` for the drawing fixture. The request is the one production sends; the
/// drawings it carries are kept so the next launch reopens them, and every section it names is acknowledged.
enum NativeEditorUITestCommitTransport {
    static func api() -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CommitURLProtocol.self]
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") {
            URLProtocol.registerClass(GeneratedMediaImageFixtureProtocol.self)
        }
        return KriaAPI(baseURL: URL(string: "https://editor-ui-testing.invalid")!, tokenStore: UITestTokenStore(), session: URLSession(configuration: configuration))
    }

    private struct UITestTokenStore: TokenStore {
        func read() throws -> MobileSession? { nil }
        func write(_ session: MobileSession) throws {}
        func delete() throws {}
    }

    private final class FailOnce: @unchecked Sendable {
        private let lock = NSLock()
        private var failed = false
        func takeFailure() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if failed { return false }
            failed = true
            return true
        }
    }

    private final class CommitURLProtocol: URLProtocol, @unchecked Sendable {
        static let capabilityFailures = FailOnce()
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

        override func startLoading() {
            guard let url = request.url else { client?.urlProtocol(self, didFailWithError: URLError(.badURL)); return }
            var status = 404
            var body = Data(#"{"detail":"not stubbed"}"#.utf8)
            if url.path.hasSuffix("/capabilities"), ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") {
                // KRIA_OVERLAY_CAPABILITY_FAIL_ONCE: the first account check of this launch fails, the retry succeeds.
                if ProcessInfo.processInfo.environment["KRIA_OVERLAY_CAPABILITY_FAIL_ONCE"] == "1", Self.capabilityFailures.takeFailure() {
                    client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
                    return
                }
                status = 200; body = Data(#"{"formats":[],"generated_images":true}"#.utf8)
            } else if url.path.contains("/generated-media"), ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") {
                if ProcessInfo.processInfo.environment["KRIA_OVERLAY_REGISTRATION_RECOVERY"] == "1",
                   url.path.hasSuffix("/generated-media"), request.httpMethod == "POST" {
                    UserDefaults.standard.set(UserDefaults.standard.integer(forKey: "overlay-fixture-generation-count") + 1, forKey: "overlay-fixture-generation-count")
                }
                let count = UserDefaults.standard.integer(forKey: "overlay-fixture-generation-count")
                let generatedID = count > 1 ? "fixture-generated-image-b" : "fixture-generated-image"
                let imageURL = "https://generated-media-image.invalid/cover.jpg"
                let response: [String: JSONValue] = ["id": .string("fixture-overlay-operation"), "state": .string("ready"),
                    "capability": .string("image"), "retryable": .bool(false), "asset_id": .string(generatedID),
                    "preview_url": .string(imageURL), "source_url": .string(imageURL)]
                status = 200; body = (try? JSONEncoder().encode(response)) ?? body
            } else if url.path.hasSuffix("/assets"), ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") {
                let response: [String: JSONValue] = ["max_assets": .number(20), "assets": .array(["fixture-generated-image", "fixture-generated-image-b"].map { id in .object([
                    "id": .string(id), "kind": .string("image"), "status": .string("ready"),
                    "source_filename": .string("Generated fixture.jpg"), "gcs_path": .string("fixture/generated.jpg"),
                    "source_url": .string("https://generated-media-image.invalid/cover.jpg")
                ]) })]
                status = 200; body = (try? JSONEncoder().encode(response)) ?? body
            } else if url.path.hasSuffix("/overlay-plan") {
                var data = request.httpBody ?? Data()
                if data.isEmpty, let stream = request.httpBodyStream {
                    stream.open(); defer { stream.close() }
                    var buffer = [UInt8](repeating: 0, count: 4096)
                    while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(buffer, count: count) }
                }
                let fields = (try? JSONDecoder().decode([String: JSONValue].self, from: data)) ?? [:]
                if fields["mode"] == .string("automatic"), fields["prompt"] != .string("") {
                    client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
                    return
                }
                let response: [String: JSONValue] = [
                    "document_fingerprint": fields["document_fingerprint"] ?? .string(""),
                    "base_generation": fields["base_generation"] ?? .string(""),
                    "message": .string(ProcessInfo.processInfo.environment["KRIA_OVERLAY_MIXED"] == "1"
                        ? "Synthetic offline overlay plan. Upload the real logo or choose it from your visual library."
                        : "Synthetic offline overlay plan."),
                    "overlays": .array([.object([
                        "id": .string("DCC14763-5702-4AE3-8510-8C72845E6B15"), "kind": .string(ProcessInfo.processInfo.environment["KRIA_OVERLAY_IMAGE"] == "1" ? "image" : "text"),
                        "title": .string("Synthetic graphic"), "prompt": .string("A synthetic image for native UI testing"), "text": .string("10"),
                        "start_s": .number(0.5), "end_s": .number(1.5),
                        "x_frac": .number(0.2), "y_frac": .number(0.3), "scale": .number(0.2),
                        "reason": .string("Synthetic placement at 0.5 seconds.")
                    ])])
                ]
                status = 200; body = (try? JSONEncoder().encode(response)) ?? body
            } else if url.path.hasSuffix("/device-render"), let data = UserDefaults.standard.data(forKey: NativeEditorUITestFixtures.gamedayRecipeKey) {
                status = 200; body = data
            } else if url.path.hasSuffix("/timeline"), let data = UserDefaults.standard.data(forKey: NativeEditorUITestFixtures.gamedayPoolKey) {
                status = 200; body = data
            } else if url.path.hasSuffix("/editor-sources") {
                var data = request.httpBody ?? Data()
                if data.isEmpty, let stream = request.httpBodyStream {
                    stream.open(); defer { stream.close() }
                    var buffer = [UInt8](repeating: 0, count: 4096)
                    while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(buffer, count: count) }
                }
                let fields = (try? JSONDecoder().decode([String: JSONValue].self, from: data)) ?? [:]
                if ProcessInfo.processInfo.environment["KRIA_OVERLAY_REGISTRATION_RECOVERY"] == "1" {
                    let key = fields["client_import_id"]?.stringValue ?? ""
                    let source = fields["source_id"]?.stringValue ?? ""
                    var registrations = UserDefaults.standard.dictionary(forKey: "overlay-fixture-registrations") as? [String: String] ?? [:]
                    if let existing = registrations[key], existing != source {
                        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 409, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
                        client?.urlProtocol(self, didLoad: Data(#"{"detail":"import_id_reused"}"#.utf8))
                        client?.urlProtocolDidFinishLoading(self)
                        return
                    }
                    let first = registrations.isEmpty
                    registrations[key] = source
                    UserDefaults.standard.set(registrations, forKey: "overlay-fixture-registrations")
                    if first {
                        client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
                        return
                    }
                }
                let response: [String: JSONValue] = ["import_id": fields["client_import_id"] ?? .string(UUID().uuidString),
                    "status": .string("ready"), "source_id": .string("gameday-istanbul-source-4"), "source_index": .number(4),
                    "source": .object(["media_id": .string("gameday-istanbul-source-4"), "duration_s": .number(4)]), "retryable": .bool(false)]
                status = 200; body = (try? JSONEncoder().encode(response)) ?? body
            } else if url.path.hasSuffix("/editor-commit") {
                var data = request.httpBody ?? Data()
                if data.isEmpty, let stream = request.httpBodyStream {
                    stream.open(); defer { stream.close() }
                    var buffer = [UInt8](repeating: 0, count: 4096)
                    while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(buffer, count: count) }
                }
                let fields = (try? JSONDecoder().decode([String: JSONValue].self, from: data)) ?? [:]
                if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays"),
                   let visuals = fields["visual_blocks"], let data = try? JSONEncoder().encode(visuals) {
                    UserDefaults.standard.set(data, forKey: "overlay-fixture-saved-visuals")
                }
                if let drawings = fields["drawings"], let saved = try? JSONEncoder().encode(drawings) {
                    UserDefaults.standard.set(saved, forKey: NativeEditorUITestFixtures.savedDrawingsKey)
                }
                if let slots = fields["timeline_slots"]?.arrayValue, let first = slots.first?.objectValue {
                    if let tilt = first["tilt"], tilt != .null, let saved = try? JSONEncoder().encode(tilt) {
                        UserDefaults.standard.set(saved, forKey: NativeEditorUITestFixtures.savedTiltKey)
                    } else if first["tilt"] != nil {
                        UserDefaults.standard.removeObject(forKey: NativeEditorUITestFixtures.savedTiltKey)
                    }
                }
                if let slots = fields["timeline_slots"]?.arrayValue,
                   slots.contains(where: { $0.objectValue?["slot_id"]?.stringValue?.hasPrefix("native-phone-talking-source") == true }),
                   let saved = try? JSONEncoder().encode(JSONValue.object(["timeline_slots": .array(slots), "caption_cues": fields["caption_cues"] ?? .null])) {
                    UserDefaults.standard.set(saved, forKey: NativeEditorUITestFixtures.savedTalkingRangeKey)
                }
                if let lane = fields["speaker_background"], let saved = try? JSONEncoder().encode(lane) {
                    UserDefaults.standard.set(saved, forKey: NativeEditorUITestFixtures.savedSpeakerBackgroundKey)
                }
                let sections = fields.keys.filter { $0 != "base_generation" && $0 != "editor_state_version" && $0 != "deletions" && $0 != "remove_music" }
                var flags = Dictionary(uniqueKeysWithValues: sections.map { ($0, JSONValue.bool(true)) })
                // The real response acknowledges the timeline section, not
                // the request's timeline_slots field name.
                if fields["timeline_slots"] != nil { flags["timeline"] = .bool(true); flags.removeValue(forKey: "timeline_slots") }
                let gamedayDuration = fields["timeline_slots"]?.arrayValue?.compactMap { $0.objectValue?["duration_s"]?.numberValue }.reduce(0, +) ?? 6
                // A deleted drawing is saved by the lane being sent (possibly empty).
                if fields["deletions"] != nil, fields["drawings"] == nil { flags["drawings"] = .bool(true) }
                let response: [String: JSONValue] = ["ok": .bool(true), "generation": url.path.contains("fixture-gameday-item") ? fields["base_generation"] ?? .string("fixture-generation") : .string("fixture-generation-2"), "sections": .object(flags),
                                                      "revision_number": .number(4), "revision_hash": .string("fixture-hash"), "expected_duration_s": .number(url.path.contains("fixture-gameday-item") ? gamedayDuration : 6)]
                status = 200
                body = (try? JSONEncoder().encode(JSONValue.object(response))) ?? body
            }
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-gameday-add") {
                NSLog("KRI669 fixture transport %@ status=%d bytes=%d", url.path, status, body.count)
            }
            guard let http = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else { return }
            client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        }

        override func stopLoading() {}
    }
}
#endif
