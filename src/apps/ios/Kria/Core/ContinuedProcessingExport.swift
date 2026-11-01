import Foundation
#if os(iOS) && !targetEnvironment(macCatalyst)
import BackgroundTasks
import UIKit
#endif

@MainActor protocol ContinuedProcessingExportTask: AnyObject, Sendable {
    var expirationHandler: (@MainActor () -> Void)? { get set }
    var isExpired: Bool { get }
    var identity: ObjectIdentifier { get }
    func reportProgress(completed: Int64, total: Int64)
    @discardableResult func complete(success: Bool) -> Bool
}

extension ContinuedProcessingExportTask {
    var identity: ObjectIdentifier { ObjectIdentifier(self) }
}

@MainActor protocol ContinuedProcessingExportScheduling: Sendable {
    var supportsGPUExport: Bool { get }
    func registerAndSubmit(identifier: String, title: String, subtitle: String,
                           launch: @escaping @MainActor (any ContinuedProcessingExportTask) -> Void) -> Bool
    func cancel(identifier: String)
}

struct ContinuedProcessingExportConfiguration: Sendable {
    private let enabled: Bool
    private let permittedIdentifiers: Set<String>

    init(infoDictionary: [String: Any]?) {
        enabled = infoDictionary?["KriaContinuedProcessingExportEnabled"] as? Bool ?? false
        permittedIdentifiers = Set(infoDictionary?["BGTaskSchedulerPermittedIdentifiers"] as? [String] ?? [])
    }

    func permits(bundleIdentifier: String) -> Bool {
        enabled && !bundleIdentifier.isEmpty
            && permittedIdentifiers.contains("\(bundleIdentifier).continued-export.*")
    }
}

@MainActor final class ContinuedProcessingExport {
    enum Origin { case save, share, automaticRecovery }
    typealias ReportProgress = @MainActor @Sendable (Int64, Int64) -> Void
    typealias Finish = @MainActor (Bool) -> Void

    struct Work: Sendable {
        let start: @MainActor (@escaping ReportProgress, @escaping Finish) -> Void
        let interrupt: @MainActor () -> Void
    }

    private enum State: Sendable {
        case idle
        case awaitingLaunch(Work)
        case running(Work, any ContinuedProcessingExportTask)
        case finished
    }

    private let scheduler: any ContinuedProcessingExportScheduling
    private let isForeground: @MainActor () -> Bool
    private let identifier: String
    private var state = State.idle
    private var deliveredTask: ObjectIdentifier?
    private var lastProgress: (completed: Int64, total: Int64)?

    init(scheduler: any ContinuedProcessingExportScheduling,
         isForeground: @escaping @MainActor () -> Bool,
         bundleIdentifier: String = Bundle.main.bundleIdentifier ?? "app.kria") {
        self.scheduler = scheduler
        self.isForeground = isForeground
        identifier = "\(bundleIdentifier).continued-export.\(UUID().uuidString)"
    }

    deinit {
        let state = state
        let scheduler = scheduler
        let identifier = identifier
        Task { @MainActor in
            switch state {
            case let .awaitingLaunch(work):
                scheduler.cancel(identifier: identifier)
                work.interrupt()
            case let .running(work, task):
                task.expirationHandler = nil
                scheduler.cancel(identifier: identifier)
                work.interrupt()
                task.complete(success: false)
            case .idle, .finished:
                break
            }
        }
    }

    func start(origin: Origin, title: String, subtitle: String, work: Work,
               fallback: @MainActor () -> Void) {
        guard case .idle = state else { return }
        guard origin != .automaticRecovery, scheduler.supportsGPUExport else {
            state = .finished
            fallback()
            return
        }
        state = .awaitingLaunch(work)
        guard isForeground() else {
            state = .finished
            fallback()
            return
        }
        let accepted = scheduler.registerAndSubmit(identifier: identifier, title: title, subtitle: subtitle) { [weak self] task in
            guard let self else { task.complete(success: false); return }
            self.launched(task)
        }
        if !accepted, case .awaitingLaunch = state {
            state = .finished
            fallback()
        }
    }

    func cancel() {
        let work: Work
        let task: (any ContinuedProcessingExportTask)?
        switch state {
        case let .awaitingLaunch(pending):
            work = pending
            task = nil
        case let .running(running, active):
            work = running
            task = active
        case .idle, .finished:
            return
        }
        state = .finished
        task?.expirationHandler = nil
        scheduler.cancel(identifier: identifier)
        work.interrupt()
        task?.complete(success: false)
    }

    func run<Value: Sendable>(origin: Origin, title: String, subtitle: String,
                              operation: @escaping @MainActor (@escaping ReportProgress) async throws -> Value,
                              discard: @escaping @MainActor (Value) -> Void) async throws -> Value {
        let pending = ContinuedExportOperation<Value>(discard: discard)
        let value = try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { continuation in
                pending.continuation = continuation
                start(origin: origin, title: title, subtitle: subtitle, work: .init(start: { progress, finish in
                    pending.start(operation: operation, progress: progress, finish: finish)
                }, interrupt: {
                    pending.cancel()
                }), fallback: {
                    pending.start(operation: operation, progress: { _, _ in }, finish: { _ in })
                })
            }
        } onCancel: {
            Task { @MainActor in
                self.cancel()
                pending.cancel()
            }
        }
        do { try Task.checkCancellation() }
        catch { discard(value); throw error }
        return value
    }

    private func launched(_ task: any ContinuedProcessingExportTask) {
        guard deliveredTask != task.identity else { return }
        guard case let .awaitingLaunch(work) = state else {
            task.complete(success: false)
            return
        }
        deliveredTask = task.identity
        state = .running(work, task)
        task.expirationHandler = { [weak self] in self?.cancel() }
        work.start({ [weak self] completed, total in
            guard let self, case let .running(_, task) = self.state,
                  total > 0, completed >= 0, completed <= total else { return }
            if let previous = self.lastProgress {
                guard total == previous.total, completed >= previous.completed else { return }
            }
            self.lastProgress = (completed, total)
            task.reportProgress(completed: completed, total: total)
        }, { [weak self] success in self?.finish(success: success) })
    }

    private func finish(success: Bool) {
        guard case let .running(work, task) = state else { return }
        guard !task.isExpired else { cancel(); return }
        state = .finished
        task.expirationHandler = nil
        if !task.complete(success: success), success {
            work.interrupt()
        }
    }
}

