import Foundation
import XCTest
@testable import Kria

/// The HTTP commit transport is captured; parsing, editing, Save payload
/// construction, acknowledgment and reopening use the real app session.
@MainActor
final class CreationWordSaveTests: XCTestCase {
    func testLaterStyleEditAndSavePreserveCreatedWordSequenceOnReopen() async throws {
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Fixtures/KRI524CreationDraft.json")
        let draft = try JSONDecoder().decode(EditorDraft.self, from: Data(contentsOf: fixtureURL))
        let payload = try XCTUnwrap(draft.serverSnapshot["editor_payload"]?.objectValue)
        let sections = try XCTUnwrap(payload["sections"]?.objectValue)
        let original = try XCTUnwrap(sections["text_elements"]?.arrayValue)
        let slots = try XCTUnwrap(sections["timeline_slots"]?.arrayValue)
        let threadID = UUID(), jobID = UUID()
        var variant: [String: JSONValue] = [
            "variant_id": .string("guided_story"), "resolved_archetype": .string("guided_story"),
            "render_generation_id": .string("g1"), "render_status": .string("ready"),
            "render_destination": .string("device"), "duration_s": .number(20),
            "editor_capabilities": .object(["text_elements": .bool(true), "timeline": .bool(true)]),
            "text_elements": .array(original), "user_timeline": .object(["slots": .array(slots)]),
        ]
        func stored(_ snapshot: [String: JSONValue], generation: String) -> DraftSnapshot {
            DraftSnapshot(draftID: "created-words", itemID: "item", variantKey: "guided_story",
                          draftRevision: 1, snapshotHash: "fixture", etag: "fixture", baseJobID: jobID.uuidString,
                          baseGenerationID: generation, snapshot: snapshot, canUndo: false, createdAt: .now)
        }
        let transport = EditorCommitSpy(draftSnapshot: stored(draft.serverSnapshot, generation: "g1"),
                                        authoritativeVariant: variant)
        let session = NativeEditorSession()
        await session.load(api: transport, threadID: threadID)
        session.setTextColor(id: "guided-title::sequence-1", color: "#FFCC00")
        XCTAssertTrue(session.hasUnsavedChanges)
        transport.commitResponse = EditorCommitResponse(ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "saved-words", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(transport.commitCount, 1)
        let sent = try XCTUnwrap(transport.lastRequest?.textElements)
        XCTAssertNil(transport.lastRequest?.timelineSlots, "A text style edit must not replace the footage")
        XCTAssertNil(transport.lastRequest?.mix, "A text style edit must not change audio")
        XCTAssertEqual(sent.count, 14)
        XCTAssertEqual(original.count, sent.count)
        let words = sent.prefix(12).compactMap { $0.objectValue?["text"]?.stringValue }
        XCTAssertEqual(words, "Join us for our favorite bakery and tea shop near the harbor".components(separatedBy: " "))
        for (before, after) in zip(original, sent) {
            for key in ["id", "text", "start_s", "end_s", "animation_phases", "x_frac", "y_frac"] {
                XCTAssertEqual(before.objectValue?[key], after.objectValue?[key], "Save changed \(key)")
            }
        }
        XCTAssertEqual(sent.first?.objectValue?["color"], .string("#FFCC00"))
        XCTAssertEqual(sent[11].objectValue?["end_s"], .number(5))
        XCTAssertFalse(session.hasUnsavedChanges)

        // The captured server acknowledges precisely the submitted text lane;
        // the app must reconstruct the same sequence from that saved response.
        variant["text_elements"] = .array(sent)
        variant["render_generation_id"] = .string("g2")
        transport.authoritativeVariant = variant
        let saved: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g2"),
            "sections": .object(["text_elements": .array(sent), "timeline_slots": .array(slots)]),
        ])]
        transport.draftSnapshot = stored(saved, generation: "g2")
        let reopened = NativeEditorSession()
        await reopened.load(api: transport, threadID: threadID)
        let reopenedSections = try XCTUnwrap(reopened.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue)
        let restored = try XCTUnwrap(reopenedSections["text_elements"]?.arrayValue)
        for (before, after) in zip(sent, restored) {
            for key in ["id", "text", "start_s", "end_s", "animation_phases", "x_frac", "y_frac", "color"] {
                XCTAssertEqual(before.objectValue?[key], after.objectValue?[key], "Reopening changed \(key)")
            }
        }
        XCTAssertEqual(restored.count, sent.count)
        XCTAssertFalse(reopened.hasUnsavedChanges)
    }
}
