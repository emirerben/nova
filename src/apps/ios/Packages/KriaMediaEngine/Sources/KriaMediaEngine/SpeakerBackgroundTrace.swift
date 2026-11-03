import Foundation
#if canImport(os)
import os
#endif

/// An always-on, content-free account of how one Talk-to-camera composition drew its background. The speaker
/// matte and the compositor count every frame's outcome here, and a small ring keeps the last few changes of
/// outcome, so a creator who saw the background vanish can be answered from a bug report instead of a guess.
/// Only fixed labels and numbers are stored: no file names, no frames, nothing the creator said or typed.
public final class SpeakerBackgroundTrace: @unchecked Sendable {
    /// One frame's outcome. The raw value is the label that is stored and uploaded.
    public enum Outcome: String, CaseIterable, Sendable {
        case cutout = "cutout"
        case backgroundOnly = "background_only"
        /// No reliable mask and none held: the frame is drawn as if the edit had no background.
        case originalNoSegmenter = "original.no_segmenter"
        case originalError = "original.error"
        case originalUnreadable = "original.unreadable"
        case originalCoverage = "original.coverage"
        /// A good mask from a moment ago stands in for a frame that had none, by the reason it had none.
        case heldError = "held.error"
        case heldUnreadable = "held.unreadable"
        case heldCoverage = "held.coverage"
        /// The held mask was dropped before use: the clip changed, or time jumped over `continuityWindow`.
        case clearedClip = "cleared.clip"
        case clearedGap = "cleared.gap"
        /// A background is part of the recipe but no clip of its subject track is on screen at that time, so
        /// nothing can be cut out and the frame is drawn without it.
        case noSpeakerLayer = "no_speaker_layer"

        /// True for the outcomes in which the creator does not see the background.
        var hidesBackground: Bool {
            switch self {
            case .originalNoSegmenter, .originalError, .originalUnreadable, .originalCoverage, .noSpeakerLayer: true
            default: false
            }
        }
    }

    public struct Event: Codable, Equatable, Sendable {
        /// Seconds since the composition was built.
        public let after: Double
        /// Seconds into the edit at which the outcome changed.
        public let at: Double
        public let label: String
    }

    public struct Snapshot: Codable, Equatable, Sendable {
        /// Clips of the background's subject track in the recipe; 0 means the background can never be drawn.
        public let speakerClips: Int
        public let counts: [String: Int]
        public let events: [Event]

        /// The longest run of consecutive frames that hid the background, a stall or failure the counts alone hide.
        public let longestHiddenRun: Int

        /// One bounded line of fixed labels and numbers, safe to upload.
        public var summary: String {
            var parts = ["clips=\(speakerClips)", "hidden_run=\(longestHiddenRun)"]
            parts += counts.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
            parts += events.suffix(8).map { "@\(String(format: "%.1f", $0.at))s:\($0.label)" }
            return String(parts.joined(separator: " ").prefix(Self.summaryLimit))
        }
        public static let summaryLimit = 600
    }

    #if canImport(os)
    /// One signpost event per change of outcome (Instruments, subsystem `com.kria.app`, category `speaker-background`).
    private static let signposter = OSSignposter(subsystem: "com.kria.app", category: "speaker-background")
    #endif
    /// Changes of outcome kept, newest last.
    static let ringSize = 24
    /// Consecutive background-hiding frames after which `onDegraded` fires once (about two seconds of video).
    static let degradedRun = 60

    private let lock = NSLock()
    private let origin = Date()
    private var speakerClips = 0
    private var counts: [Outcome: Int] = [:]
    private var ring: [Event] = []
    private var last: Outcome?
    private var hiddenRun = 0
    private var longestHiddenRun = 0
    private var degradedFired = false
    /// Called once, on the first run of `degradedRun` hidden frames, with the trace so far. Runs on the
    /// compositor queue, so it must only hand the snapshot on.
    public var onDegraded: (@Sendable (Snapshot) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return degradedHandler }
        set { lock.lock(); degradedHandler = newValue; lock.unlock() }
    }
    private var degradedHandler: (@Sendable (Snapshot) -> Void)?

    public init() {}

    func noteBuilt(speakerClips count: Int) {
        lock.lock(); speakerClips = count; lock.unlock()
    }

    /// True when the recipe has a background but none of its clips is on the background's subject track.
    /// (A stretch with a background but no speaker on screen, such as the ending clip, is legitimate and not traced.)
    var hasNoSpeakerClips: Bool {
        lock.lock(); defer { lock.unlock() }
        return speakerClips == 0
    }

    /// Counts one frame. `at` is the edit time of the frame.
    func record(_ outcome: Outcome, at time: Double) {
        var notify: (handler: @Sendable (Snapshot) -> Void, snapshot: Snapshot)?
        lock.lock()
        counts[outcome, default: 0] += 1
        if outcome != last {
            #if canImport(os)
            Self.signposter.emitEvent("speaker-background-outcome", "\(outcome.rawValue, privacy: .public) at \(time)")
            #endif
            ring.append(Event(after: Date().timeIntervalSince(origin), at: time, label: outcome.rawValue))
            if ring.count > Self.ringSize { ring.removeFirst(ring.count - Self.ringSize) }
            last = outcome
        }
        if outcome.hidesBackground {
            hiddenRun += 1
            longestHiddenRun = max(longestHiddenRun, hiddenRun)
        } else if outcome != .clearedClip, outcome != .clearedGap {
            hiddenRun = 0
        }
        if hiddenRun >= Self.degradedRun, !degradedFired, let handler = degradedHandler {
            degradedFired = true
            notify = (handler, snapshotLocked())
        }
        lock.unlock()
        if let notify { notify.handler(notify.snapshot) }
    }

    public func snapshot() -> Snapshot {
        lock.lock(); defer { lock.unlock() }
        return snapshotLocked()
    }

    private func snapshotLocked() -> Snapshot {
        Snapshot(speakerClips: speakerClips, counts: Dictionary(uniqueKeysWithValues: counts.map { ($0.key.rawValue, $0.value) }),
                 events: ring, longestHiddenRun: longestHiddenRun)
    }
}
