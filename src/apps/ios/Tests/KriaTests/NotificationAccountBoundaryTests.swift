import XCTest
@testable import Kria

@MainActor final class NotificationAccountBoundaryTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func session(owner: String, expired: Bool = false) -> MobileSession {
        let payload = Data("{\"sub\":\"\(owner)\",\"exp\":\(expired ? 1 : 4102444800)}".utf8).base64EncodedString()
        return MobileSession(accessToken: "fixture.\(payload).signature", refreshToken: UUID().uuidString, expiresIn: 3600)
    }

    func testAccountSwitchDuringPreflightRefreshNeverSendsInstallationOrOverwritesNewAccount() async throws {
        try await assertSwitchDuringRefresh(expired: true, refreshStatus: 200)
    }

    func testAccountSwitchDuring401RefreshNeverReplaysAsNewAccount() async throws {
        try await assertSwitchDuringRefresh(expired: false, refreshStatus: 200)
    }

    func testAccountSwitchDuringFailedRefreshNeverClearsNewAccount() async throws {
        try await assertSwitchDuringRefresh(expired: false, refreshStatus: 401)
    }

    private func assertSwitchDuringRefresh(expired: Bool, refreshStatus: Int) async throws {
        let first = session(owner: "notification-account-a", expired: expired)
        let next = session(owner: "notification-account-b")
        let rotated = session(owner: "notification-account-a")
        let store = NativeEditorMemoryTokenStore(first)
        let owner = try XCTUnwrap(MediaLibraryOriginals.owner(accessToken: first.accessToken))
        let body = try JSONEncoder().encode(rotated)
        let requests = NotificationRequestLog()
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                try store.write(next)
                return (refreshStatus, body)
            }
            requests.append(request)
            return (401, Data())
        }
        let api = NativeEditorTestSupport.api(tokenStore: store)
        do {
            let _: NativeNotificationReceipt = try await api.request(path: "me/notification-installations/\(UUID().uuidString)", method: "PUT", bodyData: Data("{}".utf8), decode: NativeNotificationReceipt.self, expectedOwner: owner)
            XCTFail("Account switch must stop the old operation")
        } catch {}
        XCTAssertEqual(try store.read(), next)
        XCTAssertEqual(requests.values.count, expired ? 0 : 1)
        XCTAssertTrue(requests.values.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer \(first.accessToken)" })
    }

    func testAccountSwitchDuringSuccessful401ReplayRejectsStaleReceipt() async throws {
        let first = session(owner: "notification-replay-a")
        let rotated = session(owner: "notification-replay-a")
        let next = session(owner: "notification-replay-b")
        let store = NativeEditorMemoryTokenStore(first)
        let owner = try XCTUnwrap(MediaLibraryOriginals.owner(accessToken: first.accessToken))
        let refreshedBody = try JSONEncoder().encode(rotated)
        let id = UUID()
        let requests = NotificationRequestLog()
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" { return (200, refreshedBody) }
            requests.append(request)
            if requests.values.count == 1 { return (401, Data()) }
            try store.write(next)
            return (200, Data("{\"id\":\"\(id)\",\"enabled\":true,\"registration_version\":2}".utf8))
        }
        do {
            _ = try await KriaNotificationRegistrationClient(api: NativeEditorTestSupport.api(tokenStore: store)).put(id: id, token: "0102", owner: owner)
            XCTFail("A response for the previous account must not be accepted")
        } catch {}
        XCTAssertEqual(requests.values.count, 2)
        XCTAssertEqual(try store.read(), next)
    }

    func testRegistrationBodyContainsOnlyTokenAndEnabledAndDecodesServerVersion() async throws {
        let stored = session(owner: "notification-contract")
        let store = NativeEditorMemoryTokenStore(stored)
        let owner = try XCTUnwrap(MediaLibraryOriginals.owner(accessToken: stored.accessToken))
        let id = UUID()
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            XCTAssertEqual(request.url?.path, "/me/notification-installations/\(id.uuidString)")
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(Set(object.keys), ["token", "enabled"])
            XCTAssertEqual(object["token"] as? String, "0102")
            XCTAssertEqual(object["enabled"] as? Bool, true)
            return (200, Data("{\"id\":\"\(id.uuidString)\",\"enabled\":true,\"registration_version\":19}".utf8))
        }
        let receipt = try await KriaNotificationRegistrationClient(api: NativeEditorTestSupport.api(tokenStore: store)).put(id: id, token: "0102", owner: owner)
        XCTAssertEqual(receipt.registrationVersion, 19)
    }
}

private final class NotificationRequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [URLRequest] = []
    func append(_ request: URLRequest) { lock.withLock { requests.append(request) } }
    var values: [URLRequest] { lock.withLock { requests } }
}
