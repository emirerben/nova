import XCTest
import UIKit
import AVFoundation
import KriaMediaEngine
@testable import Kria

final class NativeSplitScreenTests: XCTestCase {
    func testSectionAdvancesClockOnceIncludingEmptyTail() {
        let section = EditorSplitScreenSection(id: "s", durationS: 5, panelCount: 2)
        var a = EditorTimelineSlot(id: "a", clipIndex: 0, inS: 2, durationS: 2)
        var b = EditorTimelineSlot(id: "b", clipIndex: 1, inS: 7, durationS: 3)
        a.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        b.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 1)
        let tail = EditorTimelineSlot(id: "tail", clipIndex: 0, inS: 0, durationS: 2)
        let p = NativeEditorInteraction.timelineProjection(slots: [a, b, tail], carousel: nil, splitSections: [section])
        XCTAssertEqual(p.baseClipWindows.map(\.start), [0, 1, 5])
        XCTAssertEqual(p.totalDuration, 7)
    }

    func testTimelineSnapshotIncludesSectionOnlyChangesAndRetainsSourceFields() {
        var a = EditorTimelineSlot(id: "a", clipIndex: 0, inS: 2, durationS: 2, raw: ["playback_rate": .number(2), "source_crop": .object(["x": .number(0.2)])])
        a.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var document = EditorDocument(clips: [a])
        document.splitScreenSections = [EditorSplitScreenSection(id: "s", durationS: 2, panelCount: 2)]
        var reopened = EditorDocument(snapshot: document.encodeSnapshot())
        XCTAssertEqual(reopened.clips.first?.raw["playback_rate"], .number(2))
        XCTAssertEqual(reopened.splitScreenSections, document.splitScreenSections)
        reopened.splitScreenSections[0].durationS = 4
        XCTAssertNotEqual(document.snapshot(for: .timeline), reopened.snapshot(for: .timeline))
        XCTAssertEqual(EditorDocument(snapshot: reopened.encodeSnapshot()).splitScreenSections[0].durationS, 4)
    }
}

extension NativeSplitScreenTests {
    func testMalformedLayoutAndPanelOverlapAreRejected() {
        for fields: [String: JSONValue] in [
            ["panel_weights": .null], ["gutter": .string("0")], ["background_color": .number(1)],
            ["panel_weights": .array([.number(Double.greatestFiniteMagnitude), .number(Double.greatestFiniteMagnitude)])]
        ] {
            let base: [String: JSONValue] = ["id": .string("s"), "duration_s": .number(4), "panel_count": .number(2)]
            XCTAssertNil(EditorSplitScreenSection(json: .object(base.merging(fields) { _, new in new })))
        }
        var first = EditorTimelineSlot(id: "a", clipIndex: 0, inS: 0, durationS: 2)
        var second = EditorTimelineSlot(id: "b", clipIndex: 0, inS: 2, durationS: 2)
        first.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        second.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 1)
        var document = EditorDocument(clips: [first, second])
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        XCTAssertFalse(document.splitScreenIsValid)
        document.clips[1].raw["split_offset_s"] = .number(1.8)
        document.clips[0].transitionAfter = "crossfade"
        document.clips[0].transitionDurationS = 0.2
        XCTAssertTrue(document.splitScreenIsValid)
    }

    func testSharedProjectionFixture() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { root.deleteLastPathComponent() }
        let data = try Data(contentsOf: root.appendingPathComponent("src/apps/api/tests/fixtures/split_screen_timeline.json"))
        let object = try JSONDecoder().decode([String: JSONValue].self, from: data)
        for test in object["cases"]?.arrayValue ?? [] {
            let row = try XCTUnwrap(test.objectValue)
            let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
                "timeline_slots": row["slots"]!, "split_screen_sections": row["split_screen_sections"]!
            ])])]
            let document = EditorDocument(snapshot: snapshot)
            XCTAssertTrue(document.splitScreenIsValid, row["name"]?.stringValue ?? "")
            let projection = NativeEditorInteraction.timelineProjection(slots: document.clips, carousel: nil, splitSections: document.splitScreenSections)
            XCTAssertEqual(projection.totalDuration, try XCTUnwrap(row["expected_duration_s"]?.numberValue), accuracy: 0.000001)
            for (window, expected) in zip(projection.baseClipWindows, row["expected_windows"]?.arrayValue ?? []) {
                XCTAssertEqual(window.start, try XCTUnwrap(expected.objectValue?["start_s"]?.numberValue), accuracy: 0.000001)
                XCTAssertEqual(window.end, try XCTUnwrap(expected.objectValue?["end_s"]?.numberValue), accuracy: 0.000001)
            }
        }
    }

    func testOwnerAwareTextAndGlobalCanvasDoNotFollowAnotherPanel() {
        var top = EditorTimelineSlot(id: "top", clipIndex: 0, inS: 0, durationS: 4)
        var bottom = EditorTimelineSlot(id: "bottom", clipIndex: 1, inS: 10, durationS: 4)
        top.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        bottom.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        let layout = [EditorSplitScreenSection(id: "s", durationS: 4, panelCount: 2)]
        let owned = EditorTextElement(id: "owned", text: "Top", startS: 1, endS: 2, raw: ["segment_id": .string("top")])
        let canvas = EditorTextElement(id: "canvas", text: "Canvas", startS: 1, endS: 2)
        var changed = [top, bottom]
        changed[1].inS = 11; changed[1].durationS = 3
        let rebased = GuidedLabelRebase.rebase([owned, canvas], oldSlots: [top, bottom], newSlots: changed,
            includeFreeText: true, oldSplitSections: layout, newSplitSections: layout)
        XCTAssertEqual(rebased.map(\.startS), [1, 1])
        XCTAssertEqual(rebased.map(\.endS), [2, 2])
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testCreateCutTrimLayoutUndoAndReopenKeepIndependentMembers() throws {
        let id = UUID()
        let clip = EditorClip(id: id, assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
                              trimIn: 2, trimOut: 6, sourceDuration: 20, slotID: id.uuidString)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [],
            captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device"), "editor_capabilities": .object(["timeline": .bool(true), "split_screen": .bool(true)])]))
        session.selectClip(id)
        session.createSplitScreen(panelCount: 2, scope: .selectedClip)
        XCTAssertEqual(session.document.clips.count, 1)
        let sectionID = try XCTUnwrap(session.selectedSplitScreenSectionID)
        session.addSplitPanelClip(sectionID: sectionID, panel: 1, sourceClipIndex: 0)
        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        session.selectClip(id)
        session.currentTime = 2
        session.splitSelectedClip()
        XCTAssertEqual(session.document.clips.count, 3)
        XCTAssertEqual(session.document.clips[1].inS, 4)
        XCTAssertEqual(session.document.clips[1].splitOffsetS, 2)
        let other = try XCTUnwrap(session.document.clips.first { $0.splitPanelIndex == 1 })
        session.trimSelected(edge: .leading, to: 0.5)
        XCTAssertEqual(session.document.clips[0].splitOffsetS, 0.5)
        XCTAssertEqual(session.document.clips[0].inS, 2.5)
        XCTAssertEqual(session.document.clips.first { $0.id == other.id }, other)
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        session.setSplitScreenDuration(5)
        XCTAssertEqual(session.timelineProjection.totalDuration, 5)
        session.undo()
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        session.redo()
        XCTAssertEqual(session.timelineProjection.totalDuration, 5)
        let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(reopened.splitScreenSections.first?.id, sectionID)
        XCTAssertEqual(reopened.snapshot(for: .timeline), session.document.snapshot(for: .timeline))
        XCTAssertEqual(reopened.clips.map(\.inS), session.document.clips.map(\.inS))
        XCTAssertEqual(reopened.clips.map { $0.raw["source_duration_s"] }, session.document.clips.map { $0.raw["source_duration_s"] })
        XCTAssertTrue(session.isDirty(.timeline))
    }

    func testCommitCarriesAtomicSectionsAndSlots() throws {
        let section = EditorSplitScreenSection(id: "s", durationS: 4, panelCount: 2)
        let request = EditorCommitRequest(timelineSlots: [], splitScreenSections: [section.json], baseGeneration: "g")
        let data = try JSONEncoder().encode(request)
        let decoded = try JSONDecoder().decode(EditorCommitRequest.self, from: data)
        XCTAssertEqual(decoded.splitScreenSections, [section.json])
        XCTAssertEqual(decoded.timelineSlots, [])
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testCompilerKeepsIndependentClocksCropAudioAndSectionTail() throws {
        var top = EditorTimelineSlot(id: "top", clipIndex: 0, inS: 1, durationS: 4,
            raw: ["playback_rate": .number(0.5), "source_end_s": .number(3), "muted": .bool(false),
                  "source_crop": .object(["x": .number(0.1), "y": .number(0.2), "width": .number(0.6), "height": .number(0.5)])])
        var bottom = EditorTimelineSlot(id: "bottom", clipIndex: 0, inS: 7, durationS: 2,
            raw: ["playback_rate": .number(2), "source_end_s": .number(11), "muted": .bool(true)])
        top.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        bottom.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 1)
        var document = EditorDocument(clips: [top, bottom])
        document.splitScreenSections = [.init(id: "s", durationS: 5, panelCount: 2)]
        let clips = [
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 1, trimOut: 3, sourceDuration: 20, slotID: "top"),
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 1, end: 3, trimIn: 7, trimOut: 11, sourceDuration: 20, muted: true, slotID: "bottom")
        ]
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "source", relativePath: "source.mp4", fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 20),
            url: URL(fileURLWithPath: "/fixture/source.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: source]).recipe
        let tracks = recipe.tracks.filter { $0.kind == .video }
        XCTAssertEqual(tracks.count, 2)
        XCTAssertEqual(tracks.flatMap(\.clips).map(\.sourceStart), [1, 7])
        XCTAssertEqual(tracks.flatMap(\.clips).map(\.rate), [0.5, 2])
        XCTAssertEqual(tracks.flatMap(\.clips).map(\.volume), [1, 0])
        XCTAssertEqual(tracks[0].clips[0].sourceCrop, .init(x: 0.1, y: 0.2, width: 0.6, height: 0.5))
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 5)
        XCTAssertEqual(recipe.splitScreenSections.first?.end, 5)
    }

    func testSpeechSectionCreationDoesNotDuplicateCaptionsIntoAnotherPanel() {
        let original = EditorTimelineSlot(id: "native-phone-talking-source-a", clipIndex: 0, inS: 0, durationS: 4)
        let cue = EditorCaptionCue(id: "cue", startS: 1, endS: 2, text: "Hello", raw: ["words": .array([
            .object(["text": .string("Hello"), "start_s": .number(1), "end_s": .number(2)])
        ])])
        let old = EditorDocument(clips: [original], captionCues: [cue])
        var next = old
        next.clips[0].setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var bottom = original; bottom.id = "bottom"; bottom.parentSegmentID = original.id
        bottom.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        next.clips.append(bottom)
        next.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        XCTAssertTrue(NativeSpeechTimelineProjection.rebase(&next, from: old))
        XCTAssertEqual(next.captionCues.count, 1)
        XCTAssertEqual(next.captionCues.first?.startS, 1)
        XCTAssertEqual(next.captionCues.first?.endS, 2)
        var trimmed = next
        trimmed.clips[1].inS = 1; trimmed.clips[1].durationS = 3
        XCTAssertTrue(NativeSpeechTimelineProjection.rebase(&trimmed, from: next))
        XCTAssertEqual(trimmed.captionCues, next.captionCues)
    }
}

