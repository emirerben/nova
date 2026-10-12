import Foundation

// KRI-588: when a creator's lip-sync clips could belong to different repeats of the same verse, Kria asks in
// the chat "Which part of the song did you film?" and the creator answers with a card (From / To, or the whole
// song). The answer travels back as `song_range` on the next turn; the server stores it on the song.
//
// Same shape as the take-order question (`SongOrder.swift`): hand-rolled tolerant parsing, because an absent,
// newer, or malformed payload must mean "no card" (the text question still shows), never a failed decode that
// breaks the turn. Everything wire-shaped is here, as pure values, so the failure modes are unit-testable.

/// The optional `song_range_question` on an assistant event payload (or a turn response).
struct SongRangeQuestion: Equatable, Sendable {
    let questionID: String
    /// Which upload of the song the question was built against (informational).
    let songGeneration: Int?
    /// The song's length in seconds, when the server sends it. Always above zero.
    let songDurationS: Double?

    /// Songs shorter than this are not worth cutting up: the card offers only "Use the whole song".
    static let minimumDurationForPickers = 10.0

    init(questionID: String, songGeneration: Int? = nil, songDurationS: Double? = nil) {
        self.questionID = questionID; self.songGeneration = songGeneration; self.songDurationS = songDurationS
    }

    /// nil when the payload has no usable question (absent, not an object, no id).
    static func parse(payload: [String: JSONValue]?) -> SongRangeQuestion? {
        guard let fields = payload?["song_range_question"]?.objectValue else { return nil }
        return parse(fields: fields)
    }

    init?(json: JSONValue) {
        guard let fields = json.objectValue, let parsed = Self.parse(fields: fields) else { return nil }
        self = parsed
    }

    private static func parse(fields: [String: JSONValue]) -> SongRangeQuestion? {
        guard let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        // Both extras are optional and read one by one: a bad one is dropped, never the card.
        let generation = fields["song_generation"]?.numberValue.flatMap { value -> Int? in
            value.isFinite && value == value.rounded() && abs(value) < 1e9 ? Int(value) : nil
        }
        let duration = fields["song_duration_s"]?.numberValue.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        return SongRangeQuestion(questionID: questionID, songGeneration: generation, songDurationS: duration)
    }

    /// The song length the pickers work against: the question's own, else what the thread says about the song.
    /// nil (unknown, or under ten seconds) means no pickers at all.
    func pickableDuration(fallback: Double?) -> Double? {
        guard let duration = songDurationS ?? fallback, duration.isFinite, duration >= Self.minimumDurationForPickers else { return nil }
        return duration
    }

    /// The From / To state for a fresh card: the whole song, whole seconds. nil when no range can be offered.
    func draft(fallbackDurationS: Double?) -> SongRangeDraft? {
        SongRangeDraft(duration: pickableDuration(fallback: fallbackDurationS), existing: nil)
    }
}

/// `song_range` on the submit-turn body, exactly as the server contract names it. Both ends null = the whole song.
struct SongRangeAnswer: Encodable, Equatable, Sendable {
    enum Choice: Equatable, Sendable {
        case wholeSong
        case part(SongFilmingRange)
        /// A stored echo whose range could not be read. It still closes the question; it is never sent.
        case unreadable
    }

    let questionID: String
    let choice: Choice

    /// The 409 `code` the server answers a `song_range` for a question it has since closed.
    static let staleConflictCode = "song_range_stale"
    /// The 422 `code` for a range the server refuses (too short, past the song's end).
    static let invalidProblemCode = "song_range_invalid"

    static func wholeSong(questionID: String) -> Self { Self(questionID: questionID, choice: .wholeSong) }
    static func part(questionID: String, range: SongFilmingRange) -> Self { Self(questionID: questionID, choice: .part(range)) }

    enum CodingKeys: String, CodingKey { case questionID = "question_id", startS = "start_s", endS = "end_s" }

