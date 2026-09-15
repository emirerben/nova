import XCTest
@testable import Kria

@MainActor
final class AIConsentTests: XCTestCase {
    func testConsentPersistsForTheSameSignedInAccount() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let initialSession = session(subject: "account-a", refreshToken: "refresh-a")
        let store = ConsentTokenStore()
        let auth = AuthStore(tokenStore: store, defaults: defaults)

        try auth.signIn(with: initialSession, displayName: nil)
        XCTAssertFalse(auth.hasAIConsent)
        auth.acceptAIConsent()
        XCTAssertTrue(auth.hasAIConsent)

        try store.write(session(subject: "account-a", refreshToken: "rotated-refresh-a"))
        let restored = AuthStore(tokenStore: store, defaults: defaults)
        XCTAssertTrue(restored.hasAIConsent)
    }

    func testConsentDoesNotCrossAccounts() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = ConsentTokenStore()
        let first = session(subject: "account-a", refreshToken: "refresh-a")
        let second = session(subject: "account-b", refreshToken: "refresh-b")

        let auth = AuthStore(tokenStore: store, defaults: defaults)
        try auth.signIn(with: first, displayName: nil)
        auth.acceptAIConsent()
        try auth.signIn(with: second, displayName: nil)
        XCTAssertFalse(auth.hasAIConsent)
    }

    func testSignedOutRelaunchDoesNotExposeOrAcceptStoredConsent() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = ConsentTokenStore()
        let current = session(subject: "account-a", refreshToken: "refresh-a")
        let signedIn = AuthStore(tokenStore: store, defaults: defaults)
        try signedIn.signIn(with: current, displayName: nil)
        signedIn.acceptAIConsent()
        XCTAssertTrue(signedIn.hasAIConsent)

        defaults.set(true, forKey: "kria.mobile-session.invalidated")
        let signedOut = AuthStore(tokenStore: store, defaults: defaults)
        XCTAssertFalse(signedOut.isSignedIn)
        XCTAssertFalse(signedOut.hasAIConsent)
        signedOut.acceptAIConsent()
        XCTAssertFalse(signedOut.hasAIConsent)
    }

    private func makeDefaults() throws -> (UserDefaults, String) {
        let suite = "kria.tests.ai-consent.\(UUID().uuidString)"
        return (try XCTUnwrap(UserDefaults(suiteName: suite)), suite)
    }

    private func session(subject: String, refreshToken: String) -> MobileSession {
        let header = Data(#"{"alg":"none"}"#.utf8).base64EncodedString().trimmingCharacters(in: CharacterSet(charactersIn: "="))
        let payload = Data(#"{"sub":"\#(subject)"}"#.utf8).base64EncodedString().trimmingCharacters(in: CharacterSet(charactersIn: "="))
        return MobileSession(accessToken: "\(header).\(payload).signature", refreshToken: refreshToken, expiresIn: 3600)
    }
}

private final class ConsentTokenStore: TokenStore, @unchecked Sendable {
    private var session: MobileSession?
    func read() throws -> MobileSession? { session }
    func write(_ session: MobileSession) throws { self.session = session }
    func delete() throws { session = nil }
}
