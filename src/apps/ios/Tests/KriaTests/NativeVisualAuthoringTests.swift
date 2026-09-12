import XCTest
@testable import Kria

#if DEBUG
@MainActor
final class NativeVisualAuthoringTests: XCTestCase {
    private func session() -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.captionVisuals
        draft.serverSnapshot["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true), "captions": .bool(true),
            "caption_editor_style": .bool(true), "visual_editor_style": .bool(true),
            "visual_blocks": .bool(true), "camera_effects": .bool(true), "motion_scenes": .bool(true)
        ])
        return NativeEditorSession(draft: draft)
    }

    func testVisualStyleRejectsInvalidValuesAndMigratesTransitionsOnlyWhenAnimationChanges() throws {
        let session = session()
        let selected = EditorSelection(kind: .visualBlock, id: "paper-media")
        let before = session.document
        session.setVisualEditorStyle(selected, key: "zoom", value: .number(.nan))
        XCTAssertEqual(session.document, before)
        session.setVisualEditorStyle(selected, key: "rotation_deg", value: .number(45))
        XCTAssertEqual(session.visualRaw(selected)?["editor_style"]?.objectValue?["rotation_deg"], .number(45))
        session.undo()
        XCTAssertEqual(session.document, before)
        var animation = try XCTUnwrap(NativeVisualAuthoring.defaultStyle["animation"]?.objectValue)
        animation["entrance"] = .string("pop")
        session.setVisualEditorStyle(selected, key: "animation", value: .object(animation))
        let raw = try XCTUnwrap(session.visualRaw(selected))
        XCTAssertEqual(raw["transition_in"], .string("cut"))
        XCTAssertEqual(raw["transition_out"], .string("cut"))
        XCTAssertEqual(raw["editor_style"]?.objectValue?["animation"], .object(animation))
        session.undo()
        XCTAssertEqual(session.document, before)
    }

    func testVisualFitAndZoomRemainInBothWireRepresentations() throws {
        let session = session()
        let selected = EditorSelection(kind: .visualBlock, id: "paper-media")
        session.setVisualEditorStyle(selected, key: "fit_mode", value: .string("contain"))
        session.setVisualEditorStyle(selected, key: "zoom", value: .number(2))
        let raw = try XCTUnwrap(session.visualRaw(selected))
        XCTAssertEqual(raw["transform"]?.objectValue?["fit_mode"], .string("contain"))
        XCTAssertEqual(raw["transform"]?.objectValue?["zoom"], .number(2))
        XCTAssertEqual(raw["editor_style"]?.objectValue?["fit_mode"], .string("contain"))
        XCTAssertEqual(raw["editor_style"]?.objectValue?["zoom"], .number(2))
        XCTAssertEqual(EditorDocument(snapshot: session.document.encodeSnapshot()).visualBlocks, session.document.visualBlocks)
    }

    func testEditingAnimationPhasePreservesUntouchedLegacyTransitions() throws {
        for kind in [EditorSelectionKind.mediaOverlay, .visualBlock] {
            for phase in ["loop", "speed", "entrance", "exit"] {
                var draft = NativeEditorUITestFixtures.captionVisuals
                var document = EditorDocument(snapshot: draft.serverSnapshot)
                let selected: EditorSelection
                if kind == .mediaOverlay {
                    document.mediaOverlays = [.init(id: "legacy", startS: 0, endS: 2, raw: [
                        "entrance_token": .string("pop_in"), "exit_token": .string("dissolve-out")])]
                    document.capabilities["media_overlays"] = .init(editable: true)
                    document.capabilities["overlays"] = .init(editable: true)
                    selected = .init(kind: kind, id: "legacy")
                } else {
                    document.visualBlocks[0].raw["transition_in"] = .string("fade")
                    document.visualBlocks[0].raw["transition_out"] = .string("fade")
                    selected = .init(kind: kind, id: document.visualBlocks[0].id)
                }
                document.capabilities["visual_editor_style"] = .init(editable: true)
                document.capabilities["visual_blocks"] = .init(editable: true)
                draft.serverSnapshot = document.encodeSnapshot()
                // Capabilities are server-owned; document edits never serialize grants.
                draft.serverSnapshot["editor_capabilities"] = .object([
                    "visual_editor_style": .bool(true), "visual_blocks": .bool(true),
                    "media_overlays": .bool(true), "overlays": .bool(true)
                ])
                let session = NativeEditorSession(draft: draft)
                XCTAssertTrue(session.canEdit("visual_editor_style"))
                XCTAssertTrue(session.canEdit(kind == .mediaOverlay ? "media_overlays" : "visual_blocks"))
                let before = session.document
                var animation = try XCTUnwrap(NativeVisualAuthoring.defaultStyle["animation"]?.objectValue)
                animation[phase] = phase == "speed" ? .number(2) : .string(phase == "loop" ? "pulse" : "fade")
                session.setVisualEditorStyle(selected, key: "animation", value: .object(animation), animationPhase: phase)
                let raw = try XCTUnwrap(session.visualRaw(selected))
                let entranceKey = kind == .mediaOverlay ? "entrance_token" : "transition_in"
                let exitKey = kind == .mediaOverlay ? "exit_token" : "transition_out"
                let cleared = kind == .mediaOverlay ? "none" : "cut"
                XCTAssertEqual(raw[entranceKey], .string(phase == "entrance" ? cleared : kind == .mediaOverlay ? "pop_in" : "fade"))
                XCTAssertEqual(raw[exitKey], .string(phase == "exit" ? cleared : kind == .mediaOverlay ? "dissolve-out" : "fade"))
                XCTAssertEqual(raw["editor_style"]?.objectValue?["animation"], .object(animation))
                session.undo()
                XCTAssertEqual(session.document, before)
            }
        }
    }

    func testCardInsertionTimingSelectionAndSingleUndo() throws {
        let session = session()
        let before = session.document
        let durationBefore = session.duration
        session.seek(to: 1)
        let selection = try XCTUnwrap(session.addTextCard(text: "A new story", bold: true))
        XCTAssertEqual(session.selection, selection)
        let card = try XCTUnwrap(session.document.visualBlocks.first { $0.id == selection.id })
        XCTAssertEqual(card.startS, 1, accuracy: 0.001)
        XCTAssertEqual(card.endS, min(4, durationBefore), accuracy: 0.001)
        let text = try XCTUnwrap(session.document.textElements.first { $0.raw["visual_block_id"] == .string(card.id) })
        XCTAssertEqual(text.startS, card.startS)
        XCTAssertEqual(session.document.clips, before.clips)
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        XCTAssertTrue(session.document.visualBlocks.contains { $0.id == card.id })
        session.setVisualBlockTiming(id: card.id, startS: 2, endS: 5)
        XCTAssertEqual(session.document.textElements.first { $0.id == text.id }?.startS, 2)
        session.removeVisualSelection(selection)
        XCTAssertFalse(session.document.textElements.contains { $0.id == text.id })
        session.undo()
        XCTAssertTrue(session.document.textElements.contains { $0.id == text.id })
    }

    func testVisualTimingCannotInvertOrLeaveTheEdit() throws {
        let session = session()
        let selected = EditorSelection(kind: .visualBlock, id: "paper-media")
        let duration = session.duration
        session.setVisualTiming(selected, outputTime: 99, isStart: true)
        var block = try XCTUnwrap(session.document.visualBlocks.first { $0.id == selected.id })
        XCTAssertGreaterThan(block.endS, block.startS)
        XCTAssertLessThanOrEqual(block.endS, duration)
        session.setVisualTiming(selected, outputTime: -99, isStart: false)
        block = try XCTUnwrap(session.document.visualBlocks.first { $0.id == selected.id })
        XCTAssertEqual(block.endS - block.startS, 0.1, accuracy: 0.001)
        session.setVisualTiming(selected, outputTime: .nan, isStart: true)
        XCTAssertEqual(session.document.visualBlocks.first { $0.id == selected.id }, block)
    }

    func testMotionSpeedPreservesTimingAndNeverUpgradesLegacyRecords() throws {
        let assets = (0..<2).map { index in
            CreationVisual(id: "asset-\(index)", kind: "image", status: "ready", sourceFilename: nil,
                displayURL: nil, previewURL: nil, retryable: nil, gcsPath: "users/test/image-\(index).png")
        }
        var draft = NativeEditorUITestFixtures.captionVisuals
        var document = EditorDocument(snapshot: draft.serverSnapshot)
        document.capabilities["motion_scenes"] = .init(editable: true)
        let scene = try XCTUnwrap(NativeVisualAuthoring.motion(preset: "card_stack", assets: assets, start: 0, end: 3))
        document.motionScenes = [scene]
        draft.serverSnapshot = document.encodeSnapshot()
        let session = NativeEditorSession(draft: draft)
        session.setMotionSpeed(id: scene.id, speed: 2)
        XCTAssertEqual(session.document.motionScenes[0].startS, scene.startS)
        XCTAssertEqual(session.document.motionScenes[0].endS, scene.endS)
        XCTAssertEqual(session.document.motionScenes[0].raw["motion"]?.objectValue?["speed"], .number(2))
        session.undo()
        XCTAssertEqual(session.document.motionScenes[0].raw["motion"]?.objectValue?["speed"], .number(1))
        document.motionScenes[0].raw["preset_version"] = .number(1)
        document.motionScenes[0].raw.removeValue(forKey: "motion")
        draft.serverSnapshot = document.encodeSnapshot()
        let legacy = NativeEditorSession(draft: draft)
        let before = legacy.document
        legacy.setMotionSpeed(id: scene.id, speed: 2)
        XCTAssertEqual(legacy.document, before)
    }

    func testVideoThumbnailUsesTheImportedMedia() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let image = try await NativeVisualThumbnail.videoThumbnail(url: url)
        XCTAssertGreaterThan(image.size.width, 0)
        XCTAssertLessThanOrEqual(max(image.size.width, image.size.height), 256)
    }

    func testEveryAuthoredVisualSurvivesDocumentRoundTrip() throws {
        let assets = (0..<3).map { index in
            CreationVisual(id: "asset-\(index)", kind: "image", status: "ready", sourceFilename: "image.png",
                displayURL: nil, previewURL: nil, retryable: nil, gcsPath: "users/test/image-\(index).png")
        }
        var document = EditorDocument()
        document.visualBlocks = [try XCTUnwrap(NativeVisualAuthoring.media(asset: assets[0], start: 0, end: 3, z: 2))]
        let card = try XCTUnwrap(NativeVisualAuthoring.card(text: "Simple card", bold: false, start: 1, end: 3))
        document.visualBlocks.append(card.0); document.textElements.append(card.1)
        document.motionScenes = [try XCTUnwrap(NativeVisualAuthoring.motion(preset: "card_stack", assets: Array(assets.prefix(2)), start: 0, end: 3)),
            try XCTUnwrap(NativeVisualAuthoring.motion(preset: "film_strip", assets: assets, start: 3, end: 6))]
        document.visualBlocks[0].raw["provenance"] = .object(["future": .string("preserve")])
        let reopened = EditorDocument(snapshot: document.encodeSnapshot())
        XCTAssertEqual(reopened.encodeSnapshot(), document.encodeSnapshot())
        XCTAssertEqual(reopened.visualBlocks[0].raw["provenance"], document.visualBlocks[0].raw["provenance"])
        XCTAssertEqual(reopened.textElements[0].raw["visual_block_id"], .string(card.0.id))
        XCTAssertNil(NativeVisualAuthoring.motion(preset: "film_strip", assets: Array(assets.prefix(2)), start: 0, end: 3))
        var video = assets[0]
        video = CreationVisual(id: video.id, kind: "video", status: "ready", sourceFilename: "video.mp4", displayURL: nil,
            previewURL: nil, retryable: nil, gcsPath: "users/test/video.mp4", durationS: 1)
        XCTAssertNil(NativeVisualAuthoring.media(asset: video, start: 0, end: 3, z: 0))
    }

    func testEmptyCardAndOlderServerDoNotInsert() {
        let session = session()
        let before = session.document
        XCTAssertNil(session.addTextCard(text: "  ", bold: false))
        XCTAssertEqual(session.document, before)
        let old = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        XCTAssertNil(old.addTextCard(text: "Unsupported", bold: false))
    }

    func testCameraPulseHasBoundedTimingAndUndo() throws {
        let session = session()
        let before = session.document
        let durationBefore = session.duration
        session.seek(to: 1)
        session.addCameraPulse()
        let selected = try XCTUnwrap(session.selection)
        XCTAssertEqual(selected.kind, .cameraEffect)
        let pulse = try XCTUnwrap(session.document.cameraEffects.first { $0.id == selected.id })
        XCTAssertEqual(pulse.endS - pulse.startS, min(1.2, durationBefore - 1), accuracy: 0.001)
        XCTAssertEqual(pulse.raw["intensity"], .number(0.04))
        session.undo()
        XCTAssertEqual(session.document, before)
    }

    func testCaptionDisplayAndHighlightStayIndependent() async {
        let draft = NativeEditorUITestFixtures.allLanes
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "g1",
            snapshot: draft.serverSnapshot, canUndo: false, createdAt: .now), authoritativeVariant: [
                "resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"),
                "editor_capabilities": .object(["caption_editor_style": .bool(true), "captions": .bool(true)])
            ])
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let cues = session.document.captionCues
        session.setCaptionAppearance(key: "highlight_spoken_word", value: .bool(true))
        session.setCaptionDisplay("word")
        XCTAssertEqual(session.document.captionMeta["appearance"]?.objectValue?["highlight_spoken_word"], .bool(true))
        session.setCaptionDisplay("sentence")
        XCTAssertEqual(session.document.captionMeta["appearance"]?.objectValue?["highlight_spoken_word"], .bool(true))
        XCTAssertEqual(session.document.captionCues, cues)
    }
}
#endif
