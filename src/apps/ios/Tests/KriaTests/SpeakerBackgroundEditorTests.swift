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