extension NativeSplitScreenTests {
    func testFrozenPanelSourceClockMatchesMovementHoldAndRateCases() {
        let slot = EditorTimelineSlot(id: "frozen", clipIndex: 0, inS: 2, durationS: 4, raw: [
            "source_end_s": .number(4), "playback_rate": .number(1), "freeze_hold_s": .number(2), "freeze_frame_s": .number(4)
        ])
        let clock = NativeClipSourceClock(slot: slot)
        let crossing = clock.sliced(from: 1, to: 3)
        XCTAssertEqual(crossing.start, 3); XCTAssertEqual(crossing.end, 4)
        XCTAssertEqual(crossing.hold, 1); XCTAssertEqual(crossing.duration, 2)
        let held = clock.sliced(from: 3, to: 4)
        XCTAssertEqual(held.start, 119.0 / 30, accuracy: 0.000001)
        XCTAssertEqual(held.end, 4); XCTAssertEqual(held.frame, 4)
        XCTAssertEqual(held.hold, 29.0 / 30, accuracy: 0.000001)
        XCTAssertEqual(held.duration, 1, accuracy: 0.000001)
        var faster = clock; faster.rate = 2
        XCTAssertEqual(faster.duration, 3); XCTAssertEqual(faster.hold, 2)
        XCTAssertEqual(faster.applying(to: slot).raw["freeze_frame_s"], .number(4))
    }
}

extension NativeSplitScreenTests {
    func testCanvasEffectsFollowSectionBoundaryButNotPanelTrim() {
        var top = EditorTimelineSlot(id: "top", clipIndex: 0, inS: 0, durationS: 4)
        var bottom = EditorTimelineSlot(id: "bottom", clipIndex: 1, inS: 0, durationS: 4)
        top.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        bottom.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        var old = EditorDocument(clips: [top, bottom, .init(id: "tail", clipIndex: 0, inS: 4, durationS: 2)],
            cameraEffects: [.init(id: "zoom", startS: 4.5, endS: 5.5)])
        old.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        var local = old; local.clips[0].durationS = 3
        NativeSplitTimelineRebase.rebase(&local, from: old)
        XCTAssertEqual(local.cameraEffects, old.cameraEffects)
        var longer = old; longer.splitScreenSections[0].durationS = 6
        NativeSplitTimelineRebase.rebase(&longer, from: old)
        XCTAssertEqual(longer.cameraEffects.first?.startS, 6.5)
        XCTAssertEqual(longer.cameraEffects.first?.endS, 7.5)
        var reordered = old
        reordered.clips = NativeEditorInteraction.reorderedSlots(old.clips, from: 0, to: 2)
        NativeSplitTimelineRebase.rebase(&reordered, from: old)
        XCTAssertEqual(reordered.cameraEffects.first?.startS, 0.5)
        XCTAssertEqual(reordered.cameraEffects.first?.endS, 1.5)
    }

    @MainActor
    func testCreationRequiresExplicitDeviceCapability() {
        let id = UUID()
        let clip = EditorClip(id: id, assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 20)
        for destination in ["cloud", "device"] {
            let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [],
                captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0,
                serverSnapshot: ["render_destination": .string(destination), "editor_capabilities": .object(["timeline": .bool(true)])]))
            session.selectClip(id)
            XCTAssertFalse(session.canCreateSplitScreen)
            XCTAssertFalse(session.canCreateWholeVideoSplit)
            session.createSplitScreen(panelCount: 2, scope: .selectedClip)
            session.createSplitScreen(panelCount: 2, scope: .wholeVideo)
            XCTAssertTrue(session.document.splitScreenSections.isEmpty)
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testShorteningBeforeAllMembersRefusesWithoutInventingContent() {
        let id = UUID().uuidString
        var clip = EditorTimelineSlot(id: id, clipIndex: 0, inS: 2, durationS: 2)
        clip.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 1)
        var document = EditorDocument(clips: [clip], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        session.select(.init(kind: .clip, id: id), seekToStart: false)
        session.setSplitScreenDuration(0.5)
        XCTAssertEqual(session.document, document)
        XCTAssertNotNil(session.splitScreenActionError)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testTurningOnZeroGainRestoresAudioAndPreservesNonzeroGain() {
        let id = UUID().uuidString
        let slot = EditorTimelineSlot(id: id, clipIndex: 0, inS: 0, durationS: 2, raw: ["muted": .bool(true), "source_audio_volume": .number(0)])
        let session = NativeEditorSession()
        session.replaceDocumentForTesting(EditorDocument(clips: [slot], mix: ["original_level": .number(1)], capabilities: ["timeline": .init(editable: true), "original_audio": .init(editable: true), "clips.audio": .init(editable: true)]))
        XCTAssertFalse(session.isClipAudioOn(clipID: id))
        session.toggleClipAudio(clipID: id)
        XCTAssertTrue(session.isClipAudioOn(clipID: id))
        XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], .number(1))
        session.select(.init(kind: .clip, id: id), seekToStart: false)
        session.setSelectedClipSourceVolume(0.4)
        session.toggleClipAudio(clipID: id)
        session.toggleClipAudio(clipID: id)
        XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], .number(0.4))
    }

    func testTinyAndNonadjacentPanelOverlapsAreRejected() {
        func member(_ id: String, offset: Double, duration: Double) -> EditorTimelineSlot {
            var value = EditorTimelineSlot(id: id, clipIndex: 0, inS: 0, durationS: duration, transitionAfter: "crossfade", transitionDurationS: 0.3)
            value.setSplitMembership(sectionID: "s", panelIndex: 0, offset: offset)
            return value
        }
        var document = EditorDocument(clips: [member("a", offset: 0, duration: 0.1), member("b", offset: 0.08, duration: 0.1)])
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        XCTAssertFalse(document.splitScreenIsValid)
        document.clips = [member("a", offset: 0, duration: 2), member("b", offset: 1.8, duration: 1), member("c", offset: 1.9, duration: 1)]
        XCTAssertFalse(document.splitScreenIsValid)
    }

    func testFrozenSourceLabelsUsePiecewiseMovingAndHeldClock() {
        let old = EditorTimelineSlot(id: "f", clipIndex: 0, inS: 2, durationS: 4, raw: ["source_end_s": .number(4), "freeze_frame_s": .number(4), "freeze_hold_s": .number(2), "playback_rate": .number(1)])
        var clock = NativeClipSourceClock(slot: old); clock.rate = 2
        let next = clock.applying(to: old)
        let moving = EditorTextElement(id: "moving", text: "Moving", startS: 1, endS: 1.5, raw: ["segment_id": .string("f")])
        let held = EditorTextElement(id: "held", text: "Held", startS: 3, endS: 3.5, raw: ["segment_id": .string("f")])
        let rebased = GuidedLabelRebase.rebase([moving, held], oldSlots: [old], newSlots: [next], includeFreeText: true)
        XCTAssertEqual(rebased[0].startS, 0.5, accuracy: 0.000001)
        XCTAssertEqual(rebased[0].endS, 0.75, accuracy: 0.000001)
        XCTAssertEqual(rebased[1].startS, 2, accuracy: 0.000001)
        XCTAssertEqual(rebased[1].endS, 2.5, accuracy: 0.000001)
    }
}

extension NativeSplitScreenTests {
    func testTalkingSplitKeepsCanvasGapLayersAndMovesGlobalSpeakerWindows() {
        var top = EditorTimelineSlot(id: "native-phone-talking-source-0", clipIndex: 0, inS: 0, durationS: 2)
        var bottom = EditorTimelineSlot(id: "bottom", clipIndex: 1, inS: 0, durationS: 4)
        top.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        bottom.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        var old = EditorDocument(clips: [top, bottom, .init(id: "tail", clipIndex: 0, inS: 4, durationS: 2)],
            cameraEffects: [.init(id: "gap", startS: 3, endS: 3.5)])
        old.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        old.speakerBackground = .init(color: "#000000", fullScreenWindows: [.init(startS: 4, endS: 5)])
        old.clipAudio = [.init(id: "audio", sourceClipIndex: 0, sourceStartS: 4, sourceEndS: 6, startS: 4, gain: 1)]
        var weights = old; weights.splitScreenSections[0].panelWeights = [2, 1]
        NativeSplitTimelineRebase.rebase(&weights, from: old)
        NativeSpeechTimelineProjection.rebase(&weights, from: old)
        XCTAssertEqual(weights.cameraEffects.count, 1)
        XCTAssertEqual(weights.cameraEffects.first?.id, "gap")
        XCTAssertEqual(weights.cameraEffects.first?.startS, 3)
        XCTAssertEqual(weights.cameraEffects.first?.endS, 3.5)
        var longer = old; longer.splitScreenSections[0].durationS = 6
        NativeSplitTimelineRebase.rebase(&longer, from: old)
        NativeSpeechTimelineProjection.rebase(&longer, from: old)
        XCTAssertEqual(longer.cameraEffects.count, 1)
        XCTAssertEqual(longer.cameraEffects.first?.id, "gap")
        XCTAssertEqual(longer.cameraEffects.first?.startS, 3)
        XCTAssertEqual(longer.cameraEffects.first?.endS, 3.5)
        XCTAssertEqual(longer.speakerBackground?.fullScreenWindows.first?.startS, 6)
        XCTAssertEqual(longer.speakerBackground?.fullScreenWindows.first?.endS, 7)
        XCTAssertEqual(longer.clipAudio.first?.startS, 6)
    }
}

