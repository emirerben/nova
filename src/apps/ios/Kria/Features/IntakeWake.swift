import Foundation

/// Lets a loop that checks on something every few seconds also run the moment something changes. `signal()` is never
/// lost: one sent while nobody waits makes the next `wait` return at once.
@MainActor final class IntakeWake {
    private var pending = false
    private var waiter: CheckedContinuation<Void, Never>?
    private var timer: Task<Void, Never>?

    func signal() {
        if waiter != nil { release() } else { pending = true }
    }

    /// Returns on the next `signal()`, after `timeout`, or when the calling task is cancelled.
    func wait(timeout: Duration) async {
        if Task.isCancelled { return }
        if pending { pending = false; return }
        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                waiter = continuation
                timer = Task { [weak self] in
                    try? await Task.sleep(for: timeout)
                    guard !Task.isCancelled else { return }
                    self?.release()
                }
            }
        } onCancel: {
            Task { @MainActor [self] in release() }
        }
        timer?.cancel(); timer = nil
    }

    private func release() {
        guard let waiter else { return }
        self.waiter = nil
        waiter.resume()
    }
}
