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

    /// The server adds seconds to its expected duration based on the tail the
    /// phone declares, so the coordinator must forward what the exporter
    /// actually appended. Declaring "none" while shipping the outro is a 422 on
    /// every publish; declaring "standard" without one is a 422 the other way.
    func testCoordinatorDeclaresTheExportersBrandTailWhenPublishing() async throws {
        for tail in ["none", "standard"] {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: directory) }
            let exporter = RecordingExporter(brandTail: tail), publisher = RecordingPublisher()
            let coordinator = try DeviceRenderCoordinator(
                directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
            try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
            await coordinator.waitUntilIdle()
            let declared = await publisher.declaredTails
            XCTAssertEqual(declared, [tail], "coordinator must forward the exporter's tail")
        }
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

    /// KRI-114 P0-2: a capability decision that routes away from local
    /// rendering (e.g. thermal throttling) must tell the server via the
    /// injected reporter, classified from the decision's `reason`, before the
    /// receipt ever enters `.needsAttention`.
    func testCapabilityRouteToCloudReportsThermalReasonCode() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, failureReporter: reporter
        )
        let deviceRequest = request()
        let identity = deviceRequest.identity
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .cloud, reason: "Device thermal state is serious"))
        await coordinator.waitUntilIdle()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.identity, identity)
        XCTAssertEqual(calls.first?.reasonCode, .thermal)
    }

    /// KRI-114 P0-2: a source-resolution error during local export must report
    /// `export_failed` (source/export errors), never the generic fallback.
    func testSourceResolutionFailureReportsExportFailedReasonCode() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: FailingSources(), publisher: publisher, failureReporter: reporter
        )
        let deviceRequest = request()
        let identity = deviceRequest.identity
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.identity, identity)
        XCTAssertEqual(calls.first?.reasonCode, .exportFailed)
    }

    /// KRI-114 P0-2 review fix: plain `cancel()` is used by housekeeping (a
    /// superseded request, dropping a stale receipt before retry, or the
    /// workspace tearing down) where the identity is often still perfectly
    /// valid server-side — it must never report, or leaving the screen
    /// mid-render would wrongly flip a healthy job to `needsAttention`.
    func testPlainCancelDuringRenderDoesNotReportToServer() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, failureReporter: reporter
        )
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await coordinator.cancel()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .cancelled)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty)
        await exporter.release()
    }

    /// `cancelByUser()` is the only path that should report — it is wired
    /// exclusively to a user-facing "Stop rendering" affordance.
    func testCancelByUserDuringRenderReportsCancelledByUser() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, failureReporter: reporter
        )
        let deviceRequest = request()
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await coordinator.cancelByUser()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .cancelled)
        let calls = await reporter.calls
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(calls.first?.identity, deviceRequest.identity)
        XCTAssertEqual(calls.first?.reasonCode, .cancelledByUser)
        await exporter.release()
    }

    /// KRI-118: a renderer-version/schema mismatch must be distinguishable
    /// from a genuinely unsupported edit (`unsupportedRecipe`) so the UI can
    /// say "update the app" instead of "start a new edit" — retrying either
    /// way is pointless, but only one of them is fixable by the user at all.
    func testRendererVersionMismatchRouteReportsRendererOutdatedReasonCode() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, failureReporter: reporter
        )
        try await coordinator.start(request(), decision: CapabilityDecision(route: .cloud, reason: "Unsupported renderer version"))
        await coordinator.waitUntilIdle()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.first?.reasonCode, .rendererOutdated)
    }

    /// A `recipe.validate()` failure surfacing mid-render as a schema-version
    /// mismatch (not a specific unsupported feature) must also classify as
    /// `.rendererOutdated`, not the generic `.unsupportedRecipe` bucket.
    func testSchemaVersionMismatchDuringRenderReportsRendererOutdatedReasonCode() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: SchemaMismatchSources(), publisher: publisher, failureReporter: reporter
        )
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.first?.reasonCode, .rendererOutdated)
    }

    /// A `RecipeError` that is NOT a schema-version mismatch (a specific
    /// unsupported feature within an otherwise-understood recipe) must keep
    /// classifying as `.unsupportedRecipe`, unchanged by the new distinction.
    func testUnsupportedFeatureRecipeErrorDuringRenderKeepsUnsupportedRecipeReasonCode() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher(), reporter = RecordingFailureReporter()
        let coordinator = try DeviceRenderCoordinator(
            directory: directory, exporter: exporter, sources: InvalidTimelineSources(), publisher: publisher, failureReporter: reporter
        )
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        await reporter.waitForReport()
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.first?.reasonCode, .unsupportedRecipe)
    }

    private func request(revision: Int = 1) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: UUID(), variantID: "original_text", recipeRevision: revision, recipeDigest: "digest-\(revision)"), recipe: MediaEngineFixtures.recipe())
    }
}

private struct FixtureSources: DeviceSourceResolving {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { [:] }
}
private struct FailingSources: DeviceSourceResolving {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { throw SourceAssetError.missingOriginal("clip-1") }
}
private struct SchemaMismatchSources: DeviceSourceResolving {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { throw RecipeError.unsupportedSchema(99) }
}
private struct InvalidTimelineSources: DeviceSourceResolving {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL] { throw RecipeError.invalidTimeline }
}
private actor RecordingFailureReporter: DeviceRenderFailureReporter {
    private(set) var calls: [(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String)] = []
    private var waiter: CheckedContinuation<Void, Never>?
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async {
        calls.append((identity, reasonCode, detail))
        waiter?.resume(); waiter = nil
    }
    /// Deterministically waits for the coordinator's fire-and-forget report
    /// Task to land, instead of a fixed sleep.
    func waitForReport() async {
        guard calls.isEmpty else { return }
        await withCheckedContinuation { waiter = $0 }
    }
}
private actor RecordingExporter: LocalExporting {
    var count = 0
    let holdFirst: Bool
    /// Mirrors what AVFoundationLocalExporter reports for its branding.
    nonisolated let brandTail: String
    var started: CheckedContinuation<Void, Never>?
    var held: CheckedContinuation<Void, Never>?
    init(holdFirst: Bool = false, brandTail: String = "none") {
        self.holdFirst = holdFirst
        self.brandTail = brandTail
    }
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
    /// What the coordinator declared to the server for each publish. The server
    /// adds the matching seconds before checking the uploaded file's duration,
    /// so a wrong value here is a 422 on every real render.
    var declaredTails: [String] = []
    func setFailure(_ value: Bool) { failure = value }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        if failure { throw URLError(.notConnectedToInternet) }
        published.append(identity)
        declaredTails.append(brandTail)
        return .published
    }
}
