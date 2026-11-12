import Foundation
import KriaMediaEngine

/// Launch wiring for how many frames an on-device export draws at once (`ExportFrameOverlap`). Unset, the engine's default
/// applies (2). The launch argument `-kria.exportFramesInFlight 1` is the QA off switch: every export, the editor Save and
/// the server-driven phone renders alike, then takes the serial path it had before frames were overlapped. Values are
/// clamped to what the engine supports.
enum ExportOverlapSwitch {
    static let defaultsKey = "kria.exportFramesInFlight"

    static func installIfConfigured(defaults: UserDefaults = .standard) {
        guard defaults.object(forKey: defaultsKey) != nil else { return }
        ExportFrameOverlap.framesInFlight = defaults.integer(forKey: defaultsKey)
    }
}
