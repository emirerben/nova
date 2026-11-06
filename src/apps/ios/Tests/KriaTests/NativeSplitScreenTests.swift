import XCTest
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
        session.createSplitScreen(panelCount: 2)
        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        let sectionID = try XCTUnwrap(session.selectedSplitScreenSectionID)
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
            session.createSplitScreen(panelCount: 2)
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
        XCTAssertEqual(weights.cameraEffects, old.cameraEffects)
        var longer = old; longer.splitScreenSections[0].durationS = 6
        NativeSplitTimelineRebase.rebase(&longer, from: old)
        NativeSpeechTimelineProjection.rebase(&longer, from: old)
        XCTAssertEqual(longer.cameraEffects, old.cameraEffects)
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
