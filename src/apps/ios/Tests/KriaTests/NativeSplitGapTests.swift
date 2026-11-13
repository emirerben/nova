import XCTest
@testable import Kria

/// KRI-1335 split-flow gaps. Each test is written against a listed failure mode.
///
/// This clip: the split takes a stale selection instead of the clip under the playhead; nothing selected refuses
/// although the playhead sits on a clip; a playhead inside a split ignores the selected ordinary clip.
/// Whole video: the default scope stays "This clip" on a multi-clip edit; the reason says whole video works while
/// create refuses (or the reverse); a too-short clip is silently left out of a partial split.
/// Trailing trim: a fixed section clamps at its end, or shrinks when a member shrinks or a drag comes back; the
/// growth and the section change take two undo steps; later clips overlap or their captions stay behind; a photo
/// cannot grow while a video grows past its footage; the next clip in the panel is overrun; the 1800 s cap is
/// broken; the end snaps to a clip of another section, misses an exact neighbour end, or is not rounded; the hint
/// is missing, names the wrong panel, hides "out of footage", or outlives the drag.
/// Extend to whole video: a clip is dropped, reordered, re-identified or loses its transition; timed lanes move;
/// the section does not span the video; a second section is swallowed; a removed slot joins; undo restores half.
/// Loop fill: copies share identities or a caption parent; the fill runs past the section end or grows it; the
/// last copy is not clipped (or its source end is stale); the order is wrong; a full or empty panel records an
/// undo step; other panels move.
/// Add: a full panel claims it can take a clip; a panel that already has clips and free time refuses.
/// Full screen from/until the playhead: a clip is lost or a frame counted twice at the cut; the cut part's source
/// window is not advanced; the wrong panel goes full screen; a gap shortens the video instead of being absorbed by
/// the clip before it (or the first clip, backwards); captions move; footage that runs out leaves lanes behind;
/// the operation is offered within 0.5 s of a section edge; undo restores half.
/// Remove: the selected panel is kept instead of the chosen one; the kept clips are out of order; an empty panel
/// deletes all footage.
final class NativeSplitGapTests: XCTestCase {
    private static var editable: [String: EditorCapability] { ["timeline": .init(editable: true), "split_screen": .init(editable: true)] }

    @MainActor
    private func makeSession(_ document: EditorDocument) -> NativeEditorSession {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        return session
    }

