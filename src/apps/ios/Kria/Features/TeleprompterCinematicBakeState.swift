import Foundation

/// Where a take recorded with Cinematic on is in its life, persisted on the take manifest (JSON key `cinematic`).
///
/// A Cinematic take opens in Review as the raw movie and is baked into an ordinary one in the background. The state is
/// the only record of that, so a kill, a backgrounding or a discard at any point leaves exactly one of four readings:
///
///     rawReview ──started──▶ baking ──baked──────────▶ baked
///        ▲  │                  │ └─keptStandard──────▶ standardKept
///        │  └──baked / keptStandard (a run that had already finished)
///        └────────interrupted──┘
///
/// `baked` and `standardKept` are final: every event after them changes nothing, so replaying an event after a crash
/// converges. `rawReview` and `baking` are the same debt (a bake is owed) told apart only by whether a task is running,
/// and `interrupted` turns a `baking` nobody is running back into `rawReview`.
///
/// The raw values of the two final states are the ones the earlier two-valued `cinematic` field wrote, so manifests
/// from before this decode unchanged.
enum CinematicBakeState: String, Codable, Sendable {
    /// Recorded in Cinematic, still the raw camera movie, no bake running.
    case rawReview = "raw_review"
    /// A bake is running for this take.
    case baking
    /// The movie is an ordinary one with the Cinematic look in it.
    case baked = "applied"
    /// Cinematic was on but this take is standard: the camera dropped the mode, or the bake could not be completed and
    /// the raw (or a plain copy of the) movie was kept. Always playable and usable.
    case standardKept = "standard"

    enum Event: Equatable, Sendable {
        case started
        case baked
        case keptStandard
        /// The run stopped before finishing and left the movie as it was: cancelled, the app left the foreground, or the
        /// process died.
        case interrupted
    }

    var isSettled: Bool { self == .baked || self == .standardKept }

    func applying(_ event: Event) -> CinematicBakeState {
        guard !isSettled else { return self }
        switch event {
        case .started: return .baking
        case .baked: return .baked
        case .keptStandard: return .standardKept
        case .interrupted: return .rawReview
        }
    }

    /// What the baker's answer means. Nil is a movie that was already ordinary (a finished earlier run), which is `baked`.
    static func event(for outcome: CinematicTakeOutcome?) -> Event {
        outcome == .standard ? .keptStandard : .baked
    }

    var creatorMessage: String {
        switch self {
        case .rawReview, .baking: "Applying your Cinematic look in the background. You can keep reviewing."
        case .baked: "Recorded in Cinematic."
        case .standardKept: "Cinematic couldn’t be applied to this take, so it’s in standard mode."
        }
    }
}

/// Bakes a take's Cinematic look into its movie. A seam so the flow's lifecycle is testable and a simulator, which has no
/// Cinematic camera, can still run the screens.
protocol CinematicBaking: Sendable {
    /// Whether the movie is a raw Cinematic movie (depth tracks, no baked look).
    func needsBake(movieAt url: URL) async -> Bool
    /// Replaces the movie at `url`, in place and atomically, and says how it ended (see `CinematicTakeFinisher.finish`).
    /// Throws `CancellationError` when cancelled or when the app lost the GPU, leaving the movie untouched.
    func bake(movieAt url: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> CinematicTakeOutcome?
}

struct LiveCinematicBaker: CinematicBaking {
    func needsBake(movieAt url: URL) async -> Bool { await CinematicTakeFinisher.isCinematic(movieAt: url) }
    func bake(movieAt url: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> CinematicTakeOutcome? {
        try await CinematicTakeFinisher.finish(movieAt: url, progress: progress)
    }
}

#if DEBUG
/// UI-test baker (`KRIA_TELEPROMPTER_BAKE_FIXTURE=<seconds>` with `-ui-testing-chat`): a bake that takes that long, reports
/// progress, honours cancellation and leaves the fixture movie as it is.
struct FixtureCinematicBaker: CinematicBaking {
    let seconds: Double

    static var current: FixtureCinematicBaker? {
        let process = ProcessInfo.processInfo
        guard process.arguments.contains("-ui-testing-chat"),
              let value = process.environment["KRIA_TELEPROMPTER_BAKE_FIXTURE"], let seconds = Double(value), seconds > 0 else { return nil }
        return FixtureCinematicBaker(seconds: seconds)
    }

    func needsBake(movieAt url: URL) async -> Bool { true }

    func bake(movieAt url: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> CinematicTakeOutcome? {
        let steps = max(1, Int(seconds * 10))
        for step in 1...steps {
            try await Task.sleep(for: .milliseconds(100))
            progress(Double(step) / Double(steps))
        }
        return .applied
    }
}
#endif
