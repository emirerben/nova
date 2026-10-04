import XCTest
@testable import Kria

#if DEBUG
@MainActor
final class NativeEditorInspectorTests: XCTestCase {
    private struct PickerContract: Decodable {
        struct Bounds: Decodable { let min: Double; let max: Double }
        let lookPresets: [String]
        let transitions: [String]
        let textAnimations: [String]
        let musicAlignments: [String]
        let captionFonts: [String]
        let captionSizePX: Bounds
        let captionYFrac: Bounds
        enum CodingKeys: String, CodingKey {
            case lookPresets = "look_presets"
            case transitions
            case textAnimations = "text_animations"
            case musicAlignments = "music_alignments"
            case captionFonts = "caption_fonts"
            case captionSizePX = "caption_size_px"
            case captionYFrac = "caption_y_frac"
        }
    }

    func testPickerValuesMatchSharedEditorCommitContract() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "editor-commit-picker-contract", withExtension: "json"))
        let contract = try JSONDecoder().decode(PickerContract.self, from: Data(contentsOf: url))

        XCTAssertEqual(NativeEditorWireContract.lookPresets, contract.lookPresets)
        XCTAssertEqual(NativeEditorWireContract.transitions, contract.transitions)
        XCTAssertEqual(NativeEditorWireContract.textAnimations, contract.textAnimations)
        XCTAssertEqual(NativeEditorWireContract.musicAlignments, contract.musicAlignments)
        XCTAssertEqual(NativeEditorWireContract.captionFonts, contract.captionFonts)
        XCTAssertEqual(contract.captionSizePX.min, 36)
        XCTAssertEqual(contract.captionSizePX.max, 160)
        XCTAssertEqual(contract.captionYFrac.min, 0.30)
        XCTAssertEqual(contract.captionYFrac.max, 0.90)
    }

    /// KRI-374: `user_song` is additive; anything missing or malformed reads as "no creator song".
    func testUserSongDecodesLeniently() {
        func variant(_ fields: [String: JSONValue]) -> [String: JSONValue] { ["user_song": .object(fields)] }
        let valid: [String: JSONValue] = ["title": .string("  Midnight Drive "), "mode": .string("lipsync"),
            "duration_s": .number(214), "window_start_s": .number(108), "window_end_s": .number(123)]
        let song = NativeUserSong(variant: variant(valid))
        XCTAssertEqual(song?.title, "Midnight Drive")
        XCTAssertEqual(song?.mode, .lipsync)
        XCTAssertEqual(song?.windowStartS, 108)
        XCTAssertEqual(song?.windowEndS, 123)
        XCTAssertEqual(song?.durationS, 214)
        XCTAssertEqual(song?.volume, 1, "no volume on the wire (an older server) reads as full volume")
        XCTAssertEqual(song?.windowLengthS, 15)

        // KRI-428: `volume` is additive and lenient; anything out of 0...1 or not a number reads as full volume.
        XCTAssertEqual(NativeUserSong(variant: variant(valid.merging(["volume": .number(0.35)]) { _, new in new }))?.volume, 0.35)
        XCTAssertEqual(NativeUserSong(variant: variant(valid.merging(["volume": .number(0)]) { _, new in new }))?.volume, 0)
        for volume: JSONValue in [.number(1.5), .number(-0.1), .string("loud"), .null] {
            XCTAssertEqual(NativeUserSong(variant: variant(valid.merging(["volume": volume]) { _, new in new }))?.volume, 1, "\(volume)")
        }

        // Unsaved edits overlay the saved song; a removal drops it; the window keeps its length.
        let edited = song?.applying(EditorUserSongState(volume: 0.5, windowStartS: 20))
        XCTAssertEqual(edited?.volume, 0.5)
        XCTAssertEqual(edited?.windowStartS, 20)
        XCTAssertEqual(edited?.windowEndS, 35)
        XCTAssertEqual(song?.applying(nil), song)
        XCTAssertNil(song?.applying(EditorUserSongState(removed: true)))

        var untitled = valid; untitled["title"] = .null; untitled["duration_s"] = .string("long")
        XCTAssertNil(NativeUserSong(variant: variant(untitled))?.title)
        XCTAssertNil(NativeUserSong(variant: variant(untitled))?.durationS)
        XCTAssertEqual(NativeUserSong(variant: variant(untitled))?.mode, .lipsync)

        XCTAssertNil(NativeUserSong(variant: [:]), "every other variant has no user_song")
        XCTAssertNil(NativeUserSong(variant: ["user_song": .string("nope")]))
        for broken: [String: JSONValue] in [
            valid.merging(["mode": .string("karaoke")]) { _, new in new },
            valid.merging(["mode": .null]) { _, new in new },
            valid.merging(["window_start_s": .number(-1)]) { _, new in new },
            valid.merging(["window_end_s": .number(108)]) { _, new in new },
            valid.merging(["window_end_s": .string("2:03")]) { _, new in new },
            valid.filter { $0.key != "window_start_s" },
        ] { XCTAssertNil(NativeUserSong(variant: variant(broken)), "\(broken)") }
    }

    func testYourSongRowShowsTitleWindowAndModeOrAnHonestFallback() {
        let background = NativeUserSong(variant: ["user_song": .object(["title": .string("Midnight Drive"), "mode": .string("background"),
            "duration_s": .number(214), "window_start_s": .number(108), "window_end_s": .number(123)])])
        let row = NativeEditorYourSong.make(userSong: background, bed: nil)
        XCTAssertEqual(row, NativeEditorYourSong(title: "Midnight Drive", window: "Plays 1:48 – 2:03", mode: "Background"))
        XCTAssertEqual(row?.accessibilitySummary, "Midnight Drive, Plays 1:48 – 2:03, Background")

        let lipsync = NativeUserSong(variant: ["user_song": .object(["mode": .string("lipsync"),
            "window_start_s": .number(59.6), "window_end_s": .number(75)])])
        XCTAssertEqual(NativeEditorYourSong.make(userSong: lipsync, bed: nil),
                       NativeEditorYourSong(title: "Your song", window: "Plays 1:00 – 1:15", mode: "Lip-sync · master audio"))

        // An older server sends no user_song: the recipe's clip still connects the tab, without claiming a mode.
        let bed = NativeEditorSongBed(assetID: "song-item", sourceStart: 108, sourceDuration: 15)
        XCTAssertEqual(NativeEditorYourSong.make(userSong: nil, bed: bed),
                       NativeEditorYourSong(title: "Your song", window: "Plays 1:48 – 2:03", mode: nil))
        XCTAssertNil(NativeEditorYourSong.make(userSong: nil, bed: nil))
        XCTAssertTrue(NativeEditorYourSong.helperCopy.contains("Camera audio is muted"))
    }

    func testSoundsTabKeepsCatalogControlsWhenThereIsNoCreatorSong() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        XCTAssertNil(session.yourSong)
        XCTAssertNil(session.deviceSongBed)
    }

    func testInspectorMutationsStayInsideEditorCommitContract() {
        var draft = NativeEditorUITestFixtures.allLanes
        draft.serverSnapshot["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "captions": .bool(true), "mix": .bool(true),
        ])
        let session = NativeEditorSession(draft: draft)
        let clipID = session.document.clips[0].id!
        let textID = session.document.textElements[0].id

        session.setClipLookPreset(clipID: clipID, preset: nil)
        session.setClipTransition(clipID: clipID, transition: "crossfade", durationS: 0)
        session.setTextAnimation(id: textID, animation: "pop-in")
        session.setCaptionFont("Bebas Neue")
        XCTAssertEqual(session.document.captionMeta["font"], .string("Bebas Neue"))
        session.setCaptionFont("Not A Font")
        XCTAssertEqual(session.document.captionMeta["font"], .string("Bebas Neue"), "unknown fonts are ignored")
        session.setTextStyle(id: textID, style: "Playfair Display")
        XCTAssertEqual(session.document.textElements[0].raw["font_family"], .string("Playfair Display"))
        session.setCaptionFont("Inter")
        session.setCaptionSize(12)
        session.setCaptionPositionY(0)
        session.setMusicWindow(alignment: "resync_beats")

        XCTAssertEqual(session.document.clips[0].lookPreset, "none")
        XCTAssertEqual(session.document.clips[0].transitionAfter, "crossfade")
        XCTAssertEqual(session.document.clips[0].transitionDurationS, 0.1)
        XCTAssertEqual(session.document.textElements[0].raw["effect"], .string("pop-in"))
        XCTAssertEqual(session.document.captionMeta["font_set"], .bool(true))
        XCTAssertEqual(session.document.captionMeta["size_px"], .number(36))
        XCTAssertEqual(session.document.captionMeta["y_frac"], .number(0.30))
        XCTAssertEqual(session.document.music?.alignment, "resync_beats")

        session.setClipTransition(clipID: clipID, transition: "crossfade", durationS: 2)
        session.setCaptionSize(200)
        session.setCaptionPositionY(1)
        // KRI-167: the server's ceiling is 0.3s everywhere (guided_edit_revision.py's
        // le=0.3); anything requested above that is clamped at the mutation,
        // not silently shrunk later at Save.
        XCTAssertEqual(session.document.clips[0].transitionDurationS, 0.3)
        XCTAssertEqual(session.document.captionMeta["size_px"], .number(160))
        XCTAssertEqual(session.document.captionMeta["y_frac"], .number(0.90))
    }

    // KRI-167: "Apply to whole video" sets every boundary but the last in a
    // single transaction (one undo step), clamps duration, and no-ops on an
    // unknown transition or a fewer-than-2-clip document.
    func testSetTransitionAcrossVideoAppliesInOneTransaction() {
        var draft = NativeEditorUITestFixtures.allLanes
        draft.serverSnapshot["editor_capabilities"] = .object(["timeline": .bool(true)])
        let session = NativeEditorSession(draft: draft)
        XCTAssertEqual(session.document.clips.count, 3, "fixture must have 2 boundaries to exercise this")
        let before = session.document

        session.setTransitionAcrossVideo(transition: "crossfade", durationS: 5)

        XCTAssertEqual(session.document.clips[0].transitionAfter, "crossfade")
        XCTAssertEqual(session.document.clips[0].transitionDurationS, 0.3, "clamped to the 0.3s server ceiling")
        XCTAssertEqual(session.document.clips[1].transitionAfter, "crossfade")
        XCTAssertEqual(session.document.clips[1].transitionDurationS, 0.3)
        XCTAssertEqual(session.document.clips[2].transitionAfter, "cut", "the last clip has no outgoing boundary")
        XCTAssertNil(session.document.clips[2].transitionDurationS)

        session.undo()
        XCTAssertEqual(session.document, before, "every boundary reverts in a single undo step")
    }

    func testSetTransitionAcrossVideoIgnoresUnknownTransitionAndSingleClipDocument() {
        var draft = NativeEditorUITestFixtures.allLanes
        draft.serverSnapshot["editor_capabilities"] = .object(["timeline": .bool(true)])
        let allLanesSession = NativeEditorSession(draft: draft)
        let before = allLanesSession.document
        allLanesSession.setTransitionAcrossVideo(transition: "wipe_left", durationS: 0.3)
        XCTAssertEqual(allLanesSession.document, before, "not a value in NativeEditorWireContract.transitions")

        draft = NativeEditorUITestFixtures.captionVisuals
        draft.serverSnapshot["editor_capabilities"] = .object(["timeline": .bool(true)])
        let singleClipSession = NativeEditorSession(draft: draft)
        XCTAssertEqual(singleClipSession.document.clips.count, 1, "fixture must have no boundary to exercise this")
        let beforeSingle = singleClipSession.document
        singleClipSession.setTransitionAcrossVideo(transition: "crossfade", durationS: 0.3)
        XCTAssertEqual(singleClipSession.document, beforeSingle, "no boundary exists with a single clip")
    }

    func testOpaqueTextSelectionEditsAndUndoAsOneLocalTransaction() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.twoText)
        let id = NativeEditorUITestFixtures.twoText.serverSnapshotTextID(at: 0)
        session.select(EditorSelection(kind: .text, id: id), seekToStart: false)
        session.updateTextContent(id: id, content: "Edited opening")

        XCTAssertEqual(session.document.textElements.first(where: { $0.id == id })?.text, "Edited opening")
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.textElements.first(where: { $0.id == id })?.text, "Opening question")
        XCTAssertTrue(session.canRedo)
    }

    func testSelectedClipDeleteIsUndoableAndLeavesSaveDirty() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let clip = try! XCTUnwrap(session.draft.clips.first)
        session.select(EditorSelection(kind: .clip, id: clip.id.uuidString), seekToStart: false)
        session.removeClip(clipID: clip.slotID ?? clip.id.uuidString)

        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.document.tombstones.count, 1)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.clips.count, 3)
        XCTAssertTrue(session.canRedo)
    }

    func testAllLaneFixtureDecodesAndPersistsTypedInspectorEdits() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)

        XCTAssertEqual(session.document.soundEffects.count, 1)
        XCTAssertEqual(session.document.soundEffects.first?.pointS, 0.5)
        XCTAssertEqual(session.document.visualBlocks.first?.kind, "text_card")
        XCTAssertEqual(session.document.backgroundMusic?.gainDB, -6)
        XCTAssertEqual(session.document.title, "All lanes fixture")
        XCTAssertEqual(session.document.orientation, "portrait", "legacy 9:16 reads as portrait")
        XCTAssertNotNil(session.document.lyrics)
        XCTAssertFalse(session.canEdit("orientation"))
        XCTAssertEqual(session.capabilityReason("orientation"), "Orientation is fixed by the rendered variant.")

        session.setSoundEffectTiming(id: "sfx-1", atS: 0.75)
        session.setSoundEffectTrim(id: "sfx-1", trimStartS: 0.1, trimEndS: 0.4)
        session.setSoundEffectGain(id: "sfx-1", gain: 1.25)
        session.setMediaOverlayPosition(id: "overlay-1", x: 0.25, y: 0.7)
        session.setMediaOverlayScale(id: "overlay-1", scale: 1.4)
        session.setMediaOverlayDisplayMode(id: "overlay-1", mode: "fullscreen")
        session.setVisualBlockPreset(id: "visual-1", preset: "text-card-bold")
        session.setCameraEffectIntensity(id: "camera-1", intensity: 0.08)
        session.setCameraEffectEasing(id: "camera-1", easing: "sine_pulse")
        session.setCarouselMomentPosition("outro")

        XCTAssertEqual(session.document.soundEffects.first?.pointS, 0.75)
        XCTAssertEqual(session.document.mediaOverlays.first?.raw["display_mode"], .string("fullscreen"))
        XCTAssertEqual(session.document.visualBlocks.first?.raw["style_preset_id"], .string("text-card-bold"))
        XCTAssertEqual(session.document.cameraEffects.first?.raw["intensity"], .number(0.08))
        XCTAssertEqual(session.document.carouselMoment?["position"], .string("outro"))
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testMotionRuntimeMismatchStaysReadOnly() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        XCTAssertTrue(session.isMotionSceneReadOnly(id: "motion-1"))
        XCTAssertEqual(session.motionRuntimeMismatchReason(id: "motion-1"), "motion_runtime_mismatch")
        session.setMotionSceneTiming(id: "motion-1", startS: 1)
        XCTAssertEqual(session.document.motionScenes.first?.startS, 3)
    }

    func testMediaVisualBlockUsesSchemaValidNestedAndOverlayTransforms() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.visualMedia)
        session.setVisualBlockTransform(id: "visual-media-1", fitMode: "cover", focalX: 0.3, focalY: 0.6, zoom: 2.2)
        session.setVisualBlockDisplayMode(id: "visual-media-1", mode: "overlay")
        session.setVisualBlockOverlayLayout(id: "visual-media-1", x: 0.2, y: 0.7, scale: 0.45)

        let raw = try! XCTUnwrap(session.document.visualBlocks.first?.raw)
        let transform = nativeTestObject(raw["transform"])
        XCTAssertEqual(transform?["fit_mode"], .string("cover"))
        XCTAssertEqual(transform?["focal_x"], .number(0.3))
        XCTAssertEqual(transform?["focal_y"], .number(0.6))
        XCTAssertEqual(transform?["zoom"], .number(2.2))
        XCTAssertEqual(raw["display_mode"], .string("overlay"))
        XCTAssertEqual(raw["x_frac"], .number(0.2))
        XCTAssertEqual(raw["y_frac"], .number(0.7))
        XCTAssertEqual(raw["scale"], .number(0.45))
        XCTAssertNil(raw["rotation"])
    }

    func testDirectPreviewMoveAndResizeCommitOneUndoSnapshot() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        session.beginDirectManipulation()
        session.setMediaOverlayPosition(id: "overlay-1", x: 0.6, y: 0.65)
        session.setMediaOverlayPosition(id: "overlay-1", x: 0.7, y: 0.75)
        session.setMediaOverlayScale(id: "overlay-1", scale: 0.55)
        session.endDirectManipulation()

        let changed = try! XCTUnwrap(session.document.mediaOverlays.first)
        XCTAssertEqual(changed.raw["x_frac"], .number(0.7))
        XCTAssertEqual(changed.raw["y_frac"], .number(0.75))
        XCTAssertEqual(changed.raw["scale"], .number(0.55))
        XCTAssertFalse(session.isDirectManipulating)
        XCTAssertTrue(session.canUndo)

        session.undo()
        let restored = try! XCTUnwrap(session.document.mediaOverlays.first)
        XCTAssertEqual(restored.raw["x_frac"], .number(0.5))
        XCTAssertEqual(restored.raw["y_frac"], .number(0.5))
        XCTAssertEqual(restored.raw["scale"], .number(0.35))
        XCTAssertFalse(session.canUndo, "one canvas gesture must create one snapshot")
    }

    func testAllLaneRemovalLeavesOtherLanesUntouched() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        session.removeSoundEffect(id: "sfx-1")
        session.removeMediaOverlay(id: "overlay-1")
        session.removeVisualBlock(id: "visual-1")
        session.removeCarouselMoment()

        XCTAssertTrue(session.document.soundEffects.isEmpty)
        XCTAssertTrue(session.document.mediaOverlays.isEmpty)
        XCTAssertTrue(session.document.visualBlocks.isEmpty)
        XCTAssertNil(session.document.carouselMoment)
        XCTAssertEqual(session.document.motionScenes.count, 1)
        XCTAssertEqual(session.document.cameraEffects.count, 1)
        XCTAssertEqual(session.document.captionCues.count, 1)
        XCTAssertNotNil(session.document.music)
    }

    // MARK: - KRI-182 step 1: phone Talking (subtitled) editable sfx/overlay lanes

    private func phoneSfxEffect(id: String = "catalog-whoosh", name: String = "Whoosh", durationS: Double? = 0.8) -> NativeEditorSoundEffect {
        NativeEditorSoundEffect(id: id, name: name, durationS: durationS, previewAudioURL: nil, roleTags: [], category: "transition", searchTerms: [name.lowercased()])
    }

    func testAddSoundEffectPlacesAPointEffectAtTheClampedPlayheadWithTheWebWireShape() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.phoneSubtitledLanes)
        session.currentTime = 1.5

        session.addSoundEffect(phoneSfxEffect())

        XCTAssertEqual(session.document.soundEffects.count, 2, "the fixture's existing effect plus the newly placed one")
        let placed = try! XCTUnwrap(session.document.soundEffects.last)
        XCTAssertEqual(placed.pointS, 1.5)
        XCTAssertEqual(placed.startS, 1.5)
        XCTAssertEqual(placed.kind, "sfx")
        XCTAssertEqual(placed.raw["sound_effect_id"], .string("catalog-whoosh"))
        XCTAssertEqual(placed.raw["src_gcs_path"], .string(""))
        XCTAssertEqual(placed.raw["source"], .string("user"))
        XCTAssertEqual(placed.raw["gain"], .number(1))
        XCTAssertEqual(placed.raw["duration_s"], .number(0.8))
        XCTAssertEqual(placed.raw["label"], .string("Whoosh"))
        XCTAssertEqual(session.selection, .init(kind: .soundEffect, id: placed.id))
    }

    func testAddSoundEffectClampsToTheLastEditableClipEndNotDuration() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.phoneSubtitledLanes)
        // The fixture's single clip runs 0...4; the playhead sits past it,
        // as it would once a device composition's `duration` includes the
        // baked Kria outro tail (KRI-169's trap -- see addSoundEffect's doc).
        session.currentTime = 40

        session.addSoundEffect(phoneSfxEffect())

        let placed = try! XCTUnwrap(session.document.soundEffects.last)
        let pointS = try! XCTUnwrap(placed.pointS)
        XCTAssertEqual(pointS, 3.9, accuracy: 0.0001, "clamped to the last editable clip's end (4), not the raw playhead")
    }

    func testAddSoundEffectIsRefusedWhenTheSfxCapabilityIsClosed() {
        var draft = NativeEditorUITestFixtures.phoneSubtitledLanes
        draft.serverSnapshot["editor_capabilities"] = .object([
            "sfx": .object(["editable": .bool(false), "reason": .string("sfx_disabled")]),
            "sound_effects": .object(["editable": .bool(false)]),
            "overlays": .object(["editable": .bool(true)]),
        ])
        let session = NativeEditorSession(draft: draft)
        let before = session.document.soundEffects.count

        session.addSoundEffect(phoneSfxEffect())

        XCTAssertEqual(session.document.soundEffects.count, before, "a closed capability must not append")
    }

    func testAddSoundEffectCoalescesIntoOneUndoSnapshot() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.phoneSubtitledLanes)
        let before = session.document.soundEffects.count

        session.addSoundEffect(phoneSfxEffect())

        XCTAssertEqual(session.document.soundEffects.count, before + 1)
        XCTAssertTrue(session.canUndo)
        session.undo()
        XCTAssertEqual(session.document.soundEffects.count, before)
        XCTAssertFalse(session.canUndo, "one call must create exactly one undo snapshot")
    }

    // MARK: - KRI-288 sound-effects editor contracts

    func testSoundEffectGainClampsToOriginalLevelAndLegacyBoostIsNotRewritten() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        session.setSoundEffectGain(id: "sfx-1", gain: 1.6)
        XCTAssertEqual(session.document.soundEffects.first?.raw["gain"], .number(1))
        session.setSoundEffectGain(id: "sfx-1", gain: -0.2)
        XCTAssertEqual(session.document.soundEffects.first?.raw["gain"], .number(0))
    }

    func testSoundEffectTrimClampsToSourceDurationAndMinimumGap() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes) // duration_s 1.0
        session.setSoundEffectTrim(id: "sfx-1", trimStartS: 0.2, trimEndS: 9)
        XCTAssertEqual(session.document.soundEffects.first?.raw["trim_end_s"], .number(1))
        session.setSoundEffectTrim(id: "sfx-1", trimStartS: 0.99, trimEndS: 1)
        let effect = try! XCTUnwrap(session.document.soundEffects.first)
        let window = NativeEditorSession.soundEffectTrimWindow(effect)
        XCTAssertEqual(window.end - window.start, NativeEditorSession.sfxMinimumTrim, accuracy: 0.0001)
        XCTAssertLessThanOrEqual(window.end, 1)
    }

    func testResetSoundEffectTrimRestoresTheFullSound() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        session.setSoundEffectTrim(id: "sfx-1", trimStartS: 0.1, trimEndS: 0.4)
        session.resetSoundEffectTrim(id: "sfx-1")
        let effect = try! XCTUnwrap(session.document.soundEffects.first)
        XCTAssertNil(effect.raw["trim_start_s"])
        XCTAssertNil(effect.raw["trim_end_s"])
        let window = NativeEditorSession.soundEffectTrimWindow(effect)
        XCTAssertEqual(window.start, 0)
        XCTAssertEqual(window.end, window.source)
    }

    func testSoundEffectTrimWindowDefaultsToSourceDurationWhenUntrimmed() {
        let effect = EditorTimedEffect(id: "x", startS: 2, endS: 2.8, pointS: 2, kind: "sfx", raw: ["duration_s": .number(0.8)])
        let window = NativeEditorSession.soundEffectTrimWindow(effect)
        XCTAssertEqual(window.source, 0.8)
        XCTAssertEqual(window.start, 0)
        XCTAssertEqual(window.end, 0.8)
    }

    func testMovingThePhoneOverlayWritesXFracAndYFrac() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.phoneSubtitledLanes)

        session.setMediaOverlayPosition(id: "phone-overlay-1", x: 0.2, y: 0.85)

        let overlay = try! XCTUnwrap(session.document.mediaOverlays.first)
        XCTAssertEqual(overlay.raw["x_frac"], .number(0.2))
        XCTAssertEqual(overlay.raw["y_frac"], .number(0.85))
    }

    func testRemovingThePhoneSfxLeavesTheOverlayUntouched() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.phoneSubtitledLanes)

        session.removeSoundEffect(id: "phone-sfx-1")

        XCTAssertTrue(session.document.soundEffects.isEmpty)
        XCTAssertEqual(session.document.mediaOverlays.count, 1)
        XCTAssertEqual(session.document.mediaOverlays.first?.id, "phone-overlay-1")
    }

    func testPhoneSubtitledSfxAndOverlayEditsBothReachTheEncodedCommitRequest() async {
        let threadID = UUID()
        let draft = NativeEditorUITestFixtures.phoneSubtitledLanes
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e",
                baseJobID: UUID().uuidString, baseGenerationID: "fixture-generation", snapshot: draft.serverSnapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "editor_capabilities": .object([
                    "sfx": .object(["editable": .bool(true)]),
                    "sound_effects": .object(["editable": .bool(true)]),
                    "overlays": .object(["editable": .bool(true)]),
                    "media_overlays": .object(["editable": .bool(true)]),
                    "text_elements": .object(["editable": .bool(false)]),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: draft)
        await session.load(api: fake, threadID: threadID)

        session.addSoundEffect(phoneSfxEffect())
        session.setMediaOverlayPosition(id: "phone-overlay-1", x: 0.3, y: 0.4)
        await session.save()

        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertEqual(fake.lastRequest?.soundEffects?.count, 2, "the fixture's existing effect plus the newly added one")
        XCTAssertEqual(fake.lastRequest?.mediaOverlays?.count, 1)
    }
}

private func nativeTestObject(_ value: JSONValue?) -> [String: JSONValue]? {
    if case let .object(object) = value { return object }
    return nil
}

private extension EditorDraft {
    func serverSnapshotTextID(at index: Int) -> String {
        documentTextIDs()[index]
    }

    func documentTextIDs() -> [String] {
        guard case let .object(payload) = serverSnapshot["editor_payload"],
              case let .object(sections) = payload["sections"],
              case let .array(records) = sections["text_elements"] else { return text.map { $0.id.uuidString } }
        return records.compactMap { record in
            guard case let .object(value) = record else { return nil }
            return value["id"]?.stringValue
        }
    }
}
#endif
