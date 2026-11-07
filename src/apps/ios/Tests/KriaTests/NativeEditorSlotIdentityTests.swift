import XCTest
@testable import Kria

final class NativeEditorSlotIdentityTests: XCTestCase {
    private let project = UUID(uuidString: "00000000-0000-4000-8000-000000000715")!

    private func snapshot(_ slots: [[String: JSONValue]]) -> DraftSnapshot {
        DraftSnapshot(draftID: "identity", itemID: "item", variantKey: "initial", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: "g1",
            snapshot: ["editor_payload": .object(["sections": .object([
                "timeline_slots": .array(slots.map(JSONValue.object)),
                "text_elements": .array([.object(["id": .string("caption"), "text": .string("before"),
                    "start_s": .number(0), "end_s": .number(1)])])
            ])])], canUndo: false, createdAt: .distantPast)
    }

    private func slot(_ id: String, index: Int = 0, asset: UUID? = nil) -> [String: JSONValue] {
        var row: [String: JSONValue] = ["slot_id": .string(id), "clip_index": .number(Double(index)),
            "in_s": .number(0), "duration_s": .number(2)]
        if let asset { row["asset_id"] = .string(asset.uuidString) }
        return row
    }

    func testRepeatedOpaqueSlotHydrationPreservesClipAndSyntheticAssetIdentity() throws {
        let source = snapshot([slot("opaque/server/slot")])
        let first = source.editorDraft(projectID: project)
        let second = source.editorDraft(projectID: project)
        XCTAssertEqual(first.clips, second.clips)
        let clip = try XCTUnwrap(first.clips.first)
        XCTAssertEqual(clip.id, clip.assetID)
        XCTAssertEqual(clip.slotID, "opaque/server/slot")
    }

    func testOpaqueIdentitiesSurviveReorderingAndRemainProjectScoped() {
        let first = snapshot([slot("a"), slot("b", index: 1)]).editorDraft(projectID: project)
        let reordered = snapshot([slot("b", index: 1), slot("a")]).editorDraft(projectID: project)
        XCTAssertEqual(first.clips.map(\.id), reordered.clips.reversed().map(\.id))
        XCTAssertEqual(first.clips.map(\.assetID), reordered.clips.reversed().map(\.assetID))
        XCTAssertNotEqual(first.clips[0].id, first.clips[1].id)
        let other = snapshot([slot("a")]).editorDraft(projectID: UUID())
        XCTAssertNotEqual(first.clips[0].id, other.clips[0].id)
    }

    func testExplicitUUIDClipAndAssetMetadataRemainAuthoritative() {
        let id = UUID(), slotID = UUID(), asset = UUID()
        var row = slot(slotID.uuidString, asset: asset)
        row["id"] = .string(id.uuidString)
        let clip = snapshot([row]).editorDraft(projectID: project).clips[0]
        XCTAssertEqual(clip.id, id)
        XCTAssertEqual(clip.assetID, asset)
    }

    func testMissingIdentityIsNotCollapsedIntoOneSyntheticClip() {
        let anonymous = snapshot([slot(""), slot("", index: 1)]).editorDraft(projectID: project)
        XCTAssertNotEqual(anonymous.clips[0].id, anonymous.clips[1].id)
        XCTAssertNotEqual(anonymous.clips[0].assetID, anonymous.clips[1].assetID)
    }

    func testEmptySlotIDDoesNotHideAValidOpaqueCompatibilityID() {
        var row = slot("")
        row["id"] = .string("legacy-opaque-id")
        let source = snapshot([row])
        let first = source.editorDraft(projectID: project).clips[0]
        let second = source.editorDraft(projectID: project).clips[0]
        XCTAssertEqual(first.id, second.id)
        XCTAssertEqual(first.assetID, second.assetID)
        XCTAssertNotEqual(first.id, snapshot([slot("another-slot")]).editorDraft(projectID: project).clips[0].id)
    }

    @MainActor func testUnchangedRehydrationDoesNotConflictWithDirtyText() {
        let source = snapshot([slot("opaque")])
        let first = NativeEditorSession(draft: source.editorDraft(projectID: project))
        let reopened = NativeEditorSession(draft: source.editorDraft(projectID: project))
        XCTAssertEqual(first.document.clips, reopened.document.clips)
        var local = first.document
        local.textElements[0].text = "local edit"
        let result = NativeEditorMerge.documents(base: first.document, local: local,
            remote: reopened.document, baseSong: nil, remoteSong: nil)
        guard case .resolved(let merged) = result else {
            return XCTFail("Identical hydration must not manufacture a remote timeline change")
        }
        XCTAssertEqual(merged.textElements[0].text, "local edit")
    }

    @MainActor func testRealAssetReplacementStillConflictsWithDependentDirtyText() {
        let first = NativeEditorSession(draft: snapshot([slot("opaque", asset: UUID())]).editorDraft(projectID: project))
        let replaced = NativeEditorSession(draft: snapshot([slot("opaque", asset: UUID())]).editorDraft(projectID: project))
        var local = first.document
        local.textElements[0].text = "local edit"
        let result = NativeEditorMerge.documents(base: first.document, local: local,
            remote: replaced.document, baseSong: nil, remoteSong: nil)
        guard case .conflicts = result else {
            return XCTFail("A real source replacement must remain a creator decision")
        }
    }
}
