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
private actor PausedSessionExport: LocalExporting {
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private(set) var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        await withCheckedContinuation { waiters.append($0) }
        try Task.checkCancellation()
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("test export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
    func release() {
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
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

private actor SessionRequest {
    var request: DeviceRenderRequest
    init(_ request: DeviceRenderRequest) { self.request = request }
    func set(_ request: DeviceRenderRequest) { self.request = request }
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

    func testNewRevisionReplacesActiveObserverAndPublishesLocalOutput() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let initial = request(job), exporter = PausedSessionExport()
        let remote = SessionRequest(initial)
        let sessions = DeviceRenderSessions(fetch: { _, _ in
            DeviceRenderStatusResponse(phase: "awaiting_device", request: await remote.request)
        }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: SessionPublisher())
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        for _ in 0..<100 {
            if await exporter.calls == 1 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let next = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 2,
            recipeDigest: String(repeating: "b", count: 64)), recipe: initial.recipe)
        await remote.set(next)
        await sessions.reconcile(key, capabilities: enabled)
        for _ in 0..<100 {
            if await exporter.calls == 2 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let calls = await exporter.calls
        XCTAssertEqual(calls, 2)
        await exporter.release()
        // No further reconcile: the new revision's observer must deliver completion.
        for _ in 0..<100 {
            if sessions.presentations[key]?.phase == .synced { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(sessions.presentations[key]?.phase, .synced)
        XCTAssertNotNil(sessions.presentations[key]?.localFile)
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
    func testRelinkRequiresExactBytesAndCurrentRequest() async throws {
        let directory = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: directory.root) }
        try directory.createIfNeeded()
        let source = directory.root.appendingPathComponent("selected.mov")
        try Data("original".utf8).write(to: source)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        let asset = RenderAssetReference(id: "source", fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "server"))
        let job = UUID()
        let initial = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "source", relativePath: "source", fingerprint: fingerprint)], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])], assetManifest: RenderAssetManifest(assets: [asset])))
        let remote = SessionRequest(initial)
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: await remote.request) }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: directory.root.appendingPathComponent("render"), exporter: SessionExport(), sources: SessionSources(), publisher: SessionPublisher())
        }, projectDirectory: { _ in directory })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: .disabled)
        let missing = try await sessions.sourcesNeedingRelink(key)
        let target = try XCTUnwrap(missing.first)
        XCTAssertEqual(missing.count, 1)
        let wrong = directory.root.appendingPathComponent("wrong.mov")
        try Data("proxy".utf8).write(to: wrong)
        do { try await sessions.relink(target, for: key, from: wrong); XCTFail("Wrong file accepted") } catch {}
        XCTAssertTrue(try SourceAssetStore(project: directory).bindings().isEmpty)
        try await sessions.relink(target, for: key, from: source)
        let remaining = try await sessions.sourcesNeedingRelink(key)
        XCTAssertTrue(remaining.isEmpty)
        let next = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 2, recipeDigest: String(repeating: "b", count: 64)), recipe: initial.recipe)
        await remote.set(next)
        await sessions.reconcile(key, capabilities: .disabled)
        do { try await sessions.relink(target, for: key, from: source); XCTFail("Stale selection accepted") }
        catch APIError.conflict {} catch { XCTFail("Unexpected error: \(error)") }
    }

}