extension NativeSplitScreenTests {
    func testImportedPanelDestinationSurvivesPendingPlacementRoundTrip() throws {
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "variant", clientImportID: UUID(), baseGeneration: "base", guidedRevisionNumber: 1, sourceKind: .visual)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: nil, replacingSlotID: "panel-member")
        let decoded = try JSONDecoder().decode(PendingEditorSourcePlacement.self, from: JSONEncoder().encode(placement))
        XCTAssertEqual(decoded.replacingSlotID, "panel-member")
        let ordinary = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: nil)
        XCTAssertNil(ordinary.replacingSlotID)
    }
}

extension NativeSplitScreenTests {
    func testShorteningSectionDropsCollapsedAndTooShortSpeakerWindows() {
        var slot = EditorTimelineSlot(id: "speaker", clipIndex: 0, inS: 0, durationS: 4)
        slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var old = EditorDocument(clips: [slot])
        old.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        old.speakerBackground = .init(color: "#000000", windows: [
            .init(localID: "keep", mediaID: "photo", kind: .image, startS: 0, endS: 1),
            .init(localID: "short", mediaID: "photo", kind: .image, startS: 1.75, endS: 2.5),
            .init(localID: "collapsed", mediaID: "photo", kind: .image, startS: 3, endS: 4)
        ], fullScreenWindows: [.init(startS: 0, endS: 1), .init(startS: 1.75, endS: 2.5), .init(startS: 3, endS: 4)])
        var next = old; next.clips[0].durationS = 2; next.splitScreenSections[0].durationS = 2
        NativeSplitTimelineRebase.rebase(&next, from: old)
        XCTAssertEqual(next.speakerBackground?.windows.map(\.localID), ["keep"])
        XCTAssertEqual(next.speakerBackground?.fullScreenWindows, [.init(startS: 0, endS: 1)])
        XCTAssertNotNil(next.speakerBackground.flatMap { EditorSpeakerBackground(json: $0.json) })
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testLegacySpeakerDuplicateThenSplitPreservesSubjectWithoutCaptionParent() throws {
        let cases: [(marker: JSONValue?, parentOnly: Bool, expected: Bool)] = [
            (nil, false, true), (.null, false, true), (.bool(true), false, true),
            (.bool(false), false, false), (nil, true, true), (.null, true, true), (.bool(false), true, false)
        ]
        for testCase in cases {
            let id = testCase.parentOnly ? "legacy-child" : "native-phone-talking-source-original"
            var raw: [String: JSONValue] = [:]
            raw["speaker_subject"] = testCase.marker
            let slot = EditorTimelineSlot(id: id, clipIndex: 0, inS: 0, durationS: 4,
                parentSegmentID: testCase.parentOnly ? "native-phone-talking-source-parent" : nil, raw: raw)
            let document = EditorDocument(clips: [slot], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
            let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: ["render_destination": .string("device")]))
            session.replaceDocumentForTesting(document)
            session.select(.init(kind: .clip, id: id), seekToStart: false)
            session.duplicateSelectedClip()
            XCTAssertEqual(session.document.clips.count, 2)
            let copy = session.document.clips[1]
            XCTAssertNil(copy.parentSegmentID)
            XCTAssertEqual(copy.raw["speaker_subject"], .bool(testCase.expected))
            session.select(.init(kind: .clip, id: try XCTUnwrap(copy.id)), seekToStart: false)
            session.createSplitScreen(panelCount: 2, scope: .selectedClip)
            let members = session.document.clips.filter { $0.splitSectionID != nil }
            XCTAssertEqual(members.count, 1)
            XCTAssertTrue(members.allSatisfy { $0.raw["speaker_subject"] == .bool(testCase.expected) && $0.parentSegmentID == nil })
            session.undo()
            session.createSplitScreen(panelCount: 2, scope: .wholeVideo)
            XCTAssertEqual(session.document.clips.filter { $0.splitSectionID != nil }.count, 2)
            XCTAssertTrue(session.document.clips.allSatisfy { $0.raw["speaker_subject"] == .bool(testCase.expected) })
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testAPISnapshotReopensSplitThroughCompatibilityDraftWithoutOrphaning() throws {
        var top = EditorTimelineSlot(id: "saved-top", clipIndex: 0, inS: 2, durationS: 2)
        var bottom = EditorTimelineSlot(id: "saved-bottom", clipIndex: 1, inS: 7, durationS: 3)
        top.setSplitMembership(sectionID: "saved-section", panelIndex: 0, offset: 1)
        bottom.setSplitMembership(sectionID: "saved-section", panelIndex: 1, offset: 0)
        var saved = EditorDocument(clips: [top, bottom])
        saved.splitScreenSections = [.init(id: "saved-section", durationS: 4, panelCount: 2)]
        var snapshot = saved.encodeSnapshot()
        var payload = snapshot["editor_payload"]?.objectValue ?? [:]
        var sections = payload["sections"]?.objectValue ?? [:]
        let staleID = UUID().uuidString
        sections["ios_editor"] = .object(["clips": .array([.object([
            "id": .string(staleID), "asset_id": .string(staleID), "start": .number(0), "end": .number(99),
            "trim_in": .number(0), "trim_out": .number(99)
        ])])])
        payload["sections"] = .object(sections); snapshot["editor_payload"] = .object(payload)
        let apiSnapshot = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: nil,
            snapshot: snapshot, canUndo: false, createdAt: .now)
        let compatibility = apiSnapshot.editorDraft(projectID: UUID())
        XCTAssertEqual(compatibility.clips.map(\.slotID), ["saved-top", "saved-bottom"])
        let session = NativeEditorSession(draft: compatibility)
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.document.splitScreenSections, saved.splitScreenSections)
        XCTAssertEqual(session.document.clips.map(\.inS), [2, 7])
        XCTAssertEqual(session.document.clips.map(\.splitOffsetS), [1, 0])
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        XCTAssertEqual(session.timelineProjection.baseClipWindows.map(\.start), [1, 0])
        XCTAssertFalse(session.hasUnsavedChanges)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testVariantTimelineFallbackHydratesSplitAtomicallyAcrossPayloadVersions() {
        for stateVersion in [1, 2] {
            let slot: JSONValue = .object(["slot_id": .string("variant-panel"), "clip_index": .number(1),
                "in_s": .number(3), "duration_s": .number(2), "split_section_id": .string("variant-split"),
                "split_panel_index": .number(1), "split_offset_s": .number(1)])
            let section: JSONValue = .object(["id": .string("variant-split"), "panel_count": .number(2), "duration_s": .number(4)])
            let snapshot = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: "generation",
                snapshot: ["editor_payload": .object(["editor_state_version": .number(Double(stateVersion)), "sections": .object([:])])],
                canUndo: false, createdAt: .now)
            let variant: [String: JSONValue] = ["render_generation_id": .string("generation"), "render_destination": .string("device"),
                "user_timeline": .object(["slots": .array([slot]), "split_screen_sections": .array([section])])]
            let session = NativeEditorSession(draft: snapshot.editorDraft(projectID: UUID(), authoritativeVariant: variant))
            XCTAssertTrue(session.document.splitScreenIsValid, "state version \(stateVersion)")
            XCTAssertEqual(session.document.splitScreenSections.map(\.id), ["variant-split"])
            XCTAssertEqual(session.document.clips.first?.id, "variant-panel")
            XCTAssertEqual(session.document.clips.first?.inS, 3)
            XCTAssertEqual(session.timelineProjection.baseClipWindows.first?.start, 1)
            XCTAssertEqual(session.timelineProjection.totalDuration, 4)
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }

    @MainActor
    func testAuthoritativeOrdinaryTimelineClearsStaleSplitConfiguration() {
        for stateVersion in [1, 2] {
            var sections: [String: JSONValue] = ["split_screen_sections": .array([.object([
                "id": .string("stale"), "panel_count": .number(2), "duration_s": .number(4)
            ])])]
            if stateVersion == 1 {
                sections["timeline_slots"] = .array([.object(["slot_id": .string("old"), "clip_index": .number(0),
                    "in_s": .number(0), "duration_s": .number(4), "split_section_id": .string("stale"),
                    "split_panel_index": .number(0), "split_offset_s": .number(0)])])
            }
            let snapshot = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: "generation",
                snapshot: ["editor_payload": .object(["editor_state_version": .number(Double(stateVersion)), "sections": .object(sections)])],
                canUndo: false, createdAt: .now)
            let variant: [String: JSONValue] = ["render_generation_id": .string("generation"), "user_timeline": .object([
                "slots": .array([.object(["slot_id": .string("ordinary"), "clip_index": .number(0), "in_s": .number(1), "duration_s": .number(2)])])
            ])]
            let session = NativeEditorSession(draft: snapshot.editorDraft(projectID: UUID(), authoritativeVariant: variant))
            XCTAssertTrue(session.document.splitScreenIsValid)
            XCTAssertTrue(session.document.splitScreenSections.isEmpty)
            XCTAssertEqual(session.document.clips.map(\.id), ["ordinary"])
            XCTAssertEqual(session.timelineProjection.totalDuration, 2)
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testChatStagingTreatsSplitTimelineAsAtomicAndPreservesNewUUIDMetadata() throws {
        let firstID = UUID().uuidString, secondID = UUID().uuidString, thirdID = UUID().uuidString
        func row(_ id: String, panel: Int?) -> JSONValue {
            var raw: [String: JSONValue] = ["slot_id": .string(id), "clip_index": .number(0), "in_s": .number(1), "duration_s": .number(2)]
            if let panel { raw["split_section_id"] = .string("s"); raw["split_panel_index"] = .number(Double(panel)); raw["split_offset_s"] = .number(0) }
            return .object(raw)
        }
        func section(_ count: Int, duration: Double = 2) -> JSONValue {
            .object(["id": .string("s"), "panel_count": .number(Double(count)), "duration_s": .number(duration)])
        }
        var original = row(firstID, panel: nil).objectValue!
        original["source_duration_s"] = .number(20); original["media_id"] = .string("approved-source")
        var base: [String: JSONValue] = ["editor_payload": .object(["sections": .object(["timeline_slots": .array([.object(original)])])])]
        let cases: [(slots: [JSONValue]?, sections: [JSONValue], ids: [String], duration: Double)] = [
            ([row(firstID, panel: 0), row(secondID, panel: 1), row(thirdID, panel: 2)], [section(3)], [firstID, secondID, thirdID], 2),
            (nil, [section(3, duration: 4)], [firstID, secondID, thirdID], 4),
            ([row(firstID, panel: 0), row(secondID, panel: 1)], [section(2)], [firstID, secondID], 2),
            ([row(secondID, panel: nil)], [], [secondID], 2)
        ]
        for (index, testCase) in cases.enumerated() {
            var payload: [String: JSONValue] = ["base_generation": .string("g"), "split_screen_sections": .array(testCase.sections)]
            if let slots = testCase.slots { payload["timeline_slots"] = .array(slots) }
            let chat = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "variant", draftRevision: index + 1,
                snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: "g",
                snapshot: ["kind": .string("editor"), "editor_payload": .object(payload)], canUndo: false, createdAt: .now)
            let staged = try XCTUnwrap(chat.stagedChatEdit(over: base, variant: ["render_generation_id": .string("g")], appliedRevision: nil))
            XCTAssertEqual(staged.sections, [.timeline])
            let document = EditorDocument(snapshot: staged.snapshot)
            XCTAssertTrue(document.splitScreenIsValid)
            XCTAssertEqual(document.clips.compactMap(\.id), testCase.ids)
            XCTAssertEqual(document.clips.last?.raw["source_duration_s"], .number(20))
            XCTAssertEqual(document.clips.last?.raw["media_id"], .string("approved-source"))
            let restored = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "variant", draftRevision: index + 1,
                snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: "g", snapshot: staged.snapshot, canUndo: false, createdAt: .now)
            let session = NativeEditorSession(draft: restored.editorDraft(projectID: UUID()))
            XCTAssertTrue(session.document.splitScreenIsValid)
            XCTAssertEqual(session.timelineProjection.totalDuration, testCase.duration)
            base = staged.snapshot
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testAsyncUnusedSourceReplacementResetsSourceMetadataAndPreservesPanelEdits() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let project = ProjectDirectory(root: root)
        let cacheDirectory = root.appendingPathComponent("cache")
        let cache = NativePreviewAssetCache(project: project, directory: cacheDirectory)
        try cache.project.createIfNeeded()
        let photo = root.appendingPathComponent("photo.png")
        try UIGraphicsImageRenderer(size: CGSize(width: 32, height: 48)).image { context in
            UIColor.blue.setFill(); context.fill(CGRect(x: 0, y: 0, width: 32, height: 48))
        }.pngData()!.write(to: photo)
        let video = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let videoDuration = try await AVURLAsset(url: video).load(.duration).seconds
        var poolClips: [NativeEditorSourcePool.Clip] = []
        for (index, url, duration) in [(1, photo, Optional<Double>.none), (2, video, Optional(videoDuration))] {
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
            let assetID = "native-preview-" + fingerprint.hex
            let file = assetID + "." + url.pathExtension
            try FileManager.default.copyItem(at: url, to: cache.project.originals.appendingPathComponent(file))
            try cache.store(MediaAsset(id: assetID, relativePath: "originals/" + file, fingerprint: fingerprint, duration: duration), for: "generation:\(index):source-\(index)")
            poolClips.append(.init(clipIndex: index, nativeSource: .init(mediaID: "source-\(index)",
                sourceURL: URL(string: "https://fixture.invalid/" + file), original: nil, localRequired: false)))
        }
        let edits: [String: JSONValue] = [
            "source_crop": .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.8), "height": .number(0.8)]),
            "media_transform": .object(["scale": .number(1.2), "position_x": .number(0.1)]),
            "tilt": .object(["rotation_deg": .number(5)]), "source_audio_volume": .number(0.4)
        ]
        var original = EditorTimelineSlot(id: "panel", clipIndex: 0, inS: 3, durationS: 2, raw: edits.merging([
            "media_kind": .string("video"), "kind": .string("video"), "source_duration_s": .number(20),
            "source_duration": .number(20), "playback_rate": .number(2)
        ]) { _, new in new })
        original.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 1)
        var document = EditorDocument(clips: [original], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.revision.baseGeneration = "generation"
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        let session = NativeEditorSession()
        session.replaceDocumentForTesting(document)
        session.configureReplacementSourcesForTesting(pool: .init(clips: poolClips, baseGeneration: "generation"),
            resolver: NativeEditorSourceResolver(project: project, cacheDirectory: cacheDirectory))
        session.select(.init(kind: .clip, id: "panel"), seekToStart: false)
        for target in [1, 2] {
            if target == 2 {
                var withCard = session.document
                withCard.clips[0].raw["media_kind"] = .string("image")
                withCard.clips[0].raw["kind"] = .string("image")
                withCard.clips[0].raw["still_layout"] = .string("supporting_card")
                withCard.clips[0].raw["layout"] = .string("supporting_card")
                session.replaceDocumentForTesting(withCard)
            }
            session.replaceSelectedClipSource(clipIndex: target)
            for _ in 0..<200 where session.document.clips[0].clipIndex != target {
                try await Task.sleep(for: .milliseconds(10))
            }
            let slot = session.document.clips[0]
            XCTAssertEqual(slot.clipIndex, target)
            XCTAssertEqual(slot.raw["media_kind"], .string(target == 1 ? "image" : "video"))
            XCTAssertNotEqual(slot.raw["kind"], .string(target == 1 ? "video" : "image"))
            XCTAssertEqual(slot.inS, 0)
            XCTAssertEqual(slot.splitSectionID, "s"); XCTAssertEqual(slot.splitPanelIndex, 1); XCTAssertEqual(slot.splitOffsetS, 1)
            XCTAssertEqual(session.document.splitScreenSections, document.splitScreenSections)
            for (key, value) in edits { XCTAssertEqual(slot.raw[key], value, key) }
            if target == 1 {
                XCTAssertNil(slot.raw["source_duration_s"]); XCTAssertNil(slot.raw["source_duration"])
                XCTAssertNil(slot.raw["playback_rate"])
            } else {
                XCTAssertEqual(slot.raw["source_duration_s"], .number(videoDuration))
                XCTAssertNil(slot.raw["still_layout"])
                XCTAssertNil(slot.raw["layout"])
                XCTAssertNotEqual(session.speedBlockedReason(for: session.selection), "Photos do not have playback speed.")
            }
            let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
            XCTAssertEqual(reopened.clips[0].raw["media_kind"], slot.raw["media_kind"])
            XCTAssertEqual(reopened.clips[0].raw["source_duration_s"], slot.raw["source_duration_s"])
            XCTAssertTrue(reopened.splitScreenIsValid)
            let resolved = try await NativeEditorSourceResolver(project: project, cacheDirectory: cacheDirectory)
                .resolve(.init(clips: poolClips, baseGeneration: "generation"), generation: "generation", requiredIndices: [target])
            let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
            let recipe = try compiler.compile(document: reopened, clips: session.timelineClips, items: [], sources: resolved).recipe
            let rendered = try XCTUnwrap(recipe.tracks.filter { $0.kind == .video }.flatMap(\.clips).first)
            XCTAssertEqual(rendered.splitScreen?.panelIndex, 1)
            XCTAssertEqual(rendered.sourceCrop, try NativeEditorRenderCompiler.sourceCrop(edits["source_crop"]))
            XCTAssertEqual(rendered.transform.scale, 1.2, accuracy: 0.000001)
            XCTAssertEqual(rendered.transform.rotationDegrees, 5, accuracy: 0.000001)
            XCTAssertEqual(rendered.transform.positionX, 0.1, accuracy: 0.000001)
            XCTAssertEqual(rendered.volume, 0.4, accuracy: 0.000001)
            XCTAssertNil(rendered.stillLayout)
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testExplicitNarratedAudioEnablePersistsMissingNullAndCustomGainsIntoRecipe() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let footage = ResolvedEditorSource(clipIndex: 0, mediaID: "footage", asset: MediaAsset(id: "footage", relativePath: "footage.mp4", fingerprint: fingerprint, duration: 4), url: URL(fileURLWithPath: "/footage.mp4"))
        let narration = ResolvedEditorSource(clipIndex: -1, mediaID: "narration", asset: MediaAsset(id: "narration", relativePath: "narration.m4a", fingerprint: fingerprint, duration: 2), url: URL(fileURLWithPath: "/narration.m4a"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let gains: [JSONValue?] = [nil, .null, .number(0), .number(0.4)]
        for initial in gains {
            var raw: [String: JSONValue] = ["muted": .bool(true)]
            raw["source_audio_volume"] = initial
            var document = EditorDocument(clips: [.init(id: "source", clipIndex: 0, inS: 0, durationS: 2, raw: raw)], mix: ["original_level": .number(1)], capabilities: ["timeline": .init(editable: true), "original_audio": .init(editable: true), "clips.audio": .init(editable: true)])
            document.editFormat = "narrated"
            let session = NativeEditorSession()
            session.replaceDocumentForTesting(document)
            session.toggleClipAudio(clipID: "source")
            let expected = initial?.numberValue == 0.4 ? 0.4 : 1.0
            XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], .number(expected))
            XCTAssertTrue(session.isClipAudioOn(clipID: "source"))
            let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
            let recipe = try compiler.compile(document: reopened, clips: session.timelineClips, items: [], sources: [0: footage], audioSources: ["narration": narration]).recipe
            XCTAssertEqual(recipe.tracks.first(where: { $0.kind == .video })?.clips.first?.volume, expected)
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testUnmutedNarratedClipAudioIndicatorAndToggleMatchCompiledGain() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let footage = ResolvedEditorSource(clipIndex: 0, mediaID: "footage", asset: MediaAsset(id: "footage", relativePath: "footage.mp4", fingerprint: fingerprint, duration: 4), url: URL(fileURLWithPath: "/footage.mp4"))
        let narration = ResolvedEditorSource(clipIndex: -1, mediaID: "voice", asset: MediaAsset(id: "voice", relativePath: "voice.m4a", fingerprint: fingerprint, duration: 2), url: URL(fileURLWithPath: "/voice.m4a"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let contexts: [(variant: [String: JSONValue], pinned: Bool?, resolved: Bool, narrated: Bool)] = [
            (["resolved_archetype": .string("narrated")], nil, false, true),
            (["resolved_archetype": .string("guided_story"), "render_receipt": .object(["narration_applied": .bool(true)])], nil, false, true),
            (["render_destination": .string("device"), "resolved_archetype": .string("guided_story")], true, false, true),
            (["render_destination": .string("device"), "resolved_archetype": .string("narrated")], false, false, false),
            ([:], nil, true, true),
            ([:], nil, false, false)
        ]
        for context in contexts {
            for initial in [nil, .null, .number(0), .number(0.4)] as [JSONValue?] {
                var raw: [String: JSONValue] = ["muted": .bool(false)]
                raw["source_audio_volume"] = initial
                let document = EditorDocument(clips: [.init(id: "source", clipIndex: 0, inS: 0, durationS: 2, raw: raw)], mix: ["original_level": .number(1)], capabilities: ["timeline": .init(editable: true), "original_audio": .init(editable: true), "clips.audio": .init(editable: true)])
                let session = NativeEditorSession()
                session.replaceDocumentForTesting(document)
                session.configureClipAudioForTesting(variant: context.variant, pinnedNarrationPresent: context.pinned,
                    resolvedNarration: context.resolved ? narration : nil)
                let before = initial?.numberValue ?? (context.narrated ? 0 : 1)
                XCTAssertEqual(session.isClipAudioOn(clipID: "source"), before > 0)
                let initialRecipe = try compiler.compile(document: document, clips: session.timelineClips, items: [], sources: [0: footage],
                    audioSources: context.narrated ? ["narration": narration] : [:]).recipe
                XCTAssertEqual(initialRecipe.tracks.first(where: { $0.kind == .video })?.clips.first?.volume, before)
                session.toggleClipAudio(clipID: "source")
                XCTAssertEqual(session.isClipAudioOn(clipID: "source"), before <= 0)
                if before <= 0 {
                    XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], .number(1))
                    XCTAssertEqual(session.document.clips[0].raw["muted"], .bool(false))
                } else {
                    XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], initial)
                    XCTAssertEqual(session.document.clips[0].raw["muted"], .bool(true))
                }
                let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
                let recipe = try compiler.compile(document: reopened, clips: session.timelineClips, items: [], sources: [0: footage],
                    audioSources: context.narrated ? ["narration": narration] : [:]).recipe
                XCTAssertEqual(recipe.tracks.first(where: { $0.kind == .video })?.clips.first?.volume, before > 0 ? 0 : 1)
                session.undo()
                XCTAssertEqual(session.document.clips[0].raw["source_audio_volume"], initial)
                XCTAssertEqual(session.isClipAudioOn(clipID: "source"), before > 0)
            }
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testNewJobDiscardsResolvedAndPinnedNarrationAudioState() {
        let narration = ResolvedEditorSource(clipIndex: -1, mediaID: "old-voice",
            asset: MediaAsset(id: "old-voice", relativePath: "old-voice.m4a", duration: 2),
            url: URL(fileURLWithPath: "/old-voice.m4a"))
        for resolved in [false, true] {
            let document = EditorDocument(clips: [.init(id: "source", clipIndex: 0, inS: 0, durationS: 2)],
                mix: ["original_level": .number(1)])
            let session = NativeEditorSession()
            session.replaceDocumentForTesting(document)
            session.configureClipAudioForTesting(variant: ["render_destination": .string("device")],
                pinnedNarrationPresent: resolved ? nil : true, resolvedNarration: resolved ? narration : nil)
            XCTAssertFalse(session.isClipAudioOn(clipID: "source"))
            session.discardPlaybackForNewJobForTesting()
            XCTAssertTrue(session.isClipAudioOn(clipID: "source"), "New non-narrated job must not inherit old narration defaults")
        }
    }
}

extension NativeSplitScreenTests {
    @MainActor
    private func mutationSession(_ document: EditorDocument) -> NativeEditorSession {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        return session
    }

    @MainActor
    func testFrozenPanelSlipMovesHeldFrameAndPreservesPartitionAtSourceBounds() throws {
        let id = UUID().uuidString
        var held = EditorTimelineSlot(id: id, clipIndex: 0, inS: 2, durationS: 3,
            raw: ["playback_rate": .number(2), "source_end_s": .number(4),
                  "freeze_frame_s": .number(4), "freeze_hold_s": .number(2), "source_duration_s": .number(8)])
        held.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 1)
        var peer = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 0, durationS: 5)
        peer.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        var document = EditorDocument(clips: [held, peer], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 5, panelCount: 2)]
        let session = mutationSession(document)
        session.select(.init(kind: .clip, id: id), seekToStart: false)
        for (delta, expectedStart, expectedFrame) in [(0.1, 2.1, 4.1), (100.0, 6.0, 8.0), (-100.0, 0.0, 2.0)] {
            session.slideSourceWindow(by: delta)
            let slot = try XCTUnwrap(session.document.clips.first)
            let clock = NativeClipSourceClock(slot: slot)
            XCTAssertEqual(slot.inS, expectedStart, accuracy: 0.000001)
            XCTAssertEqual(try XCTUnwrap(slot.raw["freeze_frame_s"]?.numberValue), expectedFrame, accuracy: 0.000001)
            XCTAssertEqual(clock.movingDuration, 1, accuracy: 0.000001)
            XCTAssertEqual(clock.hold, 2, accuracy: 0.000001)
            XCTAssertEqual(clock.rate, 2)
            XCTAssertEqual(slot.durationS, 3)
            XCTAssertEqual(slot.splitOffsetS, 1)
            XCTAssertEqual(session.document.clips[1], peer)
            XCTAssertEqual(session.timelineProjection.totalDuration, 5)
        }
        session.undo()
        XCTAssertEqual(session.document.clips[0].inS, 6)
        session.redo()
        XCTAssertEqual(session.document.clips[0].inS, 0)
    }

    @MainActor
    func testWholeVideoTransitionsCascadeWithinPanelsAndUndoAtomically() {
        var members = (0..<3).map { index -> EditorTimelineSlot in
            var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: index, inS: Double(index), durationS: 2)
            slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: Double(index * 2))
            return slot
        }
        var peer = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 3, inS: 7, durationS: 8)
        peer.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        members.append(peer)
        let tail = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 4, inS: 0, durationS: 2)
        var document = EditorDocument(clips: members + [tail], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 8, panelCount: 2)]
        let session = mutationSession(document)
        session.setTransitionAcrossVideo(transition: "crossfade", durationS: 0.2)
        XCTAssertTrue(session.document.splitScreenIsValid)
        for (slot, expected) in zip(session.document.clips.prefix(3), [0.0, 1.8, 3.6]) {
            XCTAssertEqual(slot.splitOffsetS, expected, accuracy: 0.000001)
        }
        XCTAssertEqual(session.document.clips[3].splitOffsetS, 0)
        XCTAssertEqual(session.document.clips.map(\.inS), document.clips.map(\.inS))
        XCTAssertEqual(session.document.clips.map(\.durationS), document.clips.map(\.durationS))
        XCTAssertEqual(session.timelineProjection.totalDuration, 10)
        let faded = session.document
        session.undo()
        XCTAssertEqual(session.document, document)
        session.redo()
        XCTAssertEqual(session.document, faded)
        session.setTransitionAcrossVideo(transition: "cut")
        XCTAssertTrue(session.document.splitScreenIsValid)
        for (slot, expected) in zip(session.document.clips.prefix(3), [0.0, 2.0, 4.0]) {
            XCTAssertEqual(slot.splitOffsetS, expected, accuracy: 0.000001)
        }
        session.undo()
        XCTAssertEqual(session.document, faded)
    }

    @MainActor
    func testLayoutOnlyAndSectionExtensionPreserveSelectedSourceEnd() {
        let id = UUID().uuidString
        var slot = EditorTimelineSlot(id: id, clipIndex: 0, inS: 2, durationS: 2,
            raw: ["source_end_s": .number(8), "playback_rate": .number(1), "source_duration_s": .number(12)])
        slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var document = EditorDocument(clips: [slot], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        let session = mutationSession(document)
        session.select(.init(kind: .clip, id: id), seekToStart: false)
        session.setSplitScreenPanelWeight(index: 0, weight: 2)
        session.setSplitScreenGutter(0.02)
        session.setSplitScreenBackgroundColor("#112233")
        XCTAssertEqual(session.document.clips, [slot])
        session.setSplitScreenDuration(5)
        XCTAssertEqual(session.document.clips, [slot])
        session.setSplitScreenDuration(1)
        XCTAssertEqual(session.document.clips[0].durationS, 1)
        XCTAssertEqual(session.document.clips[0].raw["source_end_s"], .number(3))
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testPanelTransitionMovesSuffixPreservesGapsAndRefusesOverflow() throws {
        var slots = [0.0, 2.0, 4.0, 7.0].enumerated().map { index, offset -> EditorTimelineSlot in
            var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: index, inS: Double(index), durationS: 2)
            slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: offset)
            return slot
        }
        var peer = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 9, inS: 4, durationS: 9)
        peer.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        slots.append(peer)
        var document = EditorDocument(clips: slots, capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 9, panelCount: 2)]
        let session = mutationSession(document)
        let firstID = try XCTUnwrap(slots[0].id)
        let gapID = try XCTUnwrap(slots[2].id)
        session.setClipTransition(clipID: firstID, transition: "crossfade", durationS: 0.2)
        for (slot, expected) in zip(session.document.clips.prefix(4), [0.0, 1.8, 3.8, 6.8]) {
            XCTAssertEqual(slot.splitOffsetS, expected, accuracy: 0.000001)
        }
        XCTAssertEqual(session.document.clips[4], peer)
        let beforeGapEdit = session.document.clips.map(\.splitOffsetS)
        session.setClipTransition(clipID: gapID, transition: "crossfade", durationS: 0.2)
        XCTAssertEqual(session.document.clips.map(\.splitOffsetS), beforeGapEdit, "A transition at a deliberate gap must not join it")
        session.setClipTransition(clipID: firstID, transition: "cut")
        XCTAssertEqual(session.document.clips.map(\.splitOffsetS), slots.map(\.splitOffsetS))
        XCTAssertTrue(session.document.splitScreenIsValid)

        var full = document
        full.clips[0].transitionAfter = "crossfade"; full.clips[0].transitionDurationS = 0.2
        full.clips[1].raw["split_offset_s"] = .number(1.8)
        full.clips[2].raw["split_offset_s"] = .number(3.8)
        full.clips[3].raw["split_offset_s"] = .number(7)
        let blocked = mutationSession(full)
        blocked.setTransitionAcrossVideo(transition: "cut")
        XCTAssertEqual(blocked.document, full)
        XCTAssertFalse(blocked.canUndo)
        XCTAssertNotNil(blocked.splitScreenActionError)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testPanelSlipClampsSelectedSourceEndEvenWhenOutputIsSectionBounded() {
        let id = UUID().uuidString
        var slot = EditorTimelineSlot(id: id, clipIndex: 0, inS: 2, durationS: 2,
            raw: ["source_end_s": .number(8), "playback_rate": .number(1), "source_duration_s": .number(12)])
        slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var document = EditorDocument(clips: [slot], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 2, panelCount: 2)]
        let session = mutationSession(document)
        session.select(.init(kind: .clip, id: id), seekToStart: false)
        session.slideSourceWindow(by: 100)
        XCTAssertEqual(session.document.clips[0].inS, 6)
        XCTAssertEqual(session.document.clips[0].raw["source_end_s"], .number(12))
        XCTAssertEqual(session.document.clips[0].durationS, 2)
        XCTAssertEqual(session.timelineProjection.totalDuration, 2)
        session.undo()
        XCTAssertEqual(session.document, document)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testPanelTransitionsUseTemporalOrderWhenPredecessorIsLastStoredMember() throws {
        var ordered = (0..<3).map { index -> EditorTimelineSlot in
            var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: index, inS: Double(index), durationS: 2)
            slot.setSplitMembership(sectionID: "s", panelIndex: 0, offset: Double(index * 2))
            return slot
        }
        var peer = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 3, inS: 0, durationS: 8)
        peer.setSplitMembership(sectionID: "s", panelIndex: 1, offset: 0)
        let firstID = try XCTUnwrap(ordered[0].id)
        ordered = [ordered[2], ordered[1], peer, ordered[0]]
        var document = EditorDocument(clips: ordered, capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 8, panelCount: 2)]
        let session = mutationSession(document)
        session.setClipTransition(clipID: firstID, transition: "crossfade", durationS: 0.2)
        for slot in session.document.clips where slot.splitPanelIndex == 0 {
            XCTAssertEqual(slot.splitOffsetS, [0.0, 1.8, 3.8][slot.clipIndex], accuracy: 0.000001)
        }
        session.undo()
        XCTAssertEqual(session.document, document)
        session.setTransitionAcrossVideo(transition: "crossfade", durationS: 0.2)
        for slot in session.document.clips where slot.splitPanelIndex == 0 {
            XCTAssertEqual(slot.splitOffsetS, [0.0, 1.8, 3.6][slot.clipIndex], accuracy: 0.000001)
        }
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.document.clips.first { $0.id == peer.id }?.splitOffsetS, 0)
        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    private func loadedSpeakerSession(_ document: EditorDocument) -> NativeEditorSession {
        var snapshot = document.encodeSnapshot()
        snapshot["render_destination"] = .string("device")
        snapshot["editor_capabilities"] = .object([
            "timeline": .object(["editable": .bool(true)]),
            "split_screen": .object(["editable": .bool(true)]),
            "speaker_background": .object(["editable": .bool(true)]),
            "speaker_background_windows": .object(["editable": .bool(true)])
        ])
        return NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: snapshot))
    }

    private func assertPersistedSpeakerRowsEqual(_ reopened: EditorDocument, _ original: EditorDocument,
                                                 file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(reopened.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue?["timeline_slots"],
                       original.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue?["timeline_slots"], file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.id), original.clips.map(\.id), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.parentSegmentID), original.clips.map(\.parentSegmentID), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.clipIndex), original.clips.map(\.clipIndex), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.inS), original.clips.map(\.inS), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.durationS), original.clips.map(\.durationS), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.durationBeats), original.clips.map(\.durationBeats), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.removed), original.clips.map(\.removed), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.transitionAfter), original.clips.map(\.transitionAfter), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.transitionDurationS), original.clips.map(\.transitionDurationS), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.lookPreset), original.clips.map(\.lookPreset), file: file, line: line)
        XCTAssertEqual(reopened.clips.map(\.lookAdjustments), original.clips.map(\.lookAdjustments), file: file, line: line)
        for key in ["source_crop", "media_transform", "speaker_frame_override", "split_section_id", "split_panel_index", "split_offset_s"] {
            XCTAssertEqual(reopened.clips.map { $0.raw[key] }, original.clips.map { $0.raw[key] }, file: file, line: line)
        }
    }

    @MainActor
    private func speakerOccurrenceSession() -> (NativeEditorSession, EditorSelection, EditorSelection) {
        let first = UUID().uuidString, second = UUID().uuidString
        var slots = [first, second].enumerated().map { index, id -> EditorTimelineSlot in
            var slot = EditorTimelineSlot(id: id, clipIndex: 0, inS: 0, durationS: 4,
                raw: ["speaker_subject": .bool(true), "source_duration_s": .number(20),
                      "media_transform": .object(["scale": .number(1.2), "rotation_deg": .number(12)])])
            slot.setSplitMembership(sectionID: "s", panelIndex: index, offset: 0)
            return slot
        }
        slots[0].raw["source_crop"] = .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.8), "height": .number(0.8)])
        var document = EditorDocument(clips: slots, capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true),
            "speaker_background": .init(editable: true), "speaker_background_windows": .init(editable: true)])
        document.splitScreenSections = [.init(id: "s", durationS: 8, panelCount: 2)]
        document.speakerBackground = .init(color: "#223344", speakerFrame: .preset("small_bottom_right"))
        return (loadedSpeakerSession(document), .init(kind: .speaker, id: first), .init(kind: .speaker, id: second))
    }

    @MainActor
    func testSpeakerOccurrenceDragResizePresetsAndFullKeepPeerAndGlobalFrame() throws {
        let (session, top, bottom) = speakerOccurrenceSession()
        let before = session.document
        session.select(bottom, seekToStart: false)
        session.beginTransaction()
        session.setSpeakerFrameCenter(x: 0.4, y: 0.5, for: bottom)
        session.setSpeakerFrameCenter(x: 0.3, y: 0.6, for: bottom)
        session.endTransaction()
        XCTAssertEqual(session.speakerFrame(for: bottom)?.xFraction, 0.3)
        XCTAssertEqual(session.speakerFrame(for: top), before.speakerBackground?.speakerFrame)
        XCTAssertEqual(session.document.clips[0], before.clips[0])
        XCTAssertEqual(session.document.speakerBackground, before.speakerBackground)
        XCTAssertEqual(session.document.clips[1].raw["media_transform"], before.clips[1].raw["media_transform"])
        let dragged = session.document
        session.undo(); XCTAssertEqual(session.document, before)
        session.redo(); XCTAssertEqual(session.document, dragged)
        session.setSpeakerFrameWidth(0.65, for: bottom)
        XCTAssertEqual(session.speakerFrame(for: bottom)?.widthFraction, 0.65)
        XCTAssertEqual(session.speakerFrame(for: top), before.speakerBackground?.speakerFrame)
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: bottom))
        XCTAssertEqual(session.speakerFrame(for: bottom), .preset("small_top_left"))
        XCTAssertTrue(session.setSpeakerFrame(preset: "full", for: bottom))
        XCTAssertNil(session.speakerFrame(for: bottom))
        XCTAssertNotNil(session.speakerFrame(for: top))
        XCTAssertEqual(session.selection, bottom, "Keep local full-panel controls reachable")
        XCTAssertEqual(session.previewSpeakerItems.map(\.selection), [top])
        session.undo(); XCTAssertEqual(session.speakerFrame(for: bottom), .preset("small_top_left"))
        session.redo(); XCTAssertNil(session.speakerFrame(for: bottom))
        XCTAssertEqual(session.document.clips[0], before.clips[0])
        XCTAssertEqual(session.document.speakerBackground, before.speakerBackground)
    }

    @MainActor
    func testSpeakerOccurrenceSelectionSyncGlobalInheritanceAndReopenAreIndependent() throws {
        let (session, top, bottom) = speakerOccurrenceSession()
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: bottom))
        let local = session.speakerFrame(for: bottom)
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_bottom_left"))
        XCTAssertEqual(session.speakerFrame(for: bottom), local)
        XCTAssertEqual(session.speakerFrame(for: top), .preset("small_bottom_left"))
        let beforeSelection = session.document
        session.select(bottom, seekToStart: false)
        session.select(top, seekToStart: false)
        XCTAssertEqual(session.document, beforeSelection)
        XCTAssertTrue(session.clearSpeakerFrame())
        XCTAssertNil(session.speakerFrame(for: top))
        XCTAssertEqual(session.speakerFrame(for: bottom), local)
        XCTAssertEqual(session.previewSpeakerItems.map(\.selection), [bottom])
        let reopened = loadedSpeakerSession(EditorDocument(snapshot: session.document.encodeSnapshot()))
        XCTAssertEqual(reopened.speakerFrame(for: bottom), local)
        XCTAssertNil(reopened.speakerFrame(for: top))
        reopened.select(top, seekToStart: false)
        reopened.setSpeakerFrameWidth(0.7, for: bottom)
        XCTAssertEqual(reopened.speakerFrame(for: bottom)?.widthFraction, 0.7, "Gesture target is captured, not read from current selection")
        XCTAssertNil(reopened.speakerFrame(for: top))
    }

    @MainActor
    func testSpeakerOverrideSurvivesCutDuplicateUnstackAndSourceReplacement() throws {
        let (session, top, _) = speakerOccurrenceSession()
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: top))
        let stored = try XCTUnwrap(session.document.clips[0].raw["speaker_frame_override"])
        session.select(.init(kind: .clip, id: top.id), seekToStart: false)
        session.currentTime = 2
        session.splitSelectedClip()
        XCTAssertEqual(session.document.clips.filter { $0.splitPanelIndex == 0 }.map { $0.raw["speaker_frame_override"] }, [stored, stored])
        let tail = try XCTUnwrap(session.document.clips.first { $0.splitPanelIndex == 0 && $0.splitOffsetS == 2 }?.id)
        session.select(.init(kind: .clip, id: tail), seekToStart: false)
        session.duplicateSelectedClip()
        XCTAssertEqual(session.document.clips.filter { $0.splitPanelIndex == 0 }.count, 3)
        XCTAssertTrue(session.document.clips.filter { $0.splitPanelIndex == 0 }.allSatisfy { $0.raw["speaker_frame_override"] == stored })
        session.removeSelectedSplitScreen()
        XCTAssertTrue(session.document.splitScreenSections.isEmpty)
        XCTAssertTrue(session.document.clips.allSatisfy { $0.raw["speaker_frame_override"] == stored })
        let scoped = EditorSelection(kind: .speaker, id: tail)
        session.setSpeakerFrameWidth(0.6, for: scoped)
        XCTAssertEqual(session.speakerFrame(for: scoped)?.widthFraction, 0.6)
        XCTAssertEqual(session.speakerFrame(for: top), .preset("small_top_left"))
        let beforeReplacement = session.document.clips.first { $0.id == tail }?.raw["speaker_frame_override"]
        session.select(.init(kind: .clip, id: tail), seekToStart: false)
        session.replaceSelectedClipSource(clipIndex: 0)
        XCTAssertEqual(session.document.clips.first { $0.id == tail }?.raw["speaker_frame_override"], beforeReplacement)
        assertPersistedSpeakerRowsEqual(EditorDocument(snapshot: session.document.encodeSnapshot()), session.document)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testScopedFullWidthSeedAndExplicitInheritResetDoNotChangeGlobalFrame() throws {
        let (session, top, bottom) = speakerOccurrenceSession()
        let global = session.speakerFrame
        XCTAssertTrue(session.setSpeakerFrame(preset: "full", for: bottom))
        XCTAssertEqual(session.speakerEditingFrame(for: bottom), .init(widthFraction: 1, xFraction: 0.5, yFraction: 0.5))
        session.setSpeakerFrameWidth(0.6, for: bottom)
        XCTAssertEqual(session.speakerFrame(for: bottom), .init(widthFraction: 0.6, xFraction: 0.5, yFraction: 0.5))
        XCTAssertEqual(session.speakerFrame(for: top), global)
        XCTAssertTrue(session.resetSpeakerFrame(for: bottom))
        XCTAssertEqual(session.document.clips[1].raw["speaker_frame_override"], .object(["mode": .string("inherit")]))
        XCTAssertEqual(session.speakerFrame(for: bottom), global)
        XCTAssertEqual(session.speakerFrame, global)
        session.undo()
        XCTAssertEqual(session.speakerFrame(for: bottom)?.widthFraction, 0.6)
    }

    @MainActor
    func testRemovingGlobalSpeakerBackgroundResetsOverridesAtomicallyAndUndoRestoresThem() {
        let (session, top, bottom) = speakerOccurrenceSession()
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: top))
        XCTAssertTrue(session.setSpeakerFrame(preset: "full", for: bottom))
        let before = session.document
        XCTAssertTrue(session.removeSpeakerBackground())
        XCTAssertNil(session.document.speakerBackground)
        XCTAssertTrue(session.document.clips.allSatisfy { $0.raw["speaker_frame_override"] == .object(["mode": .string("inherit")]) })
        XCTAssertTrue(session.isDirty(.timeline))
        XCTAssertTrue(session.isDirty(.speakerBackground))
        XCTAssertTrue(session.previewSpeakerItems.isEmpty)
        let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
        assertPersistedSpeakerRowsEqual(reopened, session.document)
        XCTAssertNil(reopened.speakerBackground)
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        XCTAssertNil(session.document.speakerBackground)
        XCTAssertTrue(session.document.splitScreenIsValid)
    }
}

