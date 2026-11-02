import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1118: the editor's half of the Talk-to-camera background. The document lane `speaker_background`
/// (`null | {media_id, kind, source_start_s}`) compiles to the engine's `EditRecipe.speakerBackground`, which
/// `tests/fixtures/speaker-background-parity/v1.json` pins for the server and the engine as well.
@MainActor final class SpeakerBackgroundEditorTests: XCTestCase {
    private let clipID = UUID()
    private let fingerprint = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 400)

    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: NativeEditorUITestFixtures.savedSpeakerBackgroundKey)
    }

    // MARK: - Compiler

    private func compile(_ document: EditorDocument, backgroundSources: [String: ResolvedEditorSource] = [:]) throws -> KriaMediaEngine.EditRecipe {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: clipID, assetID: clipID, sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3,
                              sourceDuration: 6, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], mediaSources: backgroundSources).recipe
    }

    private func document(background: EditorSpeakerBackground? = nil) -> EditorDocument {
        var value = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3)])
        value.speakerBackground = background
        return value
    }

    private func backgroundSource(_ mediaID: String, generation: String? = "7") -> [String: ResolvedEditorSource] {
        let id = EditorSpeakerBackground.assetID(forMediaID: mediaID)
        return [EditorSpeakerBackground.sourceKey(forMediaID: mediaID): ResolvedEditorSource(
            clipIndex: -1, mediaID: mediaID,
            asset: MediaAsset(id: id, relativePath: "background.bin", fingerprint: AssetFingerprint(hex: String(repeating: "c", count: 64), byteCount: 90)),
            url: URL(fileURLWithPath: "/fixture/background.bin"), visualGeneration: generation)]
    }

    private func fixtureLayers() throws -> [(name: String, layer: SpeakerBackgroundLayer)] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/speaker-background-parity/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root["valid"] as? [[String: Any]]).map { item in
            let normalized = try JSONSerialization.data(withJSONObject: try XCTUnwrap(item["normalized"]))
            return (item["name"] as! String, try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: normalized))
        }
    }

    func testEveryFixtureLayerTheEditorCanExpressCompilesToTheSharedRecipeLayer() throws {
        // The editor always writes a looping layer on the Talking speaker track; the fixture's other shapes
        // (no loop, another subject track) are server-only today.
        let expressible = try fixtureLayers().filter { $0.layer.loop && $0.layer.subjectTrackID == "subtitled" }
        XCTAssertGreaterThanOrEqual(expressible.count, 2, "a photo and a video case")
        XCTAssertTrue(Set(expressible.map(\.layer.mediaKind)) == [.image, .video])
        for (name, layer) in expressible {
            let mediaID = String(layer.assetID.dropFirst("visual-".count))
            let lane = EditorSpeakerBackground(mediaID: mediaID, kind: layer.mediaKind, sourceStartS: layer.sourceStart)
            let recipe = try compile(document(background: lane), backgroundSources: backgroundSource(mediaID))
            XCTAssertEqual(recipe.speakerBackground, layer, name)
            XCTAssertTrue(recipe.effectiveCapabilities.contains(.speakerBackground), name)
            let reference = try XCTUnwrap(recipe.assetManifest?.assets.first { $0.id == layer.assetID }, name)
            XCTAssertEqual(reference.source, .visual(visualID: mediaID, generation: "7", mediaKind: layer.mediaKind), name)
            try recipe.validate()
        }
    }

    func testClearingTheBackgroundCompilesByteIdenticalToAnEditThatNeverHadOne() throws {
        func dump(_ recipe: KriaMediaEngine.EditRecipe) throws -> Data {
            try JSONSerialization.data(withJSONObject: JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)), options: [.sortedKeys])
        }
        let never = try dump(compile(document()))
        XCTAssertEqual(never, try dump(compile(document())), "the dump itself is deterministic")
        XCTAssertNil((try JSONSerialization.jsonObject(with: never) as? [String: Any])?["speaker_background"])
        var cleared = document(background: EditorSpeakerBackground(mediaID: "1a2b", kind: .image))
        _ = try compile(cleared, backgroundSources: backgroundSource("1a2b"))
        cleared.speakerBackground = nil
        XCTAssertEqual(try dump(compile(cleared)), never, "removing the lane leaves no trace in the recipe")
    }

    func testTheLaneIsNotAnUnsupportedLaneAndAnUnreadableOneIs() throws {
        XCTAssertNoThrow(try compile(document(background: .init(mediaID: "1a2b", kind: .video, sourceStartS: 2)),
                                     backgroundSources: backgroundSource("1a2b")))
        var unreadable = document()
        unreadable.unreadableSpeakerBackground = true
        XCTAssertThrowsError(try compile(unreadable)) { error in
            XCTAssertEqual(error as? NativeEditorRenderError, .unsupportedLane("speaker background"),
                           "a background this build cannot read is refused, never silently dropped")
        }
    }

    func testABackgroundWithNoResolvedBytesFailsTheCompileRatherThanRenderingWithoutIt() throws {
        XCTAssertThrowsError(try compile(document(background: .init(mediaID: "1a2b", kind: .image))))
    }

    // MARK: - Document lane

    private func snapshot(lane: JSONValue?) -> [String: JSONValue] {
        var sections: [String: JSONValue] = ["timeline_slots": .array([.object(["slot_id": .string("s"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(3)])])]
        if let lane { sections["speaker_background"] = lane }
        return ["schema_version": .number(2), "kind": .string("editor"), "edit_format": .string("subtitled"),
                "editor_payload": .object(["base_generation": .string("g1"), "sections": .object(sections)])]
    }

    private func sections(of document: EditorDocument) -> [String: JSONValue] {
        document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue ?? [:]
    }

    func testTheLaneDecodesAndASnapshotWithoutItIsUntouched() throws {
        let lane: JSONValue = .object(["media_id": .string("1a2b"), "kind": .string("video"), "source_start_s": .number(2.5)])
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        XCTAssertEqual(decoded.speakerBackground, EditorSpeakerBackground(mediaID: "1a2b", kind: .video, sourceStartS: 2.5))
        XCTAssertFalse(decoded.unreadableSpeakerBackground)
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "an unchanged lane saves as it loaded")

        let bare = EditorDocument.decode(snapshot: snapshot(lane: nil))
        XCTAssertNil(bare.speakerBackground)
        XCTAssertEqual(bare.encodeSnapshot(), snapshot(lane: nil), "a variant without the lane gains no key")
        let nulled = EditorDocument.decode(snapshot: snapshot(lane: .null))
        XCTAssertNil(nulled.speakerBackground)
        XCTAssertFalse(nulled.unreadableSpeakerBackground, "an explicit null is no background, not an unreadable one")
    }

    func testEditingTheLaneWritesTheObjectAndClearingWritesNull() throws {
        var document = EditorDocument.decode(snapshot: snapshot(lane: nil))
        document.speakerBackground = EditorSpeakerBackground(mediaID: "1a2b", kind: .image)
        XCTAssertEqual(sections(of: document)["speaker_background"],
                       .object(["media_id": .string("1a2b"), "kind": .string("image"), "source_start_s": .number(0)]))
        var saved = EditorDocument.decode(snapshot: document.encodeSnapshot())
        XCTAssertEqual(saved.speakerBackground, document.speakerBackground)
        saved.speakerBackground = nil
        XCTAssertEqual(sections(of: saved)["speaker_background"], .null)
    }

    func testAShapeThisBuildCannotRepresentIsFlaggedUnreadable() throws {
        let bad: [JSONValue] = [
            .string("beach"),
            .object(["media_id": .string("1a2b"), "kind": .string("gif")]),
            .object(["media_id": .string(""), "kind": .string("image")]),
            .object(["media_id": .string("1a2b"), "kind": .string("image"), "source_start_s": .number(1)]),
            .object(["media_id": .string("1a2b"), "kind": .string("video"), "blur": .number(0.5)]),
            .object(["kind": .string("image")]),
        ]
        for lane in bad {
            let document = EditorDocument.decode(snapshot: snapshot(lane: lane))
            XCTAssertNil(document.speakerBackground, "\(lane)")
            XCTAssertTrue(document.unreadableSpeakerBackground, "\(lane)")
        }
    }

    // MARK: - Commit request

    func testCommitRequestOmitsSendsAndNullsTheLane() throws {
        func wire(_ patch: EditorSpeakerBackgroundPatch) throws -> [String: Any] {
            let data = try JSONEncoder().encode(EditorCommitRequest(speakerBackground: patch, baseGeneration: "g"))
            return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
        XCTAssertNil(try wire(.omitted)["speaker_background"], "an untouched lane is not sent")
        XCTAssertTrue(try wire(.remove)["speaker_background"] is NSNull, "clearing sends JSON null")
        let sent = try XCTUnwrap(try wire(.replace(["media_id": .string("1a2b"), "kind": .string("video"), "source_start_s": .number(0)]))["speaker_background"] as? [String: Any])
        XCTAssertEqual(sent["media_id"] as? String, "1a2b")
        XCTAssertEqual(sent["kind"] as? String, "video")
        let decoded = try JSONDecoder().decode(EditorCommitRequest.self, from: JSONEncoder().encode(EditorCommitRequest(speakerBackground: .remove, baseGeneration: "g")))
        XCTAssertEqual(decoded.speakerBackground, .remove)
    }

    func testSaveSendsTheChosenBackgroundThenNullWhenItIsCleared() async throws {
        let threadID = UUID(), jobID = UUID()
        var variant: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(2), "output_url": .string("file:///tmp/kria-editor-test.mp4"), "resolved_archetype": .string("subtitled"),
            "base_video_path": .string("base.mp4"),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0),
                "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)])])]),
        ]
        variant["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
                                                  "speaker_background": .object(["editable": .bool(true)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant)
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditSpeakerBackground, "the server advertised the capability")

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "1a2b", kind: .image))
        XCTAssertTrue(session.hasUnsavedChanges)
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.speakerBackground,
                       .replace(["media_id": .string("1a2b"), "kind": .string("image"), "source_start_s": .number(0)]))
        XCTAssertNil(fake.lastRequest?.timelineSlots, "only the lane that changed is sent")
        XCTAssertFalse(session.hasUnsavedChanges, "an accepted background is clean even when the server does not echo the section")

        XCTAssertTrue(session.removeSpeakerBackground())
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.speakerBackground, .remove)
        XCTAssertNil(session.speakerBackground)
    }

    /// KRI-1162: the lane Save sends carries `windows` and `speaker_frame` only while they are used, and never a `bg-<n>` id.
    func testSaveCarriesWindowsAndTheSpeakerFrameAndOmitsThemOnceTheyAreGone() async throws {
        let threadID = UUID(), jobID = UUID()
        var variant: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(6), "output_url": .string("file:///tmp/kria-editor-test.mp4"), "resolved_archetype": .string("subtitled"),
            "base_video_path": .string("base.mp4"),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0),
                "duration_s": .number(6), "source_duration_s": .number(8), "removed": .bool(false)])])]),
        ]
        variant["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
                                                  "speaker_background": .object(["editable": .bool(true)]),
                                                  "speaker_background_windows": .object(["editable": .bool(true)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant)
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditSpeakerBackgroundWindows)

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "1a2b", kind: .image))
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "3c4d", kind: .video, length: 2.5))
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "1a2b", kind: .image, length: 1))
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_bottom_right"))
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await session.save()
        guard case .replace(let sent)? = fake.lastRequest?.speakerBackground else { return XCTFail("the lane was not sent") }
        XCTAssertEqual(Set(sent.keys), ["media_id", "kind", "source_start_s", "windows", "speaker_frame"])
        XCTAssertEqual(sent["speaker_frame"], .object(["width_fraction": .number(0.42), "x_fraction": .number(0.76), "y_fraction": .number(0.80)]))
        let rows = try XCTUnwrap(sent["windows"]?.arrayValue)
        XCTAssertEqual(rows, [
            .object(["media_id": .string("3c4d"), "kind": .string("video"), "source_start_s": .number(0), "start_s": .number(0), "end_s": .number(2.5)]),
            .object(["media_id": .string("1a2b"), "kind": .string("image"), "source_start_s": .number(0), "start_s": .number(2.5), "end_s": .number(3.5)]),
        ])
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(JSONValue.object(sent)), as: UTF8.self).contains("bg-"), "local ids never reach the wire")
        XCTAssertFalse(session.hasUnsavedChanges)

        // Unused again: the object is sent as it always was, with neither key.
        XCTAssertTrue(session.clearSpeakerFrame())
        XCTAssertTrue(session.setSpeakerBackgroundWindows([]))
        await session.save()
        XCTAssertEqual(fake.lastRequest?.speakerBackground,
                       .replace(["media_id": .string("1a2b"), "kind": .string("image"), "source_start_s": .number(0)]))
    }

    // MARK: - Talking source hydration

    func testOnlySpeakerBackgroundEditableStillHydratesTheTalkingSource() throws {
        func session(caps: [String: JSONValue]) -> NativeEditorSession {
            var draft = NativeEditorUITestFixtures.talkingCaptions
            draft.serverSnapshot["editor_capabilities"] = .object(caps)
            return NativeEditorSession(draft: draft)
        }
        let base: [String: JSONValue] = ["caption_cues": .object(["editable": .bool(true)])]
        XCTAssertFalse(session(caps: base).phoneTalkingLanesEditable, "no lane capability: the finished MP4")
        let only = session(caps: base.merging(["speaker_background": .object(["editable": .bool(true)])]) { $1 })
        XCTAssertFalse(only.canEdit(.mediaOverlays) || only.canEdit(.soundEffects))
        XCTAssertTrue(only.phoneTalkingLanesEditable, "the background alone opens the source preview")

        let variant: [String: JSONValue] = ["resolved_archetype": .string("subtitled"), "render_destination": .string("device")]
        let pool = NativeEditorSourcePool(clips: [.init(clipIndex: 0, nativeSource: NativeTimelineSource(mediaID: "m", sourceURL: nil, original: nil, localRequired: false))], baseGeneration: "g")
        var document = EditorDocument(clips: [])
        document.capabilities = only.document.capabilities
        XCTAssertEqual(NativePhoneTalkingSource.sourceIndices(variant: variant, document: document, pool: pool, lanesEditable: only.phoneTalkingLanesEditable), [0])
        XCTAssertNil(NativePhoneTalkingSource.sourceIndices(variant: variant, document: document, pool: pool, lanesEditable: false))
    }

    // MARK: - Session and rail

    func testRailOffersBackgroundOnlyWhenTheServerOpensIt() {
        XCTAssertFalse(NativeEditorTool.railTools(showsDraw: false, showsBackground: false).contains(.background))
        XCTAssertTrue(NativeEditorTool.railTools(showsDraw: false, showsBackground: true).contains(.background))
        XCTAssertFalse(NativeEditorTool.railTools(showsDraw: true, showsBackground: true).contains(.kria))
        XCTAssertEqual(NativeEditorTool.railTools(showsDraw: false, showsBackground: false), [.text, .captions, .visuals, .sounds],
                       "the existing rail is unchanged")
    }

    func testToolIsClosedWithoutTheCapabilityAndEditsAreRefused() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.talkingCaptions)
        XCTAssertFalse(session.canEditSpeakerBackground)
        XCTAssertFalse(session.setSpeakerBackground(mediaID: "1a2b", kind: .image))
        XCTAssertNil(session.speakerBackground)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSettingSwappingAndRemovingIsDirtyAndUndoable() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.speakerBackground)
        XCTAssertTrue(session.canEditSpeakerBackground)
        XCTAssertNil(session.speakerBackground)
        XCTAssertFalse(session.removeSpeakerBackground(), "nothing to remove is not an unsaved change")
        XCTAssertFalse(session.hasUnsavedChanges)

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image, sourceStart: 4))
        XCTAssertEqual(session.speakerBackground, EditorSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image, sourceStartS: 0),
                       "a photo never carries a start")
        XCTAssertTrue(session.isDirty(.speakerBackground))
        XCTAssertTrue(session.hasUnsavedChanges)

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video, sourceStart: 1.5))
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-video")
        XCTAssertEqual(session.speakerBackground?.sourceStartS, 1.5)

        XCTAssertTrue(session.removeSpeakerBackground())
        XCTAssertNil(session.speakerBackground)
        session.undo()
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-video")
        session.undo(); session.undo()
        XCTAssertNil(session.speakerBackground)
        XCTAssertFalse(session.hasUnsavedChanges, "back to the loaded lane is no edit")
    }

    // MARK: - Timed backgrounds and the speaker frame (KRI-1162)

    private func windowRow(_ id: String, _ kind: String = "image", start: Double, end: Double, sourceStart: Double = 0) -> JSONValue {
        .object(["media_id": .string(id), "kind": .string(kind), "source_start_s": .number(sourceStart),
                 "start_s": .number(start), "end_s": .number(end)])
    }

    private func windowsLane(_ rows: [JSONValue], frame: JSONValue? = nil) -> JSONValue {
        var fields: [String: JSONValue] = ["media_id": .string("fill"), "kind": .string("image"), "source_start_s": .number(0)]
        if !rows.isEmpty { fields["windows"] = .array(rows) }
        if let frame { fields["speaker_frame"] = frame }
        return .object(fields)
    }

    private let cornerFrame: JSONValue = .object(["width_fraction": .number(0.42), "x_fraction": .number(0.76), "y_fraction": .number(0.80)])

    /// A Talking session whose server opens both the single background and the timed ones, starting from `lane`.
    private func windowsSession(lane: JSONValue? = nil, windowsOpen: Bool = true) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        if windowsOpen { caps["speaker_background_windows"] = .object(["editable": .bool(true)]) }
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        if let lane {
            var payload = draft.serverSnapshot["editor_payload"]?.objectValue ?? [:]
            var sections = payload["sections"]?.objectValue ?? [:]
            sections["speaker_background"] = lane
            payload["sections"] = .object(sections)
            draft.serverSnapshot["editor_payload"] = .object(payload)
        }
        return NativeEditorSession(draft: draft)
    }

    func testWindowsAndTheSpeakerFrameRoundTripAndLocalIDsNeverReachTheWire() throws {
        let lane = windowsLane([windowRow("a", start: 0, end: 2), windowRow("b", "video", start: 2, end: 4.5, sourceStart: 1.5)], frame: cornerFrame)
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        let background = try XCTUnwrap(decoded.speakerBackground)
        XCTAssertFalse(decoded.unreadableSpeakerBackground)
        XCTAssertEqual(background.windows.map(\.localID), ["bg-0", "bg-1"], "ids are assigned at load, in order")
        XCTAssertEqual(background.windows[1], EditorSpeakerBackgroundWindow(localID: "anything", mediaID: "b", kind: .video, sourceStartS: 1.5, startS: 2, endS: 4.5),
                       "local ids do not take part in equality")
        XCTAssertEqual(background.speakerFrame?.presetName, "small_bottom_right")
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "an unchanged lane saves as it loaded, with no bg-<n> anywhere")
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(background.json), as: UTF8.self).contains("bg-"))
        let layer = background.layer
        XCTAssertEqual(layer.windows?.map(\.assetID), ["visual-a", "visual-b"])
        XCTAssertEqual(layer.windows?[1].sourceStart, 1.5)
        XCTAssertEqual(layer.speakerFrame, SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80))
        try layer.validate()
    }

    func testALaneWithoutWindowsOrFrameEncodesByteIdenticallyToToday() throws {
        let plain = EditorSpeakerBackground(mediaID: "1a2b", kind: .video, sourceStartS: 2.5)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        XCTAssertEqual(String(decoding: try encoder.encode(plain.json), as: UTF8.self),
                       #"{"kind":"video","media_id":"1a2b","source_start_s":2.5}"#, "no windows / speaker_frame keys while unused")
        XCTAssertNil(plain.layer.windows)
        XCTAssertNil(plain.layer.speakerFrame)
        let layerJSON = String(decoding: try RecipeJSON.encoder().encode(plain.layer), as: UTF8.self)
        XCTAssertFalse(layerJSON.contains("windows") || layerJSON.contains("speaker_frame") || layerJSON.contains("speakerFrame"))
    }

    func testShapesTheClientCannotRepresentExactlyAreUnreadable() {
        let fortyOne = (0..<41).map { windowRow("a", start: Double($0), end: Double($0) + 1) }
        let bad: [JSONValue] = [
            windowsLane([]).objectValue.map { var f = $0; f["windows"] = .array([]); return JSONValue.object(f) }!,
            windowsLane([windowRow("a", start: 0, end: 0.4)]),
            windowsLane([windowRow("a", start: 3, end: 5), windowRow("b", start: 1, end: 2)]),
            windowsLane([windowRow("a", start: 0, end: 3), windowRow("b", start: 2, end: 5)]),
            windowsLane([windowRow("a", start: 0, end: 2, sourceStart: 1)]),
            windowsLane([windowRow("a", start: 0, end: 2000)]),
            windowsLane(fortyOne),
            windowsLane([windowRow("a", start: 0, end: 2)], frame: .object(["width_fraction": .number(0.1), "x_fraction": .number(0.5), "y_fraction": .number(0.5)])),
            windowsLane([windowRow("a", start: 0, end: 2)], frame: .object(["width_fraction": .number(0.4), "x_fraction": .number(0.5), "y_fraction": .number(0.5), "angle": .number(3)])),
            windowsLane([windowRow("a", start: 0, end: 2)], frame: .null),
            windowsLane([windowRow("a", start: 0, end: 2)]).objectValue.map { var f = $0; f["windows"] = .null; return JSONValue.object(f) }!,
            .object(["media_id": .string("fill"), "kind": .string("image"), "windows": .array([.object(["media_id": .string("a"), "kind": .string("image"), "start_s": .number(0), "end_s": .number(2), "blur": .number(1)])])]),
        ]
        for lane in bad {
            let document = EditorDocument.decode(snapshot: snapshot(lane: lane))
            XCTAssertNil(document.speakerBackground, "\(lane)")
            XCTAssertTrue(document.unreadableSpeakerBackground, "\(lane)")
        }
        XCTAssertEqual(windowsLane([windowRow("a", start: 0, end: 2), windowRow("b", start: 2, end: 2.5)]).objectValue?["windows"]?.arrayValue?.count, 2)
        XCTAssertNotNil(EditorSpeakerBackground(json: windowsLane([windowRow("a", start: 0, end: 2), windowRow("b", start: 2, end: 2.5)])), "touching windows and exactly half a second are fine")
    }

    func testWindowsCannotOverlapOrShrinkBelowHalfASecond() {
        let session = windowsSession(lane: windowsLane([windowRow("a", start: 0, end: 2), windowRow("b", start: 2.5, end: 5)]))
        XCTAssertTrue(session.canEditSpeakerBackgroundWindows)
        let ids = session.speakerBackgroundWindows.map(\.localID)
        func window(_ id: String, _ start: Double, _ end: Double) -> EditorSpeakerBackgroundWindow {
            EditorSpeakerBackgroundWindow(localID: id, mediaID: "a", kind: .image, startS: start, endS: end)
        }
        // The session API refuses what the wire refuses, and changes nothing when it does.
        let before = session.speakerBackgroundWindows
        XCTAssertFalse(session.setSpeakerBackgroundWindows([window("x", 0, 3), window("y", 2, 5)]), "overlap")
        XCTAssertFalse(session.setSpeakerBackgroundWindows([window("x", 3, 5), window("y", 0, 2)]), "out of order")
        XCTAssertFalse(session.setSpeakerBackgroundWindows([window("x", 0, 0.4)]), "shorter than half a second")
        XCTAssertFalse(session.setSpeakerBackgroundWindows((0..<41).map { window("w\($0)", Double($0), Double($0) + 1) }), "more than 40")
        XCTAssertEqual(session.speakerBackgroundWindows, before)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.setSpeakerBackgroundWindows([window("x", 0, 2), window("y", 2, 2.5)]), "touching and exactly half a second are fine")
        session.undo()

        // Dragging the second block far to the left stops at its neighbour's edge; its length is kept.
        session.beginTimedBodyMove(kind: .speakerBackgroundWindow, id: ids[1])
        session.updateTimedBodyMove(by: -10)
        session.endTimedBodyMove()
        XCTAssertEqual(session.speakerBackgroundWindows[1].startS, 2.0, accuracy: 0.0001, "never overlaps the first window")
        XCTAssertEqual(session.speakerBackgroundWindows[1].endS, 4.5, accuracy: 0.0001)
        // Dragging it far right stops at the end of the 6 s video.
        session.beginTimedBodyMove(kind: .speakerBackgroundWindow, id: ids[1])
        session.updateTimedBodyMove(by: 10)
        session.endTimedBodyMove()
        XCTAssertEqual(session.speakerBackgroundWindows[1].endS, 6.0, accuracy: 0.0001, "stays inside the video")
        XCTAssertEqual(session.speakerBackgroundWindows[1].startS, 3.5, accuracy: 0.0001)
        // Trimming either edge across the whole window stops at half a second.
        session.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: ids[1], edge: .leading)
        session.updateTimedEdgeTrim(by: 10)
        session.endTimedEdgeTrim()
        XCTAssertEqual(session.speakerBackgroundWindows[1].endS - session.speakerBackgroundWindows[1].startS, 0.5, accuracy: 0.0001)
        session.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: ids[0], edge: .trailing)
        session.updateTimedEdgeTrim(by: -10)
        session.endTimedEdgeTrim()
        XCTAssertEqual(session.speakerBackgroundWindows[0].endS - session.speakerBackgroundWindows[0].startS, 0.5, accuracy: 0.0001)
        XCTAssertEqual(session.speakerBackgroundWindows.map(\.localID), ids, "ids keep their blocks")
        XCTAssertTrue(EditorSpeakerBackground.windowsAreOrdered(session.speakerBackgroundWindows), "whatever the drag did, the wire accepts it")
    }

    func testFittingASpanSnapsToNeighbourEdgesAndTheVideoEnds() {
        // Move: within 80 ms of the neighbour's end it snaps there; further away it is left alone.
        var fitted = NativeEditorSession.fittedBackgroundWindow(proposed: (2.05, 4.55), original: (2.5, 5), moving: true, edge: nil, previousEnd: 2, nextStart: 6)
        XCTAssertEqual(fitted.start, 2.0); XCTAssertEqual(fitted.end, 4.5)
        fitted = NativeEditorSession.fittedBackgroundWindow(proposed: (3.0, 5.5), original: (2.5, 5), moving: true, edge: nil, previousEnd: 2, nextStart: 6)
        XCTAssertEqual(fitted.start, 3.0); XCTAssertEqual(fitted.end, 5.5)
        // The far edge snaps to the video end.
        fitted = NativeEditorSession.fittedBackgroundWindow(proposed: (3.46, 5.96), original: (2.5, 5), moving: true, edge: nil, previousEnd: 2, nextStart: 6)
        XCTAssertEqual(fitted.end, 6.0, accuracy: 0.0001)
        // Trim the trailing edge into the next window: stops at its start.
        fitted = NativeEditorSession.fittedBackgroundWindow(proposed: (0, 9), original: (0, 2), moving: false, edge: .trailing, previousEnd: 0, nextStart: 2.5)
        XCTAssertEqual(fitted.end, 2.5)
        // The result is always on a millisecond grid and at least half a second.
        fitted = NativeEditorSession.fittedBackgroundWindow(proposed: (1.30049, 1.79), original: (0, 0.5), moving: false, edge: .leading, previousEnd: 0, nextStart: 6)
        XCTAssertGreaterThanOrEqual(fitted.end - fitted.start, 0.5)
    }

    func testTimelineWithoutWindowsHasNoBackgroundLane() {
        func windowItems(_ session: NativeEditorSession) -> [NativeEditorTimelineItem] {
            session.timelineItems.filter { $0.kind == .speakerBackgroundWindow }
        }
        XCTAssertTrue(windowItems(windowsSession()).isEmpty, "no background at all")
        XCTAssertTrue(windowItems(windowsSession(lane: windowsLane([]))).isEmpty, "a fill alone has no lane; it is a strip on the lane only while windows exist")
        let withWindows = windowsSession(lane: windowsLane([windowRow("a", start: 0, end: 2), windowRow("b", start: 3, end: 5)]))
        XCTAssertEqual(windowItems(withWindows).map(\.id), ["bg-0", "bg-1"])
        XCTAssertEqual(windowItems(withWindows).map(\.start), [0, 3])
        // Every other lane keeps its items: the same session without the lane lists the same non-window items.
        let plain = windowsSession(lane: windowsLane([]))
        func shape(_ items: [NativeEditorTimelineItem]) -> [String] { items.map { "\($0.kind.rawValue) \($0.start) \($0.end)" } }
        XCTAssertEqual(shape(withWindows.timelineItems.filter { $0.kind != .speakerBackgroundWindow }), shape(plain.timelineItems))
    }

    func testTheCompilerRegistersTheFillAndEveryWindowAssetOnce() throws {
        let lane = EditorSpeakerBackground(mediaID: "fill", kind: .image, windows: [
            .init(localID: "bg-0", mediaID: "a", kind: .image, startS: 0, endS: 1),
            .init(localID: "bg-1", mediaID: "b", kind: .video, sourceStartS: 1, startS: 1, endS: 2),
            .init(localID: "bg-2", mediaID: "a", kind: .image, startS: 2, endS: 3),
            .init(localID: "bg-3", mediaID: "fill", kind: .image, startS: 3, endS: 4),
        ], speakerFrame: EditorSpeakerFrame.preset("small_bottom_left"))
        XCTAssertEqual(lane.mediaRequirements.map(\.mediaID), ["fill", "a", "b"], "one resolve per media id, fill first")
        let sources = ["fill", "a", "b"].reduce(into: [String: ResolvedEditorSource]()) { $0.merge(backgroundSource($1)) { $1 } }
        let recipe = try compile(document(background: lane), backgroundSources: sources)
        let ids = (recipe.assetManifest?.assets ?? []).map(\.id).filter { $0.hasPrefix("visual-") }
        XCTAssertEqual(ids.sorted(), ["visual-a", "visual-b", "visual-fill"], "each window medium is pinned once, even when two windows share it")
        XCTAssertEqual(recipe.speakerBackground?.windows?.map(\.assetID), ["visual-a", "visual-b", "visual-a", "visual-fill"])
        XCTAssertEqual(recipe.speakerBackground?.speakerFrame?.xFraction, 0.24)
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundWindows]))
        try recipe.validate()
        // A window whose bytes never resolved fails the compile rather than rendering without it.
        XCTAssertThrowsError(try compile(document(background: lane), backgroundSources: sources.filter { $0.key != EditorSpeakerBackground.sourceKey(forMediaID: "b") }))
        // A fill-only lane still needs no windows capability.
        let plainRecipe = try compile(document(background: .init(mediaID: "fill", kind: .image)), backgroundSources: backgroundSource("fill"))
        XCTAssertFalse(plainRecipe.effectiveCapabilities.contains(.speakerBackgroundWindows))
    }

    func testSpeakerFramePresetsAreDetectedWithinAThousandth() throws {
        let corner = try XCTUnwrap(EditorSpeakerFrame.preset("small_top_right"))
        XCTAssertEqual(corner, EditorSpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.22))
        XCTAssertEqual(corner.presetName, "small_top_right")
        XCTAssertEqual(EditorSpeakerFrame(widthFraction: 0.4205, xFraction: 0.7595, yFraction: 0.2204).presetName, "small_top_right")
        XCTAssertNil(EditorSpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.2215).presetName, "a hair past 0.001 is Custom")
        XCTAssertNil(EditorSpeakerFrame(widthFraction: 0.5, xFraction: 0.5, yFraction: 0.5).presetName)
        XCTAssertNil(EditorSpeakerFrame.preset("full"), "full is the absent frame, not a stored one")
        XCTAssertNil(EditorSpeakerFrame.preset("small_middle"))
        XCTAssertEqual(Set(EditorSpeakerFrame.presetNames.compactMap { EditorSpeakerFrame.preset($0)?.presetName }), Set(EditorSpeakerFrame.presetNames))
    }

    func testSpeakerFramePresetsAreSetClearedAndACustomFrameIsKeptUntilAPresetIsPicked() throws {
        let session = windowsSession(lane: windowsLane([]))
        XCTAssertNil(session.speakerFrame)
        XCTAssertFalse(session.clearSpeakerFrame(), "nothing to clear is not an edit")
        XCTAssertFalse(session.setSpeakerFrame(preset: "small_middle"))
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_bottom_right"))
        XCTAssertEqual(session.speakerFrame?.presetName, "small_bottom_right")
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left"))
        XCTAssertEqual(session.speakerFrame?.presetName, "small_top_left")
        XCTAssertTrue(session.setSpeakerFrame(preset: "full"))
        XCTAssertNil(session.speakerFrame)
        session.undo(); session.undo(); session.undo(); session.undo()
        XCTAssertNil(session.speakerFrame)
        XCTAssertFalse(session.hasUnsavedChanges)

        let custom: JSONValue = .object(["width_fraction": .number(0.5), "x_fraction": .number(0.4), "y_fraction": .number(0.6)])
        let kept = windowsSession(lane: windowsLane([windowRow("a", start: 0, end: 2)], frame: custom))
        XCTAssertNil(kept.speakerFrame?.presetName, "shown as Custom")
        XCTAssertTrue(kept.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video), "swapping the fill leaves the frame and the windows alone")
        XCTAssertEqual(kept.speakerFrame?.widthFraction, 0.5)
        XCTAssertEqual(kept.speakerBackgroundWindows.map(\.mediaID), ["a"])
        XCTAssertTrue(kept.setSpeakerFrame(preset: "small_bottom_left"))
        XCTAssertEqual(kept.speakerFrame?.presetName, "small_bottom_left")
        XCTAssertTrue(kept.removeSpeakerBackground())
        XCTAssertNil(kept.speakerBackground, "removing the background clears the windows and the frame with it")
    }

    func testTheNewControlsFollowTheirOwnCapability() {
        let closed = windowsSession(lane: windowsLane([windowRow("a", start: 0, end: 2)]), windowsOpen: false)
        XCTAssertTrue(closed.canEditSpeakerBackground, "the single background keeps following speaker_background")
        XCTAssertFalse(closed.canEditSpeakerBackgroundWindows)
        XCTAssertFalse(closed.setSpeakerBackgroundWindows([]))
        XCTAssertFalse(closed.addSpeakerBackgroundWindow(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertFalse(closed.setSpeakerFrame(preset: "small_bottom_right"))
        closed.beginTimedBodyMove(kind: .speakerBackgroundWindow, id: "bg-0")
        closed.updateTimedBodyMove(by: 1)
        closed.endTimedBodyMove()
        XCTAssertEqual(closed.speakerBackgroundWindows.first?.startS, 0, "the lane cannot be dragged while the windows gate is closed")
        XCTAssertFalse(closed.hasUnsavedChanges)
        XCTAssertTrue(closed.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        XCTAssertEqual(closed.speakerBackgroundWindows.count, 1, "an existing window is carried along, never dropped silently")
    }

    func testAddingAWindowAppendsItAfterTheLastOneAndClipsItToTheVideo() {
        let session = windowsSession()
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-photo", "no fill yet: the first tile becomes the fill")
        XCTAssertEqual(session.speakerBackgroundWindows.map { [$0.startS, $0.endS] }, [[0, 4]])
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "fixture-bg-video", kind: .video))
        XCTAssertEqual(session.speakerBackgroundWindows.map { [$0.startS, $0.endS] }, [[0, 4], [4, 6]], "clipped to the 6 s video")
        XCTAssertEqual(session.speakerBackgroundWindows[1].sourceStartS, 0, "a video window starts at its own beginning")
        XCTAssertEqual(Set(session.speakerBackgroundWindows.map(\.localID)).count, 2)
        XCTAssertFalse(session.addSpeakerBackgroundWindow(mediaID: "fixture-bg-photo", kind: .image), "no room left")
        XCTAssertTrue(session.removeSpeakerBackgroundWindow(id: session.speakerBackgroundWindows[0].localID))
        XCTAssertEqual(session.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-video"])
        XCTAssertTrue(session.removeSpeakerBackgroundWindow(id: session.speakerBackgroundWindows[0].localID))
        XCTAssertTrue(session.speakerBackgroundWindows.isEmpty)
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-photo", "taking the last window away keeps the fill")
    }

    func testOnlyReadyPhotosAndVideosAreOfferedAsBackgrounds() {
        func visual(_ id: String, _ kind: String, _ status: String) -> CreationVisual {
            CreationVisual(id: id, kind: kind, status: status, sourceFilename: nil, displayURL: nil, previewURL: nil, retryable: nil)
        }
        let library = [visual("a", "image", "ready"), visual("b", "video", "ready"), visual("c", "image", "analyzing"),
                       visual("d", "video", "failed"), visual("e", "gif", "ready")]
        XCTAssertEqual(NativeEditorSession.backgroundChoices(from: library).map(\.id), ["a", "b"])
        XCTAssertEqual(NativeEditorSession.backgroundChoices(from: NativeEditorUITestFixtures.speakerBackgroundVisuals).map(\.kind), ["image", "video"])
    }
}
