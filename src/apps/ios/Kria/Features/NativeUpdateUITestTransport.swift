#if DEBUG
import Foundation

/// The update UI must be driven by the same typed-426 parser as production
/// traffic. This transport is deliberately opt-in through the app's UI-test
/// launch argument, so fixtures never depend on a developer's session or API.
enum NativeUpdateUITestTransport {
    static func triggerTypedUpdateRequirement() async {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeUpdateUITestURLProtocol.self]
        let api = KriaAPI(
            baseURL: URL(string: "https://native-update-ui-testing.invalid")!,
            tokenStore: NativeUpdateUITestTokenStore(),
            session: URLSession(configuration: configuration)
        )
        _ = try? await api.creationCapabilities()
    }
}

private struct NativeUpdateUITestTokenStore: TokenStore {
    func read() throws -> MobileSession? { nil }
    func write(_ session: MobileSession) throws {}
    func delete() throws {}
}

private final class NativeUpdateUITestURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url,
              let response = HTTPURLResponse(
                url: url,
                statusCode: 426,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
              )
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let body = Data(#"{"problem":{"code":"native_update_required","message":"Update Kria"}}"#.utf8)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
#endif
