import XCTest
@testable import Kria

/// An unreadable Keychain (for example a locked device) is an unknown owner, never a different one.
/// The remembered owner must not survive sign-out, expiry, an account switch or a proven-missing session.
@MainActor final class LocalStorageIdentityTests: XCTestCase {
    func testUnreadableStoreKeepsTheOwnerThisSessionAlreadyProved() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        let proved = try XCTUnwrap(auth.localStorageIdentity)

        store.unreadable = true
        XCTAssertEqual(auth.localStorageIdentity, proved, "A locked Keychain must not look like an owner change")
        store.unreadable = false
        XCTAssertEqual(auth.localStorageIdentity, proved)
    }

    func testUnreadableStoreBeforeAnyProofStaysUnknown() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        store.unreadable = true
        XCTAssertNil(auth.localStorageIdentity, "Nothing was proved yet, so there is no owner to fall back to")
    }

    func testSignOutForgetsTheOwner() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        XCTAssertNotNil(auth.localStorageIdentity)
        auth.signOut()
        store.unreadable = true
        XCTAssertNil(auth.localStorageIdentity)
    }

    func testAccountSwitchNeverInheritsThePreviousOwner() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        let first = try XCTUnwrap(auth.localStorageIdentity)
        try auth.signIn(with: session("account-b"), displayName: nil)
        store.unreadable = true
        XCTAssertNil(auth.localStorageIdentity, "B has proved nothing yet, and A's owner must not stand in")
        store.unreadable = false
        let second = try XCTUnwrap(auth.localStorageIdentity)
        XCTAssertNotEqual(second, first)
        store.unreadable = true
        XCTAssertEqual(auth.localStorageIdentity, second)
    }

    func testProvenMissingSessionClearsTheOwner() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        XCTAssertNotNil(auth.localStorageIdentity)
        try store.delete()
        XCTAssertNil(auth.localStorageIdentity, "A readable but empty store proves the session is gone")
        store.unreadable = true
        XCTAssertNil(auth.localStorageIdentity)
    }

    func testFailedSignInWriteKeepsTheCurrentAccountsOwner() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SwitchableTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: session("account-a"), displayName: nil)
        let proved = try XCTUnwrap(auth.localStorageIdentity)
        store.unreadable = true
        store.failsWrites = true
        XCTAssertThrowsError(try auth.signIn(with: session("account-b"), displayName: nil))
        XCTAssertEqual(auth.localStorageIdentity, proved, "A sign-in that never reached the store changes nothing")
    }

    private func makeDefaults() throws -> (UserDefaults, String) {
        let suite = "kria.local-identity.\(UUID().uuidString)"
        return (try XCTUnwrap(UserDefaults(suiteName: suite)), suite)
    }

    private func session(_ subject: String) -> MobileSession {
        let header = Data(#"{"alg":"none"}"#.utf8).base64EncodedString().trimmingCharacters(in: CharacterSet(charactersIn: "="))
        let payload = Data(#"{"sub":"\#(subject)"}"#.utf8).base64EncodedString().trimmingCharacters(in: CharacterSet(charactersIn: "="))
        return MobileSession(accessToken: "\(header).\(payload).signature", refreshToken: "refresh-\(subject)", expiresIn: 3600)
    }
}

private final class SwitchableTokenStore: TokenStore, @unchecked Sendable {
    struct Locked: Error {}
    private let lock = NSLock()
    private var session: MobileSession?
    private var locked = false
    var unreadable: Bool {
        get { lock.withLock { locked } }
        set { lock.withLock { locked = newValue } }
    }
    func read() throws -> MobileSession? { try lock.withLock { if locked { throw Locked() }; return session } }
    var failsWrites = false
    func write(_ session: MobileSession) throws { try lock.withLock { if failsWrites { throw Locked() }; self.session = session } }
    func delete() throws { lock.withLock { session = nil } }
    func updateSession(_ replacement: MobileSession?, ifCurrent expected: MobileSession) throws -> SessionStoreUpdate {
        try lock.withLock {
            if locked { throw Locked() }
            guard session == expected else { return SessionStoreUpdate(applied: false, current: session) }
            session = replacement
            return SessionStoreUpdate(applied: true, current: session)
        }
    }
}
