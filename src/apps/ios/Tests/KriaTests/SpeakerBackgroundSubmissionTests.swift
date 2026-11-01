import XCTest
@testable import Kria

/// KRI-1119 failure modes written before the code:
/// 1. the wire: PUT .../speaker-background with the protocol header; a set names the reserved upload, a clear sends no
///    reference at all, and neither ever sends both references;
/// 2. a stale revision re-reads the thread and retries ONCE with the same event id (the song-range policy);
/// 3. the server turning the feature off is silent, an ordinary 404 is not;
/// 4. a failed statement never un-attaches the take or the background upload, keeps the choice, and is retried at the next
///    opening with the same event id so a request that did land is replayed, not applied twice;
/// 5. a background cleared before Use take sends nothing, and one the server already knew sends exactly one clear.
final class SpeakerBackgroundWireTests: XCTestCase {
    private func thread(revision: Int = 5) -> Data {
        Data("""
        {"id":"\(PreviewFixtures.projectID.uuidString)","title":"T","status":"active","revision":\(revision),"runtime_version":2,"updated_at":"2026-09-10T10:00:00Z","state":{},"events":[]}
        """.utf8)
    }

    func testSetSendsPutWithTheProtocolHeaderAndTheReservedUpload() async throws {
        var seen: (method: String?, path: String?, protocolHeader: String?, body: [String: Any])?
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            seen = (request.httpMethod, request.url?.path, request.value(forHTTPHeaderField: "X-Kria-Client-Protocol"), body)
            return (200, self.thread(revision: 6))
        }
        let updated = try await NativeEditorTestSupport.api().setSpeakerBackground(
            threadID: PreviewFixtures.projectID, clientUploadID: "ios-ABC", expectedRevision: 5, clientEventID: "evt-1")
        XCTAssertEqual(updated.revision, 6)
        XCTAssertEqual(seen?.method, "PUT")
        XCTAssertEqual(seen?.path, "/creation-threads/\(PreviewFixtures.projectID.uuidString)/speaker-background")
        XCTAssertEqual(seen?.protocolHeader, "28")
        XCTAssertEqual(seen?.body["client_upload_id"] as? String, "ios-ABC")
        XCTAssertEqual(seen?.body["client_event_id"] as? String, "evt-1")
        XCTAssertEqual(seen?.body["expected_revision"] as? Int, 5)
        XCTAssertNil(seen?.body["media_id"], "never both references")
        XCTAssertEqual(Set(seen?.body.keys.map { $0 } ?? []), ["client_upload_id", "client_event_id", "expected_revision"])
    }

    func testClearSendsNoReferenceAtAll() async throws {
        var body: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            return (200, self.thread())
        }
        _ = try await NativeEditorTestSupport.api().setSpeakerBackground(
            threadID: PreviewFixtures.projectID, clientUploadID: nil, expectedRevision: 5, clientEventID: "evt-2")
        XCTAssertEqual(Set(body.keys), ["client_event_id", "expected_revision"], "no client_upload_id key, and not null either")
    }

    func testTheSameStatementSendsTheSameBodyTwiceSoTheServerCanReplayIt() async throws {
        var bodies: [Data] = []
        NativeEditorURLProtocol.handler = { request in bodies.append(NativeEditorTestSupport.bodyData(request)); return (200, self.thread()) }
        let api = NativeEditorTestSupport.api()
        for _ in 0..<2 {
            _ = try await api.setSpeakerBackground(threadID: PreviewFixtures.projectID, clientUploadID: "ios-ABC", expectedRevision: 5, clientEventID: "evt-1")
        }
        let first = try XCTUnwrap(try JSONSerialization.jsonObject(with: bodies[0]) as? NSDictionary)
        let second = try XCTUnwrap(try JSONSerialization.jsonObject(with: bodies[1]) as? NSDictionary)
        XCTAssertEqual(first, second)
    }
}