    /// The whole-song answer sends EXPLICIT nulls: an absent key and a null are different things to the server.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(questionID, forKey: .questionID)
        switch choice {
        case .wholeSong:
            try c.encodeNil(forKey: .startS)
            try c.encodeNil(forKey: .endS)
        case .part(let range):
            try c.encode(range.start, forKey: .startS)
            try c.encode(range.end, forKey: .endS)
        case .unreadable:
            throw EncodingError.invalidValue(self, .init(codingPath: [], debugDescription: "An unreadable echo is never sent"))
        }
    }

    /// The `song_range` a user message carried (the server echoes it on the stored event), or nil when the
    /// message has none or has no question id. A range that cannot be read still answers the question
    /// (`.unreadable`): the creator did answer it, so a relaunch must not ask again.
    static func parse(payload: [String: JSONValue]?) -> SongRangeAnswer? {
        guard let fields = payload?["song_range"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        let start = fields["start_s"], end = fields["end_s"]
        if isAbsent(start), isAbsent(end) { return wholeSong(questionID: questionID) }
        if let from = start?.numberValue, let to = end?.numberValue,
           from.isFinite, to.isFinite, from >= 0, to > from {
            return part(questionID: questionID, range: SongFilmingRange(start: from, end: to))
        }
        return SongRangeAnswer(questionID: questionID, choice: .unreadable)
    }

    private static func isAbsent(_ value: JSONValue?) -> Bool {
        guard let value else { return true }
        return value == .null
    }

    /// The readable chat message sent alongside the structured payload.
    var message: String {
        switch choice {
        case .wholeSong: "Use the whole song"
        case .part(let range): "I filmed from \(SongRangeMath.timecode(range.start)) to \(SongRangeMath.timecode(range.end))"
        case .unreadable: "I answered the song question"
        }
    }

    /// The read-only line once answered, e.g. "Filmed 0:40 - 1:20".
    var summary: String {
        switch choice {
        case .wholeSong: "Whole song"
        case .part(let range): range.summary
        case .unreadable: "Song part answered"
        }
    }

    /// What VoiceOver reads for the answered line.
    var spokenSummary: String {
        switch choice {
        case .wholeSong: "Whole song"
        case .part(let range): range.spokenSummary
        case .unreadable: "Song part answered"
        }
    }
}

/// How a song-range question in the transcript is presented.
enum SongRangePhase: Equatable {
    /// The newest question that no `song_range` has answered: interactive.
    case active
    /// A later user message carried a `song_range` for this question: read-only.
    case answered(SongRangeAnswer)
    /// A newer question replaced it (or the server said it is closed) and nobody answered it: hidden.
    case superseded
}

/// Folds the transcript into per-question phases, like `SongOrderFold`: only a user message carrying a matching
/// `song_range` closes a card, so a plain reply about something else leaves it open.
enum SongRangeFold {
    struct Entry: Equatable {
        let messageID: String
        let question: SongRangeQuestion?
        let isUser: Bool
        /// The `song_range` this message carried (user messages only).
        var answer: SongRangeAnswer? = nil
    }

    /// `closed` holds question ids the server has said are no longer open (409 `song_range_stale`) that no
    /// transcript message answers, so the card does not keep offering a send the server will refuse.
    static func phases(_ entries: [Entry], closed: Set<String> = []) -> [String: SongRangePhase] {
        var result: [String: SongRangePhase] = [:]
        for (index, entry) in entries.enumerated() {
            guard let question = entry.question else { continue }
            let later = entries[(index + 1)...]
            if let answer = later.lazy.compactMap({ $0.isUser ? $0.answer : nil }).first(where: { $0.questionID == question.questionID }) {
                result[entry.messageID] = .answered(answer)
            } else if later.contains(where: { $0.question != nil }) || closed.contains(question.questionID) {
                result[entry.messageID] = .superseded
            } else {
                result[entry.messageID] = .active
            }
        }
        return result
    }
}

/// A 422 `song_range_invalid` for one question, shown inline on its card.
struct SongRangeProblem: Equatable {
    let questionID: String
    let message: String

    /// Only the server's own "that range is not allowed" answer; every other failure keeps the generic handling.
    static func isInvalid(_ error: APIError) -> Bool {
        guard case .requestFailed(let status, let detail) = error else { return false }
        return status == 422 && detail.code == SongRangeAnswer.invalidProblemCode
    }

    /// The server's copy ("Pick at least five seconds of your song."), else the same sentence the pickers use.
    init(questionID: String, error: APIError) {
        self.questionID = questionID
        if case .requestFailed(_, let detail) = error {
            message = detail.problemMessage ?? detail.message ?? SongRangeDraft.tooShortMessage
        } else {
            message = SongRangeDraft.tooShortMessage
        }
    }
}
