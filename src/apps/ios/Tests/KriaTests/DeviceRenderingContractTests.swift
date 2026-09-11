import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

final class DeviceRenderingContractTests: XCTestCase {
    func testLibraryGrantUsesOpaqueAssetAndRevisionIdentity() throws {
        let identity = DeviceRenderIdentity(jobID: UUID(), variantID: "first", recipeRevision: 3, recipeDigest: String(repeating: "a", count: 64))
        let body = DeviceAssetDownloadBody(identity: identity, assetID: "music-1")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(body)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["identity", "asset_id"])
        XCTAssertEqual(object["asset_id"] as? String, "music-1")
        let nested = try XCTUnwrap(object["identity"] as? [String: Any])
        XCTAssertEqual(nested["recipe_revision"] as? Int, 3)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let target = try decoder.decode(DeviceAssetDownloadTarget.self, from: Data(#"{"asset_id":"music-1","download_url":"https://storage.example/exact?generation=42","expires_at":"2026-09-10T12:00:00Z"}"#.utf8))
        XCTAssertEqual(target.assetID, "music-1")
        XCTAssertEqual(target.downloadURL.query, "generation=42")
    }
    func testUploadAndCompleteUseBackendIdentityKeys() throws {
        let identity = DeviceRenderIdentity(jobID: UUID(), variantID: "original_text", recipeRevision: 7, recipeDigest: String(repeating: "a", count: 64))
        let attempt = UUID()
        let upload = DeviceExportUploadBody(identity: identity, attemptID: attempt, fileSizeBytes: 123, sha256: String(repeating: "b", count: 64))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(upload)) as? [String: Any])
        XCTAssertEqual(object["attempt_id"] as? String, attempt.uuidString)
        XCTAssertEqual(object["file_size_bytes"] as? Int, 123)
        let nested = try XCTUnwrap(object["identity"] as? [String: Any])
        XCTAssertEqual(nested["job_id"] as? String, identity.jobID.uuidString)
        XCTAssertEqual(nested["variant_id"] as? String, "original_text")
        XCTAssertEqual(nested["recipe_revision"] as? Int, 7)
        XCTAssertEqual(nested["recipe_digest"] as? String, identity.recipeDigest)
        let complete = DeviceExportCompleteBody(identity: identity, attemptID: attempt)
        let completion = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(complete)) as? [String: Any])
        XCTAssertEqual(Set(completion.keys), ["identity", "attempt_id"])
    }

    func testStatusPreservesMediaEngineRecipeAndAnimation() throws {
        let identity = DeviceRenderIdentity(jobID: UUID(), variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64))
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, timelineStart: 1, text: TextTreatment(text: "Hello", animation: .fadeScale))])])
        let request = DeviceRenderRequest(identity: identity, recipe: recipe)
        let raw = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(request))
        let data = try JSONSerialization.data(withJSONObject: ["phase": "awaiting_device", "request": raw])
        let status = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: data)
        XCTAssertEqual(status.request, request)
        XCTAssertEqual(status.request.recipe.tracks[0].clips[0].text?.animation, .fadeScale)
        XCTAssertEqual(status.request.recipe.tracks[0].clips[0].timelineStart, 1)
    }
    @MainActor func testProxyRecoveryKeepsItsOriginalBinding() throws {
        let proxy = AnalysisProxyDescriptor(original: OriginalMediaDescriptor(
            sha256: String(repeating: "a", count: 64), byteCount: 4_000,
            durationS: 2, width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true
        ), durationS: 2, width: 360, height: 640, frameRate: 30)
        let contract = ProjectMediaUploadContract(purpose: .analysisProxy, proxy: proxy)
        let record = UploadRecoveryRecord(id: UUID(), projectID: UUID(), localFilePath: "/proxy.mp4", filename: "clip.mp4", source: .files, purpose: .analysisProxy, uploadContract: contract, taskIdentifier: 1, retryCount: 0)
        let restored = try JSONDecoder().decode(UploadRecoveryRecord.self, from: JSONEncoder().encode(record))
        XCTAssertEqual(restored.uploadContract, contract)
        XCTAssertNoThrow(try BackgroundUploadCoordinator.validateProjectUploadPurpose(restored.purpose, contract: restored.uploadContract))
        XCTAssertThrowsError(try BackgroundUploadCoordinator.validateProjectUploadPurpose(.cloudRenderSource, contract: contract))
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(contract)) as? [String: Any])
        XCTAssertEqual(wire["purpose"] as? String, "analysis_proxy")
        let payload = try XCTUnwrap(wire["proxy"] as? [String: Any])
        XCTAssertEqual(payload["timing_version"] as? Int, 1)
        XCTAssertEqual(payload["duration_s"] as? Double, 2)
        let original = try XCTUnwrap(payload["original"] as? [String: Any])
        XCTAssertEqual(original["byte_count"] as? Int, 4_000)
        XCTAssertEqual(original["has_audio"] as? Bool, true)
    }

}
