import Combine
import XCTest
@testable import Kria

final class NativeEditorInteractionTests: XCTestCase {
    func testVisibilityIsHalfOpen() {
        XCTAssertTrue(NativeEditorInteraction.isVisible(start: 1, end: 2, at: 1))
        XCTAssertTrue(NativeEditorInteraction.isVisible(start: 1, end: 2, at: 1.99))
        XCTAssertFalse(NativeEditorInteraction.isVisible(start: 1, end: 2, at: 2))
        XCTAssertFalse(NativeEditorInteraction.isVisible(start: 1, end: 2, at: 0.99))
    }

    func testPreviewOrderAndOverlapCycleAreDeterministic() {
        let back = NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: "back"), start: 0, end: 3, zIndex: 1, sourceIndex: 0)
        let front = NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: "front"), start: 0, end: 3, zIndex: 2, sourceIndex: 1)
        let same = NativeEditorTimelineItem(selection: EditorSelection(kind: .captionCue, id: "same"), start: 0, end: 3, zIndex: 2, sourceIndex: 2)
        XCTAssertEqual(NativeEditorInteraction.previewOrder([same, front, back]).map(\.id), ["back", "front", "same"])
        XCTAssertEqual(NativeEditorInteraction.cycleSelection(in: [back, front, same], at: 1)?.id, "same")
        XCTAssertEqual(NativeEditorInteraction.cycleSelection(in: [back, front, same], at: 1, current: EditorSelection(kind: .captionCue, id: "same"))?.id, "front")
        XCTAssertEqual(NativeEditorInteraction.cycleSelection(in: [back, front, same], at: 1, current: EditorSelection(kind: .text, id: "back"))?.id, "same")
    }

    func testTimeGeometryClampsAndExpandsHitTarget() {
        XCTAssertEqual(NativeEditorInteraction.x(forTime: -1, duration: 10, width: 100), 0)
        XCTAssertEqual(NativeEditorInteraction.x(forTime: 5, duration: 10, width: 100), 50)
        XCTAssertEqual(NativeEditorInteraction.time(forX: 200, duration: 10, width: 100), 10)
        let hit = NativeEditorInteraction.hitRect(CGRect(x: 10, y: 10, width: 12, height: 20))
        XCTAssertEqual(hit.width, 44); XCTAssertEqual(hit.height, 44); XCTAssertEqual(hit.midX, 16); XCTAssertEqual(hit.midY, 20)
    }

    func testTouchingIntervalsShareLaneAndOverlapsDoNot() {
        let first = NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: "first"), start: 0, end: 1, sourceIndex: 0)
        let touching = NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: "touching"), start: 1, end: 2, sourceIndex: 1)
        let overlap = NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: "overlap"), start: 0.5, end: 1.5, sourceIndex: 2)
        let packed = NativeEditorInteraction.packLanes([first, touching, overlap])
        XCTAssertEqual(Dictionary(uniqueKeysWithValues: packed.map { ($0.item.id, $0.lane) }), ["first": 0, "touching": 0, "overlap": 1])
    }

    func testCarouselIntroMiddleAndOutroShareOnePositionalProjection() throws {
        let slots = [slot("a"), slot("b"), slot("c")]
        let expected: [(position: String, start: Double, clipStarts: [Double])] = [
            ("intro", 0, [3, 5, 7]),
            ("middle", 2, [0, 5, 7]),
            ("outro", 6, [0, 2, 4]),
        ]

        for value in expected {
            let projection = NativeEditorInteraction.timelineProjection(
                slots: slots,
                carousel: [
                    "id": .string("carousel"),
                    "position": .string(value.position),
                    "duration_s": .number(3),
                    // Absolute values from an older render must never defeat
                    // the current positional splice.
                    "start_s": .number(88),
                    "end_s": .number(99),
                ]
            )
            let carousel = try XCTUnwrap(projection.carouselItem)
            XCTAssertEqual(carousel.start, value.start, accuracy: 0.0001, value.position)
            XCTAssertEqual(carousel.end, value.start + 3, accuracy: 0.0001, value.position)
            XCTAssertEqual(projection.clipWindows.map(\.start), value.clipStarts, value.position)
            XCTAssertEqual(projection.totalDuration, 9, accuracy: 0.0001, value.position)
            XCTAssertEqual(projection.unprojectOutputTime(value.start + 1), value.start, accuracy: 0.0001, value.position)
            XCTAssertEqual(projection.unprojectOutputTime(value.start + 3.5), value.start + 0.5, accuracy: 0.0001, value.position)
        }
    }

    func testCarouselRehomesTransitionOverlapAndExcludesRemovedSlots() throws {
        let slots = [
            slot("a", duration: 4, transition: "crossfade", transitionDuration: 0.3),
            slot("removed", duration: 9, removed: true),
            slot("b", duration: 4),
        ]
        let projection = NativeEditorInteraction.timelineProjection(
            slots: slots,
            carousel: [
                "position": .string("middle"),
                "duration_s": .number(3),
                "transition_in": .string("crossfade"),
                // Editor phase values are rounded to the documented 0.1 s
                // step before the adjacent-duration cap is applied.
                "transition_in_duration_s": .number(0.37),
                "transition_out": .string("none"),
            ]
        )

        XCTAssertEqual(projection.baseClipWindows.map(\.sourceIndex), [0, 2])
        XCTAssertEqual(projection.baseClipWindows.map(\.start), [0, 3.7])
        XCTAssertEqual(try XCTUnwrap(projection.carouselItem).start, 3.6, accuracy: 0.0001)
        XCTAssertEqual(projection.clipWindows.map(\.start), [0, 6.6])
        XCTAssertEqual(projection.clipWindows.last?.overlapBefore, 0)
        XCTAssertEqual(projection.baseInsertionTime, 3.7)
        XCTAssertEqual(projection.downstreamShift, 2.9)
        XCTAssertEqual(projection.totalDuration, 10.6)
        XCTAssertEqual(projection.projectBaseTime(3.7), 6.6)
        XCTAssertEqual(projection.unprojectOutputTime(3.65), 3.7)
    }

    func testCarouselBoundaryRangesAreRightBiasedAndCrossingRangesExtend() {
        let projection = NativeEditorInteraction.timelineProjection(
            slots: [slot("a", duration: 4), slot("b", duration: 4)],
            carousel: ["position": .string("middle"), "duration_s": .number(3)]
        )

        XCTAssertEqual(projection.projectBaseInterval(start: 3, end: 5).start, 3)
        XCTAssertEqual(projection.projectBaseInterval(start: 3, end: 5).end, 8)
        XCTAssertEqual(projection.projectBaseInterval(start: 2, end: 4).start, 2)
        XCTAssertEqual(projection.projectBaseInterval(start: 2, end: 4).end, 7)
        XCTAssertEqual(projection.projectBaseTime(4), 7)
        XCTAssertEqual(projection.unprojectOutputTime(5), 4)
        XCTAssertEqual(projection.unprojectOutputTime(7.5), 4.5)
    }

    func testNoCarouselKeepsProjectionAndInverseIdentity() {
        let projection = NativeEditorInteraction.timelineProjection(
            slots: [slot("a", duration: 2), slot("b", duration: 3)],
            carousel: nil
        )

        XCTAssertNil(projection.carouselItem)
        XCTAssertEqual(projection.clipWindows.map(\.start), [0, 2])
        XCTAssertEqual(projection.totalDuration, 5)
        XCTAssertEqual(projection.projectBaseInterval(start: 1.25, end: 4.5).start, 1.25)
        XCTAssertEqual(projection.projectBaseInterval(start: 1.25, end: 4.5).end, 4.5)
        XCTAssertEqual(projection.unprojectOutputTime(3.2), 3.2)
    }

    private func slot(
        _ id: String,
        duration: Double = 2,
        removed: Bool = false,
        transition: String = "cut",
        transitionDuration: Double? = nil
    ) -> EditorTimelineSlot {
        EditorTimelineSlot(
            id: id,
            clipIndex: 0,
            inS: 0,
            durationS: duration,
            removed: removed,
            transitionAfter: transition,
            transitionDurationS: transitionDuration
        )
    }
}

