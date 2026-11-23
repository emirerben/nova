import Foundation
import XCTest
@testable import Kria

/// Ties each test client to the test that made it. A request that a finished test never
/// awaited (a profile read after sign-in, say) is refused instead of reaching the next
/// test's handler, where it would be counted as that test's own traffic (KRI-1607).
final class NativeEditorTestRun: NSObject, XCTestObservation, @unchecked Sendable {
    static let shared = NativeEditorTestRun()
    static let header = "X-Kria-Test-Case"
    private let lock = NSLock()
    private var number = 0
    private var name = ""
    private var late: [String: Int] = [:]

    private override init() {
        super.init()
        XCTestObservationCenter.shared.addTestObserver(self)
    }

    var current: String { lock.withLock { String(number) } }
    /// Requests refused so far because their client was made by an earlier test.
    var lateRequests: Int { lock.withLock { late.values.reduce(0, +) } }

    func testCaseWillStart(_ testCase: XCTestCase) { lock.withLock { number += 1; name = testCase.name } }

    func testBundleDidFinish(_ testBundle: Bundle) {
        let seen = lock.withLock { late }
        guard !seen.isEmpty else { return }
        print("Requests from an earlier test, refused (\(seen.values.reduce(0, +))):")
        for (test, count) in seen.sorted(by: { $0.key < $1.key }) { print("  \(count) during \(test)") }
    }

    /// True when `request` comes from a client made during the test that is running now,
    /// or from a session this file did not stamp.
    func admits(_ request: URLRequest) -> Bool {
        lock.withLock {
            guard let made = request.value(forHTTPHeaderField: Self.header), made != String(number) else { return true }
            late[name, default: 0] += 1
            return false
        }
    }
}

/// URLProtocol-backed transport shared by native editor characterization tests.
/// It keeps API tests account-free and records the exact request at the boundary.
final class NativeEditorURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard NativeEditorTestRun.shared.admits(request) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cancelled))
            return
        }
        do {
            // Handlers see the request the app built, without this file's own stamp.
            var request = request
            request.setValue(nil, forHTTPHeaderField: NativeEditorTestRun.header)
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
        configuration.httpAdditionalHeaders = [NativeEditorTestRun.header: NativeEditorTestRun.shared.current]
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

    func updateSession(_ replacement: MobileSession?, ifCurrent expected: MobileSession) throws -> SessionStoreUpdate {
        lock.withLock {
            guard sessionValue == expected else { return SessionStoreUpdate(applied: false, current: sessionValue) }
            sessionValue = replacement
            return SessionStoreUpdate(applied: true, current: sessionValue)
        }
    }
    func read() throws -> MobileSession? { lock.withLock { sessionValue } }
    func write(_ session: MobileSession) throws { lock.withLock { sessionValue = session } }
    func delete() throws { lock.withLock { sessionValue = nil } }
}
