import Foundation

// KRI-580: the part of the song the creator filmed in. Songs repeat their verses, so the server matches takes
// only inside this range. Everything here is plain logic with no UI, so the failure modes can be tested.

/// The stretch of the song the creator filmed in, in seconds of song time.
struct SongFilmingRange: Equatable, Sendable {
    let start: Double
    let end: Double

    /// Reads the `song_range` the server mirrors onto the song's media entry. Tolerant by design: anything that
    /// is not a usable range (missing, partial, negative, end not after start, not numbers) means "no range" and
    /// never fails the thread decode.
    static func parse(_ value: JSONValue?) -> SongFilmingRange? {
        guard case .object(let fields)? = value,
              case .number(let start)? = fields["start_s"],
              case .number(let end)? = fields["end_s"],
              start.isFinite, end.isFinite, start >= 0, end > start else { return nil }
        return SongFilmingRange(start: start, end: end)
    }

    /// "Filmed 0:40 - 1:50"
    var summary: String {
        "Filmed \(SongRangeMath.timecode(start)) - \(SongRangeMath.timecode(end))"
    }

    /// What VoiceOver reads for the row.
    var spokenSummary: String {
        "Filmed from \(SongRangeMath.timecode(start)) to \(SongRangeMath.timecode(end))"
    }
}

enum SongRangeMath {
    /// The server refuses a range shorter than this.
    static let minimumSeconds = 5
    /// Longest value the pickers and the formatter handle (99 hours): a guard against absurd metadata.
    private static let ceilingSeconds = 359_999

    /// Whole seconds of the song the pickers can reach, or nil when the length is unknown or too short to
    /// hold a range (under five whole seconds): then nothing is offered at all.
    static func pickableSeconds(duration: Double?) -> Int? {
        guard let duration, duration.isFinite else { return nil }
        let whole = Int(min(max(duration, 0), Double(ceilingSeconds)).rounded(.down))
        return whole >= minimumSeconds ? whole : nil
    }

    /// m:ss, or h:mm:ss from one hour on. Negative and non-finite input reads 0:00.
    static func timecode(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00" }
        let total = Int(min(seconds, Double(ceilingSeconds)).rounded())
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let rest = total % 60
        if hours > 0 { return String(format: "%d:%02d:%02d", hours, minutes, rest) }
        return String(format: "%d:%02d", minutes, rest)
    }
}

/// What the creator is choosing in the sheet: whole seconds, always inside the song, with the To picker free to
/// go below From + 5 s (Save is then off and says why) but From never leaving To behind.
struct SongRangeDraft: Equatable {
    static let tooShortMessage = "Pick at least five seconds of your song."

    /// Last whole second of the song.
    let limit: Int
    private(set) var from: Int
    private(set) var to: Int

    /// Nil when no range can be offered (unknown or very short song). With nothing set the draft is the whole
    /// song; an existing range is rounded to whole seconds and clamped into the song, falling back to the whole
    /// song if nothing sensible is left of it.
    init?(duration: Double?, existing: SongFilmingRange?) {
        guard let limit = SongRangeMath.pickableSeconds(duration: duration) else { return nil }
        self.limit = limit
        from = 0
        to = limit
        guard let existing else { return }
        let start = Int(min(max(existing.start, 0), Double(limit)).rounded(.down))
        let end = Int(min(max(existing.end, 0), Double(limit)).rounded(.up))
        if end - start >= SongRangeMath.minimumSeconds { from = start; to = end }
    }

    /// The highest From that still leaves room for a valid range.
    var maxFrom: Int { limit - SongRangeMath.minimumSeconds }

    /// From is kept inside the song; when it reaches To, To is pushed out to keep five seconds.
    mutating func setFrom(_ value: Int) {
        from = min(max(value, 0), maxFrom)
        if to < from + SongRangeMath.minimumSeconds { to = from + SongRangeMath.minimumSeconds }
    }

    /// To is only kept inside the song; a too-short result is reported by `message`, not silently moved.
    mutating func setTo(_ value: Int) {
        to = min(max(value, 0), limit)
    }

    /// Why Save is off, or nil when the range is valid.
    var message: String? {
        to - from >= SongRangeMath.minimumSeconds ? nil : Self.tooShortMessage
    }

    /// The range to send, or nil while it is invalid.
    var range: SongFilmingRange? {
        message == nil ? SongFilmingRange(start: Double(from), end: Double(to)) : nil
    }
}

/// How a save attempt ended.
enum SongRangeSubmission {
    case saved(CreationThread)
    /// 404 `song_range_unavailable`: the feature is off for this account. The UI disappears, with no alert.
    case unavailable
    /// Copy for the sheet to show inline.
    case failed(String)

    static let genericFailure = "Couldn’t save that. Try again."
    static let offlineFailure = "Couldn’t save that. Check your connection and try again."
    static let songMissing = "Attach a song first."

    private enum Cause { case unavailable, revisionChanged, message(String) }

    /// Sets (or, with a nil range, clears) the range. A revision or idempotency conflict (the thread moved on
    /// while the sheet was open) re-reads the thread and tries once more with the same event id, so a retry of
    /// a request that did land is answered by the server instead of applied twice.
    static func submit(client: any SongRangeClient, threadID: UUID, range: SongFilmingRange?, clientEventID: String) async -> SongRangeSubmission {
        var retried = false
        while true {
            do {
                let latest = try await client.project(threadID: threadID)
                let thread = try await client.setSongRange(threadID: threadID, range: range, expectedRevision: latest.revision, clientEventID: clientEventID)
                return .saved(thread)
            } catch {
                switch cause(of: error) {
                case .unavailable: return .unavailable
                case .message(let text): return .failed(text)
                case .revisionChanged:
                    if retried { return .failed(genericFailure) }
                    retried = true
                }
            }
        }
    }

    private static func cause(of error: Error) -> Cause {
        guard let api = error as? APIError else { return .message(genericFailure) }
        switch api {
        case .requestFailed(let status, let detail):
            if status == 404, detail.code == "song_range_unavailable" { return .unavailable }
            if status == 422, detail.code == "song_range_invalid" { return .message(detail.problemMessage ?? SongRangeDraft.tooShortMessage) }
            return .message(genericFailure)
        case .conflict(let detail):
            if detail.code == "song_missing" { return .message(detail.message ?? songMissing) }
            return .revisionChanged
        case .offline:
            return .message(offlineFailure)
        default:
            return .message(genericFailure)
        }
    }
}

/// The two calls the range setter needs; `KriaAPIClient` inherits it, and tests use a small fake.
protocol SongRangeClient: Sendable {
    func project(threadID: UUID) async throws -> CreationThread
    func setSongRange(threadID: UUID, range: SongFilmingRange?, expectedRevision: Int, clientEventID: String) async throws -> CreationThread
}

/// One user action's idempotency key: a retry of the same save reuses it, a different range gets a fresh one.
struct SongRangeEvent: Equatable {
    let id: String
    let range: SongFilmingRange?

    static func reusing(_ pending: Self?, range: SongFilmingRange?) -> Self {
        if let pending, pending.range == range { return pending }
        return Self(id: UUID().uuidString, range: range)
    }
}
