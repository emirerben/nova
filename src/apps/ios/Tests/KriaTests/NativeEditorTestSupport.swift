import Foundation
@testable import Kria

/// URLProtocol-backed transport shared by native editor characterization tests.
/// It keeps API tests account-free and records the exact request at the boundary.
final class NativeEditorURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            let (status, data) = try Self.handler?(request) ?? (500, Data())
            guard let url = request.url else { throw APIError.invalidResponse }
            let response = HTTPURLResponse(
                url: url,
                statusCode: status,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

enum NativeEditorTestSupport {
    static func api(tokenStore: TokenStore = NativeEditorMemoryTokenStore()) -> KriaAPI {
        KriaAPI(
            baseURL: URL(string: "https://native-editor.test")!,
            tokenStore: tokenStore,
            session: session()
        )
    }

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeEditorURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    static func bodyData(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            result.append(buffer, count: count)
        }
        return result
    }
}

final class NativeEditorMemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var sessionValue: MobileSession?

    init(_ session: MobileSession? = nil) {
        sessionValue = session
    }

    func read() throws -> MobileSession? { lock.withLock { sessionValue } }
    func write(_ session: MobileSession) throws { lock.withLock { sessionValue = session } }
    func delete() throws { lock.withLock { sessionValue = nil } }
}
