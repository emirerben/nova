import XCTest
@testable import Kria

/// KRI-119: the app runs several `KriaAPI` instances (AuthModel, AppModel,
/// the library audit) over one Keychain. When the access token expired, two
/// instances each refreshed with the same single-use refresh token; the
/// server treated the second as a replay, revoked the family, and the app
/// showed "Your session expired" (prod 2026-09-19 11:58:57Z). One in-flight
/// refresh must be shared by every instance in the process.
final class SessionRefreshRaceTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    private func session(_ tag: String) -> MobileSession {
        MobileSession(accessToken: "\(tag)-access", refreshToken: "\(tag)-refresh", expiresIn: 3600)
    }

    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func increment() -> Int { lock.withLock { value += 1; return value } }
        var count: Int { lock.withLock { value } }
    }

    func testTwoClientInstancesShareOneRefresh() async throws {
        let store = NativeEditorMemoryTokenStore(session("old"))
        let refreshes = Counter()
        let rotated = try JSONEncoder().encode(session("new"))
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                // A second refresh with the rotated token is what the server
                // punishes with family revocation; it must never be sent.
                return refreshes.increment() == 1
                    ? (200, rotated)
                    : (401, Data(#"{"detail":{"code":"refresh_reuse_detected"}}"#.utf8))
            }
            XCTAssertEqual(request.url?.path, "/creation-threads")
            let bearer = request.value(forHTTPHeaderField: "Authorization")
            return bearer == "Bearer new-access" ? (200, Data("[]".utf8)) : (401, Data())
        }

        let first = NativeEditorTestSupport.api(tokenStore: store)
        let second = NativeEditorTestSupport.api(tokenStore: store)
        async let a = first.projects()
        async let b = second.projects()
        let (projectsA, projectsB) = try await (a, b)

        XCTAssertEqual(projectsA.count, 0)
        XCTAssertEqual(projectsB.count, 0)
        XCTAssertEqual(refreshes.count, 1, "both instances must ride the same in-flight refresh")
        XCTAssertEqual(try store.read()?.refreshToken, "new-refresh")
    }

    func testRotationCompletedElsewhereIsAcceptedInsteadOfSigningOut() async throws {
        // Another process (a relaunch mid-rotation) already wrote the rotated
        // session; our replay is refused, but the store holds the answer.
        let store = NativeEditorMemoryTokenStore(session("old"))
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                try store.write(self.session("new"))
                return (401, Data(#"{"detail":{"code":"refresh_superseded"}}"#.utf8))
            }
            let bearer = request.value(forHTTPHeaderField: "Authorization")
            return bearer == "Bearer new-access" ? (200, Data("[]".utf8)) : (401, Data())
        }

        let projects = try await NativeEditorTestSupport.api(tokenStore: store).projects()

        XCTAssertEqual(projects.count, 0)
        XCTAssertEqual(try store.read()?.accessToken, "new-access")
    }

    func testRefusedRefreshWithUnchangedStoreStillExpiresTheSession() async throws {
        let store = NativeEditorMemoryTokenStore(session("old"))
        NativeEditorURLProtocol.handler = { request in
            request.url?.path == "/auth/mobile/refresh" ? (401, Data()) : (401, Data())
        }
        do {
            _ = try await NativeEditorTestSupport.api(tokenStore: store).projects()
            XCTFail("expected sessionExpired")
        } catch {
            XCTAssertEqual(error as? APIError, .sessionExpired)
        }
        XCTAssertNil(try store.read())
    }
}
