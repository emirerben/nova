import Foundation
import KriaMediaEngine

/// Launch wiring for how many frames an on-device export draws at once (`ExportFrameOverlap`). Unset, the engine's default
/// applies (2) on a phone with room for it. Overlap costs about 100 MB more at an export's peak (measured on a 6 GB iPhone 13
/// Pro), so a phone with under 5 GB of memory (the 4 GB iPhone 12 and SE 3 report about 3.7 GB) keeps the serial path unless
/// the switch is set. The launch argument `-kria.exportFramesInFlight 1` is the QA off switch: every export, the editor Save
/// and the server-driven phone renders alike, then takes the serial path it had before frames were overlapped. A value set
/// explicitly always wins, clamped to what the engine supports.
enum ExportOverlapSwitch {
    static let defaultsKey = "kria.exportFramesInFlight"
    /// Below this much physical memory the default is one frame at a time.
    static let lowMemoryBytes: UInt64 = 5_000_000_000

    static func defaultDepth(physicalMemory: UInt64) -> Int {
        physicalMemory < lowMemoryBytes ? 1 : ExportFrameOverlap.defaultFramesInFlight
    }

    static func installIfConfigured(
        defaults: UserDefaults = .standard,
        physicalMemory: UInt64 = ProcessInfo.processInfo.physicalMemory
    ) {
        if defaults.object(forKey: defaultsKey) != nil {
            ExportFrameOverlap.framesInFlight = defaults.integer(forKey: defaultsKey)
        } else {
            ExportFrameOverlap.framesInFlight = defaultDepth(physicalMemory: physicalMemory)
        }
    }
}