    private func member(_ panel: Int, offset: Double, duration: Double, inS: Double = 0, clipIndex: Int = 0, section: String = "s",
                        raw: [String: JSONValue] = ["source_duration_s": .number(30)]) -> EditorTimelineSlot {
        var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: clipIndex, inS: inS, durationS: duration, raw: raw)
        slot.setSplitMembership(sectionID: section, panelIndex: panel, offset: offset)
        return slot
    }

    private func ordinary(_ duration: Double, clipIndex: Int = 0, transition: String = "cut", transitionDuration: Double? = nil) -> EditorTimelineSlot {
        EditorTimelineSlot(id: UUID().uuidString, clipIndex: clipIndex, inS: 0, durationS: duration,
                           transitionAfter: transition, transitionDurationS: transitionDuration, raw: ["source_duration_s": .number(30)])
    }

    private func document(_ clips: [EditorTimelineSlot], sections: [EditorSplitScreenSection],
                          cues: [EditorCaptionCue] = [], effects: [EditorCameraEffect] = []) -> EditorDocument {
        var value = EditorDocument(clips: clips, captionCues: cues, cameraEffects: effects, capabilities: Self.editable)
        value.splitScreenSections = sections
        return value
    }

    @MainActor
    private func clipID(_ session: NativeEditorSession, _ slot: EditorTimelineSlot) throws -> UUID {
        try XCTUnwrap(session.timelineClips.first { $0.slotID == slot.id }?.id)
    }

    /// Drags the end handle by `delta` and returns the hint shown during the drag.
    @MainActor
    @discardableResult
    private func dragEnd(_ session: NativeEditorSession, _ slot: EditorTimelineSlot, by delta: Double) throws -> String? {
        XCTAssertTrue(session.beginTrim(clipID: try clipID(session, slot), edge: .trailing))
        session.updateTrim(by: delta)
        let hint = session.splitTrimHint
        session.endTrim()
        XCTAssertNil(session.splitTrimHint, "The hint ends with the drag")
        return hint
    }

    @MainActor
    private func slot(_ session: NativeEditorSession, _ id: String?) -> EditorTimelineSlot? {
        session.document.clips.first { $0.id == id }
    }

    @MainActor
    private func select(_ session: NativeEditorSession, _ slot: EditorTimelineSlot) throws {
        session.select(.init(kind: .clip, id: try XCTUnwrap(slot.id)), seekToStart: false)
    }

    // MARK: This clip

    @MainActor
    func testThisClipSplitsTheClipUnderThePlayheadNotAStaleSelection() throws {
        let a = ordinary(2), b = ordinary(3), c = ordinary(1)
        let original = document([a, b, c], sections: [])
        let session = makeSession(original)
        try select(session, a)
        session.currentTime = 3
        XCTAssertTrue(session.canCreateSplitScreen)
        session.createSplitScreen(panelCount: 2, scope: .selectedClip)
        let section = try XCTUnwrap(session.document.splitScreenSections.first)
        XCTAssertEqual(section.durationS, 3)
        XCTAssertEqual(session.document.clips.filter { $0.splitSectionID != nil }.map(\.id), [b.id])
        XCTAssertEqual(session.document.clips[0], a)
        XCTAssertEqual(session.document.clips[2], c)
        XCTAssertEqual(session.selectedSplitScreenSectionID, section.id, "The new split is the one the Split panel edits")
        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)

        let unselected = makeSession(original)
        unselected.currentTime = 0.5
        XCTAssertNil(unselected.selection)
        XCTAssertTrue(unselected.canCreateSplitScreen, "The clip under the playhead needs no selection")
        unselected.createSplitScreen(panelCount: 2, scope: .selectedClip)
        XCTAssertEqual(unselected.document.clips.filter { $0.splitSectionID != nil }.map(\.id), [a.id])
    }

    @MainActor
    func testThisClipFallsBackToTheSelectionOnlyWhenThePlayheadIsInsideASplit() throws {
        let inside = member(0, offset: 0, duration: 2), after = ordinary(2)
        let original = document([inside, after], sections: [.init(id: "s", durationS: 2, panelCount: 2)])
        let session = makeSession(original)
        try select(session, after)
        session.currentTime = 1
        session.createSplitScreen(panelCount: 2, scope: .selectedClip)
        XCTAssertEqual(session.document.splitScreenSections.count, 2)
        XCTAssertNotNil(slot(session, after.id)?.splitSectionID)
        XCTAssertNotEqual(slot(session, after.id)?.splitSectionID, "s")
        XCTAssertEqual(slot(session, inside.id), inside)

        let refused = makeSession(original)
        try select(refused, inside)
        refused.currentTime = 1
        XCTAssertFalse(refused.canCreateSplitScreen)
        refused.createSplitScreen(panelCount: 2, scope: .selectedClip)
        XCTAssertEqual(refused.document, original)
        XCTAssertFalse(refused.canUndo)
    }

    // MARK: Whole video

    @MainActor
    func testWholeVideoReasonDefaultScopeAndCreateAgree() throws {
        let two = makeSession(document([ordinary(2), ordinary(3)], sections: []))
        XCTAssertEqual(two.defaultSplitScope, .wholeVideo)
        XCTAssertNil(two.wholeVideoUnavailableReason)
        XCTAssertTrue(two.canCreateWholeVideoSplit)

        let refusals: [(EditorDocument, String)] = [
            (document([ordinary(4)], sections: []), "This edit has only one clip."),
            (document([member(0, offset: 0, duration: 2), ordinary(2)], sections: [.init(id: "s", durationS: 2, panelCount: 2)]),
             "Remove the split to cover the whole video."),
            (document([ordinary(2), ordinary(0.05), ordinary(2)], sections: []), "Some clips are too short to split."),
        ]
        for (value, reason) in refusals {
            let session = makeSession(value)
            XCTAssertEqual(session.wholeVideoUnavailableReason, reason)
            XCTAssertEqual(session.defaultSplitScope, .selectedClip, reason)
            XCTAssertFalse(session.canCreateWholeVideoSplit, reason)
            session.createSplitScreen(panelCount: 2, scope: .wholeVideo)
            XCTAssertEqual(session.document, value, "\(reason) never yields a partial split")
            XCTAssertFalse(session.canUndo)
        }

        var locked = document([ordinary(2), ordinary(3)], sections: [])
        locked.capabilities = ["timeline": .init(editable: true)]
        let closed = makeSession(locked)
        XCTAssertNotNil(closed.wholeVideoUnavailableReason)
        XCTAssertEqual(closed.defaultSplitScope, .selectedClip)
    }

    // MARK: Trailing trim

    @MainActor
    func testFixedSectionGrowsWithItsMemberAndNeverShrinks() throws {
        let lead = ordinary(1), top = member(0, offset: 0, duration: 4), side = member(1, offset: 0, duration: 2), tail = ordinary(2)
        let original = document([lead, top, side, tail], sections: [.init(id: "s", durationS: 4, panelCount: 2)],
                                cues: [.init(id: "c", startS: 5.2, endS: 5.8, text: "Tail")])
        let session = makeSession(original)
        try dragEnd(session, top, by: 1.5)
        XCTAssertEqual(slot(session, top.id)?.durationS ?? 0, 5.5, accuracy: 1e-9)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 5.5, accuracy: 1e-9)
        XCTAssertEqual(slot(session, side.id), side)
        XCTAssertEqual(session.timelineProjection.totalDuration, 8.5, accuracy: 1e-9, "The tail moves instead of overlapping")
        XCTAssertEqual(session.document.captionCues[0].startS, 6.7, accuracy: 1e-9)
        XCTAssertEqual(session.document.captionCues[0].endS, 7.3, accuracy: 1e-9)
        XCTAssertTrue(session.document.splitScreenIsValid)
        session.undo()
        XCTAssertEqual(session.document, original, "One undo restores the clip, the section and the caption")
        XCTAssertFalse(session.canUndo)

        try dragEnd(session, top, by: -1)
        XCTAssertEqual(slot(session, top.id)?.durationS ?? 0, 3, accuracy: 1e-9)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 4, "A fixed section never shrinks on its own")
        XCTAssertEqual(session.document.captionCues, original.captionCues)
        session.undo()

        XCTAssertTrue(session.beginTrim(clipID: try clipID(session, top), edge: .trailing))
        session.updateTrim(by: 2)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 6, accuracy: 1e-9)
        session.updateTrim(by: -1)
        session.endTrim()
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 4, "Dragging back returns to the given length")
        XCTAssertEqual(slot(session, top.id)?.durationS ?? 0, 3, accuracy: 1e-9)
        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)

        try dragEnd(session, side, by: 3)
        XCTAssertEqual(slot(session, side.id)?.durationS ?? 0, 5, accuracy: 1e-9, "Any panel can lengthen the section")
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 5, accuracy: 1e-9)
        XCTAssertEqual(slot(session, top.id), top)
    }

    @MainActor
    func testPhotoGrowsFreelyAndVideoStopsAtItsFootageWithAHint() throws {
        let top = member(0, offset: 0, duration: 4, raw: ["source_duration_s": .number(5)])
        let photo = member(1, offset: 0, duration: 2, raw: ["media_kind": .string("image")])
        let original = document([top, photo], sections: [.init(id: "s", durationS: 4, panelCount: 2)])

        let photos = makeSession(original)
        XCTAssertEqual(try dragEnd(photos, photo, by: 4), "Bottom ends at 6.0s")
        XCTAssertEqual(slot(photos, photo.id)?.durationS ?? 0, 6, accuracy: 1e-9, "A still has no footage end")
        XCTAssertEqual(photos.document.splitScreenSections.first?.durationS ?? 0, 6, accuracy: 1e-9)

        let videos = makeSession(original)
        XCTAssertEqual(try dragEnd(videos, top, by: 3), "Top ends at 5.0s · out of footage")
        XCTAssertEqual(slot(videos, top.id)?.durationS ?? 0, 5, accuracy: 1e-9)
        XCTAssertEqual(videos.document.splitScreenSections.first?.durationS ?? 0, 5, accuracy: 1e-9)

        let cancelled = makeSession(original)
        XCTAssertTrue(cancelled.beginTrim(clipID: try clipID(cancelled, top), edge: .trailing))
        cancelled.updateTrim(by: 0.5)
        XCTAssertEqual(cancelled.splitTrimHint, "Top ends at 4.5s")
        cancelled.cancelTimingGesture()
        XCTAssertNil(cancelled.splitTrimHint)
        XCTAssertEqual(cancelled.document, original)
        XCTAssertFalse(cancelled.canUndo)
    }

    @MainActor
    func testEndSnapsToThisSectionsOtherPanelsElseRoundsToATenth() throws {
        let top = member(0, offset: 0, duration: 3, section: "a"), bottom = member(1, offset: 0, duration: 4.1, section: "a")
        let foreign = member(0, offset: 0, duration: 3.35, section: "b")
        let original = document([top, bottom, foreign], sections: [.init(id: "a", durationS: 6, panelCount: 2), .init(id: "b", durationS: 4, panelCount: 2)])
        let cases: [(delta: Double, end: Double, hint: String)] = [
            (0.3, 3.3, "Top ends at 3.3s"),
            (0.37, 3.4, "Top ends at 3.4s"),
            (0.95, 4.1, "Top ends at 4.1s · aligned"),
            (2.9, 6, "Top ends at 6.0s · aligned"),
        ]
        for testCase in cases {
            let session = makeSession(original)
            XCTAssertEqual(try dragEnd(session, top, by: testCase.delta), testCase.hint)
            XCTAssertEqual(slot(session, top.id)?.durationS ?? 0, testCase.end, accuracy: 1e-9, "drag \(testCase.delta)")
            XCTAssertEqual(slot(session, foreign.id), foreign)
        }

        let still = makeSession(original)
        XCTAssertTrue(still.beginTrim(clipID: try clipID(still, top), edge: .trailing))
        still.updateTrim(by: 0)
        still.endTrim()
        XCTAssertEqual(still.document, original, "Touching the handle without moving changes nothing")
        XCTAssertFalse(still.canUndo)
    }

    @MainActor
    func testTrailingTrimStopsAtTheNextClipInThePanelAndAtTheTimelineCap() throws {
        let first = member(0, offset: 0, duration: 3), second = member(0, offset: 3.5, duration: 1.5), side = member(1, offset: 0, duration: 6)
        let session = makeSession(document([first, second, side], sections: [.init(id: "s", durationS: 6, panelCount: 2)]))
        try dragEnd(session, first, by: 2)
        XCTAssertEqual(slot(session, first.id)?.durationS ?? 0, 3.5, accuracy: 1e-9)
        XCTAssertEqual(slot(session, second.id), second)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 6)

        let lead = ordinary(10)
        let photo = member(0, offset: 0, duration: 4, raw: ["media_kind": .string("image")])
        let capped = makeSession(document([lead, photo], sections: [.init(id: "s", durationS: 4, panelCount: 2)]))
        try dragEnd(capped, photo, by: 5000)
        XCTAssertEqual(capped.document.splitScreenSections.first?.durationS ?? 0, 1790, accuracy: 1e-9)
        XCTAssertEqual(capped.timelineProjection.totalDuration, 1800, accuracy: 1e-9)
        XCTAssertTrue(capped.document.splitScreenIsValid)
    }

    // MARK: Extend to whole video

    @MainActor
    func testExtendToWholeVideoAbsorbsEveryClipInPlaceInOneStep() throws {
        for collapse in [false, true] {
            let a = ordinary(2, transition: "crossfade", transitionDuration: 0.3)
            let removed = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 5, inS: 0, durationS: 4, removed: true)
            let b = ordinary(3, clipIndex: 1)
            let top = member(0, offset: 0, duration: 4, clipIndex: 2), bottom = member(1, offset: 0, duration: 2, clipIndex: 3)
            let c = ordinary(2, clipIndex: 4)
            let original = document([a, removed, b, top, bottom, c],
                                    sections: [.init(id: "s", durationS: 4, panelCount: 2, panelWeights: [2, 1], collapseEmptyPanels: collapse)],
                                    cues: [.init(id: "in", startS: 4.8, endS: 5.4, text: "In"), .init(id: "after", startS: 9, endS: 9.5, text: "After")],
                                    effects: [.init(id: "zoom", startS: 1.8, endS: 2.4)])
            let session = makeSession(original)
            let before = session.timelineProjection.baseClipWindows
            XCTAssertEqual(session.timelineProjection.totalDuration, 10.7, accuracy: 1e-9)
            XCTAssertTrue(session.canExtendSplitToWholeVideo(sectionID: "s"))
            session.extendSplitToWholeVideo(sectionID: "s")

            XCTAssertEqual(session.document.splitScreenSections.count, 1)
            let section = try XCTUnwrap(session.document.splitScreenSections.first)
            XCTAssertEqual(section.id, "s")
            XCTAssertEqual(section.panelWeights, [2, 1])
            XCTAssertEqual(section.collapseEmptyPanels, collapse)
            XCTAssertEqual(section.durationS, 10.7, accuracy: 1e-9, "The section spans the video")
            XCTAssertEqual(session.document.clips.map(\.id), original.clips.map(\.id), "Nothing is dropped or reordered")
            XCTAssertEqual(session.document.clips[1], removed, "A removed slot never joins")
            let expected: [(EditorTimelineSlot, Int, Double)] = [(a, 0, 0), (b, 0, 1.7), (top, 0, 4.7), (bottom, 1, 4.7), (c, 0, 8.7)]
            for (then, panel, offset) in expected {
                let now = try XCTUnwrap(slot(session, then.id))
                XCTAssertEqual(now.splitOffsetS, offset, accuracy: 1e-6)
                var keep = then
                keep.setSplitMembership(sectionID: "s", panelIndex: panel, offset: now.splitOffsetS)
                XCTAssertEqual(now, keep, "Windows, identity and transition are kept")
            }
            let after = session.timelineProjection.baseClipWindows
            XCTAssertEqual(after.map(\.sourceIndex), before.map(\.sourceIndex))
            for (now, then) in zip(after, before) {
                XCTAssertEqual(now.start, then.start, accuracy: 0.0005)
                XCTAssertEqual(now.end, then.end, accuracy: 0.0005)
            }
            XCTAssertEqual(session.document.captionCues, original.captionCues)
            XCTAssertEqual(session.document.cameraEffects, original.cameraEffects)
            XCTAssertTrue(session.document.splitScreenIsValid)
            XCTAssertFalse(session.canExtendSplitToWholeVideo(sectionID: "s"), "Nothing is left to absorb")
            session.undo()
            XCTAssertEqual(session.document, original)
            XCTAssertFalse(session.canUndo)
        }
    }

    @MainActor
    func testExtendNeedsExactlyOneSection() throws {
        let original = document([ordinary(2), member(0, offset: 0, duration: 2, section: "s"), ordinary(1), member(0, offset: 0, duration: 2, section: "t")],
                                sections: [.init(id: "s", durationS: 2, panelCount: 2), .init(id: "t", durationS: 2, panelCount: 2)])
        let session = makeSession(original)
        XCTAssertFalse(session.canExtendSplitToWholeVideo(sectionID: "s"))
        session.extendSplitToWholeVideo(sectionID: "s")
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)
    }

    // MARK: Loop fill and adding

    @MainActor
    func testLoopFillRepeatsThePanelInOrderAndClipsTheLastCopy() throws {
        let top = member(0, offset: 0, duration: 10)
        let first = member(1, offset: 0, duration: 2, inS: 1, clipIndex: 1,
                           raw: ["source_duration_s": .number(30), "source_end_s": .number(3)])
        let second = member(1, offset: 2.5, duration: 2, clipIndex: 2)
        let original = document([top, first, second], sections: [.init(id: "s", durationS: 10, panelCount: 3)])
        let session = makeSession(original)
        session.currentTime = 9
        XCTAssertTrue(session.canAddToSplitPanel(sectionID: "s", panel: 1))
        session.loopFillSplitPanel(sectionID: "s", panel: 1)

        let panel = session.document.clips.filter { $0.splitPanelIndex == 1 }.sorted { $0.splitOffsetS < $1.splitOffsetS }
        XCTAssertEqual(panel.map(\.splitOffsetS), [0, 2.5, 4.5, 6.5, 8.5])
        XCTAssertEqual(panel.map { $0.durationS ?? 0 }, [2, 2, 2, 2, 1.5])
        XCTAssertEqual(panel.map(\.clipIndex), [1, 2, 1, 2, 1], "Copies repeat the panel in order")
        XCTAssertEqual(Set(session.document.clips.compactMap(\.id)).count, session.document.clips.count, "Every copy is its own occurrence")
        XCTAssertTrue(panel.dropFirst(2).allSatisfy { $0.parentSegmentID == nil })
        let last = try XCTUnwrap(panel.last)
        XCTAssertEqual(last.inS, 1)
        XCTAssertEqual(last.raw["source_end_s"], .number(2.5), "The clipped copy ends its source window with it")
        XCTAssertEqual(slot(session, top.id), top)
        XCTAssertEqual(session.document.splitScreenSections, original.splitScreenSections, "The fill never grows the section")
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertFalse(session.canAddToSplitPanel(sectionID: "s", panel: 1))
        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)

        for panel in [0, 2] {
            session.loopFillSplitPanel(sectionID: "s", panel: panel)
            XCTAssertEqual(session.document, original, "A full or empty panel has nothing to repeat")
            XCTAssertFalse(session.canUndo)
        }
        var nearlyFull = original
        nearlyFull.clips[2].durationS = 7.45
        let crumbs = makeSession(nearlyFull)
        crumbs.loopFillSplitPanel(sectionID: "s", panel: 1)
        XCTAssertEqual(crumbs.document, nearlyFull, "Less than a tenth of a second is not worth a copy")
    }

    @MainActor
    func testCanAddMatchesWhereAnAddLands() throws {
        let lead = ordinary(1)
        let top = member(0, offset: 0, duration: 4)
        let early = member(1, offset: 0, duration: 1), late = member(1, offset: 2, duration: 2)
        let original = document([lead, top, early, late], sections: [.init(id: "s", durationS: 4, panelCount: 3)])
        let session = makeSession(original)
        session.currentTime = 0.5
        XCTAssertFalse(session.canAddToSplitPanel(sectionID: "s", panel: 0), "A full panel has nowhere to put a clip")
        XCTAssertFalse(session.canAddToSplitPanel(sectionID: "s", panel: 1))
        XCTAssertTrue(session.canAddToSplitPanel(sectionID: "s", panel: 2))
        XCTAssertFalse(session.canAddToSplitPanel(sectionID: "s", panel: 3))
        session.currentTime = 4
        XCTAssertFalse(session.canAddToSplitPanel(sectionID: "s", panel: 1))
        session.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 0)
        XCTAssertEqual(session.document, original)
        session.currentTime = 2.5
        XCTAssertTrue(session.canAddToSplitPanel(sectionID: "s", panel: 1), "The gap under the playhead takes a clip")
        session.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 0)
        let added = session.document.clips.filter { $0.splitPanelIndex == 1 }.sorted { $0.splitOffsetS < $1.splitOffsetS }
        XCTAssertEqual(added.map(\.splitOffsetS), [0, 1, 2])
        XCTAssertEqual(added[1].durationS, 1)

        let tailed = makeSession(document([top, member(1, offset: 0, duration: 1)], sections: [.init(id: "s", durationS: 4, panelCount: 2)]))
        XCTAssertTrue(tailed.canAddToSplitPanel(sectionID: "s", panel: 1), "A panel with clips still takes one after its last")
        tailed.addSplitPanelClip(sectionID: "s", panel: 1, sourceClipIndex: 0)
        XCTAssertEqual(tailed.document.clips.filter { $0.splitPanelIndex == 1 }.map(\.splitOffsetS).sorted(), [0, 1])
    }

    // MARK: Full screen from or until the playhead

    /// lead 0-1, then a fixed 6 s section (Top: one clip; Bottom: 0-2 and 3-5), then tail 7-9.
    private func fullScreenFixture(bottomSecondRaw: [String: JSONValue] = ["source_duration_s": .number(30)], bottomSecondIn: Double = 2)
        -> (EditorDocument, top: EditorTimelineSlot, b1: EditorTimelineSlot, b2: EditorTimelineSlot) {
        let lead = ordinary(1, clipIndex: 9)
        var top = member(0, offset: 0, duration: 6, clipIndex: 0)
        top.raw["speaker_subject"] = .bool(true)
        let b1 = member(1, offset: 0, duration: 2, inS: 5, clipIndex: 1)
        let b2 = member(1, offset: 3, duration: 2, inS: bottomSecondIn, clipIndex: 2, raw: bottomSecondRaw)
        let tail = ordinary(2, clipIndex: 8)
        let value = document([lead, top, b1, b2, tail], sections: [.init(id: "s", durationS: 6, panelCount: 2)],
                             cues: [.init(id: "inside", startS: 4.2, endS: 4.8, text: "Inside")],
                             effects: [.init(id: "late", startS: 7.5, endS: 7.8)])
        return (value, top, b1, b2)
    }

    @MainActor
    private func assertNoRepeatedIdentity(_ session: NativeEditorSession, file: StaticString = #filePath, line: UInt = #line) {
        let ids = session.document.clips.compactMap(\.id)
        XCTAssertEqual(Set(ids).count, ids.count, "No occurrence is counted twice", file: file, line: line)
    }

    @MainActor
    func testFullScreenFromPlayheadTurnsTheSelectedPanelsRestIntoClips() throws {
        let (original, top, b1, b2) = fullScreenFixture()
        let session = makeSession(original)
        try select(session, b1)
        session.currentTime = 2.5
        XCTAssertTrue(session.canMakeSplitFullScreen(sectionID: "s", side: .fromPlayhead))
        session.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)

        let clips = session.document.clips
        XCTAssertEqual(clips.count, 6)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(clips[1].id, top.id)
        XCTAssertEqual(clips[1].durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(clips[1].raw["speaker_subject"], .bool(true))
        XCTAssertEqual(clips[2].id, b1.id)
        XCTAssertEqual(clips[2].durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(clips[2].inS, 5)
        let cutPart = clips[3]
        XCTAssertNil(cutPart.splitSectionID)
        XCTAssertNil(cutPart.raw["split_offset_s"])
        XCTAssertEqual(cutPart.clipIndex, 1)
        XCTAssertEqual(cutPart.inS, 6.5, accuracy: 1e-9, "The full-screen part starts where the split part stopped in the source")
        XCTAssertEqual(cutPart.durationS ?? 0, 1.5, accuracy: 1e-9, "The gap after it is absorbed")
        XCTAssertEqual(clips[4].id, b2.id)
        XCTAssertNil(clips[4].splitSectionID)
        XCTAssertEqual(clips[4].inS, 2)
        XCTAssertEqual(clips[4].durationS ?? 0, 3, accuracy: 1e-9, "The trailing gap is absorbed")
        XCTAssertEqual(clips[5], original.clips[4])
        assertNoRepeatedIdentity(session)
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.timelineProjection.totalDuration, 9, accuracy: 0.0005, "Nothing is lost or counted twice")
        let windows = session.timelineProjection.baseClipWindows
        XCTAssertEqual(windows[3].start, 2.5, accuracy: 0.0005, "Full screen begins exactly at the cut")
        XCTAssertEqual(session.document.captionCues, original.captionCues)
        XCTAssertEqual(session.document.cameraEffects, original.cameraEffects)
        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)

        let unselected = makeSession(original)
        unselected.currentTime = 2.5
        unselected.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)
        let rest = unselected.document.clips.filter { $0.splitSectionID == nil }
        XCTAssertEqual(rest.map(\.clipIndex), [9, 0, 8], "With no panel selected the first panel with footage goes full screen")
        XCTAssertEqual(rest[1].inS, 1.5, accuracy: 1e-9)
        XCTAssertEqual(rest[1].durationS ?? 0, 4.5, accuracy: 1e-9)
        XCTAssertNil(slot(unselected, b2.id), "Bottom footage after the cut leaves with the split")
        XCTAssertEqual(slot(unselected, b1.id)?.durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(unselected.timelineProjection.totalDuration, 9, accuracy: 0.0005)
    }

    @MainActor
    func testFullScreenAbsorbsALeadingGapBackwardsIntoTheFirstClip() throws {
        let (original, top, b1, b2) = fullScreenFixture()
        let session = makeSession(original)
        try select(session, b2)
        session.currentTime = 3.5
        session.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)
        let moved = try XCTUnwrap(slot(session, b2.id))
        XCTAssertNil(moved.splitSectionID)
        XCTAssertEqual(moved.inS, 1.5, accuracy: 1e-9, "The first clip reaches back to the cut")
        XCTAssertEqual(moved.durationS ?? 0, 3.5, accuracy: 1e-9)
        XCTAssertEqual(slot(session, b1.id), b1)
        XCTAssertEqual(slot(session, top.id)?.durationS ?? 0, 2.5, accuracy: 1e-9)
        XCTAssertEqual(session.timelineProjection.totalDuration, 9, accuracy: 0.0005)
        XCTAssertEqual(session.document.captionCues, original.captionCues)
    }

    @MainActor
    func testFootageThatRunsOutDropsOnlyTheEmptyTimeAndMovesLaterLanesWithIt() throws {
        let (fixture, _, _, b2) = fullScreenFixture(bottomSecondRaw: ["source_duration_s": .number(2.5)], bottomSecondIn: 0)
        var original = fixture
        original.captionCues.append(.init(id: "gap", startS: 6.6, endS: 6.9, text: "Gap"))
        let session = makeSession(original)
        try select(session, b2)
        session.currentTime = 4.5
        session.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)
        let rest = session.document.clips.filter { $0.splitSectionID == nil && $0.clipIndex == 2 }
        XCTAssertEqual(rest.count, 1)
        XCTAssertEqual(rest[0].inS, 0.5, accuracy: 1e-9)
        XCTAssertEqual(rest[0].durationS ?? 0, 2, accuracy: 1e-9, "The clip grows only as far as its footage")
        XCTAssertEqual(session.timelineProjection.totalDuration, 8.5, accuracy: 0.0005)
        XCTAssertEqual(session.document.captionCues.map(\.id), ["inside"], "A caption over the vanished gap goes with it")
        XCTAssertEqual(session.document.captionCues.first?.startS, 4.2)
        XCTAssertEqual(session.document.cameraEffects.first?.startS ?? 0, 7, accuracy: 1e-9)
        XCTAssertEqual(session.document.cameraEffects.first?.endS ?? 0, 7.3, accuracy: 1e-9)
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    @MainActor
    func testFullScreenUntilPlayheadIsTheMirror() throws {
        let (original, top, b1, b2) = fullScreenFixture()
        let session = makeSession(original)
        session.currentTime = 5
        XCTAssertTrue(session.canMakeSplitFullScreen(sectionID: "s", side: .untilPlayhead))
        session.makeSplitFullScreen(sectionID: "s", side: .untilPlayhead)
        let clips = session.document.clips
        XCTAssertEqual(clips.count, 5)
        XCTAssertEqual(clips[1].id, top.id)
        XCTAssertNil(clips[1].splitSectionID)
        XCTAssertEqual(clips[1].durationS ?? 0, 4, accuracy: 1e-9)
        XCTAssertEqual(clips[2].clipIndex, 0)
        XCTAssertEqual(clips[2].splitSectionID, "s")
        XCTAssertEqual(clips[2].splitOffsetS, 0)
        XCTAssertEqual(clips[2].inS, 4, accuracy: 1e-9)
        XCTAssertEqual(clips[2].durationS ?? 0, 2, accuracy: 1e-9)
        XCTAssertEqual(clips[3].id, b2.id, "The only surviving part keeps the occurrence")
        XCTAssertEqual(clips[3].splitOffsetS, 0)
        XCTAssertEqual(clips[3].inS, 3, accuracy: 1e-9)
        XCTAssertEqual(clips[3].durationS ?? 0, 1, accuracy: 1e-9)
        XCTAssertNil(slot(session, b1.id))
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 2, accuracy: 1e-9)
        assertNoRepeatedIdentity(session)
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.timelineProjection.totalDuration, 9, accuracy: 0.0005)
        XCTAssertEqual(session.timelineProjection.baseClipWindows[2].start, 5, accuracy: 0.0005, "The split resumes exactly at the cut")
        XCTAssertEqual(session.document.captionCues, original.captionCues)
        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)

        let bottom = makeSession(original)
        try select(bottom, b2)
        bottom.currentTime = 5
        bottom.makeSplitFullScreen(sectionID: "s", side: .untilPlayhead)
        let rest = bottom.document.clips.filter { $0.splitSectionID == nil }
        XCTAssertEqual(rest.map(\.clipIndex), [9, 1, 2, 8])
        XCTAssertEqual(rest[1].durationS ?? 0, 3, accuracy: 1e-9, "The gap after the first Bottom clip is absorbed")
        XCTAssertEqual(rest[2].id, b2.id)
        XCTAssertEqual(rest[2].durationS ?? 0, 1, accuracy: 1e-9)
        XCTAssertEqual(slot(bottom, top.id)?.inS ?? 0, 4, accuracy: 1e-9, "Top's only surviving part keeps the occurrence")
        XCTAssertEqual(bottom.timelineProjection.totalDuration, 9, accuracy: 0.0005)
        assertNoRepeatedIdentity(bottom)
    }

    @MainActor
    func testFullScreenNeedsHalfASecondOnEachSideOfThePlayhead() throws {
        let (original, _, _, _) = fullScreenFixture()
        let session = makeSession(original)
        for (time, allowed) in [(0.5, false), (1.4, false), (1.5, true), (6.5, true), (6.6, false)] {
            session.currentTime = time
            for side in [NativeSplitFullScreenSide.fromPlayhead, .untilPlayhead] {
                XCTAssertEqual(session.canMakeSplitFullScreen(sectionID: "s", side: side), allowed, "\(side) at \(time)")
            }
        }
        session.currentTime = 1.4
        session.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo)
    }

    // MARK: Remove

    @MainActor
    func testRemoveKeepsTheChosenPanelInOrderNotTheSelectedOne() throws {
        let lead = ordinary(1), top = member(0, offset: 0, duration: 4)
        let later = member(1, offset: 1.5, duration: 1.5, clipIndex: 2), earlier = member(1, offset: 0, duration: 1, clipIndex: 1)
        let tail = ordinary(2)
        let original = document([lead, top, later, earlier, tail], sections: [.init(id: "s", durationS: 4, panelCount: 3)],
                                cues: [.init(id: "tail", startS: 5.2, endS: 5.8, text: "Tail")])

        let keepTop = makeSession(original)
        try select(keepTop, earlier)
        keepTop.removeSplitScreen(sectionID: "s", keepPanel: 0)
        var plain = top
        plain.clearSplitMembership()
        XCTAssertEqual(keepTop.document.clips, [lead, plain, tail], "The chosen panel stays even with Bottom selected")
        XCTAssertTrue(keepTop.document.splitScreenSections.isEmpty)
        XCTAssertEqual(keepTop.document.captionCues, original.captionCues)
        keepTop.undo()
        XCTAssertEqual(keepTop.document, original)
        XCTAssertFalse(keepTop.canUndo)

        let keepBottom = makeSession(original)
        keepBottom.removeSplitScreen(sectionID: "s", keepPanel: 1)
        XCTAssertEqual(keepBottom.document.clips.map(\.id), [lead.id, earlier.id, later.id, tail.id], "Kept clips play in panel order")
        XCTAssertTrue(keepBottom.document.clips.allSatisfy { $0.splitSectionID == nil })
        XCTAssertEqual(keepBottom.document.captionCues.first?.startS ?? 0, 3.7, accuracy: 1e-9, "Later lanes follow the shorter video")

        let empty = makeSession(original)
        empty.removeSplitScreen(sectionID: "s", keepPanel: 2)
        XCTAssertEqual(empty.document, original, "An empty panel cannot replace the split")
        XCTAssertFalse(empty.canUndo)
    }

    // MARK: Cutting a photo

    private static let photoRaw: [String: JSONValue] = ["media_kind": .string("image")]

    @MainActor
    func testCuttingAPhotoMakesTwoIndependentPhotosWithNoParentLink() throws {
        let top = member(0, offset: 0, duration: 4)
        let photo = member(1, offset: 0, duration: 4, clipIndex: 1, raw: Self.photoRaw)
        let session = makeSession(document([top, photo], sections: [.init(id: "s", durationS: 4, panelCount: 2)]))
        try select(session, photo)
        session.currentTime = 2
        XCTAssertTrue(session.canSplitSelectedClip)
        session.splitSelectedClip()

        let pieces = session.document.clips.filter { $0.splitPanelIndex == 1 }.sorted { $0.splitOffsetS < $1.splitOffsetS }
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.map(\.parentSegmentID), [nil, nil], "The server refuses a parent link on a photo")
        XCTAssertEqual(Set(pieces.compactMap(\.id)).count, 2)
        XCTAssertEqual(pieces.map(\.splitOffsetS), [0, 2])
    }

    @MainActor
    func testCuttingAVideoStillLinksTheSecondPieceToTheFirst() throws {
        let top = member(0, offset: 0, duration: 4)
        let video = member(1, offset: 0, duration: 4, clipIndex: 1)
        let session = makeSession(document([top, video], sections: [.init(id: "s", durationS: 4, panelCount: 2)]))
        try select(session, video)
        session.currentTime = 2
        session.splitSelectedClip()
        let pieces = session.document.clips.filter { $0.splitPanelIndex == 1 }.sorted { $0.splitOffsetS < $1.splitOffsetS }
        XCTAssertEqual(pieces.map(\.parentSegmentID), [nil, video.id])
    }

    @MainActor
    func testFullScreenFromPlayheadOnAPhotoLinksNoParent() throws {
        let top = member(0, offset: 0, duration: 6)
        let photo = member(1, offset: 0, duration: 5, clipIndex: 1, raw: Self.photoRaw)
        let session = makeSession(document([top, photo], sections: [.init(id: "s", durationS: 6, panelCount: 2)]))
        try select(session, photo)
        session.currentTime = 2.5
        session.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)

        let pieces = session.document.clips.filter { $0.clipIndex == 1 }
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.map(\.parentSegmentID), [nil, nil])
        XCTAssertEqual(Set(pieces.compactMap(\.id)).count, 2)
    }
}
