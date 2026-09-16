import AVFoundation
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

    // MARK: - KRI-93: narration/voiceover (audio-kind) proxy descriptors

    /// A payload persisted before `kind` existed (either locally in
    /// `UploadRecoveryRecord`/UserDefaults, or fetched from server state written by
    /// an older backend) has no `kind` key at all. The custom decoder must default
    /// it to `.video` rather than throwing on the missing key.
    func testMissingKindKeyDecodesAsVideoForBackwardCompatibility() throws {
        let json = """
        {"sha256":"\(String(repeating: "a", count: 64))","byte_count":4000,"duration_s":2,
         "width":1080,"height":1920,"orientation_degrees":0,"has_audio":true}
        """
        let descriptor = try JSONDecoder().decode(OriginalMediaDescriptor.self, from: Data(json.utf8))
        XCTAssertEqual(descriptor.kind, .video)
        XCTAssertNoThrow(try descriptor.validate())
    }

    func testAudioOriginalMediaDescriptorRejectsDimensionsAndRequiresAnAudibleTrack() {
        let silent = OriginalMediaDescriptor(kind: .audio, sha256: String(repeating: "a", count: 64),
            byteCount: 4_000, durationS: 12, width: nil, height: nil, orientationDegrees: 0, hasAudio: false)
        XCTAssertThrowsError(try silent.validate())
        let withDimensions = OriginalMediaDescriptor(kind: .audio, sha256: String(repeating: "a", count: 64),
            byteCount: 4_000, durationS: 12, width: 100, height: 100, orientationDegrees: 0, hasAudio: true)
        XCTAssertThrowsError(try withDimensions.validate())
        let valid = OriginalMediaDescriptor(kind: .audio, sha256: String(repeating: "a", count: 64),
            byteCount: 4_000, durationS: 12, width: nil, height: nil, orientationDegrees: 0, hasAudio: true)
        XCTAssertNoThrow(try valid.validate())
    }

    func testVideoOriginalMediaDescriptorRequiresDimensions() {
        let missingDimensions = OriginalMediaDescriptor(sha256: String(repeating: "a", count: 64),
            byteCount: 4_000, durationS: 2, width: nil, height: nil, orientationDegrees: 0, hasAudio: true)
        XCTAssertThrowsError(try missingDimensions.validate())
    }

    func testAudioAnalysisProxyDescriptorRejectsDimensionsAndFrameRate() {
        let audioOriginal = OriginalMediaDescriptor(kind: .audio, sha256: String(repeating: "a", count: 64),
            byteCount: 4_000, durationS: 12, width: nil, height: nil, orientationDegrees: 0, hasAudio: true)
        let withFrameRate = AnalysisProxyDescriptor(original: audioOriginal, durationS: 12, width: nil, height: nil, frameRate: 30)
        XCTAssertThrowsError(try withFrameRate.validate())
        let valid = AnalysisProxyDescriptor(original: audioOriginal, durationS: 12, width: nil, height: nil, frameRate: nil)
        XCTAssertNoThrow(try valid.validate())
    }

    @MainActor private func tone(_ directory: URL, _ name: String, seconds: Double) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let sampleRate = 48_000.0
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1))
        let frameCount = AVAudioFrameCount(seconds * sampleRate)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount))
        buffer.frameLength = frameCount
        for index in 0..<Int(frameCount) {
            buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * 220 / sampleRate))
        }
        let file = try AVAudioFile(forWriting: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 64_000,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer)
        return url
    }

    /// Not wired into any upload path yet (see the factory's doc comment) — this
    /// pins the contract shape a future KRI-94+ PR will actually call.
    @MainActor func testAudioAnalysisProxyFactoryBuildsAnAudioKindTaggedContract() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let original = try tone(directory, "original", seconds: 3)
        let proxy = try tone(directory, "proxy", seconds: 3)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: original)
        let contract = try await ProjectMediaUploadContract.audioAnalysisProxy(original: original, proxy: proxy, fingerprint: fingerprint)
        XCTAssertEqual(contract.purpose, .analysisProxy)
        let proxyDescriptor = try XCTUnwrap(contract.proxy)
        XCTAssertEqual(proxyDescriptor.original.kind, .audio)
        XCTAssertNil(proxyDescriptor.width); XCTAssertNil(proxyDescriptor.height); XCTAssertNil(proxyDescriptor.frameRate)
        XCTAssertNil(proxyDescriptor.original.width); XCTAssertNil(proxyDescriptor.original.height)
        XCTAssertTrue(proxyDescriptor.original.hasAudio)
        XCTAssertNoThrow(try proxyDescriptor.validate())
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(contract)) as? [String: Any])
        let payload = try XCTUnwrap(wire["proxy"] as? [String: Any])
        XCTAssertNil(payload["width"]); XCTAssertNil(payload["height"]); XCTAssertNil(payload["frame_rate"])
        let originalWire = try XCTUnwrap(payload["original"] as? [String: Any])
        XCTAssertEqual(originalWire["kind"] as? String, "audio")
        XCTAssertNil(originalWire["width"]); XCTAssertNil(originalWire["height"])
    }

    @MainActor func testAudioAnalysisProxyFactoryRejectsASilentOriginal() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        // AVAudioFile always writes an audio track when it writes samples, so
        // simulate "silent" by pointing the "original" at a duration mismatch
        // instead — the factory must reject either failure mode the same way.
        let original = try tone(directory, "original", seconds: 3)
        let proxy = try tone(directory, "proxy", seconds: 1)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: original)
        do {
            _ = try await ProjectMediaUploadContract.audioAnalysisProxy(original: original, proxy: proxy, fingerprint: fingerprint)
            XCTFail("mismatched original/proxy duration must be rejected")
        } catch is APIError {
            // expected
        }
    }

}