@MainActor private final class ContinuedExportOperation<Value: Sendable> {
    var continuation: CheckedContinuation<Value, any Error>?
    private var task: Task<Void, Never>?
    private var cancelled = false
    private let discard: @MainActor (Value) -> Void

    init(discard: @escaping @MainActor (Value) -> Void) { self.discard = discard }

    func start(operation: @escaping @MainActor (@escaping ContinuedProcessingExport.ReportProgress) async throws -> Value,
               progress: @escaping ContinuedProcessingExport.ReportProgress,
               finish: @escaping ContinuedProcessingExport.Finish) {
        guard !cancelled, task == nil else { return }
        task = Task {
            defer { task = nil }
            do {
                try Task.checkCancellation()
                let value = try await operation(progress)
                if cancelled || Task.isCancelled {
                    discard(value)
                    finish(false)
                    resolve(.failure(CancellationError()))
                    return
                }
                finish(true)
                if cancelled {
                    discard(value)
                    return
                }
                resolve(.success(value))
            } catch {
                finish(false)
                resolve(.failure(error))
            }
        }
    }

    func cancel() {
        cancelled = true
        task?.cancel()
        resolve(.failure(CancellationError()))
    }

    private func resolve(_ result: Result<Value, any Error>) {
        let waiting = continuation
        continuation = nil
        waiting?.resume(with: result)
    }
}

#if os(iOS) && !targetEnvironment(macCatalyst)
@MainActor final class AppleContinuedProcessingExportScheduler: ContinuedProcessingExportScheduling {
    private let configuration: ContinuedProcessingExportConfiguration
    private let bundleIdentifier: String
    private static var registeredIdentifiers: Set<String> = []

    init(configuration: ContinuedProcessingExportConfiguration = .init(infoDictionary: Bundle.main.infoDictionary),
         bundleIdentifier: String = Bundle.main.bundleIdentifier ?? "") {
        self.configuration = configuration
        self.bundleIdentifier = bundleIdentifier
    }

    var supportsGPUExport: Bool {
        guard #available(iOS 26.0, *), configuration.permits(bundleIdentifier: bundleIdentifier) else { return false }
        return BGTaskScheduler.supportedResources.contains(.gpu)
    }

    func registerAndSubmit(identifier: String, title: String, subtitle: String,
                           launch: @escaping @MainActor (any ContinuedProcessingExportTask) -> Void) -> Bool {
        guard #available(iOS 26.0, *), supportsGPUExport,
              UIApplication.shared.applicationState == .active,
              Self.registeredIdentifiers.insert(identifier).inserted else { return false }
        guard BGTaskScheduler.shared.register(forTaskWithIdentifier: identifier, using: .main, launchHandler: { task in
            MainActor.assumeIsolated {
                guard let continued = task as? BGContinuedProcessingTask else {
                    task.setTaskCompleted(success: false)
                    return
                }
                launch(AppleContinuedProcessingExportTask(task: continued))
            }
        }) else { return false }
        let request = BGContinuedProcessingTaskRequest(identifier: identifier, title: title, subtitle: subtitle)
        request.strategy = .fail
        request.requiredResources = .gpu
        do {
            try BGTaskScheduler.shared.submit(request)
            return true
        } catch {
            return false
        }
    }

    func cancel(identifier: String) {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier)
    }
}

@available(iOS 26.0, *)
@MainActor private final class AppleContinuedProcessingExportTask: ContinuedProcessingExportTask {
    private let task: BGContinuedProcessingTask
    private let completion = ContinuedExportCompletion()
    var isExpired: Bool { completion.isExpired }
    var identity: ObjectIdentifier { ObjectIdentifier(task) }
    var expirationHandler: (@MainActor () -> Void)? {
        didSet {
            if let expirationHandler {
                let completion = completion
                task.expirationHandler = {
                    guard completion.expire() else { return }
                    Task { @MainActor in expirationHandler() }
                }
            } else {
                task.expirationHandler = nil
            }
        }
    }

    init(task: BGContinuedProcessingTask) { self.task = task }

    func reportProgress(completed: Int64, total: Int64) {
        guard !completion.isFinished else { return }
        task.progress.totalUnitCount = total
        task.progress.completedUnitCount = completed
    }

    @discardableResult func complete(success: Bool) -> Bool {
        guard let actualSuccess = completion.finish(success: success) else { return false }
        expirationHandler = nil
        task.setTaskCompleted(success: actualSuccess)
        return actualSuccess
    }
}
#endif

private final class ContinuedExportCompletion: @unchecked Sendable {
    private enum State { case active, expired, finished }
    private let lock = NSLock()
    private var state = State.active

    var isExpired: Bool { lock.withLock { state == .expired } }
    var isFinished: Bool { lock.withLock { state == .finished } }

    func expire() -> Bool {
        lock.withLock {
            guard state == .active else { return false }
            state = .expired
            return true
        }
    }

    func finish(success: Bool) -> Bool? {
        lock.withLock {
            guard state != .finished else { return nil }
            let actualSuccess = success && state == .active
            state = .finished
            return actualSuccess
        }
    }
}
