import XCTest
@testable import Kria

/// Deleting a selected text bar from the native editor (KRI-219).
@MainActor
final class TextDeletionTests: XCTestCase {
    private static let jobID = "5b1d2f0a-6c0e-4b8e-9d0a-1c2d3e4f5a6b"

    private static func text(_ id: String, _ text: String, role: String = "generative_intro", extra: [String: JSONValue] = [:]) -> JSONValue {
        var row: [String: JSONValue] = ["id": .string(id), "text": .string(text), "start_s": .number(0), "end_s": .number(2),
                                        "role": .string(role), "x_frac": .number(0.5), "y_frac": .number(0.2)]
        for (key, value) in extra { row[key] = value }
        return .object(row)
    }

    private static func variant() -> [String: JSONValue] {
        [
            "variant_id": .string("initial"), "render_generation_id": .string("g1"), "render_status": .string("ready"),
            "duration_s": .number(4), "output_url": .string("file:///tmp/kria-delete.mp4"),
            "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "caption_cues": .bool(true)]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("s1"), "clip_index": .number(0), "in_s": .number(0),
                "duration_s": .number(4), "source_duration_s": .number(6), "removed": .bool(false)])])]),
            "text_elements": .array([
                text("guided-title", "My title"),
                text("clip-label-unified-cut-1", "11"),
                text("caption-1", "spoken words", extra: ["source_params": .object(["source": .string("caption_cue")])]),
                text("card-text", "card", extra: ["visual_block_id": .string("block-1")]),
                text("lyric_L7", "la la", role: "lyric_line", extra: ["source_params": .object(["source": .string("lyric"), "key": .string("L7")])]),
            ]),
            "visual_blocks": .array([
                .object(["id": .string("block-1"), "kind": .string("text_card"), "start_s": .number(0), "end_s": .number(2),
                         "text_element_id": .string("card-text")]),
            ]),
        ]
    }

    private func snapshot(_ payload: [String: JSONValue]? = nil, revision: Int = 1) -> DraftSnapshot {
        DraftSnapshot(draftID: "d", itemID: "item", variantKey: "initial", draftRevision: revision, snapshotHash: "h", etag: "e",
            baseJobID: Self.jobID, baseGenerationID: "g1",
            snapshot: payload.map { ["kind": .string("editor"), "editor_payload": .object($0)] } ?? [:], canUndo: false, createdAt: .now)
    }

    private func loaded(_ draft: DraftSnapshot? = nil) async -> (NativeEditorSession, EditorCommitSpy) {
        let fake = EditorCommitSpy(draftSnapshot: draft ?? snapshot(), authoritativeVariant: Self.variant(),
            commitResponse: EditorCommitResponse(ok: true, generation: "g2",
                sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
                revisionNumber: 2, revisionHash: "r", expectedDuration: nil))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        return (session, fake)
    }

    private func ids(_ session: NativeEditorSession) -> [String] { session.document.textElements.map(\.id) }

    func testDeleteRemovesTextClearsSelectionIsDirtyAndUndoable() async throws {
        let (session, _) = await loaded()
        session.select(EditorSelection(kind: .text, id: "guided-title"))
        XCTAssertTrue(session.deleteText(id: "guided-title"))
        XCTAssertFalse(ids(session).contains("guided-title"))
        XCTAssertNil(session.selection)
        XCTAssertEqual(session.dirtySections, [.text])
        XCTAssertFalse(session.timelineItems.contains { $0.selection.id == "guided-title" }, "timeline bar disappears immediately")
        session.undo()
        XCTAssertTrue(ids(session).contains("guided-title"), "one undo step restores it")
        XCTAssertFalse(session.hasUnsavedChanges)
        session.redo()
        XCTAssertFalse(ids(session).contains("guided-title"))
    }

    func testSaveDropsTheElementFromTheTextLaneOnly() async throws {
        let (session, fake) = await loaded()
        XCTAssertTrue(session.deleteText(id: "clip-label-unified-cut-1"))
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        let sent = (request.textElements ?? []).compactMap { value -> String? in
            if case let .object(row) = value, case let .string(id)? = row["id"] { return id }
            return nil
        }
        XCTAssertFalse(sent.contains("clip-label-unified-cut-1"), "server derives user_removed from the missing id")
        XCTAssertTrue(sent.contains("guided-title"))
        XCTAssertNil(request.timelineSlots)
        XCTAssertNil(request.captionCues)
        XCTAssertNil(request.mix)
        XCTAssertEqual(request.editorStateVersion, 1)
        XCTAssertEqual(request.deletions, [EditorDeletion(kind: "text", id: "clip-label-unified-cut-1")])
        XCTAssertTrue(session.document.deletions.isEmpty, "successful Save consumes pending deletion intents")
    }

    func testDeletionRules() async throws {
        let (session, _) = await loaded()
        XCTAssertEqual(session.textDeletion(id: "guided-title"), .allowed)
        XCTAssertEqual(session.textDeletion(id: "clip-label-unified-cut-1"), .allowed)
        XCTAssertTrue(session.deleteText(id: "caption-1"), "caption deletion is allowed when the caption lane is editable")
        session.undo()
        XCTAssertTrue(session.deleteText(id: "lyric_L7"), "generated lyric lines use stable suppression IDs")
        XCTAssertEqual(session.document.deletions, [EditorDeletion(kind: "lyric_line", id: "L7")])
        session.undo()
        XCTAssertTrue(session.deleteText(id: "card-text"), "linked text can be removed without deleting its card")
        XCTAssertFalse(ids(session).contains("card-text"))
        XCTAssertTrue(session.document.visualBlocks.contains { $0.id == "block-1" }, "the card remains")
        XCTAssertNil(session.document.visualBlocks.first { $0.id == "block-1" }?.raw["text_element_id"], "the card no longer points at deleted text")
        XCTAssertTrue(session.document.deletions.contains(EditorDeletion(kind: "text", id: "card-text")))
        session.undo()
        XCTAssertTrue(ids(session).contains("card-text"))
        XCTAssertEqual(session.document.visualBlocks.first { $0.id == "block-1" }?.raw["text_element_id"], .string("card-text"))
        XCTAssertFalse(session.textDeletion(id: "missing").isAllowed)
        XCTAssertFalse(session.deleteText(id: "missing"))
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(ids(session).count, 5)
    }

    func testManualDeleteOverChatStagedEditsIsALocalEditAndAChatResurrectionConflicts() async throws {
        let chatText: JSONValue = .array([Self.text("guided-title", "chat title"), Self.text("clip-label-unified-cut-1", "11")])
        let chat = snapshot(["base_generation": .string("g1"), "text_elements": chatText], revision: 2)
        let (session, fake) = await loaded(chat)
        XCTAssertTrue(session.hasOnlyChatStagedChanges)
        XCTAssertTrue(session.deleteText(id: "clip-label-unified-cut-1"))
        XCTAssertFalse(session.hasOnlyChatStagedChanges, "a manual delete is a local edit: the next send flushes it")
        // A newer chat draft that still contains the deleted element must not silently bring it back.
        fake.draftSnapshot = snapshot(["base_generation": .string("g1"),
            "text_elements": .array([Self.text("guided-title", "newer"), Self.text("clip-label-unified-cut-1", "11")])], revision: 3)
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertFalse(ids(session).contains("clip-label-unified-cut-1"))
    }
}
