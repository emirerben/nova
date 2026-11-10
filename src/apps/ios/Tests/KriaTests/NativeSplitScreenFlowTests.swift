import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1335 flow redesign. Each test is written against a listed failure mode:
/// empty panels decoded away or rejected, a whole-video split losing identities or moving timed lanes,
/// a divider drag leaving intermediate undo steps or moving panels it does not touch,
/// a swap or a panel add changing the section's length or overlapping a peer.
final class NativeSplitScreenFlowTests: XCTestCase {
    private static var editable: [String: EditorCapability] { ["timeline": .init(editable: true), "split_screen": .init(editable: true)] }

    @MainActor
    private func makeSession(_ document: EditorDocument) -> NativeEditorSession {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        return session
    }

    private func member(_ panel: Int, offset: Double, duration: Double, clipIndex: Int = 0, section: String = "s",
                        raw: [String: JSONValue] = ["source_duration_s": .number(30)]) -> EditorTimelineSlot {
        var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: clipIndex, inS: 0, durationS: duration, raw: raw)
        slot.setSplitMembership(sectionID: section, panelIndex: panel, offset: offset)
        return slot
    }

    private func fullScreen(duration: Double, clipIndex: Int = 0, source: Double = 30) -> EditorTimelineSlot {
        EditorTimelineSlot(id: UUID().uuidString, clipIndex: clipIndex, inS: 0, durationS: duration, raw: ["source_duration_s": .number(source)])
    }

    @MainActor
    func testSelectedClipSplitStartsWithEmptyPanelsThatSurviveReopenAndCompile() throws {
        let lead = fullScreen(duration: 2)
        let chosen = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 5, durationS: 3, raw: [
            "source_duration_s": .number(30), "playback_rate": .number(1),
            "source_crop": .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.8), "height": .number(0.8)])])
        let tail = fullScreen(duration: 1)
        let document = EditorDocument(clips: [lead, chosen, tail], capabilities: Self.editable)
        let session = makeSession(document)
        let selected = EditorSelection(kind: .clip, id: try XCTUnwrap(chosen.id))
        session.select(selected, seekToStart: false)
        session.createSplitScreen(panelCount: 3, scope: .selectedClip)

        let section = try XCTUnwrap(session.document.splitScreenSections.first)
        XCTAssertEqual(section.panelCount, 3)
        XCTAssertEqual(section.durationS, 3)
        XCTAssertEqual(session.document.clips.map(\.id), document.clips.map(\.id), "No copy fills the new panels")
        var expected = chosen
        expected.setSplitMembership(sectionID: section.id, panelIndex: 0, offset: 0)
        XCTAssertEqual(session.document.clips[1], expected)
        XCTAssertEqual(session.timelineProjection.totalDuration, 6)
        XCTAssertEqual(session.selection, selected)
        session.currentTime = 3
        let context = try XCTUnwrap(session.splitContextAtPlayhead)
        XCTAssertEqual(context.sectionID, section.id)
        XCTAssertEqual(context.sectionStart, 2)
        XCTAssertEqual(context.emptyPanels, [1, 2])

        let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertTrue(reopened.splitScreenIsValid)
        XCTAssertEqual(reopened.splitScreenSections, session.document.splitScreenSections)
        XCTAssertEqual(reopened.snapshot(for: .timeline), session.document.snapshot(for: .timeline))

        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "source", relativePath: "source.mp4", fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 30),
            url: URL(fileURLWithPath: "/fixture/source.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: session.document, clips: session.timelineClips, items: [], sources: [0: source]).recipe
        XCTAssertEqual(recipe.tracks.filter { $0.kind == .video }.map(\.id), ["video", "split-\(section.id)-0"])
        XCTAssertEqual(recipe.splitScreenSections.map(\.start), [2])
        XCTAssertEqual(recipe.splitScreenSections.map(\.end), [5])
        XCTAssertEqual(recipe.splitScreenSections.first?.panelWeights.count, 3)
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 6, accuracy: 0.001)

        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testWholeVideoSplitKeepsEveryClipIdentityWindowAndTimedLane() throws {
        let first = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 1, durationS: 2, transitionAfter: "crossfade", transitionDurationS: 0.3,
            raw: ["source_crop": .object(["x": .number(0.2), "y": .number(0), "width": .number(0.5), "height": .number(1)])])
        let removed = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 1, inS: 0, durationS: 5, removed: true)
        let second = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 1, inS: 3, durationS: 3, raw: ["playback_rate": .number(2)])
        let photo = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 2, inS: 0, durationS: 1.5, raw: ["media_kind": .string("image")])
        let cue = EditorCaptionCue(id: "cue", startS: 4, endS: 5, text: "Later")
        let document = EditorDocument(clips: [first, removed, second, photo], captionCues: [cue],
            cameraEffects: [.init(id: "zoom", startS: 5.2, endS: 5.8)], capabilities: Self.editable)
        let session = makeSession(document)
        let before = session.timelineProjection.baseClipWindows
        XCTAssertTrue(session.canCreateWholeVideoSplit)
        session.createSplitScreen(panelCount: 2, scope: .wholeVideo)

        XCTAssertEqual(session.document.splitScreenSections.count, 1)
        let section = try XCTUnwrap(session.document.splitScreenSections.first)
        XCTAssertEqual(section.panelCount, 2)
        XCTAssertEqual(section.durationS, 6.2, accuracy: 0.000001)
        XCTAssertEqual(session.document.clips.map(\.id), document.clips.map(\.id))
        XCTAssertEqual(session.document.clips[1], removed, "A removed slot never joins the section")
        for (now, then) in zip(session.document.clips, document.clips) where !then.removed {
            var expected = then
            expected.setSplitMembership(sectionID: section.id, panelIndex: 0, offset: now.splitOffsetS)
            XCTAssertEqual(now, expected)
        }
        let offsets = session.document.clips.filter { !$0.removed }.map(\.splitOffsetS)
        for (offset, expected) in zip(offsets, [0, 1.7, 4.7]) { XCTAssertEqual(offset, expected, accuracy: 0.000001) }
        let after = session.timelineProjection.baseClipWindows
        XCTAssertEqual(after.map(\.sourceIndex), before.map(\.sourceIndex))
        for (now, then) in zip(after, before) {
            XCTAssertEqual(now.start, then.start, accuracy: 0.0005)
            XCTAssertEqual(now.end, then.end, accuracy: 0.0005)
        }
        XCTAssertEqual(session.timelineProjection.totalDuration, 6.2, accuracy: 0.0005)
        XCTAssertEqual(session.document.captionCues, [cue], "Lanes keep their time instead of collapsing to the section start")
        XCTAssertEqual(session.document.cameraEffects, document.cameraEffects)
        XCTAssertFalse(session.canCreateWholeVideoSplit)
        session.currentTime = 5
        XCTAssertEqual(session.splitContextAtPlayhead?.emptyPanels, [1])

        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    func testPanelRectsAndDividersMatchTheRenderedPanelHeights() {
        let rects = NativeSplitPanelLayout.panelRects(weights: [2, 1], gutter: 0.1)
        XCTAssertEqual(rects.count, 2)
        XCTAssertEqual(rects[0].minY, 0, accuracy: 1e-9)
        XCTAssertEqual(rects[0].height, 0.6, accuracy: 1e-9)
        XCTAssertEqual(rects[1].minY, 0.7, accuracy: 1e-9)
        XCTAssertEqual(rects[1].maxY, 1, accuracy: 1e-9)
        XCTAssertTrue(rects.allSatisfy { $0.minX == 0 && $0.width == 1 })
        let dividers = NativeSplitPanelLayout.dividerYs(weights: [2, 1], gutter: 0.1)
        XCTAssertEqual(dividers.count, 1)
        XCTAssertEqual(dividers[0], 0.65, accuracy: 1e-9)
        XCTAssertEqual(NativeSplitPanelLayout.dividerYs(weights: [1, 1, 1], gutter: 0).count, 2)
    }

    @MainActor
    func testDividerDragIsOneUndoStepAndMovesOnlyItsTwoPanelsWithTheFinger() throws {
        var document = EditorDocument(clips: [member(0, offset: 0, duration: 4), member(2, offset: 1, duration: 2)], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 3, gutter: 0.05)]
        let session = makeSession(document)
        session.currentTime = 1
        let before = NativeSplitPanelLayout.dividerYs(weights: [1, 1, 1], gutter: 0.05)
        session.beginSplitDividerDrag(sectionID: "s", boundary: 1)
        session.updateSplitDividerDrag(deltaFraction: -0.05)
        session.updateSplitDividerDrag(deltaFraction: -0.1)
        XCTAssertFalse(session.canUndo, "Live updates add no undo snapshot")
        let live = try XCTUnwrap(session.splitContextAtPlayhead).section.panelWeights
        XCTAssertEqual(live[0], 1)
        XCTAssertEqual(live[1] + live[2], 2, accuracy: 1e-12)
        let moved = NativeSplitPanelLayout.dividerYs(weights: live, gutter: 0.05)
        XCTAssertEqual(moved[0], before[0], accuracy: 1e-9)
        XCTAssertEqual(moved[1], before[1] - 0.1, accuracy: 1e-9, "The delta is cumulative and tracks the finger")
        session.endSplitDividerDrag()
        XCTAssertEqual(session.document.clips, document.clips)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 4)
        XCTAssertEqual(session.timelineProjection, NativeEditorInteraction.timelineProjection(slots: document.clips, carousel: nil, splitSections: document.splitScreenSections))
        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testDividerClampsSnapsToExactlyEqualAndCancelLeavesNoTrace() throws {
        var document = EditorDocument(clips: [member(0, offset: 0, duration: 4)], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        let session = makeSession(document)
        session.currentTime = 1
        session.beginSplitDividerDrag(sectionID: "s", boundary: 0)
        session.updateSplitDividerDrag(deltaFraction: 0.6)
        var weights = try XCTUnwrap(session.splitContextAtPlayhead).section.panelWeights
        XCTAssertEqual(weights[0], 1.7, accuracy: 1e-9)
        XCTAssertEqual(weights[1], 0.3, accuracy: 1e-9)
        session.updateSplitDividerDrag(deltaFraction: 0.009)
        weights = try XCTUnwrap(session.splitContextAtPlayhead).section.panelWeights
        XCTAssertEqual(weights[0], weights[1], "Near-equal snaps to exactly equal")
        XCTAssertEqual(weights[0] + weights[1], 2, accuracy: 1e-12)
        session.updateSplitDividerDrag(deltaFraction: -0.2)
        XCTAssertEqual(try XCTUnwrap(session.splitContextAtPlayhead).section.panelWeights[0], 0.6, accuracy: 1e-9)
        session.cancelSplitDividerDrag()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testSwapExchangesWholePanelsInOneStepWithoutTouchingClipsOrSizes() throws {
        let topA = member(0, offset: 0, duration: 2)
        let topB = member(0, offset: 2, duration: 2, raw: ["source_duration_s": .number(30), "muted": .bool(true)])
        let middle = member(1, offset: 1, duration: 1)
        var document = EditorDocument(clips: [topA, topB, middle], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 3, panelWeights: [1, 2, 3])]
        let session = makeSession(document)
        session.swapSplitPanels(sectionID: "s", 0, 2)

        var expected = document.clips
        expected[0].setSplitMembership(sectionID: "s", panelIndex: 2, offset: 0)
        expected[1].setSplitMembership(sectionID: "s", panelIndex: 2, offset: 2)
        XCTAssertEqual(session.document.clips, expected)
        XCTAssertEqual(session.document.splitScreenSections, document.splitScreenSections)
        session.currentTime = 0.5
        XCTAssertEqual(session.splitContextAtPlayhead?.emptyPanels, [0])
        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
        session.swapSplitPanels(sectionID: "s", 1, 1)
        session.swapSplitPanels(sectionID: "s", 1, 3)
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testPanelSelectionPicksTheClipUnderThePlayheadAndIgnoresEmptyPanels() throws {
        let lead = fullScreen(duration: 2)
        let top = member(0, offset: 0, duration: 4)
        let middle = member(1, offset: 1, duration: 1)
        let tail = fullScreen(duration: 1)
        var document = EditorDocument(clips: [lead, top, middle, tail], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 3)]
        let session = makeSession(document)
        let leadSelection = EditorSelection(kind: .clip, id: try XCTUnwrap(lead.id))
        session.select(leadSelection, seekToStart: false)

        session.currentTime = 1.9
        XCTAssertNil(session.splitContextAtPlayhead)
        session.currentTime = 2.5
        XCTAssertEqual(session.splitContextAtPlayhead?.sectionStart, 2)
        XCTAssertEqual(session.splitContextAtPlayhead?.emptyPanels, [2])
        session.selectSplitPanel(sectionID: "s", panel: 1)
        XCTAssertEqual(session.selection, leadSelection, "A gap in the panel selects nothing")
        session.selectSplitPanel(sectionID: "s", panel: 2)
        XCTAssertEqual(session.selection, leadSelection)
        session.currentTime = 3.2
        session.selectSplitPanel(sectionID: "s", panel: 1)
        XCTAssertEqual(session.selection, EditorSelection(kind: .clip, id: try XCTUnwrap(middle.id)))
        XCTAssertEqual(session.currentTime, 3.2, "Selecting a panel never seeks")
        session.currentTime = 6
        XCTAssertNil(session.splitContextAtPlayhead)
        XCTAssertEqual(session.document, document)
    }

    @MainActor
    func testAddingSavedFootageFillsAGapWithoutLengtheningOrOverlapping() throws {
        let lead = fullScreen(duration: 1)
        let top = member(0, offset: 0, duration: 4)
        let middle = member(1, offset: 2, duration: 1)
        let tail = fullScreen(duration: 1, clipIndex: 1, source: 10)
        var document = EditorDocument(clips: [lead, top, middle, tail], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 3)]
        let session = makeSession(document)
        func panel(_ index: Int) -> [EditorTimelineSlot] {
            session.document.clips.filter { $0.splitPanelIndex == index }.sorted { $0.splitOffsetS < $1.splitOffsetS }
        }

        session.currentTime = 2.5
        session.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 1)
        let gap = try XCTUnwrap(panel(1).first)
        XCTAssertEqual(gap.clipIndex, 1)
        XCTAssertEqual(gap.splitOffsetS, 0)
        XCTAssertEqual(gap.durationS, 2, "Clamped to the next clip in the panel")
        XCTAssertEqual(session.selection, EditorSelection(kind: .clip, id: try XCTUnwrap(gap.id)))

        session.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 1)
        XCTAssertEqual(panel(1).map(\.splitOffsetS), [0, 2, 3])
        XCTAssertEqual(panel(1).last?.durationS, 1, "Clamped to the section end")
        let full = session.document
        session.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 1)
        session.addSplitPanelClip(sectionID: "s", panel: 0, sourceClipIndex: 1)
        XCTAssertEqual(session.document, full, "A full panel takes nothing")

        session.addSplitPanelClip(sectionID: "s", panel: 2, sourceClipIndex: 1)
        XCTAssertEqual(panel(2).map(\.splitOffsetS), [0])
        XCTAssertEqual(panel(2).map(\.durationS), [4])
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.document.splitScreenSections, document.splitScreenSections)
        XCTAssertEqual(session.timelineProjection.totalDuration, 6, "Panel clips never become sequential clips")
        XCTAssertEqual(session.splitContextAtPlayhead?.emptyPanels, [])

        for _ in 0..<3 { session.undo() }
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testPanelCountAddsAnEmptyPanelAndRefusesToDropTheOnlyFootage() throws {
        let top = member(0, offset: 0, duration: 4)
        var document = EditorDocument(clips: [top], capabilities: Self.editable)
        document.splitScreenSections = [.init(id: "s", durationS: 4, panelCount: 2)]
        let session = makeSession(document)
        session.select(.init(kind: .clip, id: try XCTUnwrap(top.id)), seekToStart: false)
        session.setSplitScreenPanelCount(3)
        XCTAssertEqual(session.document.clips, document.clips)
        XCTAssertEqual(session.document.splitScreenSections.first?.panelCount, 3)
        session.currentTime = 1
        XCTAssertEqual(session.splitContextAtPlayhead?.emptyPanels, [1, 2])

        session.swapSplitPanels(sectionID: "s", 0, 2)
        let bottomOnly = session.document
        session.setSplitScreenPanelCount(2)
        XCTAssertEqual(session.document, bottomOnly, "Dropping the panel would delete the section's only clip")
    }
}
