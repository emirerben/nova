import XCTest
@testable import Kria

@MainActor final class BackgroundUploadRetryTests: XCTestCase {
    override func tearDown() { UploadRetryProtocol.handler = nil; super.tearDown() }

    func testProxyCannotEnterCloudSourceReservationContract() throws {
        XCTAssertThrowsError(try BackgroundUploadCoordinator.validateProjectUploadPurpose(.analysisProxy))
        XCTAssertNoThrow(try BackgroundUploadCoordinator.validateProjectUploadPurpose(.cloudRenderSource))
    }

    /// KRI-94: `enqueue` stages a durable copy + a `PreparingUpload` entry (kept
    /// out of `records` — see its doc comment) before `prepare()`'s potentially
    /// slow import/transcode. Simulates a crash in that exact window by writing
    /// the entry a fresh coordinator would have left behind, without ever
    /// running `enqueue` itself, then verifies the next-launch recovery path.
    func testInterruptedPreparationIsDiscoverableAndCleanedUpOnNextLaunch() throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let staged = FileManager.default.temporaryDirectory.appending(path: "staged-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: staged)
        let entry = PreparingUpload(id: UUID(), projectID: UUID(), localFilePath: staged.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil)
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        defer {
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: staged)
        }
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)

        coordinator.recoverInterruptedPreparations()

        XCTAssertEqual(coordinator.lastError, "An upload was interrupted before it could start. Choose the file again.")
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.path), "the orphaned staged copy must be cleaned up")
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertTrue(remaining.isEmpty)
        XCTAssertTrue(coordinator.records.isEmpty, "a staging entry must never surface through the published records array")

        // A second call (e.g. two cold launches without an intervening successful
        // enqueue) must be a harmless no-op, not a duplicate error or crash.
        coordinator.recoverInterruptedPreparations()
    }

    func testNoInterruptedPreparationIsANoOp() {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)
        coordinator.recoverInterruptedPreparations()
        XCTAssertNil(coordinator.lastError)
    }

    func testConcurrentRetryReservesOnlyOneReplacement() async throws {
        let fixture = try makeCoordinator()
        defer { fixture.cleanup() }
        let started = expectation(description: "Reservation suspended")
        var held: UploadRetryProtocol?
        var reservations = 0
        UploadRetryProtocol.handler = { transport in
            reservations += 1
            held = transport
            started.fulfill()
        }
        let first = Task { await fixture.coordinator.retryUpload(recordID: fixture.record.id) }
        await fulfillment(of: [started], timeout: 3)
        await fixture.coordinator.retryUpload(recordID: fixture.record.id)
        XCTAssertEqual(reservations, 1)
        try XCTUnwrap(held).finish(500, Data())
        await first.value
        XCTAssertEqual(fixture.coordinator.records.count, 1)
        XCTAssertNotNil(fixture.coordinator.lastError)
        UploadRetryProtocol.handler = { transport in
            reservations += 1
            transport.finish(500, Data())
        }
        await fixture.coordinator.retryUpload(recordID: fixture.record.id)
        XCTAssertEqual(reservations, 2) // A failed attempt releases the retry guard.
    }

    func testCancellationWhileReservationIsPendingCannotRestartUpload() async throws {
        let fixture = try makeCoordinator()
        defer { fixture.cleanup() }
        let started = expectation(description: "Reservation suspended")
        var held: UploadRetryProtocol?
        var puts = 0
        UploadRetryProtocol.handler = { transport in
            if transport.request.httpMethod == "PUT" { puts += 1; transport.finish(200, Data()); return }
            held = transport
            started.fulfill()
        }
        let retry = Task { await fixture.coordinator.retryUpload(recordID: fixture.record.id) }
        await fulfillment(of: [started], timeout: 3)
        await fixture.coordinator.cancel(recordID: fixture.record.id)
        XCTAssertTrue(fixture.coordinator.records.isEmpty)
        try XCTUnwrap(held).finish(200, Data(#"[{"media_id":"clip-1","upload_url":"https://uploads.test/put","gcs_path":"users/u/clip.mp4","content_type":"video/mp4","upload_headers":{}}]"#.utf8))
        await retry.value
        XCTAssertTrue(fixture.coordinator.records.isEmpty)
        XCTAssertEqual(puts, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.record.localFilePath))
    }

    private struct Fixture {
        let coordinator: BackgroundUploadCoordinator
        let record: UploadRecoveryRecord
        let key: String
        func cleanup() {
            UserDefaults.standard.removeObject(forKey: key)
            try? FileManager.default.removeItem(atPath: record.localFilePath)
        }
    }
    private func makeCoordinator() throws -> Fixture {
        let key = "kria.test.retry.\(UUID().uuidString)"
        let file = FileManager.default.temporaryDirectory.appending(path: "retry-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: file)
        let record = UploadRecoveryRecord(id: UUID(), projectID: UUID(), localFilePath: file.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, taskIdentifier: 999, retryCount: 0)
        UserDefaults.standard.set(try JSONEncoder().encode([record]), forKey: key)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        return Fixture(coordinator: BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration), record: record, key: key)
    }
}

private final class UploadRetryProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@MainActor (UploadRetryProtocol) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let callback = Callback(transport: self)
        DispatchQueue.main.async { UploadRetryProtocol.handler?(callback.transport) }
    }
    private struct Callback: @unchecked Sendable { let transport: UploadRetryProtocol }
    override func stopLoading() {}
    @MainActor func finish(_ status: Int, _ data: Data) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