final class SpeakerBackgroundPolicyTests: XCTestCase {
    private final class FakeClient: SpeakerBackgroundClient, @unchecked Sendable {
        var failures: [Error]
        var revisions: [Int]
        private(set) var sent: [(revision: Int, eventID: String, upload: String?)] = []
        private(set) var reads = 0
        init(failures: [Error] = [], revisions: [Int] = [5, 6, 7]) { self.failures = failures; self.revisions = revisions }
        func project(threadID: UUID) async throws -> CreationThread {
            defer { reads += 1 }
            return try Self.decode(revision: revisions[min(reads, revisions.count - 1)])
        }
        func setSpeakerBackground(threadID: UUID, clientUploadID: String?, expectedRevision: Int, clientEventID: String) async throws -> CreationThread {
            sent.append((expectedRevision, clientEventID, clientUploadID))
            if !failures.isEmpty { throw failures.removeFirst() }
            return try Self.decode(revision: expectedRevision + 1)
        }
        static func decode(revision: Int) throws -> CreationThread {
            let json = #"{"id":"t","title":"T","status":"active","revision":\#(revision),"updated_at":"2026-10-04T10:00:00Z"}"#
            let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(CreationThread.self, from: Data(json.utf8))
        }
    }

    private let intent = SpeakerBackgroundIntent.set(clientUploadID: "ios-X", eventID: "evt")
    private func conflict(_ code: String? = nil) -> APIError { .conflict(detail: ConflictDetail("Creation thread changed", code: code)) }
    private func failed(_ status: Int, _ code: String?) -> APIError { .requestFailed(status: status, detail: RequestFailureDetail(nil, code: code, problemMessage: nil)) }

    func testAStaleRevisionRereadsTheThreadAndRetriesOnceWithTheSameEventId() async {
        let client = FakeClient(failures: [conflict()], revisions: [5, 8])
        let result = await SpeakerBackgroundSubmission.submit(client: client, threadID: UUID(), intent: intent)
        guard case .saved(let thread) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(thread.revision, 9)
        XCTAssertEqual(client.sent.map(\.revision), [5, 8])
        XCTAssertEqual(client.sent.map(\.eventID), ["evt", "evt"])
        XCTAssertEqual(client.sent.map(\.upload), ["ios-X", "ios-X"])
    }

    func testASecondConflictIsAFailureNotALoop() async {
        let client = FakeClient(failures: [conflict(), conflict(), conflict()])
        let result = await SpeakerBackgroundSubmission.submit(client: client, threadID: UUID(), intent: intent)
        guard case .failed = result else { return XCTFail("\(result)") }
        XCTAssertEqual(client.sent.count, 2)
    }

    func testNoPlanItemYetIsNeverRetriedInline() async {
        let client = FakeClient(failures: [conflict("speaker_background_needs_item")])
        let result = await SpeakerBackgroundSubmission.submit(client: client, threadID: UUID(), intent: intent)
        guard case .failed = result else { return XCTFail("\(result)") }
        XCTAssertEqual(client.sent.count, 1)
    }

    func testFeatureOffIsQuietAndAnOrdinary404IsNot() async {
        let off = await SpeakerBackgroundSubmission.submit(client: FakeClient(failures: [failed(404, "speaker_background_unavailable")]), threadID: UUID(), intent: intent)
        guard case .unavailable = off else { return XCTFail("\(off)") }
        let missing = await SpeakerBackgroundSubmission.submit(client: FakeClient(failures: [failed(404, "thread_not_found")]), threadID: UUID(), intent: intent)
        guard case .failed = missing else { return XCTFail("\(missing)") }
    }

    func testValidationAndOfflineFailuresAreFailures() async {
        for error in [failed(422, "speaker_background_media_not_in_pool"), APIError.offline] {
            let result = await SpeakerBackgroundSubmission.submit(client: FakeClient(failures: [error]), threadID: UUID(), intent: intent)
            guard case .failed = result else { return XCTFail("\(error): \(result)") }
        }
    }

    func testAClearSendsNoUploadReference() async {
        let client = FakeClient()
        _ = await SpeakerBackgroundSubmission.submit(client: client, threadID: UUID(), intent: .clear(eventID: "c"))
        XCTAssertEqual(client.sent.count, 1)
        XCTAssertNil(client.sent[0].upload)
    }
}
