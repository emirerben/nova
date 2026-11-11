import XCTest
@testable import Kria

@MainActor
final class NativeTimelineGestureCancellationTests: XCTestCase {
    private func session(split: Bool = false) -> (NativeEditorSession, UUID) {
        let id = UUID()
        var document = EditorDocument(clips: [.init(id: id.uuidString, clipIndex: 0, inS: 0, durationS: 4)],
            textElements: [.init(id: "label", text: "Text", startS: 1, endS: 3)],
            capabilities: ["timeline": .init(editable: true), "text": .init(editable: true)])
        if split {
            document.clips[0].setSplitMembership(sectionID: "section", panelIndex: 0, offset: 0)
            var peer = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 1, inS: 2, durationS: 4)
            peer.setSplitMembership(sectionID: "section", panelIndex: 1, offset: 0)
            document.clips.append(peer)
            document.splitScreenSections = [.init(id: "section", durationS: 5, panelCount: 2)]
        }
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: document.encodeSnapshot()))
        return (session, id)
    }

    func testPinchRollsBackTrimAndPreservesRedoThenAllowsOneNormalTrim() {
        let (session, id) = session(split: true)
        XCTAssertFalse(session.isDirty(.timeline))
        XCTAssertFalse(session.hasUnsavedChanges)
        session.trimSelectedClipForTesting(id: id)
        let redoTarget = session.document
        session.undo()
        let baseline = session.document
        XCTAssertTrue(session.canRedo)
        let owner = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTrim(clipID: id, edge: .leading, gestureID: owner))
        session.updateTrim(by: 0.9, gestureID: owner)
        XCTAssertTrue(session.isDirty(.timeline))
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertNotEqual(session.document, baseline)
        session.setTimelinePinching(true)
        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.isDirty(.timeline))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.canUndo)
        XCTAssertTrue(session.canRedo)
        XCTAssertFalse(session.isTimingGestureActive)
        session.updateTrim(by: 2, gestureID: owner)
        session.endTrim(gestureID: owner)
        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.beginTrim(clipID: id, edge: .trailing, gestureID: NativeTimelineGestureID()))
        session.setTimelinePinching(false)
        session.redo()
        XCTAssertEqual(session.document, redoTarget)
        session.undo()
        XCTAssertEqual(session.document, baseline)
        let next = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTrim(clipID: id, edge: .trailing, gestureID: next))
        session.updateTrim(by: -1, gestureID: next)
        session.updateTrim(by: 2, gestureID: owner)
        session.endTrim(gestureID: owner)
        XCTAssertTrue(session.isTimingGestureActive)
        session.endTrim(gestureID: next)
        XCTAssertEqual(session.document.clips[0].durationS, 3)
        session.undo()
        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.canUndo)
    }

    func testCancelKeepsAnAlreadyDirtyBaselineAndItsUndoHistory() {
        let (session, id) = session()
        session.trimSelectedClipForTesting(id: id)
        let dirtyBaseline = session.document
        XCTAssertTrue(session.isDirty(.timeline))
        XCTAssertTrue(session.canUndo)
        let token = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTrim(clipID: id, edge: .leading, gestureID: token))
        session.updateTrim(by: 0.75, gestureID: token)
        session.cancelTimingGesture(gestureID: token)
        XCTAssertEqual(session.document, dirtyBaseline)
        XCTAssertTrue(session.isDirty(.timeline))
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.clips.first?.durationS, 4)
        XCTAssertFalse(session.isDirty(.timeline))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.canUndo)
    }

    func testSecondHandleCannotReplaceBaselineOrCommitOwner() {
        let (session, id) = session()
        let baseline = session.document
        let first = NativeTimelineGestureID(), second = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTrim(clipID: id, edge: .leading, gestureID: first))
        session.updateTrim(by: 0.5, gestureID: first)
        let preview = session.document
        XCTAssertFalse(session.beginTrim(clipID: id, edge: .trailing, gestureID: second))
        session.updateTrim(by: -2, gestureID: second)
        session.endTrim(gestureID: second)
        XCTAssertEqual(session.document, preview)
        session.cancelTimingGesture(gestureID: first)
        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.canUndo)
    }

    func testTimedBarCancellationRestoresExactWindowAndRejectsStaleCallbacks() {
        let (session, _) = session()
        session.beginTimedEdgeTrim(kind: .text, id: "label", edge: .trailing)
        session.updateTimedEdgeTrim(by: -0.25)
        session.endTimedEdgeTrim()
        session.undo()
        XCTAssertTrue(session.canRedo)
        let baseline = session.document
        let token = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTimedEdgeTrim(kind: .text, id: "label", edge: .leading, gestureID: token))
        session.updateTimedEdgeTrim(by: 0.5, gestureID: token)
        XCTAssertNotEqual(session.document, baseline)
        session.setTimelinePinching(true)
        session.setTimelinePinching(false)
        session.updateTimedEdgeTrim(by: 1, gestureID: token)
        session.endTimedEdgeTrim(gestureID: token)
        XCTAssertEqual(session.document, baseline)
        XCTAssertFalse(session.canUndo)
        XCTAssertTrue(session.canRedo)
        let next = NativeTimelineGestureID()
        XCTAssertTrue(session.beginTimedEdgeTrim(kind: .text, id: "label", edge: .trailing, gestureID: next))
        session.updateTimedEdgeTrim(by: -0.5, gestureID: next)
        session.endTimedEdgeTrim(gestureID: next)
        XCTAssertEqual(session.document.textElements[0].endS, 2.5)
        session.undo()
        XCTAssertEqual(session.document, baseline)
    }
}

private extension NativeEditorSession {
    func trimSelectedClipForTesting(id: UUID) {
        beginTrim(clipID: id, edge: .trailing)
        updateTrim(by: -0.5)
        endTrim()
    }
}
