import XCTest
@testable import Kria

@MainActor final class ContinuedProcessingExportTests: XCTestCase {
    func testMissingOrIncompleteBuildConfigurationKeepsContinuedProcessingDisabled() {
        let cases: [[String: Any]?] = [
            nil,
            [:],
            ["KriaContinuedProcessingExportEnabled": true],
            ["BGTaskSchedulerPermittedIdentifiers": ["com.example.kria.continued-export.*"]],
            ["KriaContinuedProcessingExportEnabled": false,
             "BGTaskSchedulerPermittedIdentifiers": ["com.example.kria.continued-export.*"]],
            ["KriaContinuedProcessingExportEnabled": true,
             "BGTaskSchedulerPermittedIdentifiers": ["com.other.kria.continued-export.*"]]
        ]
        for dictionary in cases {
            XCTAssertFalse(ContinuedProcessingExportConfiguration(infoDictionary: dictionary).permits(bundleIdentifier: "com.example.kria"))
        }
        XCTAssertFalse(AppleContinuedProcessingExportScheduler(configuration: .init(infoDictionary: nil)).supportsGPUExport)
    }

    func testBuildConfigurationAdmitsOnlyTheExactBundleWildcard() {
        let configuration = ContinuedProcessingExportConfiguration(infoDictionary: [
            "KriaContinuedProcessingExportEnabled": true,
            "BGTaskSchedulerPermittedIdentifiers": ["com.example.kria.continued-export.*"]
        ])
        XCTAssertTrue(configuration.permits(bundleIdentifier: "com.example.kria"))
        XCTAssertFalse(configuration.permits(bundleIdentifier: "com.other.kria"))
        XCTAssertFalse(configuration.permits(bundleIdentifier: ""))
    }

    func testAutomaticRecoveryAndBackgroundCallersNeverSubmit() {
        for origin in [ContinuedProcessingExport.Origin.automaticRecovery, .save, .share] {
            let h = Harness()
            h.foreground = origin == .automaticRecovery
            h.start(origin: origin)
            h.start(origin: origin)
            XCTAssertEqual(h.scheduler.submissions, 0)
            XCTAssertEqual(h.fallbacks, 1)
            XCTAssertEqual(h.starts, 0)
        }
    }

    func testUnsupportedGPUResourcesNeverSubmit() {
        let h = Harness()
        h.scheduler.supportsGPUExport = false
        h.start()
        XCTAssertEqual(h.scheduler.submissions, 0)
        XCTAssertEqual(h.fallbacks, 1)
    }

    func testRejectionFallsBackOnceAndLateLaunchCannotStartWork() {
        let h = Harness()
        h.scheduler.accept = false
        h.start()
        h.start()
        let late = MockTask()
        h.scheduler.launch?(late)
        XCTAssertEqual(h.fallbacks, 1)
        XCTAssertEqual(h.starts, 0)
        XCTAssertEqual(late.completions, [false])
    }

    func testLaunchStartsOnceAndCompletionIsExplicit() {
        let h = Harness()
        h.start()
        XCTAssertEqual(h.starts, 0)
        let task = MockTask()
        h.scheduler.launch?(task)
        h.scheduler.launch?(task)
        let duplicate = MockTask()
        h.scheduler.launch?(duplicate)
        h.progress?(10, 10)
        XCTAssertTrue(task.completions.isEmpty)
        XCTAssertEqual(task.progress.map(\.completed), [10])
        h.progress?(9, 10)
        h.progress?(11, 20)
        XCTAssertEqual(task.progress.count, 1)
        h.finish?(true)
        h.finish?(false)
        h.runner.cancel()
        XCTAssertEqual(h.starts, 1)
        XCTAssertEqual(h.interruptions, 0)
        XCTAssertEqual(task.completions, [true])
        XCTAssertEqual(duplicate.completions, [false])
        XCTAssertNil(task.expirationHandler)
    }

    func testExpirationInterruptsOnceAndLateSuccessCannotWin() {
        let h = Harness()
        h.start()
        let task = MockTask()
        h.scheduler.launch?(task)
        let expire = task.expirationHandler
        expire?()
        expire?()
        h.runner.cancel()
        h.finish?(true)
        XCTAssertEqual(h.interruptions, 1)
        XCTAssertEqual(task.completions, [false])
        XCTAssertNil(task.expirationHandler)
    }

    func testCancellationBeforeLaunchClosesLateTaskWithoutFallback() {
        let h = Harness()
        h.start()
        h.runner.cancel()
        h.runner.cancel()
        let task = MockTask()
        h.scheduler.launch?(task)
        XCTAssertEqual(h.starts, 0)
        XCTAssertEqual(h.interruptions, 1)
        XCTAssertEqual(h.fallbacks, 0)
        XCTAssertEqual(h.scheduler.cancellations, 1)
        XCTAssertEqual(task.completions, [false])
    }

    func testFailedExportAndInvalidProgressNeverReportSuccess() {
        let h = Harness()
        h.start()
        let task = MockTask()
        h.scheduler.launch?(task)
        h.progress?(-1, 10)
        h.progress?(11, 10)
        h.progress?(0, 0)
        XCTAssertTrue(task.progress.isEmpty)
        h.finish?(false)
        XCTAssertEqual(task.completions, [false])
    }

