import Foundation
import XCTest
@testable import KriaMediaEngine

final class DeviceRenderCoordinatorTests: XCTestCase {
    func testRetrySyncAfterRestartDoesNotEncodeAgain() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher()
        await publisher.setFailure(true)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        let request = request()
        try await coordinator.start(request, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let first = await coordinator.snapshot()
        XCTAssertEqual(first?.phase, .localReady)
        XCTAssertNotNil(first?.outputURL)
        let exports = await exporter.count
        XCTAssertEqual(exports, 1)
        await publisher.setFailure(false)
        let restored = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        try await restored.recover()
        await restored.waitUntilIdle()
        let final = await restored.snapshot()
        XCTAssertEqual(final?.phase, .synced)
        let count = await exporter.count
        XCTAssertEqual(count, 1)
        try await restored.start(request, decision: CapabilityDecision(route: .local))
        let unchanged = await restored.snapshot()
        XCTAssertEqual(unchanged?.attemptID, first?.attemptID)
    }

    func testSupersededExportCannotPublishOverNewRecipe() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), publisher = RecordingPublisher()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        try await coordinator.start(request(revision: 1), decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await coordinator.start(request(revision: 2), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        await exporter.release()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.request.identity.recipeRevision, 2)
        XCTAssertEqual(receipt?.phase, .synced)
        let published = await publisher.published
        XCTAssertEqual(published.map(\.recipeRevision), [2])
    }

    func testUnsupportedRecipeRequiresAttentionWithoutExportOrUpload() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .cloud, reason: "Device is too hot"))
        await coordinator.waitUntilIdle()
        let receipt = await coordinator.snapshot(), count = await exporter.count, published = await publisher.published
        XCTAssertEqual(receipt?.phase, .needsAttention)
        XCTAssertEqual(count, 0)
        XCTAssertTrue(published.isEmpty)
    }

    private func request(revision: Int = 1) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: UUID(), variantID: "original_text", recipeRevision: revision, recipeDigest: "digest-\(revision)"), recipe: MediaEngineFixtures.recipe())
    }
}

private struct FixtureSources: DeviceSourceResolving {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { [:] }
}
private actor RecordingExporter: LocalExporting {
    var count = 0
    let holdFirst: Bool
    var started: CheckedContinuation<Void, Never>?
    var held: CheckedContinuation<Void, Never>?
    init(holdFirst: Bool = false) { self.holdFirst = holdFirst }
    func waitStarted() async { if count == 0 { await withCheckedContinuation { started = $0 } } }
    func release() { held?.resume(); held = nil }
    func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        count += 1
        started?.resume(); started = nil
        if holdFirst && count == 1 { await withCheckedContinuation { held = $0 } }
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("test export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}
private actor RecordingPublisher: DeviceRenderPublishing {
    var failure = false
    var published: [DeviceRenderIdentity] = []
    func setFailure(_ value: Bool) { failure = value }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID) async throws -> DevicePublication {
        if failure { throw URLError(.notConnectedToInternet) }
        published.append(identity)
        return .published
    }
}
