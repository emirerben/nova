import AVFoundation
import os

/// Decides who may deactivate the app's one shared `AVAudioSession` (KRI-653).
///
/// The teleprompter owns its audio session by hand (`automaticallyConfiguresApplicationAudioSession
/// = false`), so while its capture graph is live the session MUST stay active. Several unrelated
/// objects used to call `setActive(false)` on every pause, interruption or scene change, which can
/// leave the microphone delivering zero-filled buffers for a whole take: a 192 s talk-to-camera
/// recording came back digitally silent. Everything that wants to deactivate the session now goes
/// through here, and a deactivation is skipped (and logged) while a capture claim is held.
final class AudioSessionArbiter: @unchecked Sendable {
    static let shared = AudioSessionArbiter()

    private let claims = OSAllocatedUnfairLock(initialState: Set<String>())
    private let deactivateSession: @Sendable () -> Void
    private let logger = Logger(subsystem: "app.kria", category: "audio-session")

    init(deactivateSession: @escaping @Sendable () -> Void = {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }) {
        self.deactivateSession = deactivateSession
    }

    var isCaptureClaimed: Bool { claims.withLock { !$0.isEmpty } }

    /// The owner of a live capture graph. Claims are idempotent per token.
    func claimCapture(_ token: String) {
        claims.withLock { _ = $0.insert(token) }
    }

    /// Releases the claim. Call from every teardown path: a leaked claim only ever makes
    /// other callers skip a deactivation, never keeps the microphone on, but it must not outlive
    /// its owner.
    func releaseCapture(_ token: String) {
        claims.withLock { _ = $0.remove(token) }
    }

    /// Deactivates the shared session unless a capture claim is held. Returns whether it ran.
    @discardableResult
    func deactivate(requester: String) -> Bool {
        if isCaptureClaimed {
            logger.notice("skipped audio session deactivation requested by \(requester, privacy: .public): capture owns it")
            return false
        }
        deactivateSession()
        return true
    }
}
