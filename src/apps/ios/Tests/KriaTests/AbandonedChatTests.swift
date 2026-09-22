import XCTest
@testable import Kria

@MainActor final class AbandonedChatTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func defaults() -> UserDefaults {
        let suite = "abandoned-chat-\(UUID().uuidString)"
        let store = UserDefaults(suiteName: suite)!
        addTeardownBlock { store.removePersistentDomain(forName: suite) }
        return store
    }

    private func thread(_ id: UUID, events: String = "[]", media: String = "") -> Data {
        Data("{\"id\":\"\(id.uuidString)\",\"title\":\"Untitled project\",\"status\":\"draft\",\"revision\":1,\"runtime_version\":2,\"updated_at\":\"2026-09-10T10:00:00Z\",\"events\":\(events)\(media)}".utf8)
    }

    private let userEvent = "[{\"id\":\"e1\",\"sequence\":1,\"revision\":1,\"role\":\"user\",\"event_type\":\"message\",\"content\":\"hi\",\"created_at\":\"2026-09-10T10:00:00Z\"}]"

    private func project(_ id: UUID) -> ProjectSummary {
        ProjectSummary(id: id, title: "Untitled project", status: .draft, updatedAt: .now, posterURL: nil, serverRevision: 1)
    }

    /// Returns the model plus a box recording whether a DELETE was issued.
    private func run(id: UUID, body: Data, draft: String = "") async -> Bool {
        var deleted = false
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "DELETE" { deleted = true; return (204, Data()) }
            if request.url?.path.contains("creation-threads") == true { return (200, body) }
            return (200, Data("{\"jobs\":[]}".utf8))
        }
        let drafts = ChatDraftStore(defaults: defaults())
        drafts.setDraft(draft, for: id)
        let model = AppModel(api: NativeEditorTestSupport.api(), chatDrafts: drafts)
        model.projects = [project(id)]
        await model.discardIfAbandoned(id)
        return deleted
    }

    func testEmptyChatWithNoDraftIsDeleted() async {
        let id = UUID()
        let deleted = await run(id: id, body: thread(id))
        XCTAssertTrue(deleted)
    }

    func testWhitespaceOnlyDraftCountsAsEmpty() async {
        let id = UUID()
        let deleted = await run(id: id, body: thread(id), draft: "  \n ")
        XCTAssertTrue(deleted)
    }

    func testUnsentTextDraftKeepsChat() async {
        let id = UUID()
        let deleted = await run(id: id, body: thread(id), draft: "make it funny")
        XCTAssertFalse(deleted)
    }

    func testSentMessageKeepsChat() async {
        let id = UUID()
        let deleted = await run(id: id, body: thread(id, events: userEvent))
        XCTAssertFalse(deleted)
    }

    func testStagedMediaKeepsChat() async {
        let id = UUID()
        let media = ",\"media_capabilities\":{\"photos\":{\"current\":2}}"
        let deleted = await run(id: id, body: thread(id, media: media))
        XCTAssertFalse(deleted)
    }

    func testFetchFailureKeepsChat() async {
        let id = UUID()
        var deleted = false
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "DELETE" { deleted = true }
            return (500, Data())
        }
        let model = AppModel(api: NativeEditorTestSupport.api(), chatDrafts: ChatDraftStore(defaults: defaults()))
        model.projects = [project(id)]
        await model.discardIfAbandoned(id)
        XCTAssertFalse(deleted)
    }

    func testDraftStoreRoundTripAndClear() {
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults())
        store.setDraft("hello", for: id)
        XCTAssertEqual(store.draft(for: id), "hello")
        store.setDraft("   ", for: id)
        XCTAssertEqual(store.draft(for: id), "")
    }
}
