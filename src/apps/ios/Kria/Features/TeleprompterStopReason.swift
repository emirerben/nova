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

    var endsAgainstCreatorIntent: Bool { self != .user }

    var creatorMessage: String {
        switch self {
        case .user: "You stopped the recording."
        case .backgrounded: "Recording stopped when Kria went to the background."
        case .audioInterruptionBegan: "Recording stopped because a call or another app took the microphone."
        case .captureSessionInterrupted: "Recording stopped because iOS took the camera away."
        case .runtimeError: "Recording stopped because the camera hit an error."
        case .routeLost: "Recording stopped because the microphone was disconnected."
        case .mediaServicesReset: "Recording stopped because iOS restarted its audio services."
        case .viewRemoved: "Recording stopped because the camera screen closed."
        case .sessionTornDown: "Recording stopped because the camera was shut down."
        case .systemFinished: "Recording stopped on its own."
        }
    }
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

/// The single table deciding which signals end a take. Only a real loss of the camera or microphone, or the
/// screen staying gone, ends one; the creator's stop is not a signal and never passes through here.
enum TakeStopPolicy {
    static func reason(for signal: TakeStopSignal) -> TakeStopReason? {
        switch signal {
        case .audioInterruptionBegan: .audioInterruptionBegan
        case .routeLost: .routeLost
        case .mediaServicesReset: .mediaServicesReset
        case .captureSessionInterrupted: .captureSessionInterrupted
        case .captureRuntimeError: .runtimeError
        case .sceneBackground: .backgrounded
        case .viewDisappeared(let reappeared): reappeared ? nil : .viewRemoved
        case .audioInterruptionEnded, .sceneInactive: nil
        }
    }
}
