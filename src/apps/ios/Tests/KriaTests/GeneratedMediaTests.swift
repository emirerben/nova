import XCTest
@testable import Kria

final class GeneratedMediaTests: XCTestCase {
    // Failure cases: old servers exposing controls, unknown results retried as generation,
    // source edits losing immutable generation, and acceptance before a usable preview.
    func testOldServersKeepGenerationHidden() throws {
        let value = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertNotEqual(value.generatedImages, true)
        XCTAssertNotEqual(value.generatedImageEditing, true)
        XCTAssertNotEqual(value.generatedNarration, true)
    }

    func testUnknownAndFailedOperationsNeverPollOrBecomeUsable() throws {
        for state in ["unknown", "failed"] {
            let value = try JSONDecoder().decode(GeneratedMediaOperation.self, from: Data("{\"id\":\"op\",\"state\":\"\(state)\",\"capability\":\"image\",\"retryable\":true}".utf8))
            XCTAssertFalse(value.shouldPoll)
            XCTAssertFalse(value.canAccept)
        }
    }

    func testReadyRequiresPreviewAndAssetBeforeUse() throws {
        let value = try JSONDecoder().decode(GeneratedMediaOperation.self, from: Data(#"{"id":"op","state":"ready","capability":"image","retryable":false}"#.utf8))
        XCTAssertFalse(value.canAccept)
    }

    func testImageEditCarriesSourceGenerationAndStableEvent() throws {
        let request = GeneratedMediaRequest(clientEventID: "event", capability: .imageEdit, prompt: "Remove the sign", editableTitle: "Street", sourceAssetID: "asset", sourceGeneration: "42")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(body["client_event_id"] as? String, "event")
        XCTAssertEqual(body["source_generation"] as? String, "42")
        XCTAssertEqual(body["capability"] as? String, "image_edit")
        XCTAssertNil(body["script_version"])
    }
    func testResumedNarrationRefusesChangedScript() throws {
        let value = try JSONDecoder().decode(GeneratedMediaOperation.self, from: Data(#"{"id":"op","state":"ready","capability":"narration","retryable":false,"script_id":"script","script_version":2,"approved_script_sha256":"original"}"#.utf8))
        let changed = GeneratedNarrationScript(scriptID: "script", scriptVersion: 3, text: "Changed", approvedScriptSHA256: "changed")
        XCTAssertFalse(value.matchesApproval(sourceAssetID: nil, sourceGeneration: nil, script: changed))
        let original = GeneratedNarrationScript(scriptID: "script", scriptVersion: 2, text: "Original", approvedScriptSHA256: "original")
        XCTAssertTrue(value.matchesApproval(sourceAssetID: nil, sourceGeneration: nil, script: original))
    }

    func testResumedImageEditRequiresOriginalSourceGeneration() throws {
        let value = try JSONDecoder().decode(GeneratedMediaOperation.self, from: Data(#"{"id":"op","state":"ready","capability":"image_edit","retryable":false,"source_asset_id":"asset","source_generation":"42"}"#.utf8))
        XCTAssertTrue(value.matchesApproval(sourceAssetID: "asset", sourceGeneration: "42", script: nil))
        XCTAssertFalse(value.matchesApproval(sourceAssetID: "asset", sourceGeneration: "43", script: nil))
        XCTAssertFalse(value.matchesApproval(sourceAssetID: "other", sourceGeneration: "42", script: nil))
    }

    func testNarrationReadyUsesOperationReceiptWithoutVisualPoolAsset() throws {
        let value = try JSONDecoder().decode(GeneratedMediaOperation.self, from: Data(#"{"id":"op","state":"ready","capability":"narration","retryable":false,"asset_id":null,"preview_url":"https://example.test/voice.wav"}"#.utf8))
        XCTAssertTrue(value.canAccept)
    }

    @MainActor func testDefiniteRejectionClearsReceiptAndRequiresExplicitReset() {
        let draft = GeneratedMediaDraft(capability: .image)
        draft.rememberRequest()
        XCTAssertTrue(draft.handleSubmissionFailure(APIError.conflict))
        XCTAssertNil(draft.recovery)
        XCTAssertNotNil(draft.submissionRejection)
        draft.resetRejectedRequest()
        XCTAssertNil(draft.submissionRejection)
        XCTAssertNil(draft.recovery)
    }

    @MainActor func testUncertainFailuresKeepStableReceiptForStatusOnlyRecovery() {
        for error: Error in [URLError(.timedOut), APIError.requestFailed(status: 503),
                             APIError.requestFailed(status: 408), APIError.requestFailed(status: 429)] {
            let draft = GeneratedMediaDraft(capability: .image)
            draft.rememberRequest()
            let event = draft.recovery?.clientEventID
            XCTAssertFalse(draft.handleSubmissionFailure(error))
            XCTAssertEqual(draft.recovery?.clientEventID, event)
            XCTAssertNil(draft.submissionRejection)
        }
    }

}
