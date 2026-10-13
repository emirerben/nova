#if DEBUG
import Foundation

/// All requests are intercepted: account UI tests never contact a real account.
enum AccountUITestTransport {
    static func api(tokenStore: TokenStore) -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AccountUITestURLProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://account-ui-testing.invalid")!,
                       tokenStore: tokenStore, session: URLSession(configuration: configuration))
    }
}

final class AccountUITestTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: MobileSession?
    func read() throws -> MobileSession? { lock.lock(); defer { lock.unlock() }; return session }
    func write(_ session: MobileSession) throws { lock.lock(); defer { lock.unlock() }; self.session = session }
    func delete() throws { lock.lock(); defer { lock.unlock() }; session = nil }
}

private final class AccountUITestURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url?.path ?? ""
        let mode = ProcessInfo.processInfo.environment["KRIA_ACCOUNT_TEST_MODE"] ?? "normal"
        let result: (Int, String)
        if path.hasPrefix("/me/memory"), mode.hasPrefix("text-preferences") {
            let result = AccountTextPreferenceFixture.shared.reply(request, stale: mode.hasSuffix("stale"))
            deliver(result)
            return
        }
        switch path {
        case "/me/account/delete-request":
            result = mode == "unavailable" ? (503, "{}") : (202, "{\"requested\":true}")
        case "/me/account/delete-confirm":
            result = mode == "invalid-code" ? (400, "{}") : (204, "")
        case "/auth/mobile/revoke": result = (204, "")
        case "/auth/mobile/me":
            result = (200, "{\"id\":\"1\",\"email\":\"creator@example.com\",\"name\":\"Test creator\",\"onboarding_status\":\"complete\",\"linked_providers\":[\"google\"]}")
        default: result = (503, "{}")
        }
        deliver(result)
    }
    private func deliver(_ result: (Int, String)) {
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: result.0, httpVersion: nil,
                                             headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(result.1.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
private final class AccountTextPreferenceFixture: @unchecked Sendable {
    static let shared = AccountTextPreferenceFixture()
    private let lock = NSLock()
    private var revision = 0
    private var item: [String: Any]?
    private func bodyData(_ request: URLRequest) -> Data? {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open(); defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while data.count < 65536 {
            let count = stream.read(&buffer, maxLength: 4096)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
    func reply(_ request: URLRequest, stale: Bool) -> (Int, String) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        if request.httpMethod == "GET" {
            let body: [String: Any] = ["feature_available": true, "text_preferences_available": true, "enabled": true, "revision": revision,
                                      "active": item.map { [$0] } ?? [], "suggestions": [], "recent_undo": NSNull()]
            let data = try! JSONSerialization.data(withJSONObject: body)
            return (200, String(decoding: data, as: UTF8.self))
        }
        if stale { return (409, "{\"detail\":{\"code\":\"stale_revision\",\"message\":\"Preferences changed\"}}") }
        if request.httpMethod == "DELETE" { item = nil }
        else if path.contains("/items"), let data = bodyData(request),
                var value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            value["id"] = "00000000-0000-0000-0000-000000000648"
            item = value
        }
        revision += 1
        return (200, "{\"id\":\"00000000-0000-0000-0000-000000000649\"}")
    }
}

#endif