@MainActor
final class NativeEditorSelectionTests: XCTestCase {
    func testCrossKindSelectionSeeksWithoutAutoplayAndRemaps() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 3, trimIn: 0, trimOut: 3)
        var draft = EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        draft.serverSnapshot = ["editor_payload": .object(["sections": .object(["text_elements": .array([.object(["id": .string("text-1"), "text": .string("hello"), "start_s": .number(2), "end_s": .number(3)])])])])]
        let session = NativeEditorSession(draft: draft)
        let text = EditorSelection(kind: .text, id: "text-1")
        session.select(text)
        XCTAssertEqual(session.selection, text); XCTAssertEqual(session.currentTime, 2); XCTAssertFalse(session.isPlaying)
        session.selectClip(clip.id)
        XCTAssertEqual(session.selectedClipID, clip.id); XCTAssertEqual(session.selection?.kind, .clip)
    }

    func testDirtySectionsAreDocumentWideAndUndoPreservesSelection() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 3, trimIn: 0, trimOut: 3)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(clip.id)
        session.markDirty(.mediaOverlays)
        XCTAssertTrue(session.isDirty(.mediaOverlays)); XCTAssertTrue(session.hasUnsavedChanges)
        session.addText(content: "new")
        XCTAssertTrue(session.isDirty(.text)); XCTAssertEqual(session.selection?.id, clip.id.uuidString)
        session.undo()
        XCTAssertEqual(session.draft.text.count, 0); XCTAssertEqual(session.selection?.id, clip.id.uuidString)
        XCTAssertTrue(session.isDirty(.mediaOverlays))
    }
}

