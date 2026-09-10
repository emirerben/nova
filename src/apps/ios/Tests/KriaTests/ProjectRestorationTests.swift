import XCTest
import SwiftData
@testable import Kria

@MainActor final class ProjectRestorationTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testWorkspaceRestoresPreferredProjectWithAuthoritativeTitleAndTranscript() async throws {
        let preferredID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let otherID = UUID(uuidString: "74A559F6-28D9-4E0D-9EB6-71CD09A958DE")!
        let list = Data("""
        [
          {"id":"\(otherID)","title":"Newest project","status":"draft","revision":1,"runtime_version":2,"updated_at":"2026-09-10T12:00:00Z"},
          {"id":"\(preferredID)","title":"Renamed on another device","status":"draft","revision":5,"runtime_version":2,"updated_at":"2026-09-09T12:00:00Z"}
        ]
        """.utf8)
        let full = Data("""
        {"id":"\(preferredID)","title":"Renamed on another device","status":"draft","revision":5,"runtime_version":2,"updated_at":"2026-09-09T12:00:00Z","events":[
          {"id":"message-1","sequence":1,"revision":2,"role":"user","event_type":"user_message","content":"Keep the opening quiet.","created_at":"2026-09-09T12:00:00Z"},
          {"id":"message-2","sequence":2,"revision":3,"role":"assistant","event_type":"assistant_response","content":"I will preserve the natural sound.","created_at":"2026-09-09T12:00:01Z"}
        ]}
        """.utf8)
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            if request.url?.path == "/creation-threads" { return (200, list) }
            XCTAssertEqual(request.url?.path, "/creation-threads/\(preferredID)")
            XCTAssertEqual(request.url?.query, "projection=full")
            return (200, full)
        }
        let container = try ModelContainer(for: CachedProject.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let cache = CacheRepository(context: container.mainContext)
        try cache.upsert([ProjectSummary(id: preferredID, title: "Old cached title", status: .draft, updatedAt: .distantPast, posterURL: nil)])
        let model = AppModel(api: NativeEditorTestSupport.api(), cache: cache)

        await model.openWorkspace(preferredProjectID: preferredID)

        XCTAssertEqual(model.selectedProject?.id, preferredID, "The persisted selection must win over the newest project")
        XCTAssertEqual(model.selectedProject?.title, "Renamed on another device")
        XCTAssertEqual(model.selectedProject?.serverRevision, 5)
        let before = try await model.api.project(threadID: XCTUnwrap(model.selectedProject?.id))
        let messages = before.events.compactMap(ChatTranscriptMessage.from)
        XCTAssertEqual(messages.map(\.content), ["Keep the opening quiet.", "I will preserve the natural sound."])

        // Reopening the workspace must preserve the selection even when the
        // caller has no persisted ID, and full hydration must retain history.
        await model.openWorkspace()
        XCTAssertEqual(model.selectedProject?.id, preferredID)
        let after = try await model.api.project(threadID: XCTUnwrap(model.selectedProject?.id))
        XCTAssertEqual(after.events.compactMap(ChatTranscriptMessage.from), messages)
        XCTAssertEqual(try cache.projects().first(where: { $0.id == preferredID })?.title, "Renamed on another device")
    }

    func testMissingPreferredProjectFallsBackToExistingProject() async {
        let existingID = UUID(uuidString: "74A559F6-28D9-4E0D-9EB6-71CD09A958DE")!
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads")
            return (200, Data("""
            [{"id":"\(existingID)","title":"Still available","status":"draft","revision":2,"runtime_version":2,"updated_at":"2026-09-10T12:00:00Z"}]
            """.utf8))
        }
        let model = AppModel(api: NativeEditorTestSupport.api())
        await model.openWorkspace(preferredProjectID: UUID(uuidString: "00000000-0000-0000-0000-000000000000")!)
        XCTAssertEqual(model.selectedProject?.id, existingID)
        XCTAssertEqual(model.projectsState, .loaded)
    }
}
