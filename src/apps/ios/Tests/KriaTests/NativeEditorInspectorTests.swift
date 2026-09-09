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
        XCTAssertEqual(session.document.clips[0].transitionDurationS, 1)
        XCTAssertEqual(session.document.captionMeta["size_px"], .number(160))
        XCTAssertEqual(session.document.captionMeta["y_frac"], .number(0.90))
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
        XCTAssertEqual(session.document.orientation, "9:16")
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
