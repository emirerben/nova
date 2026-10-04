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

    // MARK: Drawer list payload (the real GET /creation-threads shape)

    /// What `list_threads` returns for a READY slide post: `events: []`, a URL-free job whose variants carry only
    /// `variant_id` + `render_status`, and `state` copied from the row.
    private func listedSummary(state: [String: Any], variants: [[String: Any]]) throws -> ProjectSummary {
        let id = UUID().uuidString
        let object: [String: Any] = [
            "id": id, "title": "Weekend trip", "status": "active", "revision": 3, "runtime_version": 2,
            "state": state, "events": [], "active_plan_item_id": id, "active_job_id": id,
            "job": ["id": id, "status": "ready", "variants": variants],
            "updated_at": "2026-10-04T10:00:00Z"
        ]
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: JSONSerialization.data(withJSONObject: object)).summary
    }

    func testListedSlidePostWithStateOrVariantIsRecognisedBeforeThreadLoads() throws {
        let byState = try listedSummary(state: ["format": "slides", "edit_format": "slides"], variants: [])
        XCTAssertEqual(byState.status, .ready)
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: byState, thread: nil))
        let byVariant = try listedSummary(state: [:], variants: [["variant_id": "slides", "render_status": "ready"]])
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: byVariant, thread: nil))
    }

    /// The gap: a READY project whose list row carries NO format signal. Before the fix `isSlidePost` is false and the
    /// generic chat (Chat|Editor switch, Open editor, Open current cut) was reachable, i.e. NativeEditorView could open.
    func testReadyProjectWithoutAnyFormatSignalExposesNoVideoEditorEntry() throws {
        let bare = try listedSummary(state: [:], variants: [])
        XCTAssertEqual(bare.status, .ready)
        XCTAssertFalse(SlidePostRouting.isSlidePost(selectedFormat: nil, project: bare, thread: nil))
        XCTAssertFalse(SlidePostRouting.isFormatKnown(selectedFormat: nil, project: bare, thread: nil))
        XCTAssertFalse(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: bare, thread: nil))
        // Once the full thread loads and says slides, the destination is the slide workspace.
        let t = try thread(state: ["format": "slides"])
        XCTAssertTrue(SlidePostRouting.isSlidePost(selectedFormat: nil, project: bare, thread: t))
        XCTAssertFalse(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: bare, thread: t))
    }

    func testVideoEditorOpensOnlyForKnownNonSlideProjects() throws {
        let montage = try thread(state: ["format": "montage"])
        XCTAssertTrue(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: project(), thread: montage))
        XCTAssertTrue(SlidePostRouting.canOpenVideoEditor(selectedFormat: .montage, project: project(), thread: nil))
        XCTAssertTrue(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: project(variant: "original_text"), thread: nil))
        XCTAssertFalse(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: project(variant: "slides"), thread: nil))
        XCTAssertFalse(SlidePostRouting.canOpenVideoEditor(selectedFormat: .slides, project: project(), thread: montage))
    }

    func testSwiftDataCachePersistsTheFormat() throws {
        let cached = CachedProject(id: UUID(), title: "t", status: .ready, updatedAt: .now, posterURL: nil)
        cached.editFormat = "slides"; cached.outputVariantID = "slides"
        XCTAssertTrue(cached.summary.isSlidePost)
    }

    // MARK: Screen matrix (post-Back, cold open, loading)

    func testScreenMatrixNeverShowsGenericChatOrVideoForASlidePost() throws {
        let slideThread = try thread(state: ["format": "slides"])
        // Signals from any one source land in the slide workspace.
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: .slides, project: project(), thread: nil, isChoosingFormat: false), .slideWorkspace)
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: nil, project: project(editFormat: "slides"), thread: nil, isChoosingFormat: false), .slideWorkspace)
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: nil, project: project(), thread: slideThread, isChoosingFormat: false), .slideWorkspace)
        // Back no longer flips the screen: the format-chooser flag is never set for a slide post.
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: .slides, project: project(variant: "slides"), thread: slideThread, isChoosingFormat: false), .slideWorkspace)
    }

    func testStartedProjectWithNoSignalShowsNeutralLoadingNotGenericChat() throws {
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: nil, project: project(), thread: nil, isChoosingFormat: false), .resolvingFormat)
        // Once the thread proves it is not a slide post, the normal chat appears.
        let montage = try thread(state: ["format": "montage"])
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: nil, project: project(), thread: montage, isChoosingFormat: false), .genericChat)
        XCTAssertFalse(SlidePostRouting.canOpenVideoEditor(selectedFormat: nil, project: project(), thread: nil), "no video entry while unknown")
    }

    func testNewDraftChatStillShowsTheFormatChooser() {
        let draft = ProjectSummary(id: UUID(), title: "New", status: .draft, updatedAt: .now, posterURL: nil)
        XCTAssertEqual(SlidePostRouting.screen(selectedFormat: nil, project: draft, thread: nil, isChoosingFormat: false), .genericChat)
    }
}
