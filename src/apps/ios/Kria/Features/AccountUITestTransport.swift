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
        switch path {
        case "/me/account/delete-request":
            result = mode == "unavailable" ? (503, "{}") : (202, "{\"requested\":true}")
        case "/me/account/delete-confirm":
            result = mode == "invalid-code" ? (400, "{}") : (204, "")
        case "/auth/mobile/revoke": result = (204, "")
        default: result = (503, "{}")
        }
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: result.0, httpVersion: nil,
                                             headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(result.1.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
#endif
