import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1558: the editor's half of the speaker layout per section. The document lane `speaker_background.layout_windows`
/// (`[{start_s, end_s, layout, speaker_frame?}]`, cut-timeline seconds) compiles to the recipe layer's `layout_windows`,
/// which the phone draws per frame exactly as the export does. Failure modes 33-46 in
/// `docs/reviews/kri-1558-section-layout-failure-modes.md`.
@MainActor final class SpeakerLayoutWindowsEditorTests: XCTestCase {
    private let clipID = UUID()
    private let fingerprint = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 400)
    private let corner: JSONValue = .object(["width_fraction": .number(0.42), "x_fraction": .number(0.76), "y_fraction": .number(0.8)])
    private let leftCorner: JSONValue = .object(["width_fraction": .number(0.42), "x_fraction": .number(0.24), "y_fraction": .number(0.8)])

    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: NativeEditorUITestFixtures.savedSpeakerBackgroundKey)
    }

    // MARK: - Fixtures

    private func row(_ start: Double, _ end: Double, _ layout: String, frame: JSONValue? = nil) -> JSONValue {
        var fields: [String: JSONValue] = ["start_s": .number(start), "end_s": .number(end), "layout": .string(layout)]
        if let frame { fields["speaker_frame"] = frame }
        return .object(fields)
    }

    /// The planner's arrangement over a 6 s take: own take, bottom-right corner, picture alone, bottom-left corner.
    private var arrangement: [JSONValue] {
        [row(0, 1.5, "own"), row(1.5, 3.5, "small", frame: corner), row(3.5, 4.5, "picture"), row(4.5, 6, "small", frame: leftCorner)]
    }

    private func mediaLane(_ layouts: [JSONValue]?, frame: JSONValue? = nil, fullScreen: [JSONValue] = []) -> JSONValue {
        var fields: [String: JSONValue] = ["media_id": .string("fill"), "kind": .string("image"), "source_start_s": .number(0)]
        if let layouts { fields["layout_windows"] = .array(layouts) }
        if let frame { fields["speaker_frame"] = frame }
        if !fullScreen.isEmpty { fields["full_screen_windows"] = .array(fullScreen) }
        return .object(fields)
    }

    private func snapshot(lane: JSONValue?) -> [String: JSONValue] {
        var sections: [String: JSONValue] = ["timeline_slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(6)])])]
        if let lane { sections["speaker_background"] = lane }
        return ["schema_version": .number(2), "kind": .string("editor"), "edit_format": .string("subtitled"),
                "editor_payload": .object(["base_generation": .string("g1"), "sections": .object(sections)])]
    }

    private func compile(_ document: EditorDocument) throws -> KriaMediaEngine.EditRecipe {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 8),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: clipID, assetID: clipID, sourceClipIndex: 0, start: 0, end: 6, trimIn: 0, trimOut: 6,
                              sourceDuration: 8, slotID: "slot")
        let background = EditorSpeakerBackground.assetID(forMediaID: "fill")
        let backgroundSource = ResolvedEditorSource(clipIndex: -1, mediaID: "fill",
            asset: MediaAsset(id: background, relativePath: "background.bin", fingerprint: AssetFingerprint(hex: String(repeating: "c", count: 64), byteCount: 90)),
            url: URL(fileURLWithPath: "/fixture/background.bin"), visualGeneration: "7")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                    mediaSources: [EditorSpeakerBackground.sourceKey(forMediaID: "fill"): backgroundSource]).recipe
    }

    private func document(lane: JSONValue) throws -> EditorDocument {
        var value = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 6)])
        value.speakerBackground = try XCTUnwrap(EditorSpeakerBackground(json: lane))
        return value
    }

    private func dump(_ recipe: KriaMediaEngine.EditRecipe) throws -> Data {
        try JSONSerialization.data(withJSONObject: JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)), options: [.sortedKeys])
    }

    /// A Talking session with the background, the frame controls and (unless `sectionsOpen` is false) the section controls.
    private func session(lane: JSONValue, sectionsOpen: Bool = true, extraCaps: [String] = []) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_windows"] = .object(["editable": .bool(true)])
        if sectionsOpen { caps["speaker_sections"] = .object(["editable": .bool(true)]) }
        for key in extraCaps { caps[key] = .object(["editable": .bool(true)]) }
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        var payload = draft.serverSnapshot["editor_payload"]?.objectValue ?? [:]
        var sections = payload["sections"]?.objectValue ?? [:]
        sections["speaker_background"] = lane
        payload["sections"] = .object(sections)
        draft.serverSnapshot["editor_payload"] = .object(payload)
        return NativeEditorSession(draft: draft)
    }

    private func layouts(_ session: NativeEditorSession) -> [EditorLayoutWindow] { session.speakerBackground?.layoutWindows ?? [] }

    // MARK: - Document and compile (rows 33-35)

    func testLayoutWindowsRoundTripAndCompileToTheLayerTheExportDraws() throws {
        let lane = mediaLane(arrangement)
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        XCTAssertFalse(decoded.unreadableSpeakerBackground, "a lane with layout windows is readable")
        let background = try XCTUnwrap(decoded.speakerBackground)
        XCTAssertEqual(background.layoutWindows.map(\.layout), [.own, .small, .picture, .small])
        XCTAssertEqual(background.layoutWindows[1].speakerFrame, .preset("small_bottom_right"))
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "an unchanged lane saves exactly as it loaded")

        let recipe = try compile(decoded)
        let expected = [
            SpeakerLayoutWindow(start: 0, end: 1.5, layout: .own),
            SpeakerLayoutWindow(start: 1.5, end: 3.5, layout: .small, frame: SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.8)),
            SpeakerLayoutWindow(start: 3.5, end: 4.5, layout: .picture),
            SpeakerLayoutWindow(start: 4.5, end: 6, layout: .small, frame: SpeakerFrame(widthFraction: 0.42, xFraction: 0.24, yFraction: 0.8)),
        ]
        XCTAssertEqual(recipe.speakerBackground?.layoutWindows, expected, "cut-timeline seconds are the recipe's seconds")
        XCTAssertNil(recipe.speakerBackground?.speakerFrame, "the arrangement carries no whole-video frame")
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.speakerSections), "an older build is never handed sections it would ignore")
        try recipe.validate()

        // The wire the export reads: snake_case, `frame` only on a corner, and it decodes back to the same recipe.
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)) as? [String: Any])
        let layer = try XCTUnwrap(json["speaker_background"] as? [String: Any])
        let rows = try XCTUnwrap(layer["layout_windows"] as? [[String: Any]])
        XCTAssertEqual(rows.map { $0["layout"] as? String }, ["own", "small", "picture", "small"])
        XCTAssertEqual(rows.map { $0["frame"] != nil }, [false, true, false, true])
        let reread = try RecipeJSON.decoder().decode(KriaMediaEngine.EditRecipe.self, from: RecipeJSON.encode(recipe))
        XCTAssertEqual(reread.speakerBackground, recipe.speakerBackground, "a reopened edit draws the same sections")
    }

    func testAColourFillCarriesLayoutWindowsToo() throws {
        let lane: JSONValue = .object(["kind": .string("color"), "color": .string("#2B6CB0"), "layout_windows": .array(arrangement)])
        let background = try XCTUnwrap(EditorSpeakerBackground(json: lane))
        XCTAssertEqual(background.json, lane)
        XCTAssertEqual(background.layer.layoutWindows?.count, 4)
        try background.layer.validate()
    }

    func testAnOldShapeLaneEncodesAndCompilesWithoutAnyTraceOfSections() throws {
        let lane = mediaLane(nil, frame: corner)
        let decoded = EditorDocument.decode(snapshot: snapshot(lane: lane))
        XCTAssertEqual(decoded.encodeSnapshot(), snapshot(lane: lane), "no key appears on Save")
        let background = try XCTUnwrap(decoded.speakerBackground)
        XCTAssertTrue(background.layoutWindows.isEmpty)
        XCTAssertNil(background.layer.layoutWindows, "the layer carries no `layout_windows`, not even null")
        let recipe = try compile(document(lane: lane))
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.speakerSections))
        let bytes = try dump(recipe)
        XCTAssertFalse(String(decoding: bytes, as: UTF8.self).contains("layout_windows"), "the recipe is the one this build compiled before sections")
        // The same lane built through the pre-KRI-1558 initialiser compiles to the same bytes.
        var legacy = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 6)])
        legacy.speakerBackground = EditorSpeakerBackground(mediaID: "fill", kind: .image, speakerFrame: .preset("small_bottom_right"))
        XCTAssertEqual(try dump(compile(legacy)), bytes)
    }

    func testShapesTheEditorCannotHoldExactlyAreUnreadable() {
        func readable(_ field: JSONValue) -> Bool {
            guard case .object(var fields) = mediaLane(nil) else { return false }
            fields["layout_windows"] = field
            return EditorSpeakerBackground(json: .object(fields)) != nil
        }
        XCTAssertTrue(readable(.array(arrangement)))
        XCTAssertTrue(readable(.array([row(0, 3, "full"), row(3, 5, "own")])), "touching ends are fine")
        XCTAssertFalse(readable(.array([])), "[] is not absent")
        XCTAssertFalse(readable(.null), "null is not absent")
        XCTAssertFalse(readable(.array([row(0, 3, "small")])), "a corner needs its frame")
        XCTAssertFalse(readable(.array([row(0, 3, "own", frame: corner)])), "only a corner carries a frame")
        XCTAssertFalse(readable(.array([row(0, 3, "full", frame: corner)])))
        XCTAssertFalse(readable(.array([row(0, 3, "sideways")])), "an unknown layout")
        XCTAssertFalse(readable(.array([row(0, 3, "own"), row(2, 5, "picture")])), "overlap")
        XCTAssertFalse(readable(.array([row(3, 5, "own"), row(0, 3, "picture")])), "unsorted")
        XCTAssertFalse(readable(.array([row(0, 0.4, "own")])), "under half a second")
        XCTAssertFalse(readable(.array([row(-1, 3, "own")])))
        XCTAssertFalse(readable(.array([row(0, 1801, "own")])))
        XCTAssertFalse(readable(.array((0..<41).map { row(Double($0), Double($0) + 1, "own") })), "at most forty")
        XCTAssertFalse(readable(.array([.object(["start_s": .number(0), "end_s": .number(3), "layout": .string("own"), "extra": .number(1)])])))
        XCTAssertFalse(readable(.array([row(0, 3, "small", frame: .object(["width_fraction": .number(0.1), "x_fraction": .number(0.5), "y_fraction": .number(0.5)]))])),
                       "a frame below the wire's minimum width")
        XCTAssertTrue(EditorDocument.decode(snapshot: snapshot(lane: mediaLane([row(0, 3, "small")]))).unreadableSpeakerBackground,
                      "the editor refuses to preview or Save a lane it cannot hold, never drops part of it")
    }

    // MARK: - Every other background edit keeps them (row 36)

    func testEveryOtherBackgroundEditKeepsTheLayoutWindows() {
        let lane = mediaLane(arrangement, fullScreen: [.object(["start_s": .number(0), "end_s": .number(1)])])
        let editor = session(lane: lane, extraCaps: ["speaker_background_color", "background_focus", "speaker_full_opening"])
        let original = layouts(editor)
        XCTAssertEqual(original.count, 4)
        XCTAssertTrue(editor.setSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertEqual(layouts(editor), original, "a new photo fill keeps them")
        XCTAssertTrue(editor.setSpeakerBackgroundColor(hex: "#00ff00"))
        XCTAssertEqual(layouts(editor), original, "a colour fill keeps them")
        XCTAssertTrue(editor.setSpeakerBackground(mediaID: "fixture-bg-video", kind: .video, sourceStart: 1))
        XCTAssertEqual(layouts(editor), original, "a video fill keeps them")
        XCTAssertTrue(editor.setSpeakerBackgroundFocus(x: 0, zoom: 1.5))
        XCTAssertEqual(layouts(editor), original, "aiming the fill keeps them")
        XCTAssertTrue(editor.addSpeakerBackgroundWindow(mediaID: "fixture-bg-photo", kind: .image, length: 2))
        XCTAssertEqual(layouts(editor), original, "adding a timed background keeps them")
        XCTAssertTrue(editor.removeSpeakerBackgroundWindow(id: editor.speakerBackgroundWindows[0].localID))
        XCTAssertEqual(layouts(editor), original, "removing it keeps them")
        XCTAssertTrue(editor.removeFullScreenOpening())
        XCTAssertEqual(layouts(editor), original, "removing the opening keeps them")
        // Outside every section (past the last one) the corner controls act on the whole-video frame as before.
        editor.currentTime = 7
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_top_left"))
        XCTAssertEqual(editor.speakerFrame, .preset("small_top_left"))
        XCTAssertEqual(layouts(editor), original, "the whole-video corner keeps them")
        XCTAssertTrue(editor.clearSpeakerFrame())
        XCTAssertEqual(layouts(editor), original, "clearing the whole-video corner keeps them")
        XCTAssertTrue(editor.removeSpeakerBackground())
        XCTAssertNil(editor.speakerBackground, "removing the background takes the sections with it")
        editor.undo()
        XCTAssertEqual(layouts(editor), original)
    }

    func testSaveAfterAColourChangeSendsTheLayoutWindowsUnchanged() async throws {
        let threadID = UUID(), jobID = UUID()
        let lane = mediaLane(arrangement)
        var variant: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(6), "output_url": .string("file:///tmp/kria-editor-test.mp4"), "resolved_archetype": .string("subtitled"),
            "base_video_path": .string("base.mp4"), "speaker_background": lane,
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0),
                "duration_s": .number(6), "source_duration_s": .number(8), "removed": .bool(false)])])]),
        ]
        variant["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
                                                  "speaker_background": .object(["editable": .bool(true)]),
                                                  "speaker_background_color": .object(["editable": .bool(true)]),
                                                  "speaker_background_windows": .object(["editable": .bool(true)]),
                                                  "speaker_sections": .object(["editable": .bool(true)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant)
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        let editor = NativeEditorSession()
        await editor.load(api: fake, threadID: threadID)
        XCTAssertEqual(editor.speakerLayoutWindows.count, 4, "the variant's sections load")
        XCTAssertFalse(editor.hasUnsavedChanges)

        XCTAssertTrue(editor.setSpeakerBackgroundColor(hex: "#112233"))
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await editor.save()
        guard case .replace(let sent)? = fake.lastRequest?.speakerBackground else { return XCTFail("the lane was not sent") }
        XCTAssertEqual(sent["color"], .string("#112233"))
        XCTAssertEqual(sent["layout_windows"], .array(arrangement), "the sections reach the server exactly as they loaded")
    }

    // MARK: - The frame in effect follows the playhead (rows 41-43)

    func testTheFrameInEffectFollowsThePlayheadAndOwnOrPictureIsNotSelectable() {
        let editor = session(lane: mediaLane(arrangement))
        XCTAssertTrue(editor.canEditSpeakerSections)
        XCTAssertNil(editor.speakerFrame, "no whole-video frame")
        XCTAssertTrue(editor.canMoveSpeakerOnPreview, "a corner section makes the speaker a preview object")
        XCTAssertEqual(editor.previewSpeakerItems.map(\.selection), [.speaker])
        XCTAssertNil(editor.speakerFrame(for: .speaker, at: 0.5), "own take: nothing cut out to select")
        XCTAssertEqual(editor.speakerFrame(for: .speaker, at: 2), .preset("small_bottom_right"))
        XCTAssertNil(editor.speakerFrame(for: .speaker, at: 4), "picture alone: the speaker is not drawn")
        XCTAssertEqual(editor.speakerFrame(for: .speaker, at: 5), .preset("small_bottom_left"))
        XCTAssertNil(editor.speakerFrame(for: .speaker, at: 6.5), "after the sections, the whole-video frame (none)")
        editor.currentTime = 2
        XCTAssertEqual(editor.speakerFrame(for: .speaker), .preset("small_bottom_right"), "the panel reads the playhead's section")
        XCTAssertEqual(editor.speakerLayoutWindowAtPlayhead?.layout, .small)
        editor.currentTime = 0.5
        XCTAssertEqual(editor.speakerEditingFrame(for: .speaker), EditorSpeakerFrame(widthFraction: 1, xFraction: 0.5, yFraction: 0.5),
                       "a section without a cut-out resizes from the whole frame")

        // A full-screen window wins over a section, exactly as the compositor decides.
        let opening = EditorSpeakerBackground(json: mediaLane(arrangement, fullScreen: [.object(["start_s": .number(1.5), "end_s": .number(2.5)])]))
        XCTAssertNil(opening?.layoutWindowIndex(at: 2))
        XCTAssertEqual(opening?.layoutWindowIndex(at: 3), 1)
    }

    func testWithoutTheCapabilitySectionsAreKeptAndTheControlsActOnTheWholeVideo() {
        let editor = session(lane: mediaLane(arrangement, frame: corner), sectionsOpen: false)
        let original = layouts(editor)
        XCTAssertFalse(editor.canEditSpeakerSections)
        XCTAssertNil(editor.speakerLayoutWindowAtPlayhead)
        editor.currentTime = 2
        XCTAssertEqual(editor.speakerFrame(for: .speaker), .preset("small_bottom_right"), "the whole-video frame, as before")
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_top_left"))
        XCTAssertEqual(editor.speakerFrame, .preset("small_top_left"), "presets change the whole-video frame")
        XCTAssertEqual(layouts(editor), original, "and never touch the sections")
        editor.setSpeakerFrameCenter(x: 0.3, y: 0.3)
        XCTAssertEqual(layouts(editor), original)
        XCTAssertNil(editor.speakerFrameRect(for: .speaker, at: 2), "a section's frame is not editable here, so it is not a preview object")
    }

    // MARK: - Editing a section (rows 38-40, 45)

    func testAPresetInsideASectionChangesOnlyThatSection() {
        let editor = session(lane: mediaLane(arrangement))
        let before = editor.document
        editor.currentTime = 2
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_top_left", for: .speaker))
        var expected = before.speakerBackground!.layoutWindows
        expected[1].speakerFrame = .preset("small_top_left")
        XCTAssertEqual(layouts(editor), expected, "only the section under the playhead moved")
        XCTAssertNil(editor.speakerFrame, "the whole-video frame is untouched")
        XCTAssertTrue(editor.isDirty(.speakerBackground))

        XCTAssertTrue(editor.setSpeakerFrame(preset: "full", for: .speaker))
        XCTAssertEqual(layouts(editor)[1], EditorLayoutWindow(startS: 1.5, endS: 3.5, layout: .full), "Full makes the section full size")

        editor.currentTime = 0.5
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_bottom_left"))
        XCTAssertEqual(layouts(editor)[0], EditorLayoutWindow(startS: 0, endS: 1.5, layout: .small, speakerFrame: .preset("small_bottom_left")),
                       "an own-take section becomes a corner")
        editor.currentTime = 4
        XCTAssertTrue(editor.setSpeakerFrame(preset: "full"))
        XCTAssertEqual(layouts(editor)[2].layout, .full, "a picture-only section brings the speaker back full size")
        XCTAssertEqual(layouts(editor)[3], before.speakerBackground!.layoutWindows[3], "the last section never moved")

        // One undo step per tap, back to the loaded lane.
        editor.undo(); editor.undo(); editor.undo(); editor.undo()
        XCTAssertEqual(editor.document.speakerBackground, before.speakerBackground)
        XCTAssertFalse(editor.hasUnsavedChanges)
        // A tap that changes nothing is not an edit.
        editor.currentTime = 2
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_bottom_right"))
        XCTAssertFalse(editor.hasUnsavedChanges)
    }

    func testWholeVideoSetsTheOneFrameAndSendsCornerSectionsBackToIt() {
        let editor = session(lane: mediaLane(arrangement))
        let before = editor.document
        editor.currentTime = 2
        editor.speakerFrameAppliesToWholeVideo = true
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_top_right"))
        XCTAssertEqual(editor.speakerFrame, .preset("small_top_right"), "the whole-video frame")
        XCTAssertEqual(layouts(editor).map(\.layout), [.own, .picture], "corner and full sections follow it again; own and picture stay")
        XCTAssertEqual(editor.speakerFrame(for: .speaker, at: 5), .preset("small_top_right"))
        XCTAssertNil(editor.speakerLayoutWindowAtPlayhead, "the playhead's section went back to the whole video")
        editor.undo()
        XCTAssertEqual(editor.document.speakerBackground, before.speakerBackground, "one undo step")
    }

    func testMovingAndResizingInsideASectionEditsThatSectionsFrame() {
        let editor = session(lane: mediaLane(arrangement))
        let before = editor.document
        editor.currentTime = 2
        editor.select(.speaker, seekToStart: false)
        editor.beginTransaction()
        editor.setSpeakerFrameCenter(x: 0.6, y: 0.5, for: .speaker)
        editor.setSpeakerFrameCenter(x: 0.5, y: 0.4, for: .speaker)
        editor.endTransaction()
        XCTAssertEqual(layouts(editor)[1].speakerFrame, EditorSpeakerFrame(widthFraction: 0.42, xFraction: 0.5, yFraction: 0.4))
        XCTAssertEqual(layouts(editor)[3], before.speakerBackground!.layoutWindows[3])
        XCTAssertNil(editor.speakerFrame, "the drag never wrote a whole-video frame")
        editor.setSpeakerFrameWidth(0.6, for: .speaker)
        XCTAssertEqual(layouts(editor)[1].speakerFrame, EditorSpeakerFrame(widthFraction: 0.6, xFraction: 0.5, yFraction: 0.4))
        editor.undo()
        XCTAssertEqual(layouts(editor)[1].speakerFrame?.widthFraction, 0.42)
        editor.undo()
        XCTAssertEqual(editor.document.speakerBackground, before.speakerBackground, "the whole drag was one undo step")
        // Out of range samples are clamped to the wire's range, as for the whole-video frame.
        editor.setSpeakerFrameWidth(5, for: .speaker)
        XCTAssertEqual(layouts(editor)[1].speakerFrame?.widthFraction, 1)
        XCTAssertEqual(editor.selection, .speaker, "the speaker stays selected")
    }

    // MARK: - Cut takes and the timeline (rows 37, 44)

    private func cutTakeSession(sectionsOpen: Bool) -> (NativeEditorSession, EditorSelection, EditorSelection) {
        let first = UUID().uuidString, second = UUID().uuidString
        let slots = [(first, 0.0), (second, 3.0)].map { id, start in
            EditorTimelineSlot(id: id, clipIndex: 0, inS: start, durationS: 3,
                               raw: ["speaker_subject": .bool(true), "source_duration_s": .number(8)])
        }
        var caps: [String: EditorCapability] = ["timeline": .init(editable: true), "speaker_background": .init(editable: true),
                                                "speaker_background_windows": .init(editable: true)]
        if sectionsOpen { caps["speaker_sections"] = .init(editable: true) }
        var document = EditorDocument(clips: slots, capabilities: caps)
        document.speakerBackground = .init(color: "#223344", speakerFrame: .preset("small_bottom_right"))
        var snapshot = document.encodeSnapshot()
        snapshot["render_destination"] = .string("device")
        snapshot["editor_capabilities"] = .object(caps.mapValues { JSONValue.object(["editable": .bool($0.editable)]) })
        let editor = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        return (editor, .init(kind: .speaker, id: first), .init(kind: .speaker, id: second))
    }

    func testACutTakeFramesEachPieceOnlyWithTheSectionCapability() {
        let (editor, head, tail) = cutTakeSession(sectionsOpen: true)
        XCTAssertTrue(editor.usesScopedSpeakerFrame(for: tail), "two pieces of the take: each can be framed")
        XCTAssertEqual(editor.previewSpeakerItems.map(\.selection), [head, tail])
        XCTAssertTrue(editor.setSpeakerFrame(preset: "small_top_left", for: tail))
        XCTAssertEqual(editor.speakerFrame(for: tail), .preset("small_top_left"))
        XCTAssertEqual(editor.speakerFrame(for: head), .preset("small_bottom_right"), "the other piece keeps the whole-video frame")
        XCTAssertEqual(editor.speakerFrame, .preset("small_bottom_right"))
        XCTAssertNil(editor.document.clips[0].raw["speaker_frame_override"], "only the framed piece carries its own frame")

        let (closed, _, closedTail) = cutTakeSession(sectionsOpen: false)
        XCTAssertFalse(closed.usesScopedSpeakerFrame(for: closedTail), "without the seamless split, one frame for the take as before")
        XCTAssertEqual(closed.previewSpeakerItems.map(\.selection), [.speaker])
    }

    func testRemovingTimeMovesTheSectionsAndKeepsThemValid() throws {
        var next = try document(lane: mediaLane(arrangement))
        NativeSplitTimelineRebase.removeTime([(start: 1, end: 2)], from: &next)
        let moved = try XCTUnwrap(next.speakerBackground?.layoutWindows)
        XCTAssertEqual(moved.map(\.startS), [0, 1, 2.5, 3.5])
        XCTAssertEqual(moved.map(\.endS), [1, 2.5, 3.5, 5])
        XCTAssertEqual(moved.map(\.layout), [.own, .small, .picture, .small], "each layout stays with its words")
        XCTAssertTrue(EditorSpeakerBackground.layoutWindowsAreOrdered(moved))

        // A section inside the removed span goes; the list never keeps one under half a second.
        var inside = try document(lane: mediaLane([row(0, 1, "own"), row(1, 1.8, "picture"), row(1.8, 4, "small", frame: corner)]))
        NativeSplitTimelineRebase.removeTime([(start: 0.9, end: 1.9)], from: &inside)
        let kept = try XCTUnwrap(inside.speakerBackground?.layoutWindows)
        XCTAssertEqual(kept.map(\.layout), [.own, .small])
        XCTAssertTrue(EditorSpeakerBackground.layoutWindowsAreOrdered(kept))
        // Every section gone: the key leaves the wire.
        var gone = try document(lane: mediaLane([row(1, 2, "own")]))
        NativeSplitTimelineRebase.removeTime([(start: 0.5, end: 2.5)], from: &gone)
        XCTAssertEqual(gone.speakerBackground?.layoutWindows, [])
        XCTAssertNil(gone.speakerBackground?.json.objectValue?["layout_windows"])
    }

    func testTheProtocolDeclaresTheSectionsBuild() {
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 58, "58 = plays layout sections and a split take as one shot")
    }
}
