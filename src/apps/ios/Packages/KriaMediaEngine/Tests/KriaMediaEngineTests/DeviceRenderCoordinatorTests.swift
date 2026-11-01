import Foundation
import XCTest
@testable import KriaMediaEngine

#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreGraphics
#endif

final class DeviceRenderCoordinatorTests: XCTestCase {
    func testStopWinsAgainstEarlierReplacementReturningFromCleanupLater() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), gate = CommandReturnGate(held: .superseded)
        let background = CommandRaceBackground(gate: gate)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: RecordingPublisher(), backgroundExecution: background)
        try await coordinator.start(request(), decision: .init(route: .local))
        await exporter.waitStarted()
        let replacement = request(revision: 2)
        let start = Task { try await coordinator.start(replacement, decision: .init(route: .local), origin: .userInitiated(UUID())) }
        await gate.waitHeld()
        try await coordinator.cancelByUser()
        try await coordinator.recover()
        try await coordinator.retrySync()
        await gate.release()
        try await start.value
        await exporter.release()
        await coordinator.waitUntilIdle()
        let saved = await coordinator.snapshot(), begins = await background.begins, exports = await exporter.count
        XCTAssertEqual(saved?.phase, .cancelled)
        XCTAssertEqual(begins, 1, "An older start must not acquire a window after Stop")
        XCTAssertEqual(exports, 1)
    }

    func testOlderStopCannotClearNewerExplicitStartAfterCleanup() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), gate = CommandReturnGate(held: .cancelled)
        let background = CommandRaceBackground(gate: gate), reporter = ScriptedReporter(outcomes: [.accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: RecordingPublisher(), failureReporter: reporter, backgroundExecution: background)
        try await coordinator.start(request(), decision: .init(route: .local))
        await exporter.waitStarted()
        let stop = Task { try await coordinator.cancelByUser() }
        await gate.waitHeld()
        let replacement = request(revision: 2)
        try await coordinator.start(replacement, decision: .init(route: .local), origin: .userInitiated(UUID()))
        await coordinator.waitUntilIdle()
        await gate.release()
        try await stop.value
        await exporter.release()
        let saved = await coordinator.snapshot(), busy = await coordinator.isBusy()
        XCTAssertEqual(saved?.request, replacement)
        XCTAssertEqual(saved?.phase, .synced, "Old Stop must not overwrite the newer run")
        XCTAssertFalse(busy)
        await coordinator.waitForPendingReport()
        let reports = await reporter.calls
        XCTAssertTrue(reports.isEmpty, "A superseded Stop command must not report against the newer run")
    }

    func testAutomaticRecoveryCannotOutrunPendingStopAfterOldRunParks() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), cleanup = CommandReturnGate(held: .cancelled), sleeping = SleepGate()
        let background = CommandRaceBackground(gate: cleanup)
        let publisher = ParkedPublicationPublisher()
        let retry = DeviceRenderRetryPolicy(transientDelays: [.seconds(60)], reportBackoffCap: .zero) { delay in
            await sleeping.enter()
            try await Task.sleep(for: delay)
        }
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, retryPolicy: retry, backgroundExecution: background)
        try await coordinator.start(request(), decision: .init(route: .local))
        await sleeping.waitEntered()
        let stop = Task { try await coordinator.cancel() }
        await cleanup.waitHeld()
        await coordinator.waitUntilIdle()
        let parked = await coordinator.snapshot()
        XCTAssertEqual(parked?.phase, .localReady)
        try await coordinator.start(request(revision: 2), decision: .init(route: .local))
        try await coordinator.recover()
        try await coordinator.retrySync()
        await coordinator.waitUntilIdle()
        let begins = await background.begins, publications = await publisher.calls
        XCTAssertEqual(begins, 1, "Automatic publication recovery must wait behind pending Stop")
        XCTAssertEqual(publications, 1)
        await cleanup.release()
        try await stop.value
        let final = await coordinator.snapshot()
        XCTAssertEqual(final?.phase, .cancelled)
        XCTAssertEqual(final?.outputURL, parked?.outputURL)
    }

    func testWindowCompletesOnlyAfterPublicationAndRecoveryHasANewRun() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let background = RecordingBackground(), publisher = CheckingPublisher(pendingFirst: 1)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RecordingExporter(), sources: FixtureSources(), publisher: publisher, backgroundExecution: background)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local), origin: .userInitiated(UUID()))
        await coordinator.waitUntilIdle()
        let first = await background.contexts
        let parked = await background.outcomes
        XCTAssertEqual(parked, [.suspended])
        await background.fireOldExpirationOnNextBegin()
        try await coordinator.recover()
        await coordinator.waitUntilIdle()
        let contexts = await background.contexts
        let outcomes = await background.outcomes
        XCTAssertEqual(outcomes, [.suspended, .completed])
        XCTAssertEqual(contexts[0].attemptID, contexts[1].attemptID)
        XCTAssertNotEqual(contexts[0].runID, contexts[1].runID)
        XCTAssertEqual(contexts[1].origin, .automaticRecovery)
        XCTAssertEqual(first.count, 1)
    }

    func testExecutionWindowRejectsNonfiniteProgressAndFinishesExactlyOnce() async {
        let recorder = WindowRecorder()
        let window = DeviceRenderExecutionWindow(reportProgress: { await recorder.progress($0) }, finish: { await recorder.finish($0) })
        await window.reportProgress(0.6)
        await window.reportProgress(.nan)
        await window.reportProgress(0.1)
        await window.finish(.suspended)
        await window.reportProgress(1)
        await window.finish(.completed)
        let fractions = await recorder.fractions, outcomes = await recorder.outcomes
        XCTAssertEqual(fractions, [0.6, 0.6])
        XCTAssertEqual(outcomes, [.suspended])
    }

    func testConcurrentFinishWaitsForTheOwnedCleanup() async {
        let gate = WindowCleanupGate(), recorder = WindowRecorder()
        let window = DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in await gate.clean() })
        let first = Task { await window.finish(.completed) }
        await gate.waitEntered()
        let second = Task { await window.finish(.cancelled); await recorder.finish(.cancelled) }
        for _ in 0..<20 { await Task.yield() }
        let premature = await recorder.outcomes
        XCTAssertTrue(premature.isEmpty, "Releasing run ownership must wait for the first finish action")
        await gate.release()
        await first.value
        await second.value
        let finished = await recorder.outcomes
        XCTAssertEqual(finished, [.cancelled])
    }

    func testOptionalDurationLimitKeepsLegacyPayloadAndDecodesServerSnakeCase() throws {
        let legacy = request()
        let legacyJSON = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(legacy)) as? [String: Any]
        XCTAssertNil(legacyJSON?["max_export_duration_s"], "nil must preserve the old request wire form")

        let limited = request(maxExportDurationS: 59.5)
        let encoded = try RecipeJSON.encoder().encode(limited)
        let serverJSON = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        XCTAssertEqual(serverJSON?["max_export_duration_s"] as? Double, 59.5)
        XCTAssertEqual(try RecipeJSON.decoder().decode(DeviceRenderRequest.self, from: encoded).maxExportDurationS, 59.5)
    }

    func testDurationLimitFailureReportsExportFailedAndFormatsTurkishWithoutNonfiniteDuration() {
        let invalidOutput = DeviceExportDurationLimitError(limitS: 40, durationS: .infinity, isMeasuredOutput: true)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRenderFailure(invalidOutput), .exportFailed)
        let turkish = invalidOutput.message(locale: Locale(identifier: "tr_TR"))
        XCTAssertTrue(turkish.contains("süresi okunamadı"))
        XCTAssertTrue(turkish.contains("Düzenlemeyi kısaltın"))
        XCTAssertFalse(turkish.localizedCaseInsensitiveContains("infinity"))
        XCTAssertFalse(turkish.localizedCaseInsensitiveContains("nan"))
    }

    func testRetrySyncAfterRestartDoesNotEncodeAgain() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher()
        await publisher.setFailure(true)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher,
                                                      retryPolicy: .immediate)
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

    func testAppUpdateRelocatesFinishedExportWithoutEncodingAgain() async throws {
        for synced in [false, true] {
            let oldDirectory = scratch(), newDirectory = scratch()
            defer { try? FileManager.default.removeItem(at: oldDirectory); try? FileManager.default.removeItem(at: newDirectory) }
            let exporter = RecordingExporter(), publisher = RecordingPublisher()
            await publisher.setFailure(!synced)
            let first = try DeviceRenderCoordinator(directory: oldDirectory, exporter: exporter, sources: FixtureSources(), publisher: publisher, retryPolicy: .immediate)
            try await first.start(request(), decision: CapabilityDecision(route: .local))
            await first.waitUntilIdle()
            let snapshot = await first.snapshot()
            let saved = try XCTUnwrap(snapshot)
            try FileManager.default.moveItem(at: oldDirectory, to: newDirectory)
            let restored = try DeviceRenderCoordinator(directory: newDirectory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
            let reopened = await restored.snapshot()
            XCTAssertEqual(reopened?.outputURL, newDirectory.appendingPathComponent(saved.attemptID.uuidString + ".mp4"))
            await publisher.setFailure(false)
            try await restored.recover()
            await restored.waitUntilIdle()
            let final = await restored.snapshot()
            let count = await exporter.count
            XCTAssertEqual(final?.phase, .synced)
            XCTAssertEqual(final?.attemptID, saved.attemptID)
            XCTAssertEqual(count, 1, "An app update must reuse the checksum-verified export")
        }
    }

    func testExistingReservationConflictPublishesTheSameExportWithANewAttempt() async throws {
        let directory = scratch()
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = ReusedAttemptPublisher()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        for _ in 0..<100 {
            if await coordinator.snapshot()?.phase == .synced { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let saved = await coordinator.snapshot()
        let attempts = await publisher.attempts
        let count = await exporter.count
        XCTAssertEqual(saved?.phase, .synced)
        XCTAssertEqual(attempts.count, 2)
        XCTAssertEqual(Set(attempts).count, 2)
        XCTAssertEqual(count, 1, "Repair the upload identity without rebuilding the creator's video")
    }

    func testLostOrChangedExportGetsANewUploadAttemptBeforeEncoding() async throws {
        for corrupt in [false, true] {
            let directory = scratch()
            defer { try? FileManager.default.removeItem(at: directory) }
            let exporter = RecordingExporter(), publisher = RecordingPublisher()
            await publisher.setFailure(true)
            let first = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher, retryPolicy: .immediate)
            try await first.start(request(), decision: CapabilityDecision(route: .local))
            await first.waitUntilIdle()
            let snapshot = await first.snapshot()
            let saved = try XCTUnwrap(snapshot)
            let file = try XCTUnwrap(saved.outputURL)
            if corrupt { try Data("different bytes".utf8).write(to: file) }
            else { try FileManager.default.removeItem(at: file) }
            await publisher.setFailure(false)
            let restored = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
            try await restored.recover()
            await restored.waitUntilIdle()
            let final = await restored.snapshot()
            XCTAssertEqual(final?.phase, .synced)
            XCTAssertNotEqual(final?.attemptID, saved.attemptID, "Different bytes cannot reuse a server upload reservation")
        }
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

    /// A missing original is the creator's to fix (relink it), so it reports its own
    /// `original_missing` code instead of a generic `export_failed` (prod fafa1746).
    func testSourceResolutionFailureReportsOriginalMissingReasonCode() async throws {
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
        XCTAssertEqual(calls.first?.reasonCode, .originalMissing)
    }

    /// Prod c3e51ae5 reported `missingAsset("1")` as a generic export failure. A missing
    /// asset that is one of the recipe's originals is `original_missing`; a missing font
    /// or library file is still an export failure the creator cannot relink.
    func testMissingAssetIsOriginalMissingOnlyForTheRecipesOwnOriginals() {
        let recipe = MediaEngineFixtures.recipe(assetIDs: ["1"])
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRenderFailure(MediaEngineError.missingAsset("1"), recipe: recipe), .originalMissing)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRenderFailure(MediaEngineError.missingAsset("font-7"), recipe: recipe), .exportFailed)
        XCTAssertEqual(DeviceRenderFailureReasonCode.forRenderFailure(SourceAssetError.changedOriginal("1")), .originalMissing)
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

    /// A strict server limit is exclusive: allowing an edit whose predicted
    /// length equals the limit lets the branding/encoder rounding turn it into
    /// a server rejection. This must stop before source resolution or export,
    /// not trim the video or drop its branding to make it fit.
    func testBoundAtRecipeDurationRejectsBeforeExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = RecordingPublisher()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        let limited = request(maxExportDurationS: 2)

        try await coordinator.start(limited, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()

        let receipt = await coordinator.snapshot()
        let exports = await exporter.count
        let published = await publisher.published
        XCTAssertEqual(receipt?.phase, .needsAttention)
        XCTAssertEqual(exports, 0)
        XCTAssertTrue(published.isEmpty)
        XCTAssertTrue(
            receipt?.error?.contains("Shorten the edit or select less speech") == true ||
            receipt?.error?.contains("Düzenlemeyi kısaltın") == true,
            "the stored limit error must remain actionable in the device locale"
        )
    }

    /// A standard-branded export has a real outro after the recipe timeline.
    /// The preflight must measure that current bundled tail rather than assume
    /// the timeline alone fits and discover the over-limit file after encoding.
    func testBoundIncludesCurrentStandardBrandingTailBeforeExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(brandTail: "standard"), publisher = RecordingPublisher()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)

        try await coordinator.start(request(maxExportDurationS: 2.01), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()

        let receipt = await coordinator.snapshot()
        let exports = await exporter.count
        XCTAssertEqual(receipt?.phase, .needsAttention)
        XCTAssertEqual(exports, 0)
    }

    #if canImport(AVFoundation)
    /// The recipe estimate can be under the limit while the actual media is
    /// exactly on it (for example after an outro or encoder frame rounding).
    /// Measure the file before `.localReady`; otherwise an offline retry after
    /// app relaunch could publish the already-invalid cached output.
    @MainActor func testMeasuredOutputAtBoundRejectsAndCachedRetryCannotPublish() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let media = try await SolidVideoFixture.make(directory: directory, name: "two-seconds", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1), seconds: 2)
        let exporter = MediaFixtureExporter(media: media), publisher = RecordingPublisher()
        let limited = request(clipDuration: 1, maxExportDurationS: 2)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)

        try await coordinator.start(limited, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()

        let first = await coordinator.snapshot()
        let firstExports = await exporter.count
        let firstPublished = await publisher.published
        XCTAssertEqual(first?.phase, .needsAttention)
        XCTAssertNil(first?.outputURL, "a fresh invalid file must not enter .localReady")
        XCTAssertEqual(firstExports, 1)
        XCTAssertTrue(firstPublished.isEmpty)

        // A receipt written by an interrupted older build can already contain
        // a fingerprinted file. Recovery must measure that real file too,
        // before it retries publication.
        let cachedOutput = directory.appendingPathComponent("\(try XCTUnwrap(first?.attemptID).uuidString).mp4")
        XCTAssertTrue(FileManager.default.fileExists(atPath: cachedOutput.path))
        let cached = DeviceRenderReceipt(request: limited, attemptID: try XCTUnwrap(first?.attemptID), phase: .localReady,
                                         outputURL: cachedOutput, outputFingerprint: try SHA256Fingerprinter().fingerprint(file: cachedOutput))
        try JSONEncoder().encode(cached).write(to: directory.appendingPathComponent("device-render.json"), options: .atomic)
        let restored = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(), publisher: publisher)
        try await restored.retrySync()
        await restored.waitUntilIdle()

        let final = await restored.snapshot()
        let finalExports = await exporter.count
        let finalPublished = await publisher.published
        XCTAssertEqual(final?.phase, .needsAttention)
        XCTAssertEqual(finalExports, 1, "a cached output must be measured, not re-encoded")
        XCTAssertTrue(finalPublished.isEmpty, "an equality-bound cached file must never reach publish")
    }
    #endif

    // MARK: - Stop is a resumable pause the server always learns about (TLA+ ClientServerAgree)

    /// Offline at the moment of Stop: the report must keep going until the server
    /// accepts it, or the phone says "stopped" while the server waits for a day.
    func testStopReportIsRetriedUntilTheServerAcceptsIt() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true)
        let reporter = ScriptedReporter(outcomes: [.transient, .transient, .accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                      publisher: RecordingPublisher(), failureReporter: reporter, retryPolicy: .immediate)
        let deviceRequest = request()
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await coordinator.cancelByUser()
        await coordinator.waitForPendingReport()
        let calls = await reporter.calls
        XCTAssertEqual(calls.map(\.reasonCode), [.cancelledByUser, .cancelledByUser, .cancelledByUser])
        XCTAssertTrue(calls.allSatisfy { $0.identity == deviceRequest.identity })
        let receipt = await coordinator.snapshot()
        XCTAssertEqual(receipt?.phase, .cancelled)
        XCTAssertNil(receipt?.pendingReport)
        await exporter.release()
    }

    /// The app is killed before the Stop report lands: the next launch delivers it.
    func testUndeliveredStopReportIsSentAgainAfterRelaunch() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), stuck = BlockingReporter()
        let first = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                publisher: RecordingPublisher(), failureReporter: stuck, retryPolicy: .immediate)
        let deviceRequest = request()
        try await first.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await first.cancelByUser()
        await stuck.waitStarted()

        let reporter = ScriptedReporter(outcomes: [.accepted])
        let relaunched = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                     publisher: RecordingPublisher(), failureReporter: reporter, retryPolicy: .immediate)
        try await relaunched.recover()
        await relaunched.waitForPendingReport()
        let calls = await reporter.calls
        XCTAssertEqual(calls.map(\.reasonCode), [.cancelledByUser])
        XCTAssertEqual(calls.first?.identity, deviceRequest.identity)
        let pending = await relaunched.snapshot()?.pendingReport
        XCTAssertNil(pending)
        await stuck.release(); await exporter.release()
    }

    /// A refused report (e.g. the render already published) stops; it never loops.
    func testRefusedReportIsNotRetried() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), reporter = ScriptedReporter(outcomes: [.rejected])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                      publisher: RecordingPublisher(), failureReporter: reporter, retryPolicy: .immediate)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        try await coordinator.cancelByUser()
        await coordinator.waitForPendingReport()
        let calls = await reporter.calls
        XCTAssertEqual(calls.count, 1)
        let pending = await coordinator.snapshot()?.pendingReport
        XCTAssertNil(pending)
        await exporter.release()
    }

    // MARK: - Transient network trouble waits; it is never a device failure (S3-c)

    func testOfflineDuringPreparationWaitsAndFinishesWithoutReporting() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let publisher = FlakyPublisher(failures: [URLError(.notConnectedToInternet), AppError(transient: true)])
        let reporter = ScriptedReporter(outcomes: [.accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RecordingExporter(), sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: .immediate)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let phase = await coordinator.snapshot()?.phase
        XCTAssertEqual(phase, .synced)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty, "a connectivity blip must never tell the server the phone gave up")
    }

    /// An outage longer than the retries parks the attempt where the next poll's
    /// `start` continues it, still without a needs-attention screen or a report.
    func testLongOutageParksResumableAndTheNextStartContinues() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let publisher = FlakyPublisher(failures: Array(repeating: URLError(.timedOut), count: 3))
        let reporter = ScriptedReporter(outcomes: [.accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RecordingExporter(), sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: .immediate)
        let deviceRequest = request()
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let parked = await coordinator.snapshot()
        XCTAssertEqual(parked?.phase, .preparing)
        XCTAssertNil(parked?.error, "a parked render is waiting, never worded as a failure")
        try await coordinator.start(deviceRequest, decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let final = await coordinator.snapshot()?.phase
        XCTAssertEqual(final, .synced)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty)
    }

    /// A non-transient app error (a real 4xx refusal) still fails and is reported.
    func testNonTransientAppErrorStillReports() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let publisher = FlakyPublisher(failures: [AppError(transient: false)])
        let reporter = ScriptedReporter(outcomes: [.accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RecordingExporter(), sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: .immediate)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        await coordinator.waitForPendingReport()
        let phase = await coordinator.snapshot()?.phase
        XCTAssertEqual(phase, .needsAttention)
        let calls = await reporter.calls
        XCTAssertEqual(calls.map(\.reasonCode), [.unknown])
    }

    /// Offline on the subway, phone locked past the background window while the
    /// coordinator waits between retries: the suspension parks it, never a Stop.
    func testSuspensionDuringATransientRetryWaitParksResumable() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let publisher = FlakyPublisher(failures: [URLError(.notConnectedToInternet)])
        let reporter = ScriptedReporter(outcomes: [.accepted]), background = RecordingBackground()
        let gate = SleepGate()
        let policy = DeviceRenderRetryPolicy(transientDelays: [.seconds(60)], reportBackoffCap: .zero) { delay in
            await gate.enter()
            try await Task.sleep(for: delay)
        }
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RecordingExporter(), sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: policy,
                                                      backgroundExecution: background)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await gate.waitEntered()
        await background.expire()
        await coordinator.waitUntilIdle()
        let parked = await coordinator.snapshot()
        XCTAssertEqual(parked?.phase, .preparing, "a suspension is not 'Rendering stopped'")
        XCTAssertNil(parked?.error)
        try await coordinator.recover()
        await coordinator.waitUntilIdle()
        let final = await coordinator.snapshot()?.phase
        XCTAssertEqual(final, .synced)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty)
    }

    /// The upload landed and the server is still checking it: wait in `.localReady`
    /// with no error and no retry loop; the next recover resumes without re-encoding.
    func testVerificationStillRunningParksTheFinishedExportWithoutRetrying() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(), publisher = CheckingPublisher(pendingFirst: 1)
        let reporter = ScriptedReporter(outcomes: [.accepted])
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: .immediate)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let parked = await coordinator.snapshot(), publishes = await publisher.publishes
        XCTAssertEqual(parked?.phase, .localReady)
        XCTAssertNil(parked?.error)
        XCTAssertEqual(publishes, 1, "a pending check is not retried in place")
        try await coordinator.recover()
        await coordinator.waitUntilIdle()
        let final = await coordinator.snapshot()?.phase, exports = await exporter.count
        XCTAssertEqual(final, .synced)
        XCTAssertEqual(exports, 1)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty)
    }

    // MARK: - Leaving Kria mid-render is a suspension, not export_failed (S3-d)

    func testBackgroundExpiryKeepsTheFinishedExportAndRecoverPublishesIt() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let exporter = RecordingExporter(holdFirst: true), publisher = RecordingPublisher()
        let reporter = ScriptedReporter(outcomes: [.accepted]), background = RecordingBackground()
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: FixtureSources(),
                                                      publisher: publisher, failureReporter: reporter, retryPolicy: .immediate,
                                                      backgroundExecution: background)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await exporter.waitStarted()
        await background.expire()
        await exporter.release()
        await coordinator.waitUntilIdle()
        let parked = await coordinator.snapshot()
        XCTAssertEqual(parked?.phase, .localReady, "the encoded file survives the suspension")
        XCTAssertNil(parked?.error)
        try await coordinator.recover()
        await coordinator.waitUntilIdle()
        let final = await coordinator.snapshot()?.phase, exports = await exporter.count
        XCTAssertEqual(final, .synced)
        XCTAssertEqual(exports, 1, "foregrounding publishes the same file, no re-encode")
        let calls = await reporter.calls, ended = await background.ended
        XCTAssertTrue(calls.isEmpty)
        XCTAssertGreaterThanOrEqual(ended, 1, "every background window is ended")
    }

    #if canImport(AVFoundation)
    func testSystemInterruptedExportParksResumableWithoutReporting() async throws {
        let directory = scratch(); defer { try? FileManager.default.removeItem(at: directory) }
        let reporter = ScriptedReporter(outcomes: [.accepted])
        let interrupted = NSError(domain: AVFoundationErrorDomain, code: -11847)
        let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: ThrowingExporter(error: interrupted), sources: FixtureSources(),
                                                      publisher: RecordingPublisher(), failureReporter: reporter, retryPolicy: .immediate)
        try await coordinator.start(request(), decision: CapabilityDecision(route: .local))
        await coordinator.waitUntilIdle()
        let phase = await coordinator.snapshot()?.phase
        XCTAssertEqual(phase, .preparing)
        let calls = await reporter.calls
        XCTAssertTrue(calls.isEmpty)
    }
    #endif

    private func scratch() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }

    private func request(revision: Int = 1, clipDuration: Double = 2, maxExportDurationS: Double? = nil) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: UUID(), variantID: "original_text", recipeRevision: revision, recipeDigest: "digest-\(revision)"), recipe: MediaEngineFixtures.recipe(clipDuration: clipDuration), maxExportDurationS: maxExportDurationS)
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