extension NativeSplitScreenTests {
    func testFallbackOnlySplitSurvivesUnrelatedTextEditAndReopen() {
        let section = EditorSplitScreenSection(id: "fallback", durationS: 4, panelCount: 2)
        let rows: [JSONValue] = (0..<2).map { panel in
            .object(["slot_id": .string("member-\(panel)"), "clip_index": .number(Double(panel)),
                "in_s": .number(0), "duration_s": .number(4), "split_section_id": .string("fallback"),
                "split_panel_index": .number(Double(panel)), "split_offset_s": .number(0)])
        }
        let snapshot: [String: JSONValue] = [
            "user_timeline": .object(["slots": .array(rows), "split_screen_sections": .array([section.json])]),
            "text_elements": .array([.object(["id": .string("title"), "text": .string("Before"),
                "start_s": .number(0), "end_s": .number(2)])])]
        var document = EditorDocument(snapshot: snapshot)
        XCTAssertTrue(document.splitScreenIsValid)
        XCTAssertEqual(document.encodeSnapshot(), snapshot, "Untouched fallback envelope remains identical")
        let members = document.clips
        document.textElements[0].text = "After"
        let encoded = document.encodeSnapshot()
        let reopened = EditorDocument(snapshot: encoded)
        XCTAssertEqual(reopened.textElements.first?.text, "After")
        XCTAssertEqual(reopened.splitScreenSections, [section])
        XCTAssertEqual(reopened.clips, members)
        XCTAssertTrue(reopened.splitScreenIsValid)
        XCTAssertEqual(encoded["split_screen_sections"], .array([section.json]),
            "A root field must not shadow the fallback with an empty layout")
    }

