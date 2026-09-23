import XCTest
@testable import Kria

/// The API answers a request Postgres aborted as a deadlock victim with a
/// retryable `409 {"code": "concurrent_update"}`. Nothing changed server-side, so
/// the client resends instead of telling the creator their edit "changed elsewhere"
/// (2026-09-23: two chosen photos failed that way).
@MainActor final class ConcurrentUpdateRetryTests: XCTestCase {
    private final class Calls: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func next() -> Int { lock.lock(); defer { lock.unlock() }; value += 1; return value }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    private let concurrentUpdate = Data(#"{"detail":"That project was being updated at the same time. Try again.","code":"concurrent_update","retryable":true}"#.utf8)

    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func removeVisual() async throws {
        try await NativeEditorTestSupport.api().removeVisual(itemID: "item-1", assetID: "asset-1")
    }

    func testRetriesAConcurrentUpdateAndSucceeds() async throws {
        let calls = Calls()
        let deadlocked = concurrentUpdate
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/plan-items/item-1/assets/asset-1")
            XCTAssertEqual(request.httpMethod, "DELETE")
            return calls.next() == 1 ? (409, deadlocked) : (200, Data("{}".utf8))
        }

        try await removeVisual()

        XCTAssertEqual(calls.count, 2)
    }

    func testGivesUpAfterTheRetryLimitWithAConflict() async throws {
        let calls = Calls()
        let deadlocked = concurrentUpdate
        NativeEditorURLProtocol.handler = { _ in _ = calls.next(); return (409, deadlocked) }

        do {
            try await removeVisual()
            XCTFail("Expected a conflict once the retries are spent")
        } catch let error as APIError {
            XCTAssertEqual(error, .conflict)
        }
        XCTAssertEqual(calls.count, 1 + KriaAPI.concurrentUpdateRetryLimit)
    }

    func testAnOrdinaryConflictIsNotRetried() async throws {
        let calls = Calls()
        NativeEditorURLProtocol.handler = { _ in
            _ = calls.next()
            return (409, Data(#"{"detail":"Creation thread changed"}"#.utf8))
        }

        do {
            try await removeVisual()
            XCTFail("Expected a conflict")
        } catch let error as APIError {
            XCTAssertEqual(error, .conflict)
            XCTAssertEqual(error.conflictDetail, "Creation thread changed")
        }
        XCTAssertEqual(calls.count, 1)
    }

    func testOnlyTheServersRetryableConcurrentUpdateQualifies() throws {
        let url = try XCTUnwrap(URL(string: "https://native-editor.test/x"))
        func response(_ status: Int, retryAfter: String? = nil) -> HTTPURLResponse {
            HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: retryAfter.map { ["Retry-After": $0] })!
        }
        XCTAssertNotNil(KriaAPI.concurrentUpdateRetryDelay(data: concurrentUpdate, response: response(409)))
        XCTAssertNil(KriaAPI.concurrentUpdateRetryDelay(data: concurrentUpdate, response: response(500)))
        let notRetryable = Data(#"{"code":"concurrent_update","retryable":false}"#.utf8)
        XCTAssertNil(KriaAPI.concurrentUpdateRetryDelay(data: notRetryable, response: response(409)))
        XCTAssertNil(KriaAPI.concurrentUpdateRetryDelay(data: Data(), response: response(409)))

        let advised = try XCTUnwrap(KriaAPI.concurrentUpdateRetryDelay(data: concurrentUpdate, response: response(409, retryAfter: "1")))
        XCTAssertGreaterThanOrEqual(advised, .seconds(1))
        XCTAssertLessThanOrEqual(advised, .milliseconds(1250))
        let capped = try XCTUnwrap(KriaAPI.concurrentUpdateRetryDelay(data: concurrentUpdate, response: response(409, retryAfter: "30")))
        XCTAssertLessThanOrEqual(capped, .milliseconds(2250))
    }
}
