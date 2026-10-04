import XCTest
@testable import Kria

/// A slide post must never open the video editor, whichever signal is missing.
final class SlidePostRoutingTests: XCTestCase {
    private func thread(state: [String: Any]? = nil, planItemID: String? = nil) throws -> CreationThread {
        var object: [String: Any] = [
            "id": UUID().uuidString, "title": "Trip", "status": "ready", "revision": 4,
            "runtime_version": 2, "updated_at": "2026-10-04T10:00:00Z"
        ]
        if let state { object["state"] = state }
        if let planItemID { object["active_plan_item_id"] = planItemID }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func project(editFormat: String? = nil, variant: String? = nil) -> ProjectSummary {
        ProjectSummary(id: UUID(), title: "Trip", status: .ready, updatedAt: .now, posterURL: nil,
                       outputVariantID: variant, editFormat: editFormat)
    }

    func testReadySlidePostWithNilPlanItemAndNilSelectedFormatIsSlidePost() throws {
        // Thread state says slides, but active_plan_item_id is nil and the local format is not yet set.
        let t = try thread(state: ["format": "slides", "edit_format": "slides"], planItemID: nil)
        XCTAssertNil(t.activePlanItemID)
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: project(), thread: t))
    }

    func testProjectRowAloneIdentifiesSlidePostBeforeThreadLoads() {
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: project(editFormat: "slides"), thread: nil))
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: project(variant: "slides"), thread: nil))
    }

    func testStaleSelectedFormatCannotHideASlidePost() throws {
        let t = try thread(state: ["format": "slides"])
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: .montage, project: project(), thread: t))
    }

    func testMontageIsNotASlidePost() throws {
        let t = try thread(state: ["format": "montage", "edit_format": "montage"], planItemID: "item")
        XCTAssertFalse(SlidePostRouting.isSlidePost(selectedFormat: .montage, project: project(editFormat: "montage"), thread: t))
        XCTAssertFalse(SlidePostRouting.isSlidePost(selectedFormat: nil, project: project(), thread: nil))
    }

    func testOpenEditorTargetSelection() {
        XCTAssertEqual(SlidePostRouting.editorDestination(isSlidePost: true), .slideWorkspace)
        XCTAssertEqual(SlidePostRouting.editorDestination(isSlidePost: false), .videoEditor)
    }
}
