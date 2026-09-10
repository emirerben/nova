import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

private actor SessionExport: LocalExporting {
    var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("test export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}
private struct SessionSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}
private actor SessionPublisher: DeviceRenderPublishing {
    var shouldFail = false
    func fail(_ value: Bool) { shouldFail = value }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID) async throws -> DevicePublication {
        if shouldFail { throw APIError.requestFailed }
        return .published
    }
}

@MainActor final class DeviceRenderSessionTests: XCTestCase {
    private func request(_ job: UUID) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
                            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])]))
    }
    private let enabled = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))

    func testDisabledGateNeverStartsExport() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let request = request(job), exporter = SessionExport()
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: request) }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: SessionPublisher())
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: .disabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .needsAttention)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 0)
    }

    func testSyncRetryKeepsFinishedExportAndAttempt() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let request = request(job), exporter = SessionExport(), publisher = SessionPublisher()
        await publisher.fail(true)
        let coordinator = try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: publisher)
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: request) }, factory: { _, _ in coordinator })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        await coordinator.waitUntilIdle()
        let savedReceipt = await coordinator.snapshot()
        let saved = try XCTUnwrap(savedReceipt)
        XCTAssertEqual(saved.phase, .localReady)
        await sessions.reconcile(key, capabilities: .disabled)
        XCTAssertEqual(sessions.presentations[key]?.localFile, saved.outputURL)
        await publisher.fail(false)
        await sessions.reconcile(key, capabilities: enabled, retry: true)
        await coordinator.waitUntilIdle()
        let syncedReceipt = await coordinator.snapshot()
        let synced = try XCTUnwrap(syncedReceipt)
        XCTAssertEqual(synced.phase, .synced)
        XCTAssertEqual(saved.attemptID, synced.attemptID)
        XCTAssertEqual(saved.outputURL, synced.outputURL)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 1)
        await sessions.stopAll()
    }

    func testCapabilityManifestDecodesWithoutEnablingOlderServers() throws {
        let legacy = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertNil(legacy.phoneRendering)
        let current = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"phone_rendering":{"enabled":true,"recipe_versions":[2],"verified_features":["basicComposition"]}}"#.utf8))
        XCTAssertEqual(current.phoneRendering?.recipeVersions, [2])
        XCTAssertEqual(DeviceRenderSessions.decision(request(UUID()).recipe, capabilities: current.phoneRendering!).route, .cloud)
    }

    func testAttachmentDestinationNeverFallsBackFromPhoneWithoutConsent() {
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [], role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, sourcePurposes: [phone], role: .clip), .paused)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: .disabled, sourcePurposes: [phone], role: .clip), .paused)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone], role: .voiceover), .unsupportedRole)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone], role: .visual), .unsupportedRole)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [cloud], role: .clip), .cloud)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone, cloud], role: .clip), .mixed)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: ["future"], role: .clip), .mixed)
    }

    func testAttachedProxyIdentityKeepsDestinationWhenContractIsOmitted() {
        let media = CreationAttachedMedia.parse(["media": .array([
            .object(["media_id": .string("analysis-proxy-123")]),
            .object(["media_id": .string("456"), "upload_contract": .object(["purpose": .string("analysis_proxy")])]),
            .object(["media_id": .string("789")]),
        ])])
        XCTAssertEqual(media.map(\.uploadPurpose), ["analysis_proxy", "analysis_proxy", "cloud_render_source"])
    }
}