    func testNativeSplitClearRetainsExplicitFieldWithoutChangingUntouchedLegacySnapshot() {
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
            "timeline_slots": .array([.object(["slot_id": .string("legacy"), "clip_index": .number(0),
                "in_s": .number(0), "duration_s": .number(4)])]), "unknown_lane": .string("preserve")])])]
        var document = EditorDocument(snapshot: snapshot)
        XCTAssertEqual(document.encodeSnapshot(), snapshot)
        document.splitScreenSections = [.init(id: "temporary", durationS: 4, panelCount: 2)]
        document.splitScreenSections = []
        XCTAssertNotEqual(document.snapshot(for: .timeline), EditorDocument(snapshot: snapshot).snapshot(for: .timeline),
            "An explicit split clear owns the timeline even when arrays return to their loaded values")
        let encoded = document.encodeSnapshot()
        let sections = encoded["editor_payload"]?.objectValue?["sections"]?.objectValue
        XCTAssertEqual(sections?["split_screen_sections"], .array([]))
        XCTAssertEqual(sections?["unknown_lane"], .string("preserve"))
        XCTAssertEqual(EditorDocument(snapshot: encoded).encodeSnapshot(), encoded)
        XCTAssertEqual(EditorDocument(snapshot: snapshot).encodeSnapshot(), snapshot)
    }

    @MainActor
    private func assertSplitSaveRetryPreservesTimeline(unstack: Bool) async throws {
        let jobID = UUID()
        let slotID = UUID().uuidString
        let slot: JSONValue = .object(["slot_id": .string(slotID), "clip_index": .number(0),
            "in_s": .number(0), "duration_s": .number(4), "source_duration_s": .number(20),
            "speaker_subject": .bool(true)])
        let variant: [String: JSONValue] = ["variant_id": .string("initial"),
            "render_destination": .string("device"), "render_generation_id": .string("generation-1"),
            "editor_state_version": .number(2), "duration_s": .number(4),
            "editor_timeline_mode": .string("authored"),
            "editor_capabilities": .object(["timeline": .bool(true), "speaker_background": .bool(true),
                "speaker_background_windows": .bool(true), "clips": .object(["split_screen": .object(["editable": .bool(true)])])]),
            "speaker_background": .object(["kind": .string("color"), "color": .string("#223344"),
                "speaker_frame": .object(["width_fraction": .number(0.42), "x_fraction": .number(0.76), "y_fraction": .number(0.8)])]),
            "user_timeline": .object(["slots": .array([slot])])]
        let snapshot = DraftSnapshot(draftID: "retry-split", itemID: "item", variantKey: "initial", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
            snapshot: [:], canUndo: false, createdAt: .now)
        let fake = EditorCommitSpy(draftSnapshot: snapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item", variantID: "initial"), authoritativeVariant: variant,
            commitResponse: EditorCommitResponse(ok: true, generation: "saved-generation",
                sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false),
                revisionNumber: 2, revisionHash: "saved", expectedDuration: nil, renderStarted: false))
        let session = NativeEditorSession(project: ProjectSummary(id: jobID, title: "Retry split", status: .ready,
            updatedAt: .now, posterURL: nil))
        await session.load(libraryJobID: jobID, api: fake)
        XCTAssertTrue(session.document.splitScreenSections.isEmpty)
        session.selectClip(try XCTUnwrap(session.timelineClips.first?.id))
        session.createSplitScreen(panelCount: 2, scope: .selectedClip)
        XCTAssertEqual(session.document.splitScreenSections.count, 1)
        let sectionID = try XCTUnwrap(session.document.splitScreenSections.first?.id)
        session.addSplitPanelClip(sectionID: sectionID, panel: 1, sourceClipIndex: 0)
        XCTAssertEqual(session.document.clips.count, 2)
        let top = EditorSelection(kind: .speaker, id: try XCTUnwrap(session.document.clips.first?.id))
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: top))
        let peer = session.document.clips.last
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
        if unstack {
            session.select(.init(kind: .clip, id: top.id), seekToStart: false)
            session.removeSelectedSplitScreen()
            XCTAssertTrue(session.document.splitScreenSections.isEmpty)
            await session.save()
            XCTAssertEqual(fake.commitCount, 2)
            XCTAssertEqual(fake.lastRequest?.splitScreenSections, [])
        } else {
            XCTAssertEqual(session.document.clips.last, peer)
        }
        guard case .renderRetryNeeded = session.saveState else { return XCTFail("Durable save must expose failed-render retry") }
        XCTAssertFalse(session.hasUnsavedChanges)
        let sentSlots = try XCTUnwrap(fake.lastRequest?.timelineSlots)
        let sentSections = try XCTUnwrap(fake.lastRequest?.splitScreenSections)
        let before = session.document
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, unstack ? 3 : 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "saved-generation")
        XCTAssertEqual(fake.lastRequest?.timelineSlots, sentSlots, "Retry preserves scoped framing and every peer field")
        XCTAssertEqual(fake.lastRequest?.splitScreenSections, sentSections, "Retry includes definitions or explicit empty unstack")
        XCTAssertEqual(session.document.clips, before.clips)
        XCTAssertEqual(session.document.splitScreenSections, before.splitScreenSections)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    @MainActor
    func testCleanFailedRenderRetryResendsNativeCreatedSplitDefinitionsAndScopedFrames() async throws {
        try await assertSplitSaveRetryPreservesTimeline(unstack: false)
    }

    @MainActor
    func testCleanFailedRenderRetryResendsExplicitEmptyAfterNativeCreatedSplitIsUnstacked() async throws {
        try await assertSplitSaveRetryPreservesTimeline(unstack: true)
    }

    @MainActor
    func testAuthoredAPISnapshotSpeakerFramingRequiresAdvertisedCapabilitiesAfterReopen() {
        let (fixture, top, bottom) = speakerOccurrenceSession()
        // The production API emits these boolean leaves only when each gate is open.
        // A pinned background alone must never grant editing permission.
        for gates in [(base: false, windows: false), (base: true, windows: false), (base: true, windows: true)] {
            var snapshot = fixture.document.encodeSnapshot()
            snapshot["render_destination"] = .string("device")
            snapshot["editor_timeline_mode"] = .string("authored")
            snapshot["resolved_archetype"] = .string("subtitled")
            var capabilities: [String: JSONValue] = [
                "timeline": .bool(true), "clips": .object(["split_screen": .object(["editable": .bool(true)])])]
            if gates.base { capabilities["speaker_background"] = .bool(true) }
            if gates.windows { capabilities["speaker_background_windows"] = .bool(true) }
            snapshot["editor_capabilities"] = .object(capabilities)
            func reopen(_ value: [String: JSONValue]) -> NativeEditorSession {
                let api = DraftSnapshot(draftID: "authored-speaker", itemID: "item", variantKey: "variant", draftRevision: 1,
                    snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: nil,
                    snapshot: value, canUndo: false, createdAt: .now)
                return NativeEditorSession(draft: api.editorDraft(projectID: UUID()))
            }
            let session = reopen(snapshot)
            let allowed = gates.base && gates.windows
            XCTAssertEqual(session.canEditSpeakerFrame(for: bottom), allowed)
            XCTAssertEqual(session.speakerSelection(forClip: .init(kind: .clip, id: bottom.id)) != nil, allowed)
            let before = session.document
            XCTAssertEqual(session.setSpeakerFrame(preset: "small_top_left", for: bottom), allowed)
            if allowed {
                XCTAssertEqual(session.speakerFrame(for: bottom), .preset("small_top_left"))
                XCTAssertEqual(session.speakerFrame(for: top), before.speakerBackground?.speakerFrame)
            } else {
                XCTAssertEqual(session.document, before)
                XCTAssertFalse(session.canUndo)
            }
            let saved = reopen(session.document.encodeSnapshot())
            XCTAssertEqual(saved.canEditSpeakerFrame(for: bottom), allowed)
            XCTAssertEqual(saved.speakerFrame(for: bottom), session.speakerFrame(for: bottom))
            XCTAssertEqual(saved.setSpeakerFrame(preset: "full", for: bottom), allowed)
            XCTAssertEqual(saved.speakerFrame(for: top), before.speakerBackground?.speakerFrame)
        }
    }

    @MainActor
    func testOrdinaryCropAndTiltKeepOccurrenceFramingAndPeerUnchanged() {
        let (session, top, bottom) = speakerOccurrenceSession()
        var document = session.document
        document.capabilities["clips.source_crop"] = .init(editable: true)
        document.capabilities["clips.tilt"] = .init(editable: true)
        session.replaceDocumentForTesting(document)
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: top))
        let before = session.document
        let target = EditorSelection(kind: .clip, id: top.id)
        let crop = NormalizedSourceRect(x: 0.2, y: 0.1, width: 0.6, height: 0.8)
        session.setFootageCrop(target, crop: crop)
        XCTAssertEqual(session.document.clips[0].raw["source_crop"], .object([
            "x": .number(0.2), "y": .number(0.1), "width": .number(0.6), "height": .number(0.8)]))
        let tilt = ClipTilt(rotationDeg: 15, backdrop: .blur)
        session.setClipTilt(tilt, for: target)
        XCTAssertEqual(session.clipTilt(for: target), tilt)
        XCTAssertEqual(session.document.clips[0].raw["speaker_frame_override"], before.clips[0].raw["speaker_frame_override"])
        XCTAssertEqual(session.speakerFrame(for: top), .preset("small_top_left"))
        XCTAssertEqual(session.speakerFrame(for: bottom), before.speakerBackground?.speakerFrame)
        XCTAssertEqual(session.document.clips[1], before.clips[1])
        XCTAssertEqual(session.document.speakerBackground, before.speakerBackground)
    }

    @MainActor
    func testSpeakerReplacementPreservesVideoOverrideAndRequiresExplicitResetForPhoto() throws {
        let (session, top, _) = speakerOccurrenceSession()
        var document = session.document
        document.clips.append(EditorTimelineSlot(id: UUID().uuidString, clipIndex: 1, inS: 0, durationS: 2,
            raw: ["media_kind": .string("image"), "source_duration_s": .number(20)]))
        session.replaceDocumentForTesting(document)
        XCTAssertTrue(session.setSpeakerFrame(preset: "small_top_left", for: top))
        session.select(.init(kind: .clip, id: top.id), seekToStart: false)
        let custom = session.document.clips[0].raw["speaker_frame_override"]
        let transform = session.document.clips[0].raw["media_transform"]
        session.replaceSelectedClipSource(clipIndex: 0)
        XCTAssertEqual(session.document.clips[0].raw["speaker_frame_override"], custom)
        XCTAssertEqual(session.document.clips[0].raw["speaker_subject"], .bool(true))
        XCTAssertEqual(session.document.clips[0].raw["media_transform"], transform)
        let beforePhoto = session.document
        session.replaceSelectedClipSource(clipIndex: 1)
        XCTAssertEqual(session.document, beforePhoto)
        XCTAssertTrue(session.splitScreenActionError?.contains("Use default framing") == true)
        XCTAssertTrue(session.resetSpeakerFrame(for: top))
        session.replaceSelectedClipSource(clipIndex: 1)
        XCTAssertEqual(session.document.clips[0].clipIndex, 1)
        XCTAssertEqual(session.document.clips[0].raw["speaker_frame_override"], .object(["mode": .string("inherit")]))
        XCTAssertEqual(session.document.clips[0].raw["speaker_subject"], .bool(false))
        XCTAssertEqual(session.document.clips[0].raw["media_transform"], transform)
        XCTAssertEqual(session.document.clips[1], beforePhoto.clips[1])
        XCTAssertNil(session.splitScreenActionError)
    }
}

