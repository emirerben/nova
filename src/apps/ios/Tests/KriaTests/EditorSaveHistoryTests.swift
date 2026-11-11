import XCTest
@testable import Kria

final class EditorSaveHistoryTests: XCTestCase {
    func testLateCheckpointsSurviveAnAlreadyFullDisplayHistory() {
        var history = EditorSaveHistory(submitted: 100, limit: 100)
        for value in 100..<205 { history.appendUndo(value) }
        XCTAssertEqual(history.undoCheckpoints(current: 205), Array(105..<205))
    }

    func testUndoBeforeSaveAcknowledgementRetainsActualRedoContent() {
        var history = EditorSaveHistory(submitted: "submitted")
        history.appendUndo("submitted")
        history.recordUndo(to: "submitted", from: "late edit")
        XCTAssertEqual(history.undoCheckpoints(current: "submitted"), [])
        XCTAssertEqual(history.redoCheckpoints, ["late edit"])
        history.appendUndo("submitted", isRedo: true)
        XCTAssertEqual(history.undoCheckpoints(current: "late edit"), ["submitted"])
        XCTAssertEqual(history.redoCheckpoints, [])
    }

    func testUndoOfWorkSubmittedBeforeTheSaveCanBeUndoneAfterAcknowledgement() {
        var history = EditorSaveHistory(submitted: "submitted")
        history.recordUndo(to: "older", from: "submitted")
        XCTAssertEqual(history.undoCheckpoints(current: "older"), ["submitted"])
        XCTAssertEqual(history.redoCheckpoints, ["submitted"])
    }

    func testContinuedGestureWithoutANewCheckpointFallsBackToSubmittedContent() {
        let history = EditorSaveHistory(submitted: "submitted")
        XCTAssertEqual(history.undoCheckpoints(current: "continued gesture"), ["submitted"])
        XCTAssertEqual(history.undoCheckpoints(current: "submitted"), [])
    }

    func testCancelledGestureRestoresTheJournalRedoHistory() {
        var history = EditorSaveHistory(submitted: 0)
        history.appendUndo(0)
        history.recordUndo(to: 0, from: 1)
        history.appendUndo(0)
        history.cancelLastCheckpoint(matching: 0)
        XCTAssertEqual(history.undoCheckpoints(current: 0), [])
        XCTAssertEqual(history.redoCheckpoints, [1])
    }

    func testEachSaveHasIndependentOwnershipAndBoundedRedoHistory() {
        var first = EditorSaveHistory(submitted: 100, limit: 2)
        let second = EditorSaveHistory(submitted: 100, limit: 2)
        XCTAssertNotEqual(first.id, second.id)
        first.recordUndo(to: 99, from: 100)
        first.recordUndo(to: 98, from: 99)
        first.recordUndo(to: 97, from: 98)
        XCTAssertEqual(first.redoCheckpoints, [99, 98])
        XCTAssertEqual(second.redoCheckpoints, [])
    }

    func testUndoingAllRetainedCheckpointsDoesNotInventAnOlderStepPastTheCap() {
        var history = EditorSaveHistory(submitted: 0, limit: 2)
        for value in 0..<3 { history.appendUndo(value) }
        history.recordUndo(to: 2, from: 3)
        history.recordUndo(to: 1, from: 2)
        XCTAssertEqual(history.undoCheckpoints(current: 1), [])
        XCTAssertEqual(history.redoCheckpoints, [3, 2])
    }
}
