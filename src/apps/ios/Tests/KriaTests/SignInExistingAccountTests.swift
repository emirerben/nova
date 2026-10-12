import XCTest
@testable import Kria

/// KRI-609: Sign in with Apple on an email that already has a Kria account made
/// with Google answers `409 {"detail": {"code": "provider_link_required",
/// "message": ...}}`. The app dropped that object detail and showed the editor's
/// "This edit changed elsewhere…" line in small grey text, so the creator saw
/// nothing useful and stayed on the sign-in screen.
@MainActor final class SignInExistingAccountTests: XCTestCase {
    private static let serverMessage = "This email already has a Kria account. Sign in with Google to open it."

    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func exchangeError(status: Int, body: String) async throws -> APIError {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/auth/mobile/exchange")
            return (status, Data(body.utf8))
        }
        do {
            _ = try await NativeEditorTestSupport.api().exchangeMobileToken(
                AuthCredential(token: "apple-id-token", displayName: nil, nonce: "nonce"),
                provider: "apple"
            )
        } catch let error as APIError {
            return error
        }
        XCTFail("Expected the exchange to fail")
        throw APIError.invalidResponse
    }

    // MARK: 409 decoding

    func testObjectDetailConflictCarriesTheServersCodeAndMessage() async throws {
        let error = try await exchangeError(
            status: 409,
            body: #"{"detail":{"code":"provider_link_required","message":"\#(Self.serverMessage)","existing_provider":"google"}}"#
        )

        XCTAssertEqual(error, .conflict)
        XCTAssertEqual(error.conflictCode, "provider_link_required")
        XCTAssertEqual(error.conflictDetail, Self.serverMessage)
        XCTAssertFalse(String(describing: error).contains("Google"), "Diagnostics must not copy server text")
    }

    func testStringDetailConflictIsUnchanged() async throws {
        let error = try await exchangeError(status: 409, body: #"{"detail":"Creation thread changed"}"#)

        XCTAssertEqual(error, .conflict)
        XCTAssertEqual(error.conflictDetail, "Creation thread changed")
        XCTAssertNil(error.conflictCode)
    }

    func testProblemEnvelopeConflictIsUnchanged() async throws {
        let error = try await exchangeError(
            status: 409,
            body: #"{"problem":{"code":"speech_cleanup_pending","message":"Kria is still checking the speech."}}"#
        )

        XCTAssertEqual(error.conflictCode, "speech_cleanup_pending")
        XCTAssertEqual(error.conflictDetail, "Kria is still checking the speech.")
    }

    // MARK: Sign-in copy

    func testSignInShowsTheServersExistingAccountMessage() {
        let error = APIError.conflict(detail: ConflictDetail(Self.serverMessage, code: "provider_link_required"))

        XCTAssertEqual(SignInFailureCopy.message(for: error), Self.serverMessage)
    }

    func testSignInFallsBackWhenTheServerSentNoUsableMessage() {
        let details = [
            ConflictDetail(nil, code: "provider_link_required"),
            ConflictDetail("   ", code: "provider_link_required"),
            // The server's wording before KRI-609 (still live until the API deploys).
            ConflictDetail("Sign in with the existing provider or explicitly link this provider first", code: "provider_link_required"),
        ]
        for detail in details {
            XCTAssertEqual(SignInFailureCopy.message(for: APIError.conflict(detail: detail)), SignInFailureCopy.existingAccount)
        }
        XCTAssertEqual(
            SignInFailureCopy.existingAccount,
            "This email already has a Kria account. Sign in with the method you used before."
        )
    }

    func testSignInNeverShowsTheEditorConflictCopy() {
        let editorCopy = APIError.conflict.errorDescription ?? ""
        XCTAssertTrue(editorCopy.contains("changed elsewhere"))
        for error in [APIError.conflict, APIError.conflict(detail: ConflictDetail("Creation thread changed"))] {
            let message = SignInFailureCopy.message(for: error)
            XCTAssertNotEqual(message, editorCopy)
            XCTAssertFalse(message.contains("changed elsewhere"))
            XCTAssertFalse(message.contains("Creation thread"))
        }
    }

    func testOtherSignInFailuresKeepTheirCopy() {
        XCTAssertEqual(SignInFailureCopy.message(for: AuthError.cancelled), AuthError.cancelled.localizedDescription)
        XCTAssertEqual(SignInFailureCopy.message(for: APIError.offline), APIError.offline.localizedDescription)
        XCTAssertEqual(
            SignInFailureCopy.message(for: APIError.requestFailed(status: 401)),
            APIError.requestFailed(status: 401).localizedDescription
        )
    }
}
