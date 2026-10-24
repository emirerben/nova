import XCTest
@testable import Kria

@MainActor final class CameraRollRecoveryTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private let held = UUID(uuidString: "11111111-1111-1111-1111-111111111111")!
    private let successor = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!

    private func attempt(returnedID: String, status: String) async throws -> TurnAccepted {
        let original = held
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["recover_held_turn_id"] as? String, original.uuidString)
            XCTAssertEqual(body["client_event_id"] as? String, "stable-selection")
            XCTAssertEqual(body["expected_thread_revision"] as? Int, 9)
            return (200, try JSONSerialization.data(withJSONObject: ["turn_id": returnedID, "thread_revision": 10, "status": status, "replayed": true]))
        }
        let story = CameraRollStory(id: UUID(), revision: 2, title: "Crossing", hook: "Crossing", payoff: "Arrival", rationale: "Journey", duration: 14, moments: [], rejected: false, audioPolicy: "original")
        return try await NativeEditorTestSupport.api().recoverCameraRollStory(collectionID: UUID(), story: story, collectionRevision: 7, threadID: UUID(), threadRevision: 9, eventID: "stable-selection", heldTurnID: held)
    }

    func testOriginalHeldTurnFromOldServerCannotSucceedAsRecovery() async throws {
        do {
            _ = try await attempt(returnedID: held.uuidString.lowercased(), status: "completed")
            XCTFail("HTTP200 replaying the original hold is not recovery")
        } catch { XCTAssertEqual(error as? APIError, .invalidResponse) }
    }

    func testMalformedSuccessorIdentityCannotSucceedAsRecovery() async throws {
        do {
            _ = try await attempt(returnedID: "invalid-turn", status: "pending")
            XCTFail("A malformed identity cannot replace the held selection")
        } catch { XCTAssertEqual(error as? APIError, .invalidResponse) }
    }

    func testDistinctSuccessorAndItsReplayRemainAsynchronous() async throws {
        for status in ["pending", "planning", "awaiting_approval", "completed"] {
            let accepted = try await attempt(returnedID: successor.uuidString, status: status)
            XCTAssertEqual(UUID(uuidString: accepted.turnID), successor)
            XCTAssertEqual(accepted.status, status)
        }
    }
}
