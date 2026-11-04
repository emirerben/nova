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
            let assetID = try XCTUnwrap(layer.assetID, name)
            let kind = try XCTUnwrap(layer.visualMediaKind, name)
            let mediaID = String(assetID.dropFirst("visual-".count))
            let lane = EditorSpeakerBackground(mediaID: mediaID, kind: kind, sourceStartS: layer.sourceStart)
            let recipe = try compile(document(background: lane), backgroundSources: backgroundSource(mediaID))
            XCTAssertEqual(recipe.speakerBackground, layer, name)
            XCTAssertTrue(recipe.effectiveCapabilities.contains(.speakerBackground), name)
            XCTAssertFalse(recipe.effectiveCapabilities.contains(.speakerBackgroundColor), name)
            let reference = try XCTUnwrap(recipe.assetManifest?.assets.first { $0.id == assetID }, name)
            XCTAssertEqual(reference.source, .visual(visualID: mediaID, generation: "7", mediaKind: kind), name)
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

    // MARK: - Solid-colour fill (KRI-1173)

    private func v3Cases() throws -> (valid: [[String: Any]], invalid: [[String: Any]]) {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/speaker-background-parity/v3.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return (try XCTUnwrap(root["valid"] as? [[String: Any]]), try XCTUnwrap(root["invalid"] as? [[String: Any]]))
    }

    private func colourLane(_ hex: String = "#00FF00") -> JSONValue { .object(["kind": .string("color"), "color": .string(hex)]) }

    /// The editor's colour lane compiles to exactly the layer `v3.json`'s `white-fill` normalizes to, and that layer
    /// encodes (through the engine's encoder) back to the fixture's normalized object.
    func testAColourLaneCompilesToTheSharedFixtureLayerAndEncodesToItsNormalizedForm() throws {
        let white = try XCTUnwrap(v3Cases().valid.first { $0["name"] as? String == "white-fill" })
        let normalized = try XCTUnwrap(white["normalized"] as? [String: Any])
        let expected = try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: JSONSerialization.data(withJSONObject: normalized))

        let lane = EditorSpeakerBackground(color: "#FFFFFF")
        XCTAssertEqual(lane.layer, expected)
        let recipe = try compile(document(background: lane))
        XCTAssertEqual(recipe.speakerBackground, expected)
        let encoded = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(lane.layer)) as? [String: Any]
        XCTAssertEqual(NSDictionary(dictionary: try XCTUnwrap(encoded)), NSDictionary(dictionary: normalized))
        XCTAssertNil(encoded?["asset_id"], "a colour has no asset id on the wire")
    }

    func testAColourFillRegistersNoFillAssetAndTheRecipeValidates() throws {
        let recipe = try compile(document(background: EditorSpeakerBackground(color: "#00FF00")))
        XCTAssertNoThrow(try recipe.validate())
        XCTAssertFalse((recipe.assetManifest?.assets ?? []).contains { $0.id.hasPrefix("visual-") }, "no pool visual is pinned for a colour")
        XCTAssertFalse(recipe.assets.contains { $0.id.hasPrefix("visual-") })
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundColor]))
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.speakerBackgroundWindows))
    }

    func testAColourFillUnderWindowsStillPinsEachWindowVisual() throws {
        let window = EditorSpeakerBackgroundWindow(localID: "bg-0", mediaID: "3c4d", kind: .video, sourceStartS: 1, startS: 0.5, endS: 2)
        let lane = EditorSpeakerBackground(color: "#1A2B3C", windows: [window], speakerFrame: EditorSpeakerFrame.preset("small_bottom_right"))
        XCTAssertEqual(lane.mediaRequirements.map(\.mediaID), ["3c4d"], "only the window needs bytes")
        XCTAssertThrowsError(try compile(document(background: lane)), "the window's bytes are still required")
        let recipe = try compile(document(background: lane), backgroundSources: backgroundSource("3c4d"))
        XCTAssertNoThrow(try recipe.validate())
        XCTAssertEqual(recipe.assetManifest?.assets.filter { $0.id.hasPrefix("visual-") }.map(\.id), ["visual-3c4d"])
        XCTAssertEqual(recipe.speakerBackground?.color, "#1A2B3C")
        XCTAssertEqual(recipe.speakerBackground?.windows?.map(\.assetID), ["visual-3c4d"])
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundColor, .speakerBackgroundWindows]))
    }

    func testAColourLaneRoundTripsAndEveryMalformedShapeIsUnreadable() throws {
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: colourLane()))
        XCTAssertEqual(decoded.speakerBackground, EditorSpeakerBackground(color: "#00FF00"))
        XCTAssertEqual(decoded.speakerBackground?.color, "#00FF00")
        XCTAssertNil(decoded.speakerBackground?.mediaID)
        XCTAssertFalse(decoded.unreadableSpeakerBackground)
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: colourLane()), "an unchanged colour lane saves as it loaded")
        XCTAssertEqual(EditorSpeakerBackground(color: "#00FF00").json, colourLane())

        // With windows and a frame the keys follow the media shape's rules.
        let full: JSONValue = .object(["kind": .string("color"), "color": .string("#1A2B3C"),
            "windows": .array([windowRow("3c4d", start: 0, end: 2)]),
            "speaker_frame": .object(["width_fraction": .number(0.42), "x_fraction": .number(0.76), "y_fraction": .number(0.8)])])
        let withExtras = try XCTUnwrap(EditorSpeakerBackground(json: full))
        XCTAssertEqual(withExtras.json, full)
        XCTAssertEqual(withExtras.windows.count, 1)

        let bad: [JSONValue] = [
            .object(["kind": .string("color"), "color": .string("#00FF00"), "media_id": .string("1a2b")]),
            .object(["kind": .string("color"), "color": .string("#00FF00"), "source_start_s": .number(0)]),
            .object(["kind": .string("color")]),
            .object(["kind": .string("color"), "color": .string("#00ff00")]),
            .object(["kind": .string("color"), "color": .string("#0F0")]),
            .object(["kind": .string("color"), "color": .string("#00FF0080")]),
            .object(["kind": .string("color"), "color": .string("green")]),
            .object(["kind": .string("color"), "color": .string("00FF00")]),
            .object(["kind": .string("color"), "color": .null]),
            .object(["kind": .string("image"), "color": .string("#00FF00")]),
            .object(["kind": .string("image"), "media_id": .string("1a2b"), "color": .string("#00FF00")]),
            .object(["kind": .string("video")]),
        ]
        for lane in bad {
            let document = EditorDocument.decode(snapshot: snapshot(lane: lane))
            XCTAssertNil(document.speakerBackground, "\(lane)")
            XCTAssertTrue(document.unreadableSpeakerBackground, "\(lane)")
        }
    }

    private func colourSession(windows: Bool = false) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_color"] = .object(["editable": .bool(true)])
        if windows { caps["speaker_background_windows"] = .object(["editable": .bool(true)]) }
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        return NativeEditorSession(draft: draft)
    }

    func testTheColourControlsExistOnlyWhileTheServerOpensTheColourCapability() {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps.removeValue(forKey: "speaker_background_color")
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        let closed = NativeEditorSession(draft: draft)
        XCTAssertTrue(closed.canEditSpeakerBackground)
        XCTAssertFalse(closed.canEditSpeakerBackgroundColor)
        XCTAssertFalse(closed.setSpeakerBackgroundColor(hex: "#00FF00"))
        XCTAssertNil(closed.speakerBackground)
        XCTAssertFalse(closed.hasUnsavedChanges)

        let open = colourSession()
        XCTAssertTrue(open.canEditSpeakerBackgroundColor)
        XCTAssertTrue(NativeEditorUITestFixtures.speakerBackground.serverSnapshot["editor_capabilities"]?.objectValue?["speaker_background_color"] != nil,
                      "the UI-test fixture opens the colour capability")
    }

    func testSettingAColourReplacesAMediaFillKeepsWindowsAndFrameAndIsUndoable() {
        let session = colourSession(windows: true)
        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "fixture-bg-video", kind: .video, length: 2))
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left"))
        let windows = session.speakerBackgroundWindows, frame = session.speakerFrame

        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#00ff00"), "lowercase input is normalised")
        XCTAssertEqual(session.speakerBackgroundColor, "#00FF00")
        XCTAssertNil(session.speakerBackground?.mediaID, "the photo is replaced")
        XCTAssertEqual(session.speakerBackgroundWindows, windows)
        XCTAssertEqual(session.speakerFrame, frame)
        XCTAssertTrue(session.isDirty(.speakerBackground))

        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#0000FF"), "a colour replaces a colour")
        XCTAssertEqual(session.speakerBackgroundColor, "#0000FF")

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video, sourceStart: 1))
        XCTAssertNil(session.speakerBackgroundColor, "a photo or video replaces the colour")
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-video")
        XCTAssertEqual(session.speakerBackgroundWindows, windows)

        session.undo()
        XCTAssertEqual(session.speakerBackgroundColor, "#0000FF")
        session.undo()
        XCTAssertEqual(session.speakerBackgroundColor, "#00FF00")
        session.undo()
        XCTAssertEqual(session.speakerBackground?.mediaID, "fixture-bg-photo")
    }

    func testABadColourChangesNothing() {
        let session = colourSession()
        for bad in ["", "green", "#0F0", "#GGGGGG", "#00FF0080", "00FF00"] {
            XCTAssertFalse(session.setSpeakerBackgroundColor(hex: bad), bad)
        }
        XCTAssertNil(session.speakerBackground)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#FFFFFF"))
        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#FFFFFF"), "the colour that is already there is accepted")
        session.undo()
        XCTAssertNil(session.speakerBackground, "and it was one edit, not two")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSaveSendsTheColourObjectWithoutAMediaId() async throws {
        let threadID = UUID(), jobID = UUID()
        var variant: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(2), "output_url": .string("file:///tmp/kria-editor-test.mp4"), "resolved_archetype": .string("subtitled"),
            "base_video_path": .string("base.mp4"),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0),
                "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)])])]),
        ]
        variant["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
                                                  "speaker_background": .object(["editable": .bool(true)]),
                                                  "speaker_background_color": .object(["editable": .bool(true)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant)
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditSpeakerBackgroundColor)

        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#00FF00"))
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.speakerBackground, .replace(["kind": .string("color"), "color": .string("#00FF00")]))
        XCTAssertNil(fake.lastRequest?.timelineSlots, "only the lane that changed is sent")
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.speakerBackgroundColor, "#00FF00")

        XCTAssertTrue(session.removeSpeakerBackground())
        await session.save()
        XCTAssertEqual(fake.lastRequest?.speakerBackground, .remove)
    }

    func testTheProtocolDeclaresTheColourBuild() {
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 34, "34 = decodes a colour speaker background and the speakerBackgroundColor capability")
    }

    // MARK: - Qendresa item 16: the full-screen opening

    private func fullScreenRow(_ start: Double, _ end: Double) -> JSONValue { .object(["start_s": .number(start), "end_s": .number(end)]) }

    /// A lane with a media fill, optionally the corner frame, and the given `full_screen_windows` rows.
    private func openingLane(_ rows: [JSONValue] = [JSONValue.object(["start_s": .number(0), "end_s": .number(7.2)])],
                             frame: Bool = true, windows: [JSONValue] = []) -> JSONValue {
        var fields: [String: JSONValue] = ["media_id": .string("fill"), "kind": .string("image"), "source_start_s": .number(0)]
        if !windows.isEmpty { fields["windows"] = .array(windows) }
        if frame { fields["speaker_frame"] = cornerFrame }
        if !rows.isEmpty { fields["full_screen_windows"] = .array(rows) }
        return .object(fields)
    }

    /// A Talking session that opens the background, the timed ones, the colour fill and (unless `openingOpen` is false) the opening.
    private func openingSession(lane: JSONValue? = nil, openingOpen: Bool = true) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_windows"] = .object(["editable": .bool(true)])
        caps["speaker_background_color"] = .object(["editable": .bool(true)])
        if openingOpen { caps["speaker_full_opening"] = .object(["editable": .bool(true)]) }
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

    func testFullScreenWindowsRoundTripAndTheLayerCarriesThem() throws {
        let lane = openingLane([fullScreenRow(0, 7.2), fullScreenRow(50, 55)])
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        XCTAssertFalse(decoded.unreadableSpeakerBackground)
        let background = try XCTUnwrap(decoded.speakerBackground)
        XCTAssertEqual(background.fullScreenWindows, [EditorFullScreenWindow(startS: 0, endS: 7.2), EditorFullScreenWindow(startS: 50, endS: 55)])
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "an unchanged lane saves as it loaded, full_screen_windows included")
        let layer = background.layer
        XCTAssertEqual(layer.fullScreenWindows, [SpeakerFullScreenWindow(start: 0, end: 7.2), SpeakerFullScreenWindow(start: 50, end: 55)])
        try layer.validate()
        XCTAssertTrue(layer.showsSpeakerFullScreen(at: 3))
        // A colour fill carries it too.
        let colour = EditorSpeakerBackground(color: "#111111", fullScreenWindows: [EditorFullScreenWindow(startS: 0, endS: 5)])
        XCTAssertEqual(EditorSpeakerBackground(json: colour.json), colour)
        XCTAssertEqual(colour.layer.fullScreenWindows, [SpeakerFullScreenWindow(start: 0, end: 5)])
    }

    func testTheCompiledRecipeDeclaresTheOpeningCapability() throws {
        let background = EditorSpeakerBackground(mediaID: "fill", kind: .image, fullScreenWindows: [EditorFullScreenWindow(startS: 0, endS: 2)])
        let recipe = try compile(document(background: background), backgroundSources: backgroundSource("fill"))
        XCTAssertEqual(recipe.speakerBackground?.fullScreenWindows, [SpeakerFullScreenWindow(start: 0, end: 2)])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.speakerFrameWindows))
        try recipe.validate()
    }

    func testALaneWithoutAnOpeningEncodesByteIdenticallyToToday() throws {
        let plain = EditorSpeakerBackground(mediaID: "1a2b", kind: .video, sourceStartS: 2.5)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        XCTAssertEqual(String(decoding: try encoder.encode(plain.json), as: UTF8.self),
                       #"{"kind":"video","media_id":"1a2b","source_start_s":2.5}"#)
        XCTAssertTrue(plain.fullScreenWindows.isEmpty)
        XCTAssertNil(plain.layer.fullScreenWindows)
        XCTAssertFalse(String(decoding: try RecipeJSON.encoder().encode(plain.layer), as: UTF8.self).contains("full_screen"))
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: openingLane([], frame: true)))
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: openingLane([], frame: true)))
    }

    func testMalformedFullScreenWindowsAreUnreadable() {
        func readable(_ field: JSONValue) -> Bool {
            guard case .object(var fields) = openingLane([]) else { return false }
            fields["full_screen_windows"] = field
            return EditorSpeakerBackground(json: .object(fields)) != nil
        }
        XCTAssertTrue(readable(.array([fullScreenRow(0, 3)])))
        XCTAssertFalse(readable(.array([])), "[] is invalid")
        XCTAssertFalse(readable(.null), "null is not absent here")
        XCTAssertFalse(readable(.array([fullScreenRow(0, 3), fullScreenRow(2, 5)])), "overlap")
        XCTAssertFalse(readable(.array([fullScreenRow(5, 8), fullScreenRow(0, 3)])), "unsorted")
        XCTAssertFalse(readable(.array([fullScreenRow(0, 0.4)])), "under half a second")
        XCTAssertFalse(readable(.array([fullScreenRow(-1, 3)])))
        XCTAssertFalse(readable(.array([fullScreenRow(0, 1801)])))
        XCTAssertFalse(readable(.array((0..<5).map { fullScreenRow(Double($0) * 2, Double($0) * 2 + 1) })), "at most four")
        XCTAssertFalse(readable(.array([.object(["start_s": .number(0), "end_s": .number(3), "extra": .number(1)])])), "unknown key")
        XCTAssertTrue(readable(.array([fullScreenRow(0, 3), fullScreenRow(3, 5)])), "touching ends are fine")
    }

    func testChangingTheFillTheWindowsOrTheCornerKeepsTheOpening() {
        let opening = [EditorFullScreenWindow(startS: 0, endS: 7.2)]
        let session = openingSession(lane: openingLane())
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening)
        XCTAssertEqual(session.speakerFullScreenOpening, opening.first)

        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "a new photo fill keeps it")
        XCTAssertTrue(session.setSpeakerBackgroundColor(hex: "#00ff00"))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "a colour fill keeps it")
        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video, sourceStart: 1))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "a video fill keeps it")
        XCTAssertTrue(session.addSpeakerBackgroundWindow(mediaID: "fixture-bg-photo", kind: .image, length: 2))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "adding a window keeps it")
        let id = session.speakerBackgroundWindows[0].localID
        XCTAssertTrue(session.removeSpeakerBackgroundWindow(id: id))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "removing a window keeps it")
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left"))
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "moving the corner keeps it")
        XCTAssertTrue(session.clearSpeakerFrame())
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, opening, "clearing the corner keeps it")
    }

    func testRemovingTheOpeningClearsOnlyTheOpeningAndIsUndoable() {
        let session = openingSession(lane: openingLane([fullScreenRow(0, 7.2), fullScreenRow(50, 55)]))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.removeFullScreenOpening())
        XCTAssertNil(session.speakerFullScreenOpening)
        XCTAssertEqual(session.speakerBackground?.fullScreenWindows, [EditorFullScreenWindow(startS: 50, endS: 55)], "the ending stays")
        XCTAssertNotNil(session.speakerFrame, "the corner stays")
        XCTAssertTrue(session.isDirty(.speakerBackground))
        XCTAssertFalse(session.removeFullScreenOpening(), "nothing left to remove is not an unsaved change")
        session.undo()
        XCTAssertEqual(session.speakerFullScreenOpening, EditorFullScreenWindow(startS: 0, endS: 7.2))
        XCTAssertFalse(session.hasUnsavedChanges, "back to the loaded lane is no edit")
        // Removing the only window drops the key from the wire.
        let only = openingSession(lane: openingLane())
        XCTAssertTrue(only.removeFullScreenOpening())
        XCTAssertTrue(only.speakerBackground?.fullScreenWindows.isEmpty == true)
        XCTAssertNil(only.speakerBackground?.json.objectValue?["full_screen_windows"])
    }

    func testTheOpeningIsReadOnlyWithoutItsCapability() {
        let session = openingSession(lane: openingLane(), openingOpen: false)
        XCTAssertNotNil(session.speakerFullScreenOpening, "still shown")
        XCTAssertFalse(session.canEditSpeakerFullOpening)
        XCTAssertFalse(session.removeFullScreenOpening())
        XCTAssertNotNil(session.speakerFullScreenOpening)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testRemovingTheBackgroundClearsTheOpeningToo() {
        let session = openingSession(lane: openingLane())
        XCTAssertTrue(session.removeSpeakerBackground())
        XCTAssertNil(session.speakerBackground)
        XCTAssertNil(session.speakerFullScreenOpening)
    }

    func testTheProtocolDeclaresTheOpeningBuild() {
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 38, "38 = decodes, draws and round-trips speaker_background.full_screen_windows")
    }

    // MARK: - Qendresa item 18: aim the background (rows F1, F11, F12, F15 in `docs/reviews/background-focus-failure-modes.md`)

    private func focusRow(_ x: Double, _ zoom: Double, y: Double = 0.5) -> JSONValue {
        .object(["x": .number(x), "y": .number(y), "zoom": .number(zoom)])
    }

    /// A Talking session that opens the single background, the timed ones and (unless `focusOpen` is false) the aim controls.
    private func focusSession(lane: JSONValue? = nil, focusOpen: Bool = true) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_windows"] = .object(["editable": .bool(true)])
        if focusOpen { caps["background_focus"] = .object(["editable": .bool(true)]) }
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

    private func focusLane(fill: JSONValue? = nil, windows: [JSONValue] = []) -> JSONValue {
        var fields: [String: JSONValue] = ["media_id": .string("fill"), "kind": .string("image"), "source_start_s": .number(0)]
        if let fill { fields["focus"] = fill }
        if !windows.isEmpty { fields["windows"] = .array(windows) }
        return .object(fields)
    }

    private func focusWindow(_ id: String, start: Double, end: Double, focus: JSONValue? = nil) -> JSONValue {
        guard case .object(var fields) = windowRow(id, start: start, end: end) else { return .null }
        if let focus { fields["focus"] = focus }
        return .object(fields)
    }

    func testFocusRoundTripsOnTheFillAndOnWindows() throws {
        let lane = focusLane(fill: focusRow(0.2, 1.2), windows: [focusWindow("a", start: 0, end: 4, focus: focusRow(0, 2.5)), focusWindow("b", start: 4, end: 8)])
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        XCTAssertFalse(decoded.unreadableSpeakerBackground)
        let background = try XCTUnwrap(decoded.speakerBackground)
        XCTAssertEqual(background.focus, SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 1.2))
        XCTAssertEqual(background.windows[0].focus, SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 2.5))
        XCTAssertNil(background.windows[1].focus)
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "an unchanged lane saves as it loaded, focus included")
        XCTAssertEqual(EditorSpeakerBackground(json: background.json), background)
        // Strict shapes: extra keys, a word, a missing part, an out-of-range part and a colour with a focus are unreadable.
        for bad in [JSONValue.object(["x": .number(0.5), "y": .number(0.5), "zoom": .number(1), "angle": .number(1)]), .string("left"),
                    .object(["x": .number(0.5), "y": .number(0.5)]), focusRow(1.01, 1), focusRow(0.5, 4.5), focusRow(0.5, 0.1)] {
            XCTAssertTrue(EditorDocument.decode(snapshot: snapshot(lane: focusLane(fill: bad))).unreadableSpeakerBackground, "\(bad)")
        }
        XCTAssertNil(EditorSpeakerBackground(json: .object(["kind": .string("color"), "color": .string("#112233"), "focus": focusRow(0.5, 1.2)])))
    }

    func testALaneWithoutFocusEncodesExactlyAsBefore() throws {
        let plain = EditorSpeakerBackground(mediaID: "1a2b", kind: .video, sourceStartS: 2.5,
                                            windows: [EditorSpeakerBackgroundWindow(localID: "bg-0", mediaID: "c", kind: .image, startS: 0, endS: 2)])
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let text = String(decoding: try encoder.encode(plain.json), as: UTF8.self)
        XCTAssertFalse(text.contains("focus"))
        XCTAssertNil(plain.layer.focus)
        XCTAssertNil(plain.layer.windows?.first?.focus)
        XCTAssertFalse(String(decoding: try RecipeJSON.encoder().encode(plain.layer), as: UTF8.self).contains("focus"))
    }

    func testFocusIsPartOfDocumentEqualityAndCompilesToTheSharedV5Layer() throws {
        let a = EditorSpeakerBackground(mediaID: "fill", kind: .image)
        var b = a; b.focus = SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 1.2)
        XCTAssertNotEqual(a, b, "a fill focus edit must mark the document dirty")
        let w = EditorSpeakerBackgroundWindow(localID: "x", mediaID: "a", kind: .image, startS: 0, endS: 4)
        var wf = w; wf.focus = SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 2.5)
        XCTAssertNotEqual(w, wf, "a window focus edit must mark the document dirty")

        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/speaker-background-parity/v5.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let mixed = try XCTUnwrap((root["valid"] as? [[String: Any]])?.first { $0["name"] as? String == "photo-fill-focus-and-mixed-windows" })
        let expected = try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: JSONSerialization.data(withJSONObject: XCTUnwrap(mixed["normalized"])))

        let lane = EditorSpeakerBackground(
            mediaID: "fill", kind: .image,
            windows: [EditorSpeakerBackgroundWindow(localID: "bg-0", mediaID: "a", kind: .image, startS: 0, endS: 4, focus: SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 2.5)),
                      EditorSpeakerBackgroundWindow(localID: "bg-1", mediaID: "b", kind: .image, startS: 4, endS: 8)],
            speakerFrame: EditorSpeakerFrame.preset("small_bottom_right"), focus: SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 1.2))
        XCTAssertEqual(lane.layer, expected)
        let recipe = try compile(document(background: lane), backgroundSources: backgroundSource("fill").merging(backgroundSource("a")) { $1 }.merging(backgroundSource("b")) { $1 })
        XCTAssertEqual(recipe.speakerBackground, expected)
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.backgroundFocus))
        try recipe.validate()
    }

    func testSetSpeakerBackgroundFocusChangesAndDirtiesTheDocument() throws {
        let session = focusSession(lane: focusLane(windows: [focusWindow("a", start: 0, end: 4), focusWindow("b", start: 4, end: 8)]))
        XCTAssertTrue(session.canEditSpeakerBackgroundFocus)
        XCTAssertFalse(session.hasUnsavedChanges)
        // The fill.
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: 1, zoom: nil, windowIndex: nil))
        XCTAssertEqual(session.speakerBackground?.focus, SpeakerBackgroundFocus(x: 1, y: 0.5, zoom: 1))
        XCTAssertTrue(session.hasUnsavedChanges)
        // A nil part keeps the other part; the zoom is clamped to 0.3...3.
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: nil, zoom: 9, windowIndex: nil))
        XCTAssertEqual(session.speakerBackground?.focus, SpeakerBackgroundFocus(x: 1, y: 0.5, zoom: 3))
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: nil, zoom: 0.01, windowIndex: nil))
        XCTAssertEqual(session.speakerBackground?.focus?.zoom, 0.3)
        // x is clamped to 0...1.
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: -4, zoom: nil, windowIndex: nil))
        XCTAssertEqual(session.speakerBackground?.focus?.x, 0)
        // One window only.
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: 0, zoom: 1.5, windowIndex: 1))
        XCTAssertNil(session.speakerBackgroundWindows[0].focus)
        XCTAssertEqual(session.speakerBackgroundWindows[1].focus, SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 1.5))
        // The same value again, a missing window, a missing value and non-finite values change nothing.
        XCTAssertFalse(session.setSpeakerBackgroundFocus(x: 0, zoom: 1.5, windowIndex: 1))
        XCTAssertFalse(session.setSpeakerBackgroundFocus(x: 0.5, zoom: nil, windowIndex: 7))
        XCTAssertFalse(session.setSpeakerBackgroundFocus(x: nil, zoom: nil, windowIndex: 0))
        XCTAssertFalse(session.setSpeakerBackgroundFocus(x: .nan, zoom: nil, windowIndex: 0))
        // The centre at 1x is today's drawing: the aim goes (key absent).
        XCTAssertTrue(session.setSpeakerBackgroundFocus(x: 0.5, zoom: 1, windowIndex: 1))
        XCTAssertNil(session.speakerBackgroundWindows[1].focus)
        // Undo walks back.
        session.undo()
        XCTAssertEqual(session.speakerBackgroundWindows[1].focus, SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 1.5))
    }

    func testTheAimControlsFollowTheCapabilityAndAColourHasNothingToAim() {
        let closed = focusSession(lane: focusLane(), focusOpen: false)
        XCTAssertFalse(closed.canEditSpeakerBackgroundFocus)
        XCTAssertFalse(closed.setSpeakerBackgroundFocus(x: 0, zoom: nil, windowIndex: nil))
        XCTAssertNil(closed.speakerBackground?.focus)
        let colour = focusSession(lane: .object(["kind": .string("color"), "color": .string("#112233")]))
        XCTAssertFalse(colour.setSpeakerBackgroundFocus(x: 0, zoom: nil, windowIndex: nil), "a colour fill has no picture")
        XCTAssertNil(colour.speakerBackground?.focus)
    }

    func testFocusIsCarriedThroughOtherBackgroundEdits() throws {
        let session = focusSession(lane: focusLane(fill: focusRow(0.2, 1.2), windows: [focusWindow("a", start: 0, end: 4, focus: focusRow(0, 2.5))]))
        // The same photo again keeps the fill aim; another photo drops it.
        XCTAssertTrue(session.setSpeakerBackground(mediaID: "fill", kind: .image))
        XCTAssertEqual(session.speakerBackground?.focus, SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 1.2))
        XCTAssertTrue(session.setSpeakerBackground(mediaID: "other", kind: .image))
        XCTAssertNil(session.speakerBackground?.focus)
        XCTAssertEqual(session.speakerBackgroundWindows.first?.focus, SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 2.5), "the windows keep theirs")
        // A colour drops the fill aim and keeps the windows' (Colour needs the colour gate).
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_windows"] = .object(["editable": .bool(true)])
        caps["background_focus"] = .object(["editable": .bool(true)])
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        var payload = draft.serverSnapshot["editor_payload"]?.objectValue ?? [:]
        var sections = payload["sections"]?.objectValue ?? [:]
        sections["speaker_background"] = focusLane(fill: focusRow(0.2, 1.2), windows: [focusWindow("a", start: 0, end: 4, focus: focusRow(0, 2.5))])
        payload["sections"] = .object(sections)
        draft.serverSnapshot["editor_payload"] = .object(payload)
        let coloured = NativeEditorSession(draft: draft)
        XCTAssertTrue(coloured.setSpeakerBackgroundColor(hex: "#112233"))
        XCTAssertNil(coloured.speakerBackground?.focus)
        XCTAssertEqual(coloured.speakerBackgroundWindows.first?.focus, SpeakerBackgroundFocus(x: 0, y: 0.5, zoom: 2.5))
    }

    func testANewWindowOfAnAimedRecordingStartsAimedTheSame() throws {
        let session = focusSession(lane: focusLane(fill: focusRow(0.2, 1.2), windows: [focusWindow("a", start: 0, end: 2, focus: focusRow(1, 2))]))
        // Same media as the fill -> the fill's aim; same media as an existing window -> that window's aim; unknown media -> none.
        func fresh(_ id: String, _ mediaID: String, _ start: Double) -> EditorSpeakerBackgroundWindow {
            EditorSpeakerBackgroundWindow(localID: id, mediaID: mediaID, kind: .image, startS: start, endS: start + 1)
        }
        XCTAssertTrue(session.setSpeakerBackgroundWindows(session.speakerBackgroundWindows
            + [fresh("bg-5", "fill", 2), fresh("bg-6", "a", 3), fresh("bg-7", "fresh", 4)]))
        let focuses = session.speakerBackgroundWindows.map(\.focus)
        XCTAssertEqual(focuses, [SpeakerBackgroundFocus(x: 1, y: 0.5, zoom: 2), SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 1.2),
                                 SpeakerBackgroundFocus(x: 1, y: 0.5, zoom: 2), nil])
    }

    func testTheProtocolDeclaresTheFocusBuild() {
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 39, "39 = decodes, draws and round-trips speaker_background focus")
    }
}