#if canImport(AVFoundation)
private actor MediaFixtureExporter: LocalExporting {
    let media: URL
    var count = 0
    nonisolated let brandTail = "none"
    init(media: URL) { self.media = media }
    func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        count += 1
        try FileManager.default.copyItem(at: media, to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}
#endif
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

extension DeviceRenderRetryPolicy {
    static let immediate = DeviceRenderRetryPolicy(transientDelays: [.zero, .zero], reportBackoffCap: .zero, sleep: { _ in await Task.yield() })
}
private struct AppError: DeviceRenderTransientFailure, LocalizedError {
    let transient: Bool
    var isTransientDeviceRenderFailure: Bool { transient }
    var errorDescription: String? { transient ? "Kria hit a problem on its side." : "Refused." }
}
private actor ScriptedReporter: DeviceRenderFailureReporter {
    private var outcomes: [DeviceRenderReportOutcome]
    private(set) var calls: [(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode)] = []
    init(outcomes: [DeviceRenderReportOutcome]) { self.outcomes = outcomes }
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async {}
    func deliver(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async -> DeviceRenderReportOutcome {
        calls.append((identity, reasonCode))
        return outcomes.count > 1 ? outcomes.removeFirst() : (outcomes.first ?? .accepted)
    }
}
private actor BlockingReporter: DeviceRenderFailureReporter {
    private var started: CheckedContinuation<Void, Never>?, held: CheckedContinuation<Void, Never>?, didStart = false
    func waitStarted() async { if !didStart { await withCheckedContinuation { started = $0 } } }
    func release() { held?.resume(); held = nil }
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async {}
    func deliver(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async -> DeviceRenderReportOutcome {
        didStart = true; started?.resume(); started = nil
        await withCheckedContinuation { held = $0 }
        return .rejected
    }
}
private actor FlakyPublisher: DeviceRenderPublishing {
    private var failures: [Error]
    init(failures: [Error]) { self.failures = failures }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool {
        if !failures.isEmpty { throw failures.removeFirst() }
        return true
    }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}
private actor RecordingBackground: DeviceRenderBackgroundExecution {
    private var expiring: (@Sendable () -> Void)?
    private(set) var ended = 0
    private var firePrevious = false
    func fireOldExpirationOnNextBegin() { firePrevious = true }
    private(set) var contexts: [DeviceRenderExecutionContext] = []
    private(set) var outcomes: [DeviceRenderWindowOutcome] = []
    func begin(context: DeviceRenderExecutionContext, expiring: @escaping @Sendable () -> Void) async throws -> DeviceRenderExecutionWindow {
        if firePrevious {
            self.expiring?()
            firePrevious = false
            for _ in 0..<20 { await Task.yield() }
        }
        self.expiring = expiring
        contexts.append(context)
        return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { [weak self] outcome in await self?.end(outcome) })
    }
    private func end(_ outcome: DeviceRenderWindowOutcome) { ended += 1; outcomes.append(outcome) }
    /// Fires the system's expiration handler and lets the coordinator's hop land.
    func expire() async {
        expiring?()
        for _ in 0..<20 { await Task.yield() }
    }
}
private actor SleepGate {
    private var entered = false, waiter: CheckedContinuation<Void, Never>?
    func enter() { entered = true; waiter?.resume(); waiter = nil }
    func waitEntered() async { if !entered { await withCheckedContinuation { waiter = $0 } } }
}
private actor CheckingPublisher: DeviceRenderPublishing {
    private var pending: Int
    private(set) var publishes = 0
    init(pendingFirst: Int) { pending = pendingFirst }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        publishes += 1
        if pending > 0 { pending -= 1; throw DeviceRenderVerificationPending() }
        return .published
    }
}
private struct ThrowingExporter: LocalExporting {
    let error: Error
    let brandTail = "none"
    func export(recipe: EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        throw error
    }
}

private actor ReusedAttemptPublisher: DeviceRenderPublishing {
    private(set) var attempts: [UUID] = []
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        attempts.append(attemptID)
        if attempts.count == 1 { throw DeviceExportAttemptReused() }
        return .published
    }
}