extension NativeSplitScreenTests {
    @MainActor
    func testAPIDraftSessionLoadPreservesCanonicalAssetProvenance() throws {
        let alias = UUID(uuidString: "A32D7E41-7590-46F3-8888-012345678901")!
        let asset = UUID(uuidString: "B32D7E41-7590-46F3-8888-012345678902")!
        let identities: [JSONValue?] = [nil, .null, .string(asset.uuidString), .string("registered-photo-asset")]
        for opaqueSlot in [true, false] {
            for identity in identities {
                var row: [String: JSONValue] = [
                    "id": .string(alias.uuidString), "clip_index": .number(3),
                    "in_s": .number(1), "duration_s": .number(2), "source_duration_s": .number(12),
                    "media_id": .string("registered-photo"), "media_kind": .string("image"),
                    "split_section_id": .string("import-section"), "split_panel_index": .number(1),
                    "split_offset_s": .number(0), "source_crop": .object([
                        "x": .number(0.1), "y": .number(0.2), "width": .number(0.7), "height": .number(0.6)])]
                if opaqueSlot { row["slot_id"] = .string("imported-panel-occurrence") }
                row["asset_id"] = identity
                let snapshot: [String: JSONValue] = ["edit_format": .string("montage"), "editor_payload": .object([
                    "editor_state_version": .number(2), "sections": .object([
                        "timeline_slots": .array([.object(row)]), "split_screen_sections": .array([.object([
                            "id": .string("import-section"), "duration_s": .number(2), "panel_count": .number(2)])])])])]
                let api = DraftSnapshot(draftID: "import-reopen", itemID: "item", variantKey: "variant", draftRevision: 1,
                    snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: nil,
                    snapshot: snapshot, canUndo: false, createdAt: .now)
                let draft = api.editorDraft(projectID: UUID())
                XCTAssertEqual(draft.clips.count, 1)
                XCTAssertEqual(draft.clips.first?.id, alias)
                let session = NativeEditorSession(draft: draft)
                let rows = try XCTUnwrap(session.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?
                    .objectValue?["timeline_slots"]?.arrayValue)
                XCTAssertEqual(rows.count, 1)
                let loaded = try XCTUnwrap(rows.first?.objectValue)
                XCTAssertEqual(loaded["asset_id"], identity, "Loading must not turn a compatibility UUID into source provenance")
                XCTAssertEqual(loaded["slot_id"], .string(opaqueSlot ? "imported-panel-occurrence" : alias.uuidString))
                for key in ["clip_index", "in_s", "duration_s", "source_duration_s", "media_id", "media_kind",
                            "source_crop", "split_section_id", "split_panel_index", "split_offset_s"] {
                    XCTAssertEqual(loaded[key], row[key], key)
                }
                XCTAssertTrue(session.document.splitScreenIsValid)
                XCTAssertFalse(session.isDirty(.timeline))
                XCTAssertFalse(session.canUndo)
                XCTAssertFalse(session.canRedo)
            }
        }
    }

