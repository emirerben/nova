import Foundation
import KriaMediaEngine
import UIKit

@MainActor final class DeviceRenderUserInitiation {
    static let shared = DeviceRenderUserInitiation()
    private var grants: [UUID: DeviceRenderIdentity] = [:]
    private var generation = UUID()
    private let isForeground: @MainActor () -> Bool
    private var foregroundObserver: RenderForegroundObserver?

    init(isForeground: @escaping @MainActor () -> Bool = { UIApplication.shared.applicationState == .active }) {
        self.isForeground = isForeground
        foregroundObserver = RenderForegroundObserver(NotificationCenter.default.addObserver(
            forName: UIApplication.willResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.invalidate() }
        })
    }

    func retry(identity: DeviceRenderIdentity,
               mutation: () async throws -> DeviceRenderRetryAck) async throws -> DeviceRenderRetryAck {
        invalidate(identity: identity)
        let generation = generation
        let initiatedInForeground = isForeground()
        let ack = try await mutation()
        guard initiatedInForeground, self.generation == generation, ack.phase == "awaiting_device",
              ack.identity.jobID == identity.jobID, ack.identity.variantID == identity.variantID,
              isForeground(), !Task.isCancelled else { return ack }
        grants = grants.filter { $0.value.jobID != identity.jobID || $0.value.variantID != identity.variantID }
        grants[UUID()] = ack.identity
        return ack
    }

    func origin(for identity: DeviceRenderIdentity) -> DeviceRenderExecutionOrigin {
        guard let token = grants.first(where: { $0.value == identity })?.key else { return .automaticRecovery }
        return .userInitiated(token)
    }

    func consume(_ origin: DeviceRenderExecutionOrigin, identity: DeviceRenderIdentity) -> Bool {
        guard case let .userInitiated(token) = origin,
              let authorized = grants.removeValue(forKey: token) else { return false }
        return authorized == identity
    }

    func invalidate(jobID: UUID, variantID: String) {
        generation = UUID()
        grants = grants.filter { $0.value.jobID != jobID || $0.value.variantID != variantID }
    }

    func invalidate(identity: DeviceRenderIdentity? = nil) {
        generation = UUID()
        if let identity { grants = grants.filter { $0.value != identity } }
        else { grants.removeAll() }
    }
}

@MainActor final class DurableDeviceRenderWindow {
    enum State { case idle, awaitingLaunch, continued, fallback, finished }
    private(set) var state = State.idle
    private let scheduler: any ContinuedProcessingExportScheduling
    private let isForeground: @MainActor () -> Bool
    private let deadline: @Sendable () async throws -> Void
    private let identifier: String
    private let expiring: @Sendable () -> Void
    private var continuation: CheckedContinuation<Bool, any Error>?
    private var timer: Task<Void, Never>?
    private var task: (any ContinuedProcessingExportTask)?
    // Retention prevents identity reuse until this window and its completion history end.
    private var deliveredTasks: [ObjectIdentifier: any ContinuedProcessingExportTask] = [:]
    private var progress: Int64 = 0

    init(scheduler: any ContinuedProcessingExportScheduling,
         isForeground: @escaping @MainActor () -> Bool,
         deadline: @escaping @Sendable () async throws -> Void = { try await Task.sleep(for: .seconds(2)) },
         bundleIdentifier: String = Bundle.main.bundleIdentifier ?? "app.kria",
         expiring: @escaping @Sendable () -> Void) {
        self.scheduler = scheduler
        self.isForeground = isForeground
        self.deadline = deadline
        self.expiring = expiring
        identifier = "\(bundleIdentifier).continued-export.\(UUID().uuidString)"
    }

    func acquire(authorized: Bool) async throws -> Bool {
        try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { waiting in
                guard state == .idle else { waiting.resume(throwing: CancellationError()); return }
                guard authorized, scheduler.supportsGPUExport, isForeground() else {
                    state = .fallback
                    waiting.resume(returning: false)
                    return
                }
                continuation = waiting
                state = .awaitingLaunch
                let accepted = scheduler.registerAndSubmit(identifier: identifier, title: "Rendering your video", subtitle: "Preparing and syncing your edit") { [weak self] task in
                    guard let self else { task.complete(success: false); return }
                    self.launched(task)
                }
                guard state == .awaitingLaunch else { return }
                guard accepted else { fallback(); return }
                timer = Task { [weak self, deadline] in
                    do { try await deadline() } catch { return }
                    self?.fallback()
                }
            }
        } onCancel: {
            Task { @MainActor in self.cancel() }
        }
    }

    private func launched(_ incoming: any ContinuedProcessingExportTask) {
        let identity = incoming.identity
        guard deliveredTasks[identity] == nil else { return }
        deliveredTasks[identity] = incoming
        guard state == .awaitingLaunch else { incoming.complete(success: false); return }
        timer?.cancel(); timer = nil
        task = incoming
        state = .continued
        incoming.expirationHandler = { [weak self] in
            guard let self, self.state == .continued else { return }
            self.expiring()
            self.finish(.suspended)
        }
        if incoming.isExpired {
            expiring()
            finish(.suspended)
            resolve(.failure(CancellationError()))
        } else {
            resolve(.success(true))
        }
    }

    private func fallback() {
        guard state == .awaitingLaunch else { return }
        state = .fallback
        timer?.cancel(); timer = nil
        scheduler.cancel(identifier: identifier)
        resolve(.success(false))
    }

    private func cancel() {
        guard state != .finished else { return }
        finish(.cancelled)
        resolve(.failure(CancellationError()))
    }

    func reportProgress(_ fraction: Double) {
        guard state == .continued, fraction.isFinite else { return }
        progress = max(progress, Int64(min(1, max(0, fraction)) * 1000))
        task?.reportProgress(completed: progress, total: 1000)
    }

    func finish(_ outcome: DeviceRenderWindowOutcome) {
        guard state != .finished else { return }
        state = .finished
        timer?.cancel(); timer = nil
        task?.expirationHandler = nil
        scheduler.cancel(identifier: identifier)
        task?.complete(success: outcome == .completed)
        task = nil
    }

    private func resolve(_ result: Result<Bool, any Error>) {
        let waiting = continuation
        continuation = nil
        waiting?.resume(with: result)
    }
}

private final class RenderForegroundObserver: @unchecked Sendable {
    let token: any NSObjectProtocol
    init(_ token: any NSObjectProtocol) { self.token = token }
    deinit { NotificationCenter.default.removeObserver(token) }
}
