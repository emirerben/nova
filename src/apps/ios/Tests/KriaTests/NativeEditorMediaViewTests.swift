import XCTest
@testable import Kria

#if DEBUG
@MainActor
final class NativeEditorMediaViewTests: XCTestCase {
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
}
#endif
