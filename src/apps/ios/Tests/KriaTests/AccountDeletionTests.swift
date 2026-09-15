import XCTest
@testable import Kria

@MainActor final class AccountDeletionTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testRequestPreservesLegacyResponseAndReadsAppleRequirements() async throws {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/account/delete-request")
            XCTAssertEqual(request.httpMethod, "POST")
            return (202, Data(#"{"requested":true}"#.utf8))
        }
        let api = NativeEditorTestSupport.api()
        let legacy = try await api.requestAccountDeletion()
        XCTAssertTrue(legacy.requested)
        XCTAssertEqual(legacy.appleAuthorizationCount, 0)
        NativeEditorURLProtocol.handler = { _ in
            (202, Data(#"{"requested":true,"apple_authorization_required":true,"apple_authorization_count":2}"#.utf8))
        }
        let current = try await api.requestAccountDeletion()
        XCTAssertEqual(current.appleAuthorizationCount, 2)
    }

    func testConfirmationSendsProofsAndAcceptsEmpty204() async throws {
        let store = NativeEditorMemoryTokenStore(.init(accessToken: "test-access", refreshToken: "test-refresh", expiresIn: 3600))
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/me/account/delete-confirm")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-access")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["token"] as? String, "confirmation")
            let proofs = try XCTUnwrap(body["apple_authorizations"] as? [[String: String]])
            XCTAssertEqual(proofs, [["id_token": "id-token", "nonce": "nonce", "authorization_code": "single-use-code"]])
            return (204, Data())
        }
        try await NativeEditorTestSupport.api(tokenStore: store).confirmAccountDeletion(.init(
            token: "confirmation", appleAuthorizations: [.init(idToken: "id-token", nonce: "nonce", authorizationCode: "single-use-code")]
        ))
    }

    func testInvalidConfirmationAndUnavailableServiceAreActionable() async throws {
        for (status, expected) in [(400, AccountDeletionError.invalidConfirmation), (503, .unavailable)] {
            NativeEditorURLProtocol.handler = { _ in (status, Data()) }
            do {
                try await NativeEditorTestSupport.api().confirmAccountDeletion(.init(token: "wrong", appleAuthorizations: []))
                XCTFail("Expected account error")
            } catch let error as AccountDeletionError { XCTAssertEqual(error, expected) }
        }
    }

    func testModelRequiresEveryAppleAccountAndDropsSingleUseProofAfterFailure() async {
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/me/account/delete-request" {
                return (202, Data(#"{"requested":true,"apple_authorization_required":true,"apple_authorization_count":2}"#.utf8))
            }
            return (400, Data())
        }
        let model = AccountDeletionModel(api: NativeEditorTestSupport.api())
        await model.requestCode()
        XCTAssertFalse(model.hasRequiredAppleAuthorization)
        let proof = AppleDeletionAuthorization(idToken: "id", nonce: "nonce", authorizationCode: "code")
        model.authorizeAppleAccount(subject: "first", authorization: proof)
        model.authorizeAppleAccount(subject: "first", authorization: proof)
        XCTAssertEqual(model.authorizedAppleAccounts, 1, "Repeated SIWA for one account must not count as two")
        XCTAssertFalse(model.hasRequiredAppleAuthorization)
        model.authorizeAppleAccount(subject: "second", authorization: proof)
        XCTAssertTrue(model.hasRequiredAppleAuthorization)
        let deleted = await model.confirm(token: "email-code")
        XCTAssertFalse(deleted)
        XCTAssertEqual(model.authorizedAppleAccounts, 0)
        XCTAssertNotNil(model.errorMessage)
    }

    func testModelRecoversWhenAppleIdentityWasLinkedAfterEmailRequest() async {
        NativeEditorURLProtocol.handler = { request in
            if request.url?.path == "/me/account/delete-request" { return (202, Data(#"{"requested":true}"#.utf8)) }
            return (409, Data(#"{"detail":{"code":"apple_authorization_required","apple_authorization_count":1}}"#.utf8))
        }
        let model = AccountDeletionModel(api: NativeEditorTestSupport.api())
        await model.requestCode()
        let deleted = await model.confirm(token: "code")
        XCTAssertFalse(deleted)
        XCTAssertEqual(model.appleAuthorizationCount, 1)
        XCTAssertFalse(model.hasRequiredAppleAuthorization)
        XCTAssertNotNil(model.errorMessage)
    }

    func testUnrequestedOrEmptyConfirmationNeverCallsAPI() async {
        NativeEditorURLProtocol.handler = { _ in XCTFail("No request should be made"); return (500, Data()) }
        let model = AccountDeletionModel(api: NativeEditorTestSupport.api())
        let unrequested = await model.confirm(token: "code")
        let empty = await model.confirm(token: "   ")
        XCTAssertFalse(unrequested)
        XCTAssertFalse(empty)
    }
}
