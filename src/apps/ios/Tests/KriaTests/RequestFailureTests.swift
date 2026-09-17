import XCTest
@testable import Kria

/// An HTTP error means the server was reached, so recovery copy must not blame
/// the connection. On 2026-09-16 a 500 from "Create this video" was shown as
/// "Connection interrupted" with a Reconnect button.
final class RequestFailureTests: XCTestCase {
    private let serverMessage = "Kria hit a problem on its side. Your chat and footage are safe. Try again in a moment."

    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testServerStatusesSayTheProblemIsOnKriasSide() {
        for status in [500, 502, 503, 504, 599] {
            let error = APIError.requestFailed(status: status)
            XCTAssertEqual(RequestFailureCause(error), .server, "HTTP \(status)")
            XCTAssertEqual(error.localizedDescription, serverMessage, "HTTP \(status)")
        }
    }

    func testOtherStatusesUseNeutralCopy() {
        for status in [400, 403, 404, 422, 429, 499, 600] {
            let error = APIError.requestFailed(status: status)
            XCTAssertEqual(RequestFailureCause(error), .other, "HTTP \(status)")
            XCTAssertEqual(error.localizedDescription, "Kria couldn’t complete that request.", "HTTP \(status)")
        }
        XCTAssertEqual(APIError.invalidResponse.localizedDescription, "Kria couldn’t complete that request.")
    }

    func testOnlyTransportFailuresBlameTheConnection() {
        let transport: [Error] = [
            URLError(.notConnectedToInternet), URLError(.timedOut), URLError(.networkConnectionLost),
            URLError(.cannotConnectToHost), NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet),
            APIError.offline,
        ]
        for error in transport {
            XCTAssertEqual(RequestFailureCause(error), .connection, "\(error)")
        }
        XCTAssertEqual(APIError.offline.localizedDescription, "Kria couldn’t complete that request. Check your connection and try again.")

        let responded: [Error] = [
            APIError.requestFailed(status: 500), APIError.requestFailed(status: 404), APIError.invalidResponse,
            APIError.sessionExpired, URLError(.cancelled), URLError(.badServerResponse), CancellationError(),
        ]
        for error in responded {
            XCTAssertNotEqual(RequestFailureCause(error), .connection, "\(error)")
        }
    }

    func testKriaAPIKeepsTheStatusOfAFailedRequest() async {
        for status in [500, 503, 404] {
            NativeEditorURLProtocol.handler = { _ in (status, Data(#"{"detail":"Internal Server Error"}"#.utf8)) }
            do {
                _ = try await NativeEditorTestSupport.api().project(threadID: UUID())
                XCTFail("Expected HTTP \(status) to throw")
            } catch {
                XCTAssertEqual(error as? APIError, .requestFailed(status: status))
            }
        }
    }

    /// The incident path: a failed chat action shows "That change wasn’t saved."
    /// followed by the explanation of the error.
    func testCreateThisVideoServerErrorIsNotShownAsAConnectionProblem() async {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path.hasSuffix("/actions"), true)
            return (500, Data(#"{"detail":"Internal Server Error"}"#.utf8))
        }
        do {
            _ = try await NativeEditorTestSupport.api().creationAction(
                threadID: UUID(), action: "generate", payload: [:], expectedRevision: 3, clientActionID: UUID().uuidString
            )
            XCTFail("Expected the server error to throw")
        } catch {
            let failure = ChatFailure("That change wasn’t saved.", error: error)
            XCTAssertEqual(failure.title, "Something went wrong")
            XCTAssertEqual(failure.retryLabel, "Refresh")
            XCTAssertEqual(failure.message, "That change wasn’t saved. \(serverMessage)")
            XCTAssertFalse(failure.message.localizedCaseInsensitiveContains("connection"))
        }
    }

    func testTransportFailureKeepsConnectionRecovery() {
        let failure = ChatFailure("That change wasn’t saved.", error: URLError(.notConnectedToInternet))

        XCTAssertEqual(failure.title, "Connection interrupted")
        XCTAssertEqual(failure.retryLabel, "Reconnect")
        XCTAssertEqual(failure.message, "That change wasn’t saved. Check your connection and try again.")
    }

    func testRejectedRequestsAndLocalProblemsDoNotAskToReconnect() {
        let rejected = ChatFailure("That change wasn’t saved.", error: APIError.requestFailed(status: 422))
        XCTAssertEqual(rejected.message, "That change wasn’t saved. Kria couldn’t complete that request.")
        XCTAssertEqual(rejected.title, "Something went wrong")
        XCTAssertEqual(rejected.retryLabel, "Refresh")

        let conflict = ChatFailure("This project changed. Review the latest options and try again.")
        XCTAssertEqual(conflict.title, "Something went wrong")
        XCTAssertEqual(conflict.retryLabel, "Refresh")
    }
}
