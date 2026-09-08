import XCTest
@testable import Kria

final class EditorDocumentTests: XCTestCase {
    func testCanonicalNoOpRoundTripIsByteASTEquivalent() {
        let snapshot: [String: JSONValue] = [
            "schema_version": .number(2), "kind": .string("editor"), "future_root": .object(["enabled": .bool(true)]),
            "editor_payload": .object(["base_generation": .string("g-1"), "sections": .object([
                "timeline_slots": .array([.object(["slot_id": .string("a"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(false), "future_slot": .string("keep")]), .object(["opaque": .string("record")])]),
                "text_elements": .array([.object(["id": .string("t"), "text": .string("hello"), "start_s": .number(0), "end_s": .number(1), "future_text": .bool(true)])]),
                "future_section": .object(["v": .number(7)])
            ])])
        ]
        let document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.encodeSnapshot(), snapshot)
        XCTAssertEqual(document.opaqueRecords[.timeline]?.count, 1)
    }

    func testLegacySnapshotStaysLegacyAndUnknownFieldsSurviveAnEdit() {
        let snapshot: [String: JSONValue] = [
            "base_generation": .string("legacy"), "text_elements": .array([.object(["id": .string("text-1"), "text": .string("old"), "start_s": .number(0), "end_s": .number(2), "vendor": .string("x")])]),
            "unknown": .array([.object(["a": .number(1)])])
        ]
        var document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.encodeSnapshot(), snapshot)
        document.textElements[0].text = "new"
        let encoded = document.encodeSnapshot()
        XCTAssertNil(encoded["editor_payload"])
        let text = encoded["text_elements"]
        guard case let .array(rows)? = text, case let .object(value) = rows[0] else { return XCTFail("text lane missing") }
        XCTAssertEqual(value["text"], .string("new")); XCTAssertEqual(value["vendor"], .string("x")); XCTAssertEqual(encoded["unknown"], snapshot["unknown"])
    }

    func testAllEditorLanesDecodeIntoTypedProjections() {
        let sections: [String: JSONValue] = [
            "timeline_slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(3), "in_s": .number(1), "duration_s": .number(4)]), .object(["slot_id": .string("removed"), "clip_index": .number(4), "in_s": .number(0), "duration_s": .number(1), "removed": .bool(true)])]),
            "text_elements": .array([.object(["id": .string("text"), "text": .string("hi"), "start_s": .number(1), "end_s": .number(3)])]),
            "caption_cues": .array([.object(["id": .string("cue"), "start_s": .number(1), "end_s": .number(2), "text": .string("word")])]),
            "music": .object(["track_id": .string("song"), "start_s": .number(2)]),
            "background_music": .object(["track_id": .string("bed"), "enabled": .bool(true)]),
            "sound_effects": .array([.object(["id": .string("sfx"), "start_s": .number(1), "end_s": .number(1.5), "type": .string("pop")])]),
            "media_overlays": .array([.object(["id": .string("overlay"), "start_s": .number(2), "end_s": .number(4)])]),
            "visual_blocks": .array([.object(["id": .string("visual"), "kind": .string("media"), "start_s": .number(0), "end_s": .number(2)])]),
            "motion_scenes": .array([.object(["id": .string("motion"), "start_s": .number(0), "end_s": .number(2), "runtime_hash": .string("hash")])]),
            "camera_effects": .array([.object(["id": .string("camera"), "start_s": .number(0), "end_s": .number(1), "effect": .string("push")])]),
            "carousel_moment": .object(["mode": .string("rolling")])
        ]
        let document = EditorDocument.decode(snapshot: ["schema_version": .number(2), "kind": .string("editor"), "editor_payload": .object(["sections": .object(sections)])])
        XCTAssertEqual(document.clips.count, 1); XCTAssertEqual(document.tombstones.count, 1); XCTAssertEqual(document.textElements.first?.startS, 1)
        XCTAssertEqual(document.captionCues.first?.text, "word"); XCTAssertEqual(document.music?.trackID, "song"); XCTAssertEqual(document.backgroundMusic?.trackID, "bed")
        XCTAssertEqual(document.soundEffects.first?.kind, "pop"); XCTAssertEqual(document.mediaOverlays.first?.id, "overlay"); XCTAssertEqual(document.visualBlocks.first?.kind, "media")
        XCTAssertEqual(document.motionScenes.first?.runtimeHash, "hash"); XCTAssertEqual(document.cameraEffects.first?.effect, "push"); XCTAssertEqual(document.carouselMoment?["mode"], .string("rolling"))
    }

    func testSoundEffectPointPlacementsRemainTypedAndRoundTripAtS() throws {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "sound_effects": .array([.object([
                    "id": .string("sfx-point"),
                    "sound_effect_id": .string("whoosh"),
                    "src_gcs_path": .string("sound-effects/whoosh.wav"),
                    "at_s": .number(1.25),
                    "trim_start_s": .number(0.2),
                    "trim_end_s": .number(0.7),
                    "gain": .number(0.8),
                ])]),
            ])]),
        ]

        var document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.soundEffects.count, 1)
        XCTAssertEqual(document.soundEffects[0].pointS, 1.25)
        XCTAssertEqual(document.soundEffects[0].startS, 1.25)
        XCTAssertEqual(document.soundEffects[0].endS, 1.75)

        document.soundEffects[0].pointS = 2.5
        let encoded = document.encodeSnapshot()
        let sections = try XCTUnwrap(Self.object(Self.object(encoded["editor_payload"])?["sections"]))
        let row = try XCTUnwrap(Self.object(Self.array(sections["sound_effects"])?[0]))
        XCTAssertEqual(row["at_s"], .number(2.5))
        XCTAssertNil(row["start_s"])
        XCTAssertNil(row["end_s"])
        XCTAssertEqual(row["trim_start_s"], .number(0.2))
        XCTAssertEqual(row["trim_end_s"], .number(0.7))
        XCTAssertEqual(row["gain"], .number(0.8), "effect-specific fields stay lossless")
    }

    func testSoundEffectPointDisplaySpanUsesDurationAndMinimum() {
        let snapshot: [String: JSONValue] = [
            "sound_effects": .array([
                .object(["id": .string("duration"), "at_s": .number(3), "duration_s": .number(0.04)]),
                .object(["id": .string("default"), "at_s": .number(5)]),
            ]),
        ]

        let effects = EditorDocument.decode(snapshot: snapshot).soundEffects
        XCTAssertEqual(effects.map(\.startS), [3, 5])
        XCTAssertEqual(effects.map(\.endS), [3.1, 5.1])
    }

    func testMotionRuntimeHashIsASectionLevelCommitField() throws {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "motion_runtime_hash": .string("runtime-v1"),
                "motion_scenes": .array([.object([
                    "id": .string("scene"),
                    "start_s": .number(0),
                    "end_s": .number(1),
                    "preset_id": .string("route_trace"),
                ])]),
            ])]),
        ]
        var document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.motionRuntimeHash, "runtime-v1")
        document.motionRuntimeHash = "runtime-v2"
        let changed = document.encodeSnapshot()
        let changedSections = try XCTUnwrap(Self.object(Self.object(changed["editor_payload"])?["sections"]))
        XCTAssertEqual(changedSections["motion_runtime_hash"], .string("runtime-v2"))

        document.motionRuntimeHash = nil
        let cleared = document.encodeSnapshot()
        let clearedSections = try XCTUnwrap(Self.object(Self.object(cleared["editor_payload"])?["sections"]))
        XCTAssertEqual(clearedSections["motion_runtime_hash"], .null)
    }

    func testEditorCommitPreservesOmittedEmptyAndCarouselNullSemantics() throws {
        let omitted = EditorCommitRequest(baseGeneration: "g")
        let empty = EditorCommitRequest(timelineSlots: [], textElements: [], soundEffects: [], baseGeneration: "g")
        let remove = EditorCommitRequest(removeMusic: true, carouselMoment: .remove, titlePatch: .remove, baseGeneration: "g")
        let encoder = JSONEncoder()
        let omittedObject = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(omitted)) as? [String: Any])
        let emptyObject = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(empty)) as? [String: Any])
        let removeObject = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(remove)) as? [String: Any])
        XCTAssertNil(omittedObject["timeline_slots"]); XCTAssertEqual((emptyObject["timeline_slots"] as? [Any])?.count, 0)
        XCTAssertTrue(removeObject["carousel_moment"] is NSNull); XCTAssertTrue(removeObject["title"] is NSNull); XCTAssertEqual(removeObject["remove_music"] as? Bool, true)
        let decoded = try JSONDecoder().decode(EditorCommitRequest.self, from: encoder.encode(remove))
        if case .remove = decoded.carouselMoment {} else { XCTFail("explicit carousel null was lost") }
        if case .remove = decoded.titlePatch {} else { XCTFail("explicit title null was lost") }

        let lyricClear = EditorCommitRequest(
            lyrics: EditorCommitLyrics(lineOverridesPatch: .remove),
            baseGeneration: "g"
        )
        let lyricObject = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(lyricClear)) as? [String: Any])
        XCTAssertTrue((lyricObject["lyrics"] as? [String: Any])?["line_overrides"] is NSNull)
        let decodedLyrics = try JSONDecoder().decode(EditorCommitRequest.self, from: encoder.encode(lyricClear)).lyrics
        if case .remove = decodedLyrics?.lineOverridesPatch {} else { XCTFail("explicit lyric override null was lost") }
    }

    func testCapabilitiesDecodeBooleanAndReasonObject() throws {
        let document = EditorDocument.decode(snapshot: ["capabilities": .object(["timeline": .bool(true), "effects": .object(["editable": .bool(false), "reason": .string("read only")])])])
        XCTAssertEqual(document.capabilities["timeline"]?.editable, true); XCTAssertEqual(document.capabilities["effects"]?.reason, "read only")
    }

    func testNestedCapabilityReasonsAndSiblingReasonsRemainAddressable() throws {
        let document = EditorDocument.decode(snapshot: [
            "editor_capabilities": .object([
                "clips": .object(["trim": .object(["editable": .bool(false), "reason": .string("trim locked")])]),
                "music_operations": .object(["window": .object(["editable": .bool(true), "reason": .null])]),
                "lanes": .object(["overlays": .bool(true)]),
                "sfx": .bool(false),
                "sfx_reason": .string("sound effects disabled"),
                "reason": .string("top-level summary"),
            ]),
        ])
        XCTAssertEqual(document.capabilities["clips.trim"]?.editable, false)
        XCTAssertEqual(document.capabilities["clips.trim"]?.reason, "trim locked")
        XCTAssertEqual(document.capabilities["music_operations.window"]?.editable, true)
        XCTAssertNil(document.capabilities["music_operations.window"]?.reason)
        XCTAssertEqual(document.capabilities["lanes.overlays"]?.editable, true)
        XCTAssertEqual(document.capabilities["sfx"]?.reason, "sound effects disabled")
    }

    func testAuthoritativeCapabilitiesAndMusicUseCommitWireShape() throws {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "music_track_id": .string("track-1"),
                "music_window": .object(["start_s": .number(2), "alignment": .string("resync_beats")]),
            ])]),
            "editor_capabilities": .object(["timeline": .bool(true), "music_window": .object(["editable": .bool(false), "reason": .string("no track")])]),
        ]
        var document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.music?.trackID, "track-1")
        XCTAssertEqual(document.music?.startS, 2)
        XCTAssertEqual(document.music?.alignment, "resync_beats")
        XCTAssertEqual(Self.object(document.snapshot(for: .music))?["music_window"], .object(["start_s": .number(2), "alignment": .string("resync_beats")]))
        XCTAssertEqual(document.capabilities["timeline"]?.editable, true)
        XCTAssertEqual(document.capabilities["music_window"]?.reason, "no track")

        document.music?.startS = 4
        let encoded = document.encodeSnapshot()
        let sections = try XCTUnwrap(Self.object(Self.object(encoded["editor_payload"])?["sections"]))
        XCTAssertEqual(sections["music_track_id"], .string("track-1"))
        XCTAssertEqual(Self.object(sections["music_window"])?["start_s"], .number(4))
        XCTAssertNil(sections["music"])
    }

    func testAuthoritativeCaptionFieldsNormalizeToCommitMeta() throws {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "caption_meta": .object([
                    "enabled": .bool(false),
                    "color": .null,
                    "vendor_caption_option": .string("keep"),
                ]),
                "captions_enabled": .bool(true),
                "voiceover_caption_style": .string("word"),
                "caption_style": .string("sentence"),
                "voiceover_caption_font": .string("TikTok Sans Bold"),
                "caption_size_px": .number(92),
                "caption_text_color": .string("#112233"),
                "caption_highlight_color": .string("#A3E635"),
                "caption_stroke_width": .number(7),
                "caption_shadow_enabled": .bool(false),
                "caption_margin_v": .number(384),
                "caption_font_user_edited": .bool(true),
            ])]),
        ]

        let document = EditorDocument.decode(snapshot: snapshot)

        XCTAssertEqual(document.captionMeta["enabled"], .bool(false), "nested value wins")
        XCTAssertEqual(document.captionMeta["style"], .string("word"), "voiceover alias wins")
        XCTAssertEqual(document.captionMeta["font"], .string("TikTok Sans Bold"))
        XCTAssertEqual(document.captionMeta["font_set"], .bool(true))
        XCTAssertEqual(document.captionMeta["size_px"], .number(92))
        XCTAssertEqual(document.captionMeta["color"], .null, "nested null wins over sibling")
        XCTAssertEqual(document.captionMeta["highlight_color"], .string("#A3E635"))
        XCTAssertEqual(document.captionMeta["stroke_width"], .number(7))
        XCTAssertEqual(document.captionMeta["shadow_enabled"], .bool(false))
        XCTAssertEqual(document.captionMeta["y_frac"], .number(0.8))
        XCTAssertEqual(document.captionMeta["vendor_caption_option"], .string("keep"))
    }

    func testCaptionMetadataNormalizationPreservesOmittedAndNullFields() throws {
        let omitted = EditorDocument.decode(snapshot: [
            "editor_payload": .object(["sections": .object([
                "captions_enabled": .bool(true),
            ])]),
        ])
        XCTAssertEqual(omitted.captionMeta["enabled"], .bool(true))
        XCTAssertNil(omitted.captionMeta["style"])
        XCTAssertNil(omitted.captionMeta["font"])
        XCTAssertNil(omitted.captionMeta["y_frac"])

        let explicitNull = EditorDocument.decode(snapshot: [
            "editor_payload": .object(["sections": .object([
                "caption_meta": .object(["style": .null]),
                "caption_style": .string("word"),
                "caption_margin_v": .null,
                "caption_size_px": .null,
            ])]),
        ])
        XCTAssertEqual(explicitNull.captionMeta["style"], .null)
        XCTAssertEqual(explicitNull.captionMeta["size_px"], .null)
        XCTAssertEqual(explicitNull.captionMeta["y_frac"], .null)
        XCTAssertNil(explicitNull.captionMeta["font"])

        let nullSectionSnapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "caption_meta": .null,
                "caption_style": .string("word"),
            ])]),
        ]
        let nullSection = EditorDocument.decode(snapshot: nullSectionSnapshot)
        XCTAssertTrue(nullSection.captionMeta.isEmpty)
        XCTAssertEqual(nullSection.encodeSnapshot(), nullSectionSnapshot)
    }

    func testUnchangedSectionsKeepNullShapeWhenAnotherLaneChanges() throws {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "timeline_slots": .null,
                "mix": .null,
                "lyrics": .null,
                "title": .string("Keep me"),
                "text_elements": .array([.object(["id": .string("t"), "text": .string("old"), "start_s": .number(0), "end_s": .number(1)])]),
            ])]),
        ]
        var document = EditorDocument.decode(snapshot: snapshot)
        document.textElements[0].text = "new"
        document.title = nil
        let encoded = document.encodeSnapshot()
        let sections = try XCTUnwrap(Self.object(Self.object(encoded["editor_payload"])?["sections"]))
        XCTAssertEqual(sections["timeline_slots"], .null)
        XCTAssertEqual(sections["mix"], .null)
        XCTAssertEqual(sections["lyrics"], .null)
        XCTAssertEqual(sections["title"], .null)
    }

    func testMalformedAndDuplicateRecordsSurviveTypedLaneEditInOriginalOrder() throws {
        let snapshot: [String: JSONValue] = [
            "text_elements": .array([
                .object(["id": .string("duplicate"), "text": .string("one"), "start_s": .number(0), "end_s": .number(1)]),
                .object(["id": .string("malformed"), "text": .string("missing timing"), "vendor": .string("keep")]),
                .object(["id": .string("duplicate"), "text": .string("two"), "start_s": .number(1), "end_s": .number(2)]),
                .object(["opaque": .bool(true)]),
            ]),
        ]
        var document = EditorDocument.decode(snapshot: snapshot)
        XCTAssertEqual(document.textElements.count, 2)
        document.textElements[0].text = "edited"
        let encoded = document.encodeSnapshot()
        guard case let .array(rows)? = encoded["text_elements"] else { return XCTFail("text lane missing") }
        XCTAssertEqual(rows.count, 4)
        XCTAssertEqual(Self.object(rows[0])?["text"], .string("edited"))
        XCTAssertEqual(Self.object(rows[1])?["vendor"], .string("keep"))
        XCTAssertEqual(Self.object(rows[2])?["text"], .string("two"))
        XCTAssertEqual(Self.object(rows[3])?["opaque"], .bool(true))
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue]? {
        if case let .object(value) = value { return value }
        return nil
    }

    private static func array(_ value: JSONValue?) -> [JSONValue]? {
        if case let .array(value) = value { return value }
        return nil
    }
}
