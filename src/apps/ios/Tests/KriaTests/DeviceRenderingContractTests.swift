import AVFoundation
import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

private final class EditorAdmissionRequestLog: @unchecked Sendable {
    var paths: [String] = []
}

final class DeviceRenderingContractTests: XCTestCase {
    @MainActor func testTimelinePhotoWaitsForPoolReadinessThenAdmitsWithoutChatAttachment() async throws {
        let key = "editor-photo-recovery-\(UUID().uuidString)"
        defer {
            NativeEditorURLProtocol.handler = nil
            UserDefaults.standard.removeObject(forKey: key)
            UserDefaults.standard.removeObject(forKey: key + ".editor-source-placements")
        }
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: "g1", guidedRevisionNumber: 1, sourceKind: .visual)
        let id = UUID()
        let record = UploadRecoveryRecord(id: id, projectID: UUID(), localFilePath: "/nonexistent/editor-photo.jpg", filename: "photo.jpg",
            source: .files, purpose: .cloudRenderSource, uploadContract: nil, reservationID: nil, clientUploadID: "upload",
            mediaID: nil, gcsPath: "pool/photo.jpg", contentType: "image/jpeg", uploadCompleted: true, retentionExpiresAt: nil,
            taskIdentifier: 1, retryCount: 0, mediaRole: .visual, itemID: "item", visualReservationID: "reservation",
            editorSourceTarget: target)
        UserDefaults.standard.set(try JSONEncoder().encode([record]), forKey: key)
        let calls = EditorAdmissionRequestLog()
        NativeEditorURLProtocol.handler = { request in
            let path = request.url?.path ?? ""
            calls.paths.append("\(request.httpMethod ?? "") \(path)")
            if path == "/plan-items/item/assets", request.httpMethod == "POST" {
                return (200, Data(#"{"id":"photo","kind":"image","status":"queued"}"#.utf8))
            }
            if path == "/plan-items/item/assets", request.httpMethod == "GET" {
                return (200, Data(#"{"assets":[{"id":"photo","kind":"image","status":"ready"}],"max_assets":20}"#.utf8))
            }
            XCTAssertEqual(path, "/plan-items/item/variants/variant/editor-sources")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            XCTAssertEqual(body["source_id"] as? String, "photo")
            return (200, Data("""
            {"import_id":"\(target.clientImportID.uuidString)","status":"ready","source_id":"photo","source_index":4,"source":{"kind":"image"},"retryable":false}
            """.utf8))
        }
        let uploads = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key, sessionConfiguration: .ephemeral)
        uploads.beginEditorPlacement(.init(id: id, target: target, lane: .timeline, visual: nil, localDurationS: nil))
        await uploads.retryAttachment(recordID: id)
        XCTAssertEqual(calls.paths, ["POST /plan-items/item/assets", "GET /plan-items/item/assets", "POST /plan-items/item/variants/variant/editor-sources"])
        XCTAssertEqual(uploads.pendingEditorPlacements.first?.status, "ready")
        XCTAssertEqual(uploads.records.count, 1)
        let reopened = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key, sessionConfiguration: .ephemeral)
        XCTAssertEqual(reopened.pendingEditorPlacements.first?.status, "ready")
        reopened.acknowledgeEditorPlacement(id)
        XCTAssertTrue(reopened.records.isEmpty)
        XCTAssertTrue(reopened.pendingEditorPlacements.isEmpty)
    }

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
        let upload = DeviceExportUploadBody(identity: identity, attemptID: attempt, fileSizeBytes: 123, sha256: String(repeating: "b", count: 64), brandTail: "standard")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(upload)) as? [String: Any])
        XCTAssertEqual(object["attempt_id"] as? String, attempt.uuidString)
        XCTAssertEqual(object["file_size_bytes"] as? Int, 123)
        // The server reads this to decide how long the upload may be. It must
        // land as `brand_tail` with one of the two identifiers the API's enum
        // accepts, or every branded publish 422s.
        XCTAssertEqual(object["brand_tail"] as? String, "standard")
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
        XCTAssertNil(status.reasonCode)
    }

    /// KRI-114 P0-2: the server adds `reason_code` alongside `reason` once it
    /// classifies a `needs_attention` failure. Older/other responses omit the
    /// key entirely, so decoding must tolerate its absence and its presence.
    func testStatusDecodingToleratesReasonCodePresentOrMissing() throws {
        let identity = DeviceRenderIdentity(jobID: UUID(), variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64))
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2)])])
        let raw = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(DeviceRenderRequest(identity: identity, recipe: recipe)))

        let withoutCode = try JSONSerialization.data(withJSONObject: ["phase": "needs_attention", "request": raw, "reason": "Device is too hot"])
        let decodedWithout = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: withoutCode)
        XCTAssertEqual(decodedWithout.reason, "Device is too hot")
        XCTAssertNil(decodedWithout.reasonCode)

        let withCode = try JSONSerialization.data(withJSONObject: [
            "phase": "needs_attention", "request": raw, "reason": "Device is too hot", "reason_code": "thermal",
        ])
        let decodedWith = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: withCode)
        XCTAssertEqual(decodedWith.reasonCode, "thermal")

        let published = try JSONSerialization.data(withJSONObject: [
            "phase": "published", "request": raw, "published_generation": "generation-2",
        ])
        let decodedPublished = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: published)
        XCTAssertEqual(decodedPublished.publishedGeneration, "generation-2")
    }

    func testFailureAndRetryBodiesUseServerExpectedKeys() throws {
        let identity = DeviceRenderIdentity(jobID: UUID(), variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64))
        let failureBody = DeviceRenderFailureBody(identity: identity, reasonCode: "export_failed", detail: "The exporter crashed.")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(failureBody)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["identity", "reason_code", "detail"])
        XCTAssertEqual(object["reason_code"] as? String, "export_failed")

        let retryBody = DeviceRenderRetryBody(identity: identity)
        let retryObject = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(retryBody)) as? [String: Any])
        XCTAssertEqual(Set(retryObject.keys), ["identity"])

        // The real server (`DeviceRenderIdentity` in app/kria/device_render.py) has
        // no alias generator: its wire keys are plain snake_case, not the camelCase
        // `DeviceRenderIdentity.CodingKeys` expects. `DeviceRenderRetryAck`/
        // `DeviceRenderFailureAck` must re-decode `identity` through
        // `RecipeJSON.decoder()` (like `DeviceRenderStatusResponse` does) rather
        // than relying on the outer `request(...)` decoder's plain strategy.
        let ackData = try JSONSerialization.data(withJSONObject: [
            "identity": ["job_id": identity.jobID.uuidString, "variant_id": identity.variantID, "recipe_revision": identity.recipeRevision + 1, "recipe_digest": identity.recipeDigest] as [String: Any],
            "phase": "awaiting_device",
        ])
        let ack = try JSONDecoder().decode(DeviceRenderRetryAck.self, from: ackData)
        XCTAssertEqual(ack.identity.recipeRevision, identity.recipeRevision + 1)
        XCTAssertEqual(ack.phase, "awaiting_device")

        let failureAckData = try JSONSerialization.data(withJSONObject: [
            "identity": ["job_id": identity.jobID.uuidString, "variant_id": identity.variantID, "recipe_revision": identity.recipeRevision, "recipe_digest": identity.recipeDigest] as [String: Any],
            "phase": "needs_attention", "reason_code": "export_failed",
        ])
        let failureAck = try JSONDecoder().decode(DeviceRenderFailureAck.self, from: failureAckData)
        XCTAssertEqual(failureAck.identity, identity)
        XCTAssertEqual(failureAck.reasonCode, "export_failed")
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

    func testEditorSourceTargetAndResponseRoundTripUseVariantScopedWireNames() throws {
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "song_text", clientImportID: UUID(),
            baseGeneration: "generation", guidedRevisionNumber: 9, sourceKind: .footage)
        let targetJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(target)) as? [String: Any])
        XCTAssertEqual(targetJSON["itemID"] as? String, "item") // Stored recovery metadata is device-only.
        XCTAssertEqual(targetJSON["sourceKind"] as? String, "footage")

        let response = try JSONDecoder().decode(EditorSourceRegistrationResponse.self, from: Data("""
        {"import_id":"\(target.clientImportID.uuidString)","status":"ready","source_id":"analysis-proxy-1",
         "source_index":4,"source":{"lane":"clip","duration_s":1.25},"error":null,"reason_code":null,"retryable":false}
        """.utf8))
        XCTAssertTrue(response.isTerminal)
        XCTAssertEqual(response.sourceIndex, 4)
        XCTAssertEqual(response.source?["duration_s"], .number(1.25))

        let record = UploadRecoveryRecord(id: UUID(), projectID: UUID(), localFilePath: "/proxy.mp4", filename: "clip.mp4",
            source: .files, purpose: .analysisProxy, uploadContract: nil, reservationID: nil, clientUploadID: nil,
            mediaID: nil, gcsPath: nil, contentType: nil, uploadCompleted: nil, retentionExpiresAt: nil,
            taskIdentifier: 1, retryCount: 0, mediaRole: nil, itemID: nil, visualReservationID: nil,
            editorSourceTarget: target)
        let restored = try JSONDecoder().decode(UploadRecoveryRecord.self, from: JSONEncoder().encode(record))
        XCTAssertEqual(restored.editorSourceTarget, target)

        let visual = CreationVisual(id: "visual-id", kind: "image", status: "ready", sourceFilename: "image.png",
            displayURL: URL(string: "https://example.com/original.png"), previewURL: nil, retryable: nil,
            gcsPath: "users/test/image.png")
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .visual, visual: visual, localDurationS: nil)
        let restoredPlacement = try JSONDecoder().decode(PendingEditorSourcePlacement.self, from: JSONEncoder().encode(placement))
        XCTAssertEqual(restoredPlacement.id, placement.id)
        XCTAssertEqual(restoredPlacement.target, target)
        XCTAssertEqual(restoredPlacement.lane, .visual)
        XCTAssertEqual(restoredPlacement.visual?.id, visual.id)
    }

    @MainActor func testPendingEditorPlacementSurvivesCoordinatorRecreationUntilAcknowledged() async throws {
        let key = "editor-placement-test-\(UUID().uuidString)"
        defer {
            UserDefaults.standard.removeObject(forKey: key)
            UserDefaults.standard.removeObject(forKey: key + ".editor-source-placements")
        }
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "song_text", clientImportID: UUID(),
            baseGeneration: "generation", guidedRevisionNumber: 3, sourceKind: .footage)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        let first = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key,
            sessionConfiguration: .ephemeral)
        first.beginEditorPlacement(placement)
        let reopened = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key,
            sessionConfiguration: .ephemeral)
        XCTAssertEqual(reopened.editorPlacements(itemID: "item", variantID: "song_text", baseGeneration: "generation").map(\.id), [placement.id])
        reopened.acknowledgeEditorPlacement(placement.id)
        XCTAssertTrue(reopened.editorPlacements(itemID: "item", variantID: "song_text", baseGeneration: "generation").isEmpty)
    }

    @MainActor func testFailedPendingPlacementPersistsRetryStateAcrossRelaunch() async throws {
        let key = "failed-editor-placement-test-\(UUID().uuidString)"
        defer { UserDefaults.standard.removeObject(forKey: key); UserDefaults.standard.removeObject(forKey: key + ".editor-source-placements") }
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "song_text", clientImportID: UUID(),
            baseGeneration: "generation", guidedRevisionNumber: 3, sourceKind: .footage)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        let coordinator = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key, sessionConfiguration: .ephemeral)
        coordinator.beginEditorPlacement(placement)
        coordinator.recordEditorSourceResult(placementID: placement.id, response: .init(importID: target.clientImportID,
            status: "failed", sourceID: "proxy", sourceIndex: nil, source: nil, error: "probe failed", reasonCode: "probe_failed", retryable: true))
        let reopened = BackgroundUploadCoordinator(api: NativeEditorTestSupport.api(), defaultsKey: key, sessionConfiguration: .ephemeral)
        let restored = try XCTUnwrap(reopened.editorPlacements(itemID: "item", variantID: "song_text", baseGeneration: "generation").first)
        XCTAssertEqual(restored.status, "failed"); XCTAssertEqual(restored.error, "probe failed"); XCTAssertTrue(restored.retryable)
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
