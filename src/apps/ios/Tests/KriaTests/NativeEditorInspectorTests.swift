import XCTest
@testable import Kria

#if DEBUG
@MainActor
final class NativeEditorInspectorTests: XCTestCase {
    func testOpaqueTextSelectionEditsAndUndoAsOneLocalTransaction() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.twoText)
        let id = NativeEditorUITestFixtures.twoText.serverSnapshotTextID(at: 0)
        session.select(EditorSelection(kind: .text, id: id), seekToStart: false)
        session.updateTextContent(id: id, content: "Edited opening")

        XCTAssertEqual(session.document.textElements.first(where: { $0.id == id })?.text, "Edited opening")
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.textElements.first(where: { $0.id == id })?.text, "Opening question")
        XCTAssertTrue(session.canRedo)
    }

    func testSelectedClipDeleteIsUndoableAndLeavesSaveDirty() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        let clip = try! XCTUnwrap(session.draft.clips.first)
        session.select(EditorSelection(kind: .clip, id: clip.id.uuidString), seekToStart: false)
        session.removeClip(clipID: clip.slotID ?? clip.id.uuidString)

        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.document.tombstones.count, 1)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.clips.count, 3)
        XCTAssertTrue(session.canRedo)
    }
}

private extension EditorDraft {
    func serverSnapshotTextID(at index: Int) -> String {
        documentTextIDs()[index]
    }

    func documentTextIDs() -> [String] {
        guard case let .object(payload) = serverSnapshot["editor_payload"],
              case let .object(sections) = payload["sections"],
              case let .array(records) = sections["text_elements"] else { return text.map { $0.id.uuidString } }
        return records.compactMap { record in
            guard case let .object(value) = record else { return nil }
            return value["id"]?.stringValue
        }
    }
}
#endif
