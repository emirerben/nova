import XCTest
@testable import Kria

/// KRI-1165: opening the native editor from a chat rebases the runtime draft onto the status variant.
/// The variant owns the Talk-to-camera background, so the rebased document must show it (and must not
/// revive a cleared one from an older draft copy).
final class SpeakerBackgroundDraftRebaseTests: XCTestCase {
    private func draft(sections: [String: JSONValue]) -> DraftSnapshot {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["base_generation": .string("old"), "sections": .object(sections)]),
        ]
        return DraftSnapshot(
            draftID: "draft", itemID: "item", variantKey: "subtitled", draftRevision: 1,
            snapshotHash: "hash", etag: "etag", baseJobID: nil, baseGenerationID: "old",
            snapshot: snapshot, canUndo: false, createdAt: .now
        )
    }

    private func variant(_ extra: [String: JSONValue]) -> [String: JSONValue] {
        var value: [String: JSONValue] = [
            "render_generation_id": .string("fresh"),
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
        ]
        for (key, entry) in extra { value[key] = entry }
        return value
    }

    func testStatusVariantSpeakerBackgroundReachesTheRebasedDocument() {
        let authoritative = variant([
            "speaker_background": .object([
                "media_id": .string("visual-1"), "kind": .string("image"), "source_start_s": .number(0),
            ]),
        ])
        let rebased = draft(sections: ["caption_cues": .array([])])
            .editorDraft(projectID: UUID(), authoritativeVariant: authoritative)
        let document = EditorDocument.decode(snapshot: rebased.serverSnapshot)

        XCTAssertEqual(document.speakerBackground?.mediaID, "visual-1")
        XCTAssertEqual(document.speakerBackground?.kind, .image)
    }

    func testVariantWithoutSpeakerBackgroundDropsAStaleDraftCopy() {
        let stale: JSONValue = .object([
            "media_id": .string("stale-visual"), "kind": .string("image"), "source_start_s": .number(0),
        ])
        let rebased = draft(sections: ["speaker_background": stale])
            .editorDraft(projectID: UUID(), authoritativeVariant: variant([:]))
        let document = EditorDocument.decode(snapshot: rebased.serverSnapshot)

        XCTAssertNil(document.speakerBackground)
    }
}