private actor WindowRecorder {
    var fractions: [Double] = []
    var outcomes: [DeviceRenderWindowOutcome] = []
    func progress(_ fraction: Double) { fractions.append(fraction) }
    func finish(_ outcome: DeviceRenderWindowOutcome) { outcomes.append(outcome) }
}

private actor WindowCleanupGate {
    private var entered = false
    private var entryWaiter: CheckedContinuation<Void, Never>?
    private var cleanup: CheckedContinuation<Void, Never>?
    func clean() async {
        entered = true
        entryWaiter?.resume(); entryWaiter = nil
        await withCheckedContinuation { cleanup = $0 }
    }
    func waitEntered() async { if !entered { await withCheckedContinuation { entryWaiter = $0 } } }
    func release() { cleanup?.resume(); cleanup = nil }
}

private actor CommandReturnGate {
    let held: DeviceRenderWindowOutcome
    private var entered = false
    private var released = false
    private var entry: CheckedContinuation<Void, Never>?
    private var waiters: [CheckedContinuation<Void, Never>] = []
    init(held: DeviceRenderWindowOutcome) { self.held = held }
    func barrier(_ outcome: DeviceRenderWindowOutcome) async {
        guard outcome == held, !released else { return }
        entered = true; entry?.resume(); entry = nil
        await withCheckedContinuation { waiters.append($0) }
    }
    func waitHeld() async { if !entered { await withCheckedContinuation { entry = $0 } } }
    func release() { released = true; let saved = waiters; waiters.removeAll(); for waiter in saved { waiter.resume() } }
}

private actor CommandRaceBackground: DeviceRenderBackgroundExecution {
    let gate: CommandReturnGate
    private(set) var begins = 0
    init(gate: CommandReturnGate) { self.gate = gate }
    func begin(context: DeviceRenderExecutionContext, expiring: @escaping @Sendable () -> Void) async throws -> DeviceRenderExecutionWindow {
        begins += 1
        guard begins == 1 else { return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in }) }
        return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in }, completionBarrier: { [gate] in await gate.barrier($0) })
    }
}

private actor ParkedPublicationPublisher: DeviceRenderPublishing {
    private(set) var calls = 0
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        calls += 1
        if calls == 1 { throw URLError(.notConnectedToInternet) }
        return .published
    }
}
