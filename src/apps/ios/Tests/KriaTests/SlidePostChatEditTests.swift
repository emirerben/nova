import XCTest
@testable import Kria

/// KRI-298 Lane E: chat edits on slide posts. An `edited` reply is staged (unsaved, undoable);
/// everything else only adds a bubble. Failure modes first.
@MainActor final class SlidePostChatEditTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var defaults: UserDefaults!
    private var suite: String!
    override func setUp() {
        super.setUp(); suite = "SlidePostChatEditTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite); defaults = nil
        super.tearDown()
    }

    private func state(version: Int = 1) -> SlidePostState {
        let refs = (1...3).map { SlidePostSlide(id: "slide-\($0)", assetID: "asset-\($0)", kind: "image") }
        let draft = SlidePostDraft(version: version, platformProfile: "instagram_carousel", slides: refs, caption: "A day outside")
        return SlidePostState(itemID: itemID, title: "A day outside", jobID: nil, draft: draft,
            assets: refs.map { .init(id: $0.assetID, kind: "image", status: "ready", sourceURL: URL(string: "https://storage.test/source"), durationS: nil, mediaStatus: "available") },
            renderStatus: "not_rendered", renderedVersion: nil, slides: [], bundleURL: nil)
    }

    private struct Captured { var chatBodies: [[String: Any]] = []; var putBodies: [[String: Any]] = [] }
    private var captured = Captured()

    /// Serves GET slide-post, POST chat-edit (with `reply`), PUT slide-post.
    private func serve(remote: SlidePostState, reply: @escaping () throws -> (Int, Data)) {
        captured = Captured()
        NativeEditorURLProtocol.handler = { [unowned self] request in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/chat-edit") {
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                self.captured.chatBodies.append(body)
                return try reply()
            }
            if request.httpMethod == "PUT" {
                let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
                self.captured.putBodies.append(body)
                var saved = try XCTUnwrap(remote.draft); saved.version = (body["expected_version"] as? Int ?? 0) + 1
                saved.caption = body["caption"] as? String ?? ""
                return (200, try JSONSerialization.data(withJSONObject: ["slide_post": JSONSerialization.jsonObject(with: JSONEncoder().encode(saved))]))
            }
            return (200, try JSONEncoder().encode(remote))
        }
    }
    private func response(_ outcome: String, reply: String = "ok", draft: SlidePostDraft? = nil, baseVersion: Int = 1, changes: [String] = []) throws -> (Int, Data) {
        var object: [String: Any] = ["outcome": outcome, "reply": reply, "base_version": baseVersion, "changes": changes, "suggestions": []]
        if let draft { object["draft"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(draft)) }
        return (200, try JSONSerialization.data(withJSONObject: object))
    }
    private func session(remote: SlidePostState) async -> SlidePostSession {
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        return session
    }
    private func reordered(_ draft: SlidePostDraft) -> SlidePostDraft {
        var value = draft; value.slides.reverse(); value.version = 99; return value
    }

    // MARK: No draft change

    func testNonEditedOutcomesOnlyAddAnAssistantBubble() async throws {
        for outcome in ["clarification", "unsupported", "no_effect", "failed"] {
            defaults.removePersistentDomain(forName: suite)  // the transcript persists per item; start each case clean
            let remote = state()
            serve(remote: remote) { try self.response(outcome, reply: "Server says \(outcome).") }
            let session = await session(remote: remote)
            let before = session.draft
            let staged = await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "do something")
            XCTAssertFalse(staged, outcome)
            XCTAssertEqual(session.draft, before, outcome)
            XCTAssertFalse(session.hasUnsavedChanges, outcome)
            XCTAssertFalse(session.canUndoEdit, outcome)
            XCTAssertEqual(session.chat.map(\.role), ["user", "assistant"], outcome)
            XCTAssertEqual(session.chat.last?.text, "Server says \(outcome).", "the reply is shown verbatim")
        }
    }

    func testEditedWithoutADraftIsNotStaged() async throws {
        let remote = state()
        serve(remote: remote) { try self.response("edited", reply: "Done.") }
        let session = await session(remote: remote)
        let staged = await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "reorder")
        XCTAssertFalse(staged)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testNetworkErrorLeavesTheDraftAndOffersRetry() async throws {
        let remote = state()
        var fail = true
        serve(remote: remote) {
            if fail { throw URLError(.notConnectedToInternet) }
            return try self.response("clarification", reply: "Which photos?")
        }
        let session = await session(remote: remote)
        let before = session.draft
        let staged = await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "sort them")
        XCTAssertFalse(staged)
        XCTAssertEqual(session.draft, before)
        XCTAssertFalse(session.isChatting)
        let failure = try XCTUnwrap(session.chat.last)
        XCTAssertEqual(failure.retryText, "sort them")
        fail = false
        await session.retryChat(api: NativeEditorTestSupport.api(), itemID: itemID, bubble: failure)
        XCTAssertEqual(session.chat.map(\.text), ["sort them", "Which photos?"], "retry replaces the failed pair instead of stacking")
    }

    func testEditsMadeWhileKriaWorksAreNeverOverwritten() async throws {
        let remote = state()
        let local = SlidePostSessionBox()
        serve(remote: remote) {
            // The user edits the caption while the request is in flight.
            DispatchQueue.main.sync { MainActor.assumeIsolated { local.session?.setCaption("Mine") } }
            var draft = remote.draft!; draft.slides.reverse()
            return try self.response("edited", reply: "Reordered.", draft: draft)
        }
        let session = await session(remote: remote)
        local.session = session
        let staged = await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "reverse")
        XCTAssertFalse(staged)
        XCTAssertEqual(session.draft?.caption, "Mine")
        XCTAssertEqual(session.draft?.slides.map(\.id), ["slide-1", "slide-2", "slide-3"])
    }

    // MARK: Staging

    func testEditedReplyIsStagedDirtyAndUndoable() async throws {
        let remote = state()
        serve(remote: remote) { [self] in try response("edited", reply: "Reordered.", draft: reordered(remote.draft!), baseVersion: 1, changes: ["Reordered 3"]) }
        let session = await session(remote: remote)
        let staged = await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "reverse them")
        XCTAssertTrue(staged)
        XCTAssertEqual(session.draft?.slides.map(\.id), ["slide-3", "slide-2", "slide-1"])
        XCTAssertEqual(session.draft?.version, 1, "the meaningless returned version is replaced by the editor's")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndoEdit)
        XCTAssertEqual(session.chat.last?.changes, ["Reordered 3"])
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides.map(\.id), ["slide-1", "slide-2", "slide-3"])
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testCleanEditorOmitsTheDraftAndDirtyEditorSendsIt() async throws {
        let remote = state()
        serve(remote: remote) { try self.response("clarification") }
        let session = await session(remote: remote)
        await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "first")
        XCTAssertNil(captured.chatBodies.last?["draft"], "a clean editor lets the server use its stored copy")
        session.setCaption("Unsaved caption")
        await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "second")
        let sent = try XCTUnwrap(captured.chatBodies.last?["draft"] as? [String: Any])
        XCTAssertEqual(sent["caption"] as? String, "Unsaved caption", "the editor's draft is the newest truth")
        XCTAssertEqual(captured.chatBodies.last?["message"] as? String, "second")
        XCTAssertNotNil(captured.chatBodies.last?["client_request_id"] as? String)
    }

    func testTurnsAreCappedAtTwelveAndOldestDropFirst() async throws {
        let remote = state()
        serve(remote: remote) { try self.response("clarification", reply: "reply") }
        let session = await session(remote: remote)
        for index in 0..<10 { await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "m\(index)") }
        let turns = try XCTUnwrap(captured.chatBodies.last?["turns"] as? [[String: Any]])
        XCTAssertEqual(turns.count, 12)
        XCTAssertEqual(turns.first?["role"] as? String, "user")
        XCTAssertEqual(turns.first?["content"] as? String, "m3", "the 18 prior bubbles are trimmed to the latest 12")
        XCTAssertEqual(turns.last?["role"] as? String, "assistant")
    }

    func testSaveAfterChatEditPutsTheServersBaseVersion() async throws {
        let remote = state(version: 3)
        serve(remote: remote) { [self] in try response("edited", reply: "Done.", draft: reordered(remote.draft!), baseVersion: 3) }
        let session = await session(remote: remote)
        await session.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "reverse")
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.save(api: NativeEditorTestSupport.api(), itemID: itemID)
        let put = try XCTUnwrap(captured.putBodies.last)
        XCTAssertEqual(put["expected_version"] as? Int, 3, "save quotes base_version, not the returned draft's version")
        XCTAssertEqual((put["slides"] as? [[String: Any]])?.compactMap { $0["id"] as? String }, ["slide-3", "slide-2", "slide-1"])
        XCTAssertNil(session.error)
    }

    func testTranscriptPersistsAndRestores() async throws {
        let remote = state()
        serve(remote: remote) { try self.response("clarification", reply: "Which photos?") }
        let first = await session(remote: remote)
        await first.chatEdit(api: NativeEditorTestSupport.api(), itemID: itemID, message: "sort them")
        let reopened = await session(remote: remote)
        XCTAssertEqual(reopened.chat.map(\.text), ["sort them", "Which photos?"])
        XCTAssertEqual(reopened.chat.map(\.role), ["user", "assistant"])
    }

    // MARK: Contract

    func testCapabilityDecodesAndDefaultsOffSoTheProposeFlowStays() throws {
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_chat_edit":true}"#.utf8))
        XCTAssertTrue(on.slidePostChatEditEnabled)
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_rich_text":true}"#.utf8))
        XCTAssertFalse(off.slidePostChatEditEnabled)
    }

    func testResponseDecodesEveryOutcomeAndMissingLists() throws {
        for outcome in ["edited", "clarification", "unsupported", "no_effect", "failed"] {
            let data = Data(#"{"outcome":"\#(outcome)","reply":"r","base_version":2}"#.utf8)
            let decoded = try JSONDecoder().decode(SlidePostChatEditResponse.self, from: data)
            XCTAssertEqual(decoded.baseVersion, 2); XCTAssertEqual(decoded.changes, [])
        }
    }

    func testNotesAreSeparatedFromSuccessChips() {
        XCTAssertTrue(SlidePostChatMessage.isNote("2 photos have no location"))
        XCTAssertFalse(SlidePostChatMessage.isNote("Location on 6"))
    }
}

@MainActor private final class SlidePostSessionBox { var session: SlidePostSession? }
