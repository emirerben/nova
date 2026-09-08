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
