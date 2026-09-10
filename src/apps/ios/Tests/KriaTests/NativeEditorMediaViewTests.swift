import AVFoundation
import XCTest
@testable import Kria

#if DEBUG
@MainActor
final class NativeEditorMediaViewTests: XCTestCase {
    func testPlaybackTogglePausesWithoutChangingThePlayhead() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.twoText)
        session.player = AVPlayer()
        session.currentTime = 1

        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.togglePlayback()

        XCTAssertFalse(session.isPlaying)
        XCTAssertEqual(session.player?.rate, 0)
        XCTAssertEqual(session.currentTime, 1)
    }

    func testPreviewProjectionUsesHalfOpenTextWindows() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.twoText)
        let text = session.timelineItems.filter { $0.kind == .text }

        XCTAssertEqual(NativeEditorInteraction.visible(text, at: 0).map(\.id), [text[0].id])
        XCTAssertEqual(NativeEditorInteraction.visible(text, at: 1.5).map(\.id), [text[1].id])
        XCTAssertTrue(NativeEditorInteraction.visible(text, at: 3).isEmpty)
    }

    func testSelectingTimelineItemSeeksWithoutStartingPlayback() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.twoText)
        let second = try! XCTUnwrap(session.timelineItems.first { $0.kind == .text && $0.start == 1.5 })

        session.select(second)

        XCTAssertEqual(session.selection, second.selection)
        XCTAssertEqual(session.currentTime, second.start)
        XCTAssertFalse(session.isPlaying)
    }

    func testOverlappingPreviewSelectionCyclesTopmostFirst() {
        let low = NativeEditorTimelineItem(
            selection: EditorSelection(kind: .text, id: "low"),
            start: 0, end: 2, zIndex: 1, sourceIndex: 0
        )
        let high = NativeEditorTimelineItem(
            selection: EditorSelection(kind: .text, id: "high"),
            start: 0, end: 2, zIndex: 2, sourceIndex: 1
        )

        XCTAssertEqual(NativeEditorInteraction.cycleSelection(in: [low, high], at: 1)?.id, "high")
        XCTAssertEqual(
            NativeEditorInteraction.cycleSelection(
                in: [low, high], at: 1,
                current: EditorSelection(kind: .text, id: "high")
            )?.id,
            "low"
        )
    }

    func testPersistedProjectionIncludesEveryTimedLaneAndCarousel() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let items = nativePersistedTimelineItems(for: session)
        let kinds = Set(items.map(\.kind))

        XCTAssertTrue(kinds.isSuperset(of: [
            .clip, .text, .captionCue, .soundEffect, .mediaOverlay,
            .visualBlock, .motionScene, .cameraEffect, .carousel,
        ]))
        XCTAssertEqual(items.first { $0.kind == .soundEffect }?.start, 0.5)
        XCTAssertEqual(items.first { $0.kind == .mediaOverlay }?.end, 4)
        XCTAssertEqual(items.first { $0.kind == .carousel }?.selection.id, "carousel-1")
        XCTAssertEqual(session.timelineClips.map(\.start), [0, 4, 6])
        XCTAssertEqual(session.duration, 8)
        let music = items.first { $0.kind == .music }
        XCTAssertEqual(music?.start, 0, "the output-clock bed is intentionally not rippled")
        XCTAssertEqual(music?.end, 8)
        XCTAssertEqual(items.first { $0.kind == .visualBlock }?.start, 4)
        XCTAssertEqual(items.first { $0.kind == .motionScene }?.start, 5)
        XCTAssertEqual(items.first { $0.kind == .cameraEffect }?.start, 6)
    }

    func testCarouselPositionAndDurationProjectToARealTimelineWindow() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let timing = session.timelineProjection.carouselItem

        XCTAssertEqual(timing?.id, "carousel-1")
        XCTAssertEqual(timing?.start, 2)
        XCTAssertEqual(timing?.end, 4)
    }

    func testPersistedProjectionIsChronologicalAndStable() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let first = nativePersistedTimelineItems(for: session)
        let second = nativePersistedTimelineItems(for: session)

        XCTAssertEqual(first, second)
        XCTAssertEqual(first, first.sorted {
            if $0.start != $1.start { return $0.start < $1.start }
            if $0.end != $1.end { return $0.end < $1.end }
            if $0.zIndex != $1.zIndex { return $0.zIndex < $1.zIndex }
            if $0.sourceIndex != $1.sourceIndex { return $0.sourceIndex < $1.sourceIndex }
            if $0.kind != $1.kind { return $0.kind.rawValue < $1.kind.rawValue }
            return $0.id < $1.id
        })
        XCTAssertEqual(session.timelineProjectionBuildCount, 1)
    }

    func testPersistedLanesUseHalfOpenVisibilityAtTheirStoredWindows() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let items = nativePersistedTimelineItems(for: session)

        XCTAssertEqual(
            NativeEditorInteraction.visible(items, at: 0.6).filter { $0.kind == .soundEffect }.map(\.id),
            ["sfx-1"]
        )
        XCTAssertEqual(
            NativeEditorInteraction.visible(items, at: 4).filter { $0.kind == .visualBlock }.map(\.id),
            ["visual-1"]
        )
        XCTAssertTrue(NativeEditorInteraction.visible(items, at: 7.99).contains { $0.kind == .music })
        XCTAssertFalse(NativeEditorInteraction.visible(items, at: 5).contains { $0.kind == .cameraEffect })
        XCTAssertTrue(NativeEditorInteraction.visible(items, at: 6).contains { $0.kind == .cameraEffect })
    }

    func testTimedLaneMoveUsesOneImmutableBaseline() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)

        session.beginTimedBodyMove(kind: .mediaOverlay, id: "overlay-1")
        session.updateTimedBodyMove(by: 0.5)
        session.updateTimedBodyMove(by: 0.9)
        session.endTimedBodyMove()

        let overlay = try! XCTUnwrap(session.document.mediaOverlays.first { $0.id == "overlay-1" })
        XCTAssertEqual(overlay.startS, 1.9, accuracy: 0.0001)
        XCTAssertEqual(overlay.endS, 2.9, accuracy: 0.0001)
        XCTAssertTrue(session.canUndo)
    }

    func testTimedLaneMoveReturningToOriginRestoresBaselineWithoutUndo() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let baseline = session.document

        session.beginTimedBodyMove(kind: .mediaOverlay, id: "overlay-1")
        session.updateTimedBodyMove(by: 0.5)
        session.updateTimedBodyMove(by: 0)
        session.endTimedBodyMove()

        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.canUndo)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSoundEffectEdgeTrimKeepsPlacementAndChangesSourceTrim() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)

        session.beginTimedEdgeTrim(kind: .soundEffect, id: "sfx-1", edge: .leading)
        session.updateTimedEdgeTrim(by: 0.1)
        session.endTimedEdgeTrim()

        let effect = try! XCTUnwrap(session.document.soundEffects.first { $0.id == "sfx-1" })
        XCTAssertEqual(effect.startS, 0.5, accuracy: 0.0001)
        XCTAssertEqual(effect.endS, 0.8, accuracy: 0.0001)
        XCTAssertEqual(effect.raw["trim_start_s"]?.numberValue, 0.1)
    }
}
#endif
