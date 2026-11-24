import XCTest
@testable import Kria

@MainActor final class NotificationAtomicSessionTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func session(_ owner: String) -> MobileSession {
        let payload = Data("{\"sub\":\"\(owner)\"}".utf8).base64EncodedString()
        return MobileSession(accessToken: "fixture.\(payload).sig", refreshToken: owner, expiresIn: 3600)
    }

    func testSignOutInsideOldReadWriteGapCannotResurrectSession() async throws {
        try await assertInterleaving(next: nil)
    }

    func testAccountBWriteInsideOldReadWriteGapCannotBeOverwritten() async throws {
        try await assertInterleaving(next: session("atomic-b"))
    }

    /// KRI-1607: the test before this one signs in, which starts a profile read nobody awaits.
    /// When that read answered 401 here, its refresh was counted as a second interleaving.
    func testRefreshFromAnEarlierTestsClientIsNotCounted() async throws {
        let leftover = NativeEditorTestSupport.api(tokenStore: NativeEditorMemoryTokenStore(session("atomic-leftover")))
        NativeEditorTestRun.shared.testCaseWillStart(self)
        let refusedBefore = NativeEditorTestRun.shared.lateRequests
        try await assertInterleaving(next: nil, leftover: leftover)
        XCTAssertGreaterThan(NativeEditorTestRun.shared.lateRequests, refusedBefore, "The leftover client did send a request")
    }

    func testAClientMadeInThisTestReachesItsHandler() async throws {
        NativeEditorTestRun.shared.testCaseWillStart(self)
        let requests = ExpectedRequests()
        NativeEditorURLProtocol.handler = { _ in requests.increment(); return (500, Data()) }
        _ = try? await NativeEditorTestSupport.api(tokenStore: NativeEditorMemoryTokenStore(session("atomic-own"))).currentUser()
        XCTAssertEqual(requests.count, 1)
    }

    private func assertInterleaving(next: MobileSession?, leftover: KriaAPI? = nil) async throws {
        let first = session("atomic-a")
        let store = InterleavingNotificationStore(first)
        let refreshed = try JSONEncoder().encode(session("atomic-a-rotated"))
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/auth/mobile/refresh" {
                store.arm(replacement: next)
                return (200, refreshed)
            }
            return (401, Data())
        }
        let api = NativeEditorTestSupport.api(tokenStore: store)
        let owner = try XCTUnwrap(api.mediaLibraryOwner)
        do { _ = try await api.nativeActivity(id: UUID(), owner: owner) } catch {}
        if let leftover { _ = try? await leftover.currentUser() }
        XCTAssertEqual(try store.read(), next)
        XCTAssertEqual(store.interleavings, 1)
    }

    func testConditionalExpiryCannotDeleteCompetingAccount() throws {
        let first = session("delete-a"); let next = session("delete-b")
        let store = InterleavingNotificationStore(first)
        store.arm(replacement: next)
        let update = try store.updateSession(nil, ifCurrent: first)
        XCTAssertFalse(update.applied)
        XCTAssertEqual(update.current, next)
        XCTAssertEqual(try store.read(), next)
    }

    func testUnreadableStoreDoesNotLetDelayedExpirySignOutNewAccount() async throws {
        let first = session("unreadable-a"); let next = session("unreadable-b")
        let store = UnreadableNotificationStore(first)
        NativeEditorURLProtocol.handler = { _ in (500, Data()) }
        let auth = AuthStore(tokenStore: store, api: NativeEditorTestSupport.api(tokenStore: store), defaults: UserDefaults(suiteName: UUID().uuidString)!)
        try store.delete()
        try auth.signIn(with: next, displayName: "New account")
        store.failReads = true
        NotificationCenter.default.post(name: .kriaSessionExpired, object: first)
        try await Task.sleep(for: .milliseconds(40))
        store.failReads = false
        XCTAssertTrue(auth.isSignedIn)
        XCTAssertEqual(try store.read(), next)
    }

    func testDelayedExpiryEventDoesNotSignOutNewAccount() async throws {
        let first = session("expiry-a"); let next = session("expiry-b")
        let store = NativeEditorMemoryTokenStore(first)
        NativeEditorURLProtocol.handler = { _ in (500, Data()) }
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let auth = AuthStore(tokenStore: store, api: NativeEditorTestSupport.api(tokenStore: store), defaults: defaults)
        let update = try store.updateSession(nil, ifCurrent: first)
        XCTAssertTrue(update.applied)
        NotificationCenter.default.post(name: .kriaSessionExpired, object: first)
        try auth.signIn(with: next, displayName: "New account")
        try await Task.sleep(for: .milliseconds(40))
        XCTAssertTrue(auth.isSignedIn)
        XCTAssertEqual(try store.read(), next)
    }
}

private final class ExpectedRequests: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func increment() { lock.withLock { value += 1 } }
}

private final class InterleavingNotificationStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: MobileSession?
    private var armed = false
    private var next: MobileSession?
    private var count = 0
    init(_ session: MobileSession) { value = session }
    var interleavings: Int { lock.withLock { count } }
    func arm(replacement: MobileSession?) { lock.withLock { armed = true; next = replacement } }
    func read() throws -> MobileSession? {
        lock.withLock {
            let snapshot = value
            if armed { applyCompetingWriter() }
            return snapshot
        }
    }
    func write(_ session: MobileSession) throws { lock.withLock { value = session } }
    func delete() throws { lock.withLock { value = nil } }
    func updateSession(_ replacement: MobileSession?, ifCurrent expected: MobileSession) throws -> SessionStoreUpdate {
        lock.withLock {
            if armed { applyCompetingWriter() }
            guard value == expected else { return SessionStoreUpdate(applied: false, current: value) }
            value = replacement
            return SessionStoreUpdate(applied: true, current: value)
        }
    }
    private func applyCompetingWriter() {
        armed = false
        value = nil
        value = next
        count += 1
    }
}

private final class UnreadableNotificationStore: TokenStore, @unchecked Sendable {
    private let base: NativeEditorMemoryTokenStore
    private let lock = NSLock()
    private var unreadable = false
    init(_ session: MobileSession) { base = NativeEditorMemoryTokenStore(session) }
    var failReads: Bool {
        get { lock.withLock { unreadable } }
        set { lock.withLock { unreadable = newValue } }
    }
    func read() throws -> MobileSession? {
        if failReads { throw APIError.unsupported }
        return try base.read()
    }
    func write(_ session: MobileSession) throws { try base.write(session) }
    func delete() throws { try base.delete() }
}