    @MainActor
    func testAPIDraftSessionLoadKeepsLegacyAndUnrepresentedClipAssets() throws {
        let oldID = UUID(uuidString: "A32D7E41-7590-46F3-8888-012345678901")!
        let oldAsset = UUID(uuidString: "B32D7E41-7590-46F3-8888-012345678902")!
        let newID = UUID(uuidString: "C32D7E41-7590-46F3-8888-012345678903")!
        let newAsset = UUID(uuidString: "D32D7E41-7590-46F3-8888-012345678904")!
        for legacy in [true, false] {
            let sections: [String: JSONValue] = legacy
                ? ["ios_editor": .object(["clips": .array([.object([
                    "id": .string(oldID.uuidString), "asset_id": .string(oldAsset.uuidString),
                    "clip_index": .number(2), "start": .number(0), "end": .number(2),
                    "trim_in": .number(1), "trim_out": .number(3), "source_duration": .number(8)])])])]
                : ["timeline_slots": .array([.object([
                    "id": .string(oldID.uuidString), "slot_id": .string("existing-panel"),
                    "asset_id": .string("opaque-existing-asset"), "clip_index": .number(2),
                    "in_s": .number(1), "duration_s": .number(2), "source_duration_s": .number(8)])])]
            let api = DraftSnapshot(draftID: "compatibility-load", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: nil,
                snapshot: ["editor_payload": .object(["sections": .object(sections)])], canUndo: false, createdAt: .now)
            var draft = api.editorDraft(projectID: UUID())
            // A compatibility client can append a clip before a canonical server row exists.
            draft.clips.append(EditorClip(id: newID, assetID: newAsset, sourceClipIndex: 5,
                start: 2, end: 4, trimIn: 3, trimOut: 5, sourceDuration: 11))
            let session = NativeEditorSession(draft: draft)
            let rows = try XCTUnwrap(session.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?
                .objectValue?["timeline_slots"]?.arrayValue)
            XCTAssertEqual(rows.count, 2)
            XCTAssertEqual(rows.first?.objectValue?["asset_id"], .string(legacy ? oldAsset.uuidString : "opaque-existing-asset"))
            let added = try XCTUnwrap(rows.last?.objectValue)
            XCTAssertEqual(added["slot_id"], .string(newID.uuidString))
            XCTAssertEqual(added["asset_id"], .string(newAsset.uuidString))
            XCTAssertEqual(added["clip_index"], .number(5))
            XCTAssertEqual(added["in_s"], .number(3))
            XCTAssertEqual(added["duration_s"], .number(2))
            XCTAssertEqual(added["source_duration_s"], .number(11))
            XCTAssertFalse(session.isDirty(.timeline))
            XCTAssertFalse(session.canUndo)
            XCTAssertFalse(session.canRedo)
        }
    }
}