@MainActor
final class NativeEditorTimelinePerformanceTests: XCTestCase {
    func testThousandBarsBuildOnceAndClockTicksReuseProjection() {
        let rows = (0..<1000).map { index in
            JSONValue.object(["id": .string("text-\(index)"), "text": .string("bar"), "start_s": .number(Double(index) * 0.1), "end_s": .number(Double(index) * 0.1 + 0.08)])
        }
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object(["text_elements": .array(rows)])])]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        XCTAssertEqual(session.timelineItems.count, 1000)
        let builds = session.timelineProjectionBuildCount
        for tick in 0..<100 { session.currentTime = Double(tick) * 0.01; _ = session.timelineItems }
        XCTAssertEqual(session.timelineProjectionBuildCount, builds)
    }

    func testPlaybackClockTicksDoNotInvalidateTheWholeEditorSession() {
        let session = NativeEditorSession()
        var sessionInvalidations = 0
        let cancellable = session.objectWillChange.sink { sessionInvalidations += 1 }

        for tick in 0..<100 { session.currentTime = Double(tick) * 0.05 }

        XCTAssertEqual(sessionInvalidations, 0)
        withExtendedLifetime(cancellable) {}
    }

    func testRepeatedPlaybackStateDoesNotInvalidateTheWholeEditorSession() {
        let session = NativeEditorSession()
        var sessionInvalidations = 0
        let cancellable = session.objectWillChange.sink { sessionInvalidations += 1 }

        session.reconcilePlaybackState(false)
        session.reconcilePlaybackState(false)

        XCTAssertEqual(sessionInvalidations, 0)
        withExtendedLifetime(cancellable) {}
    }

    func testTimelineProjectionUsesPersistedLayerOrderForOverlappingObjects() {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object([
                "text_elements": .array([.object(["id": .string("text"), "text": .string("T"), "start_s": .number(0), "end_s": .number(2), "z": .number(30)])]),
                "visual_blocks": .array([.object(["id": .string("visual"), "kind": .string("media"), "start_s": .number(0), "end_s": .number(2), "z": .number(10)])]),
                "media_overlays": .array([.object(["id": .string("overlay"), "start_s": .number(0), "end_s": .number(2), "z_index": .number(20)])]),
            ])]),
        ]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        let items = NativeEditorInteraction.previewOrder(
            session.timelineItems.filter { $0.kind == .text || $0.kind == .visualBlock || $0.kind == .mediaOverlay }
        )
        XCTAssertEqual(items.map(\.selection.id), ["visual", "overlay", "text"])
        XCTAssertEqual(items.map(\.zIndex), [10, 20, 30])
    }
}
