import XCTest
@testable import Kria

@MainActor final class SlidePostTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var defaults: UserDefaults!
    private var suite: String!
    override func setUp() {
        super.setUp(); suite = "SlidePostTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite); defaults = nil
        super.tearDown()
    }
    private func fixture(version: Int = 1, rendered: Bool = true) -> SlidePostState {
        let refs = (1...2).map { SlidePostSlide(id: "slide-\($0)", assetID: "asset-\($0)", kind: $0 == 2 ? "video" : "image") }
        let draft = SlidePostDraft(version: version, platformProfile: "instagram_carousel", slides: refs, caption: "A day outside", renderedVersion: rendered ? version : nil)
        return SlidePostState(itemID: itemID, title: "A day outside", jobID: UUID(uuidString: "22222222-2222-2222-2222-222222222222"), draft: draft,
            assets: refs.map { .init(id: $0.assetID, kind: $0.kind, status: "ready", sourceURL: URL(string: "https://storage.test/source"), durationS: $0.kind == "video" ? 8 : nil, mediaStatus: "available") },
            renderStatus: rendered ? "ready" : "rendering", renderedVersion: rendered ? version : nil,
            slides: refs.map { .init(id: $0.id, assetID: $0.assetID, kind: $0.kind, url: rendered ? URL(string: "https://storage.test/\($0.id)") : nil) }, bundleURL: rendered ? URL(string: "https://storage.test/bundle.zip") : nil)
    }
    func testExportRequiresExactCurrentCompleteOrderedAssets() {
        let good = fixture(); XCTAssertTrue(good.canExport)
        var changed = good; changed.renderedVersion = 0; XCTAssertFalse(changed.canExport)
        changed = good; changed.slides.removeLast(); XCTAssertFalse(changed.canExport)
        changed = good; changed.slides.reverse(); XCTAssertFalse(changed.canExport)
        changed = good; changed.slides[0].url = nil; XCTAssertFalse(changed.canExport)
        changed = good; changed.slides[0].url = URL(string: "http://storage.test/slide"); XCTAssertFalse(changed.canExport)
        changed = good; changed.validationErrors = [.init(code: "missing", message: "Slide unavailable")]; XCTAssertFalse(changed.canExport)
        changed = good; changed.renderStatus = "rendering"; XCTAssertFalse(changed.canExport)
    }
    func testValidationRespectsMediaProfileAndTextLimits() throws {
        var draft = try XCTUnwrap(fixture().draft)
        XCTAssertNil(draft.validationMessage)
        draft.platformProfile = "tiktok_photo"; XCTAssertNotNil(draft.validationMessage)
        draft.platformProfile = "instagram_carousel"
        draft.slides[0].edits = .init(text: .init(content: String(repeating: "a", count: 121), position: "top"))
        XCTAssertNotNil(draft.validationMessage)
        draft.slides[0].edits = .init(text: .init(content: "Welcome", position: "top"), lookPreset: "golden_hour")
        XCTAssertNil(draft.validationMessage)
        draft.coverIndex = 2; XCTAssertNotNil(draft.validationMessage)
    }
    func testAuthenticatedProposalIsReadOnlyUntilExplicitAcceptance() async throws {
        let saved = fixture()
        let response = SlidePostProposal(draft: try XCTUnwrap(saved.draft), baseVersion: 1, fallbackUsed: false, summary: "Review the order")
        var paths: [String] = []
        NativeEditorURLProtocol.handler = { request in
            paths.append(request.url!.path)
            if request.httpMethod == "POST" {
                XCTAssertEqual(request.url?.path, "/plan-items/\(self.itemID)/slide-post/propose")
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                XCTAssertEqual(body["expected_version"] as? Int, 1)
                XCTAssertEqual(body["instruction"] as? String, "Lead with the landscape")
                XCTAssertNil(body["gcs_path"])
                return (200, try JSONEncoder().encode(response))
            }
            return (200, try JSONEncoder().encode(saved))
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        await session.propose(api: api, itemID: itemID, instruction: "Lead with the landscape")
        XCTAssertEqual(session.draft, saved.draft)
        XCTAssertNotNil(session.proposal)
        XCTAssertEqual(paths.count, 2)
        XCTAssertFalse(session.hasUnsavedChanges)
    }
    func testSaveSendsExpectedVersionAndNeverServerOwnedMetadata() async throws {
        let saved = fixture(version: 3)
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["expected_version"] as? Int, 2)
            XCTAssertNil(body["version"]); XCTAssertNil(body["rendered_version"])
            XCTAssertNil(body["user_edited"]); XCTAssertNil(body["job_id"])
            return (200, try JSONSerialization.data(withJSONObject: ["slide_post": JSONSerialization.jsonObject(with: JSONEncoder().encode(saved.draft!))]))
        }
        let draft = try await NativeEditorTestSupport.api().saveSlidePost(itemID: itemID, request: .init(draft: saved.draft!, expectedVersion: 2))
        XCTAssertEqual(draft.version, 3)
    }
    func testPollingKeepsLocalEditsAndMarksRemoteConflict() async throws {
        var remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let session = SlidePostSession(defaults: defaults)
        let api = NativeEditorTestSupport.api()
        await session.refresh(api: api, itemID: itemID)
        session.draft?.caption = "My unsaved caption"
        await session.refresh(api: api, itemID: itemID)
        XCTAssertEqual(session.draft?.caption, "My unsaved caption")
        XCTAssertFalse(session.canExport)
        remote = fixture(version: 2)
        await session.refresh(api: api, itemID: itemID)
        XCTAssertTrue(session.hasConflict)
        XCTAssertEqual(session.draft?.caption, "My unsaved caption")
        session.discardLocalChanges()
        XCTAssertFalse(session.hasConflict)
        XCTAssertEqual(session.draft?.version, 2)
        XCTAssertTrue(session.canExport)
    }
    func testReorderKeepsCoverIdentityAndNavigationKeepsInstruction() async throws {
        let remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.selectedID = "slide-2"
        session.instruction = "Keep the final sunset"
        session.moveSlide(id: "slide-1", offset: 1)
        XCTAssertEqual(session.draft?.coverIndex, 1)
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertEqual(reopened.instruction, "Keep the final sunset")
        XCTAssertEqual(reopened.selectedID, "slide-2")
        XCTAssertEqual(reopened.draft?.slides.first?.id, "slide-2")
        XCTAssertFalse(reopened.hasConflict)
    }
    func testCleanRestoredDraftAdoptsNewServerVersionWithoutConflict() async throws {
        var remote = fixture()
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(remote)) }
        let api = NativeEditorTestSupport.api()
        let first = SlidePostSession(defaults: defaults)
        await first.refresh(api: api, itemID: itemID)
        first.instruction = "Keep this prompt"
        remote = fixture(version: 2)
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertFalse(reopened.hasConflict)
        XCTAssertEqual(reopened.draft?.version, 2)
        XCTAssertEqual(reopened.instruction, "Keep this prompt")
    }
    func testFailedVersionedSavePreservesLocalDraft() async throws {
        let remote = fixture()
        NativeEditorURLProtocol.handler = { request in
            request.httpMethod == "PUT" ? (409, Data(#"{"detail":{"code":"slide_post_version_conflict","current_version":2}}"#.utf8)) : (200, try JSONEncoder().encode(remote))
        }
        let api = NativeEditorTestSupport.api()
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: api, itemID: itemID)
        session.draft?.caption = "Keep this edit"
        await session.save(api: api, itemID: itemID)
        XCTAssertTrue(session.hasConflict)
        XCTAssertEqual(session.draft?.caption, "Keep this edit")
        XCTAssertFalse(session.canExport)
    }
    func testGalleryKeepsSlideItemIdentity() async throws {
        NativeEditorURLProtocol.handler = { _ in
            (200, Data(#"{"jobs":[{"id":"22222222-2222-2222-2222-222222222222","title":"Weekend","status":"ready","output_variant_id":"slides","content_plan_item_id":"11111111-1111-1111-1111-111111111111","created_at":"2026-09-22T12:00:00Z"}],"next_cursor":null}"#.utf8))
        }
        let rows = try await NativeEditorTestSupport.api().library()
        XCTAssertTrue(try XCTUnwrap(rows.first).isSlidePost)
        XCTAssertEqual(rows.first?.activePlanItemID, itemID)
    }
}