    func testExpirationBeforeMainActorDeliveryPreventsRacingSuccess() {
        let h = Harness()
        h.start()
        let task = MockTask()
        h.scheduler.launch?(task)
        task.isExpired = true
        h.finish?(true)
        task.expirationHandler?()
        XCTAssertEqual(h.interruptions, 1)
        XCTAssertEqual(task.completions, [false])
    }

    func testExpirationDuringCompletionInterruptsWithoutReportingSuccess() {
        let h = Harness()
        h.start()
        let task = MockTask()
        h.scheduler.launch?(task)
        task.expiresDuringCompletion = true
        h.finish?(true)
        h.runner.cancel()
        XCTAssertEqual(h.interruptions, 1)
        XCTAssertEqual(task.completions, [false])
    }

    func testReleasingActiveRunnerInterruptsAndFinishesUnsuccessfully() async {
        let scheduler = MockScheduler()
        var runner: ContinuedProcessingExport? = ContinuedProcessingExport(scheduler: scheduler, isForeground: { true })
        weak let weakRunner = runner
        var interruptions = 0
        runner?.start(origin: .save, title: "Export", subtitle: "Video", work: .init(start: { _, _ in }, interrupt: { interruptions += 1 }), fallback: {})
        let task = MockTask()
        scheduler.launch?(task)
        runner = nil
        XCTAssertNil(weakRunner)
        for _ in 0..<20 where task.completions.isEmpty { await Task.yield() }
        XCTAssertEqual(interruptions, 1)
        XCTAssertEqual(task.completions, [false])
        XCTAssertNil(task.expirationHandler)
    }

    func testFinishedRunnerReleasesExportClosuresWhileRegistrationRemains() {
        let scheduler = MockScheduler()
        let runner = ContinuedProcessingExport(scheduler: scheduler, isForeground: { true })
        var captured: ExportLifetime? = ExportLifetime()
        weak let weakCaptured = captured
        var finish: ContinuedProcessingExport.Finish?
        runner.start(origin: .save, title: "Export", subtitle: "Video", work: .init(start: { [captured] _, completion in
            captured?.touched = true
            finish = completion
        }, interrupt: { [captured] in captured?.touched = true }), fallback: {})
        captured = nil
        let task = MockTask()
        scheduler.launch?(task)
        XCTAssertNotNil(weakCaptured)
        finish?(true)
        XCTAssertNil(weakCaptured)
        XCTAssertNotNil(scheduler.launch)
    }

    func testRetainedLaunchHandlerDoesNotRetainRunnerOrExport() {
        let scheduler = MockScheduler()
        var runner: ContinuedProcessingExport? = ContinuedProcessingExport(scheduler: scheduler, isForeground: { true })
        weak let weakRunner = runner
        runner?.start(origin: .save, title: "Export", subtitle: "Video", work: .init(start: { _, _ in }, interrupt: {}), fallback: {})
        runner?.cancel()
        runner = nil
        XCTAssertNil(weakRunner)
        let late = MockTask()
        scheduler.launch?(late)
        XCTAssertEqual(late.completions, [false])
    }
}

@MainActor private final class Harness {
    let scheduler = MockScheduler()
    var foreground = true
    var starts = 0
    var interruptions = 0
    var fallbacks = 0
    var progress: ContinuedProcessingExport.ReportProgress?
    var finish: ContinuedProcessingExport.Finish?
    lazy var runner = ContinuedProcessingExport(scheduler: scheduler, isForeground: { [weak self] in self?.foreground == true })

    func start(origin: ContinuedProcessingExport.Origin = .save) {
        runner.start(origin: origin, title: "Export", subtitle: "Video", work: .init(start: { [weak self] progress, finish in
            self?.starts += 1
            self?.progress = progress
            self?.finish = finish
        }, interrupt: { [weak self] in self?.interruptions += 1 }), fallback: { [weak self] in self?.fallbacks += 1 })
    }
}

@MainActor private final class MockScheduler: ContinuedProcessingExportScheduling {
    var supportsGPUExport = true
    var accept = true
    var submissions = 0
    var cancellations = 0
    var launch: (@MainActor (any ContinuedProcessingExportTask) -> Void)?

    func registerAndSubmit(identifier: String, title: String, subtitle: String,
                           launch: @escaping @MainActor (any ContinuedProcessingExportTask) -> Void) -> Bool {
        submissions += 1
        self.launch = launch
        return accept
    }

    func cancel(identifier: String) { cancellations += 1 }
}

@MainActor private final class MockTask: ContinuedProcessingExportTask {
    var expirationHandler: (@MainActor () -> Void)?
    var isExpired = false
    var expiresDuringCompletion = false
    var completions: [Bool] = []
    var progress: [(completed: Int64, total: Int64)] = []
    func reportProgress(completed: Int64, total: Int64) { progress.append((completed, total)) }
    @discardableResult func complete(success: Bool) -> Bool {
        let actualSuccess = success && !isExpired && !expiresDuringCompletion
        completions.append(actualSuccess)
        return actualSuccess
    }
}

@MainActor private final class ExportLifetime { var touched = false }
