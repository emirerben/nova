import Foundation

/// Why a teleprompter take stopped. Raw values are the labels the recorder diagnostic sends to the server.
enum TakeStopReason: String, Codable, Sendable, CaseIterable {
    case user
    case backgrounded
    case audioInterruptionBegan = "audio_interruption_began"
    case captureSessionInterrupted = "capture_session_interrupted"
    case runtimeError = "runtime_error"
    case routeLost = "route_lost"
    case mediaServicesReset = "media_services_reset"
    case viewRemoved = "view_removed"
    case sessionTornDown = "session_torn_down"
    case systemFinished = "system_finished"
}

/// Something the system or the screen reported while a take may be running.
enum TakeStopSignal: Equatable, Sendable {
    case audioInterruptionBegan
    case audioInterruptionEnded
    case routeLost
    case mediaServicesReset
    case captureSessionInterrupted
    case captureRuntimeError
    case sceneBackground
    case sceneInactive
    case viewDisappeared(reappeared: Bool)
}

enum TakeStopPolicy {
    static func reason(for signal: TakeStopSignal) -> TakeStopReason? { nil }
}
