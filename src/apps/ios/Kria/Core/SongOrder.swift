import Foundation

// KRI-374: takes the server could not place against a creator's song with confidence are shown in chat as
// video widgets in a proposed order. The creator previews and reorders them, then sends the confirmed
// order back on the next turn (`song_order`).
//
// Hand-rolled tolerant parsing on purpose, like `ClipQuestion`: an absent, newer, or malformed payload
// means "no order card" (the text question still shows), never a decode failure that breaks the turn.
// Everything wire-shaped is in this file; the models are pure values so reorder logic is unit-testable.

/// The optional `song_order_question` on an assistant event payload (or a turn response).
struct SongOrderQuestion: Equatable, Sendable {
    enum Status: String, Equatable, Sendable {
        case confident, ambiguous, unmatched
        /// Anything but `confident` is a take the creator should check.
        var isUncertain: Bool { self != .confident }
    }

    struct Alternate: Equatable, Sendable {
        let deltaS: Double
        let score: Double
    }

    /// One place in the song a take could start (`song_order_placements` servers send up to four per take).
    /// `deltaS` is the take's song start and may be NEGATIVE: the take was filmed before the song begins.
    struct Candidate: Equatable, Sendable {
        let deltaS: Double
        let likelihood: Double
        let matchStartS: Double?
        let matchEndS: Double?
    }

    /// Why the server was unsure about a take.
    enum Reason: String, Equatable, Sendable { case tie, weak, noEvidence = "no_evidence" }

    struct Item: Equatable, Sendable, Identifiable {
        let mediaID: String
        let status: Status
        /// Where in the song the server thinks this take starts, in seconds (negative = filmed before the song);
        /// nil when it could not place it.
        let songStartS: Double?
        let alternates: [Alternate]
        /// The take's length, when the server sends it.
        let durationS: Double?
        let candidates: [Candidate]
        let likelihood: Double?
        let reason: Reason?
        var id: String { mediaID }

        init(mediaID: String, status: Status, songStartS: Double?, alternates: [Alternate],
             durationS: Double? = nil, candidates: [Candidate] = [], likelihood: Double? = nil, reason: Reason? = nil) {
            self.mediaID = mediaID; self.status = status; self.songStartS = songStartS; self.alternates = alternates
            self.durationS = durationS; self.candidates = candidates; self.likelihood = likelihood; self.reason = reason
        }
    }

    let questionID: String
    let proposedOrder: [String]
    let items: [Item]
    /// All of the following are optional additions (`song_order_placements` servers); nil on an older server.
    let songDurationS: Double?
    /// The longest the finished video may be.
    let maxWindowS: Double?
    let firstLineS: Double?
    let songGeneration: Int?

    /// nil when the payload has no usable question (absent, no id, no items).
    static func parse(payload: [String: JSONValue]?) -> SongOrderQuestion? {
        guard let fields = payload?["song_order_question"]?.objectValue else { return nil }
        return parse(fields: fields)
    }

    init?(json: JSONValue) {
        guard let fields = json.objectValue, let parsed = Self.parse(fields: fields) else { return nil }
        self = parsed
    }

    init(questionID: String, proposedOrder: [String], items: [Item], songDurationS: Double? = nil,
         maxWindowS: Double? = nil, firstLineS: Double? = nil, songGeneration: Int? = nil) {
        self.questionID = questionID; self.proposedOrder = proposedOrder; self.items = items
        self.songDurationS = songDurationS; self.maxWindowS = maxWindowS; self.firstLineS = firstLineS
        self.songGeneration = songGeneration
    }

    /// What the finished video may run when the server does not say.
    static let defaultMaxWindowS = 120.0

    private static func parse(fields: [String: JSONValue]) -> SongOrderQuestion? {
        guard let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        var seen = Set<String>()
        let items: [Item] = (fields["items"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let mediaID = object["media_id"]?.stringValue, !mediaID.isEmpty, seen.insert(mediaID).inserted else { return nil }
            // A status this build does not know is treated as uncertain: showing a "?" is the safe direction.
            let status = object["status"]?.stringValue.flatMap(Status.init(rawValue:)) ?? .unmatched
            // Negative is real: the take was filmed before the song starts (its song start == its delta).
            let start = object["song_start_s"]?.numberValue.flatMap { $0.isFinite ? $0 : nil }
            let alternates: [Alternate] = (object["alternates"]?.arrayValue ?? []).compactMap { alternate in
                guard let delta = alternate.objectValue?["delta_s"]?.numberValue, delta.isFinite else { return nil }
                return Alternate(deltaS: delta, score: alternate.objectValue?["score"]?.numberValue ?? 0)
            }
            // The new fields are all optional and parsed one by one: a bad one is dropped, never the card.
            let candidates: [Candidate] = (object["candidates"]?.arrayValue ?? []).compactMap { entry in
                guard let candidate = entry.objectValue, let delta = candidate["delta_s"]?.numberValue, delta.isFinite else { return nil }
                return Candidate(deltaS: delta, likelihood: candidate["likelihood"]?.numberValue.flatMap { $0.isFinite ? $0 : nil } ?? 0,
                                 matchStartS: candidate["match_start_s"]?.numberValue.flatMap { $0.isFinite ? $0 : nil },
                                 matchEndS: candidate["match_end_s"]?.numberValue.flatMap { $0.isFinite ? $0 : nil })
            }
            return Item(mediaID: mediaID, status: status, songStartS: start, alternates: alternates,
                        durationS: positive(object["duration_s"]), candidates: Array(candidates.prefix(4)),
                        likelihood: object["likelihood"]?.numberValue.flatMap { $0.isFinite ? $0 : nil },
                        reason: object["reason"]?.stringValue.flatMap(Reason.init(rawValue:)))
        }
        guard !items.isEmpty else { return nil }
        let proposed = (fields["proposed_order"]?.arrayValue ?? []).compactMap(\.stringValue)
        let generation = fields["song_generation"]?.numberValue.flatMap { $0.isFinite && $0 == $0.rounded() && abs($0) < 1e9 ? Int($0) : nil }
        return SongOrderQuestion(questionID: questionID, proposedOrder: proposed, items: items,
                                 songDurationS: positive(fields["song_duration_s"]), maxWindowS: positive(fields["max_window_s"]),
                                 firstLineS: fields["first_line_s"]?.numberValue.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil },
                                 songGeneration: generation)
    }

    /// A finite number above zero, or nil.
    private static func positive(_ value: JSONValue?) -> Double? {
        value?.numberValue.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
    }

    /// Takes the creator should look at.
    var uncertainCount: Int { items.filter { $0.status.isUncertain }.count }
    /// Takes whose place in the song is doubtful but have a best guess: the ones the creator can fix by reordering.
    var ambiguousCount: Int { items.filter { $0.status == .ambiguous }.count }
    /// Takes the server could not place at all. It can only use them as filler, so they are not reorderable.
    var unmatchedCount: Int { items.filter { $0.status == .unmatched }.count }

    /// The proposed order cleaned for display: only known items, each once; items the server listed in `items`
    /// but left out of `proposed_order` follow, in item order. Never drops a take.
    var normalizedOrder: [String] {
        let known = Set(items.map(\.mediaID))
        var seen = Set<String>()
        var result = proposedOrder.filter { known.contains($0) && seen.insert($0).inserted }
        for item in items where seen.insert(item.mediaID).inserted { result.append(item.mediaID) }
        return result
    }

    func item(for mediaID: String) -> Item? { items.first { $0.mediaID == mediaID } }
}

/// `song_order` on the submit-turn body, exactly as the server contract names it.
struct SongOrderSubmission: Encodable, Equatable, Sendable {
    /// A take placed on the song at `deltaS` (its song start; may be negative).
    struct Placement: Encodable, Equatable, Sendable {
        let mediaID: String
        let deltaS: Double
        enum CodingKeys: String, CodingKey { case mediaID = "media_id", deltaS = "delta_s" }
    }

    let questionID: String
    /// Every take exactly once.
    let orderedMediaIDs: [String]
    /// The takes placed on the song timeline (`song_order_placements` servers only). nil = key omitted; `[]` = the
    /// creator placed nothing (every take stays in the tray as background footage).
    let placements: [Placement]?
    enum CodingKeys: String, CodingKey { case questionID = "question_id", orderedMediaIDs = "ordered_media_ids", placements }
    /// The 409 `code` the server answers a `song_order` for a question it has since replaced.
    static let staleConflictCode = "song_order_stale"

    init(questionID: String, orderedMediaIDs: [String], placements: [Placement]? = nil) {
        self.questionID = questionID; self.orderedMediaIDs = orderedMediaIDs; self.placements = placements
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(questionID, forKey: .questionID)
        try c.encode(orderedMediaIDs, forKey: .orderedMediaIDs)
        try c.encodeIfPresent(placements, forKey: .placements)
    }

    /// The `song_order` a user message carried (the server echoes it on the stored event), or nil when the
    /// message has none or it is malformed. This is how a relaunch knows which question was really answered.
    static func parse(payload: [String: JSONValue]?) -> SongOrderSubmission? {
        guard let fields = payload?["song_order"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        let ids = (fields["ordered_media_ids"]?.arrayValue ?? []).compactMap(\.stringValue)
        guard !ids.isEmpty else { return nil }
        // Placements are tolerant: an absent or malformed list is "no placements", never a lost answer.
        let placements = fields["placements"]?.arrayValue?.compactMap { entry -> Placement? in
            guard let object = entry.objectValue, let id = object["media_id"]?.stringValue, !id.isEmpty,
                  let delta = object["delta_s"]?.numberValue, delta.isFinite else { return nil }
            return Placement(mediaID: id, deltaS: delta)
        }
        return SongOrderSubmission(questionID: questionID, orderedMediaIDs: ids, placements: placements)
    }

    /// Short read-only summary once answered, e.g. "Clip 2 · Clip 1 · Clip 3". A timeline answer names where
    /// each placed clip sits, e.g. "Clip 2 at 0:08 · Clip 1 at 0:21 · Clip 3 in the background".
    func summary(positions: [String: Int]) -> String {
        guard placements != nil else {
            return orderedMediaIDs.compactMap { positions[$0] }.map { "Clip \($0)" }.joined(separator: " · ")
        }
        return arrangementParts(positions: positions) { number, time in "Clip \(number) at \(time)" } tray: { "Clip \($0) in the background" }
            .joined(separator: " · ")
    }

    /// The readable chat message sent alongside the structured payload.
    func message(positions: [String: Int]) -> String {
        guard placements != nil else {
            let numbers = orderedMediaIDs.compactMap { positions[$0] }.map(String.init).joined(separator: ", ")
            return numbers.isEmpty ? "Use this order" : "Use this order: clips \(numbers)"
        }
        let parts = arrangementParts(positions: positions) { number, time in "clip \(number) at \(time)" } tray: { "clip \($0) as background footage" }
        return parts.isEmpty ? "Use this arrangement" : "Use this arrangement: " + parts.joined(separator: ", ")
    }

    /// Placed takes by song time ("clip 2 at 0:08"), then tray takes ("clip 3 as background footage").
    private func arrangementParts(positions: [String: Int], placed: (Int, String) -> String, tray: (Int) -> String) -> [String] {
        let placements = placements ?? []
        let placedIDs = Set(placements.map(\.mediaID))
        var parts = placements.compactMap { placement in
            positions[placement.mediaID].map { placed($0, DurationFormatter.clock(placement.deltaS)) }
        }
        parts += orderedMediaIDs.filter { !placedIDs.contains($0) }.compactMap { positions[$0] }.map(tray)
        return parts
    }
}

/// Mutable order for one question. Pure value type so the interactions are unit-testable.
struct SongOrderState: Equatable {
    let question: SongOrderQuestion
    private let proposed: [String]
    private(set) var order: [String]

    init(question: SongOrderQuestion) {
        self.question = question
        proposed = question.normalizedOrder
        order = proposed
    }

    /// True once the creator has moved anything away from the proposal.
    var isChanged: Bool { order != proposed }

    func position(of mediaID: String) -> Int? { order.firstIndex(of: mediaID).map { $0 + 1 } }
    func canMoveUp(_ mediaID: String) -> Bool { (order.firstIndex(of: mediaID) ?? 0) > 0 }
    func canMoveDown(_ mediaID: String) -> Bool { order.firstIndex(of: mediaID).map { $0 < order.count - 1 } ?? false }

    /// `List.onMove` semantics: `destination` is an index into the pre-move array.
    mutating func move(from source: IndexSet, to destination: Int) {
        guard !source.isEmpty, source.allSatisfy({ order.indices.contains($0) }) else { return }
        let moving = source.sorted().map { order[$0] }
        let insertion = destination - source.filter { $0 < destination }.count
        for index in source.sorted(by: >) { order.remove(at: index) }
        order.insert(contentsOf: moving, at: max(0, min(order.count, insertion)))
    }

    mutating func moveUp(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index > 0 else { return }
        order.swapAt(index, index - 1)
    }

    mutating func moveDown(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index < order.count - 1 else { return }
        order.swapAt(index, index + 1)
    }

    /// Resets to the order the server proposed.
    mutating func reset() { order = proposed }

    var submission: SongOrderSubmission { SongOrderSubmission(questionID: question.questionID, orderedMediaIDs: order) }
}

/// Pure state behind the song timeline card: which takes sit where on the song, which are left in the tray,
/// and the rules the server applies when it tiles them (so the card never offers an arrangement the server
/// would silently rewrite). Positions are song seconds; a take's song start is its `delta` (may be negative).
struct SongTimelineState: Equatable {
    /// A drag within this of one of the take's candidate positions lands exactly on it.
    static let snapTolerance = 0.25
    /// Uncovered song shorter than this is not offered as an empty spot.
    static let minGap = 0.6
    /// A block that extends the previous visible one by less than this is dropped by the server.
    static let minExtension = 1.0
    /// A take whose length is unknown is drawn (and counted) as this long.
    static let fallbackDuration = 8.0
    private static let epsilon = 1e-6

    enum Refusal: Equatable {
        case tooLong(maxSeconds: Int)
        var message: String {
            switch self {
            case .tooLong(let seconds): "That makes the video longer than \(seconds) seconds."
            }
        }
    }

    /// A neighbour that was shifted to make room for the clip being placed. Its footage offset is untouched, so its
    /// `delta` (and with it where in the song it plays) changed: it is no longer at the position the server measured.
    struct Moved: Equatable {
        let mediaID: String
        let from: Double
        let to: Double
    }

    /// What a move did, so the card can say so. A `refusal` means nothing changed.
    struct MoveOutcome: Equatable {
        /// Other clips pushed aside (still on the song), by new position.
        var moved: [Moved] = []
        /// Clips pushed aside with nowhere to go: they are in the tray now.
        var sentToTray: [String] = []
        var refusal: Refusal?

        var displacedAnything: Bool { !moved.isEmpty || !sentToTray.isEmpty }

        /// "Clip 2 moved over to make room." / "Clips 2 and 3 didn't fit and went to the tray." (nil when nothing happened).
        func message(clipNumber: (String) -> Int) -> String? {
            if let refusal { return refusal.message }
            var parts: [String] = []
            if !moved.isEmpty { parts.append(Self.sentence(moved.map(\.mediaID), clipNumber, "moved over to make room.")) }
            if !sentToTray.isEmpty { parts.append(Self.sentence(sentToTray, clipNumber, "didn't fit and went to the tray.")) }
            return parts.isEmpty ? nil : parts.joined(separator: " ")
        }

        private static func sentence(_ ids: [String], _ clipNumber: (String) -> Int, _ tail: String) -> String {
            let numbers = ids.map(clipNumber).sorted().map(String.init)
            guard numbers.count > 1, let last = numbers.last else { return "Clip \(numbers.first ?? "") \(tail)" }
            return "Clips \(numbers.dropLast().joined(separator: ", ")) and \(last) \(tail)"
        }
    }

    /// A placed take on the song axis, already clipped to the song. `hidden` blocks are ones the server drops.
    struct Segment: Equatable, Identifiable {
        let mediaID: String
        let start: Double
        let end: Double
        let hidden: Bool
        var id: String { mediaID }
    }

    /// Uncovered song strictly between the first and last visible block.
    struct Gap: Equatable, Identifiable {
        let index: Int
        let start: Double
        let end: Double
        var id: Int { index }
        var length: Double { end - start }
    }

    let question: SongOrderQuestion
    let maxWindowS: Double
    let songDurationS: Double?
    private let durations: [String: Double]
    private let captureOrder: [String]
    private let seed: [String: Double]
    /// mediaID -> delta for every take placed on the song. Takes not in here are in the tray.
    private(set) var placed: [String: Double]

    /// `media` supplies capture order (the clip numbers) and fallback take lengths.
    init(question: SongOrderQuestion, media: [CreationAttachedMedia] = [], songDurationS: Double? = nil) {
        self.question = question
        maxWindowS = question.maxWindowS ?? SongOrderQuestion.defaultMaxWindowS
        self.songDurationS = question.songDurationS ?? songDurationS
        var durations: [String: Double] = [:]
        for clip in media { if let seconds = clip.durationS, seconds.isFinite, seconds > 0 { durations[clip.id] = seconds } }
        self.durations = durations
        let known = Set(question.items.map(\.mediaID))
        let numbers = SongOrderPositions.map(media: media, question: question)
        captureOrder = question.items.map(\.mediaID).sorted { (numbers[$0] ?? .max, $0) < (numbers[$1] ?? .max, $1) }.filter(known.contains)
        var seed: [String: Double] = [:]
        for item in question.items {
            if item.status != .unmatched, let start = item.songStartS { seed[item.mediaID] = start }
        }
        self.seed = seed
        placed = seed
    }

    // MARK: reading

    func duration(for mediaID: String) -> Double {
        if let seconds = question.item(for: mediaID)?.durationS, seconds > 0 { return seconds }
        return durations[mediaID] ?? Self.fallbackDuration
    }

    /// Where a take starting at `delta` sits on the song axis: clipped to the song (a negative delta starts at 0).
    func range(of mediaID: String, delta: Double) -> (start: Double, end: Double) {
        var end = delta + duration(for: mediaID)
        if let songDurationS { end = min(end, songDurationS) }
        return (max(0, delta), end)
    }

    func delta(of mediaID: String) -> Double? { placed[mediaID] }
    func isPlaced(_ mediaID: String) -> Bool { placed[mediaID] != nil }
    var isChanged: Bool { placed != seed }

    /// Takes left off the song, in capture order.
    var tray: [String] { captureOrder.filter { placed[$0] == nil } }

    /// Every placed take on the song axis, by start, with the ones the server would drop flagged `hidden`.
    func segments() -> [Segment] { tile(placed) }

    func gaps() -> [Gap] {
        let visible = segments().filter { !$0.hidden }
        guard var coveredEnd = visible.first?.end else { return [] }
        var result: [Gap] = []
        for segment in visible.dropFirst() {
            if segment.start - coveredEnd >= Self.minGap - Self.epsilon {
                result.append(Gap(index: result.count, start: coveredEnd, end: segment.start))
            }
            coveredEnd = max(coveredEnd, segment.end)
        }
        return result
    }

    /// The one line under the timeline about what the server does with the tray and with uncovered song. The planner
    /// fills an interior gap only from tray clips; a gap nothing can fill makes it split the montage and keep one side.
    enum FooterNotice: Equatable {
        case trayFillsEmptySpots
        case emptySpotsCannotBeFilled
        case trayIsBackground
        var message: String {
            switch self {
            case .trayFillsEmptySpots: "Clips in the tray will fill empty spots where they fit."
            case .emptySpotsCannotBeFilled: "Empty spots can't be filled — clips on one side may be left out."
            case .trayIsBackground: "Clips in the tray play as background footage."
            }
        }
        /// A warning (still sendable) rather than a hint.
        var isWarning: Bool { self == .emptySpotsCannotBeFilled }
    }

    var footerNotice: FooterNotice? {
        let hasGaps = !gaps().isEmpty, hasTray = !tray.isEmpty
        if hasGaps { return hasTray ? .trayFillsEmptySpots : .emptySpotsCannotBeFilled }
        return hasTray ? .trayIsBackground : nil
    }

    /// First visible start to last visible end, in song seconds; 0 with nothing placed.
    var span: Double { Self.span(of: tile(placed)) }

    // MARK: editing

    /// Puts a tray take into an empty spot. Uses the take's own best candidate that starts inside the spot,
    /// else the spot's start. A take longer than the spot pushes its neighbours aside.
    @discardableResult
    mutating func place(_ mediaID: String, inGap gap: Gap) -> MoveOutcome {
        guard question.item(for: mediaID) != nil else { return MoveOutcome() }
        let inside = (question.item(for: mediaID)?.candidates ?? []).filter {
            max(0, $0.deltaS) >= gap.start - Self.epsilon && max(0, $0.deltaS) < gap.end
        }
        let delta = inside.max { $0.likelihood < $1.likelihood }?.deltaS ?? gap.start
        return commit(mediaID, delta: delta)
    }

    /// "Add to the song" for a tray take when there is no empty spot to tap: its likeliest candidate that lands
    /// without pushing anything, else straight after the last visible clip (or the start of the song with nothing placed).
    @discardableResult
    mutating func placeAnywhere(_ mediaID: String) -> MoveOutcome {
        guard let item = question.item(for: mediaID) else { return MoveOutcome() }
        for candidate in item.candidates.sorted(by: { $0.likelihood > $1.likelihood }) {
            let result = plan(mediaID, delta: candidate.deltaS)
            if result.outcome.refusal == nil, !result.outcome.displacedAnything {
                placed = result.placed
                return result.outcome
            }
        }
        let lastEnd = segments().filter { !$0.hidden }.map(\.end).max() ?? 0
        return commit(mediaID, delta: clamped(mediaID, lastEnd))
    }

    /// Moves (or places) a take so it starts at `toDelta`, snapping to one of its candidates within 0.25 s. The take
    /// lands exactly there; any other clip it would cover is pushed aside (see `plan`).
    @discardableResult
    mutating func move(_ mediaID: String, toDelta raw: Double) -> MoveOutcome {
        guard question.item(for: mediaID) != nil, raw.isFinite else { return MoveOutcome() }
        return commit(mediaID, delta: snapped(mediaID, raw))
    }

    /// The delta a drop at `raw` lands on: a candidate within the snap tolerance, else `raw` clamped and rounded to 0.1 s.
    func snapped(_ mediaID: String, _ raw: Double) -> Double {
        let candidates = question.item(for: mediaID)?.candidates ?? []
        if let near = candidates.min(by: { abs($0.deltaS - raw) < abs($1.deltaS - raw) }),
           abs(near.deltaS - raw) <= Self.snapTolerance + Self.epsilon {
            return near.deltaS
        }
        return (clamped(mediaID, raw) * 10).rounded() / 10
    }

    /// Keeps at least a second of the take on the song at either end.
    func clamped(_ mediaID: String, _ delta: Double) -> Double {
        let lower = -(duration(for: mediaID) - Self.minExtension)
        let upper = max(lower, (songDurationS ?? .infinity) - Self.minExtension)
        return min(max(delta, lower), upper)
    }

    mutating func moveToTray(_ mediaID: String) { placed[mediaID] = nil }

    mutating func reset() { placed = seed }

    /// A copy with the move applied, for showing where things would land while a clip is still being dragged.
    func previewing(_ mediaID: String, toDelta raw: Double) -> SongTimelineState {
        var copy = self
        copy.move(mediaID, toDelta: raw)
        return copy
    }

    private mutating func commit(_ mediaID: String, delta: Double) -> MoveOutcome {
        let result = plan(mediaID, delta: delta)
        if result.outcome.refusal == nil { placed = result.placed }
        return result.outcome
    }

    // MARK: pushing aside

    private struct PushPlan {
        /// New delta per displaced clip; nil = it did not fit and goes to the tray.
        var moves: [(mediaID: String, delta: Double?)] = []
        var trayCount = 0
        var totalShift = 0.0
    }

    /// Places `mediaID` at `delta` and pushes aside every other visible clip it overlaps. The placed clip is the
    /// anchor and never moves. A clip it overlaps goes to whichever side needs the smaller shift (a tie goes right)
    /// and the clips behind it are pushed along, keeping order and staying adjacent. Left pushes stop at 0 and right
    /// pushes at the song's end: a clip that would cross either goes to the tray, and a side that keeps every clip
    /// placed beats one that does not. If the video then exceeds the cap, the pushed clips farthest from the anchor go
    /// to the tray until it fits; an anchor longer than the cap by itself is refused.
    private func plan(_ mediaID: String, delta: Double) -> (placed: [String: Double], outcome: MoveOutcome) {
        var outcome = MoveOutcome()
        let anchor = range(of: mediaID, delta: delta)
        if anchor.end - anchor.start > maxWindowS + Self.epsilon {
            outcome.refusal = .tooLong(maxSeconds: Int(maxWindowS.rounded()))
            return (placed, outcome)
        }
        var next = placed
        next[mediaID] = delta
        var originals: [String: Double] = [:]
        var trayed: [String] = []
        // One round normally; another only when pushing a clip away uncovers a seed block that was hidden under it.
        for _ in 0..<(placed.count + 2) {
            let visible = tile(next.filter { $0.key != mediaID }).filter { !$0.hidden }
            let hits = visible.indices.filter { Self.overlap(visible[$0], anchor) > Self.epsilon }
            guard let first = hits.first, let last = hits.last else { break }
            let push = bestPush(visible, hits: first...last, anchor: anchor, deltas: next)
            for move in push.moves {
                if originals[move.mediaID] == nil { originals[move.mediaID] = next[move.mediaID] }
                next[move.mediaID] = move.delta
                if move.delta == nil { trayed.append(move.mediaID) }
            }
        }
        let before = Self.span(of: tile(placed))
        var span = Self.span(of: tile(next))
        if span > maxWindowS + Self.epsilon {
            let center = (anchor.start + anchor.end) / 2
            let pushed = originals.keys.filter { next[$0] != nil }.sorted { distance($0, from: center, in: next) > distance($1, from: center, in: next) }
            for id in pushed where span > maxWindowS + Self.epsilon {
                next[id] = nil
                trayed.append(id)
                span = Self.span(of: tile(next))
            }
            if span > maxWindowS + Self.epsilon, span > before + Self.epsilon {
                outcome.refusal = .tooLong(maxSeconds: Int(maxWindowS.rounded()))
                return (placed, outcome)
            }
        }
        outcome.moved = originals.compactMap { id, old in
            next[id].flatMap { abs($0 - old) > 1e-9 ? Moved(mediaID: id, from: old, to: $0) : nil }
        }.sorted { ($0.to, $0.mediaID) < ($1.to, $1.mediaID) }
        let order = Dictionary(uniqueKeysWithValues: captureOrder.enumerated().map { ($1, $0) })
        outcome.sentToTray = Array(Set(trayed)).sorted { (order[$0] ?? .max) < (order[$1] ?? .max) }
        return (next, outcome)
    }

    private func distance(_ mediaID: String, from center: Double, in deltas: [String: Double]) -> Double {
        guard let delta = deltas[mediaID] else { return 0 }
        let block = range(of: mediaID, delta: delta)
        return abs((block.start + block.end) / 2 - center)
    }

    private static func overlap(_ segment: Segment, _ anchor: (start: Double, end: Double)) -> Double {
        min(segment.end, anchor.end) - max(segment.start, anchor.start)
    }

    /// Tries every way of splitting the overlapped clips into "go left" (an earlier prefix) and "go right" and keeps
    /// the one that sends the fewest clips to the tray, then shifts the least; a tie prefers right.
    private func bestPush(_ visible: [Segment], hits: ClosedRange<Int>, anchor: (start: Double, end: Double),
                          deltas: [String: Double]) -> PushPlan {
        var best: PushPlan?
        for split in hits.lowerBound...(hits.upperBound + 1) {
            let candidate = simulatePush(visible, split: split, anchor: anchor, deltas: deltas)
            guard let current = best else { best = candidate; continue }
            if candidate.trayCount < current.trayCount
                || (candidate.trayCount == current.trayCount && candidate.totalShift < current.totalShift - 1e-6) {
                best = candidate
            }
        }
        return best ?? PushPlan()
    }

    /// Clips before `split` are pushed left of the anchor (nearest first), the rest right of it.
    private func simulatePush(_ visible: [Segment], split: Int, anchor: (start: Double, end: Double),
                              deltas: [String: Double]) -> PushPlan {
        var plan = PushPlan()
        var limit = anchor.start
        var index = split - 1
        while index >= 0 {
            let segment = visible[index]
            guard segment.end > limit + Self.epsilon, let current = deltas[segment.mediaID] else { break }
            let target = limit - duration(for: segment.mediaID)
            if target >= -Self.epsilon {
                plan.moves.append((segment.mediaID, target))
                plan.totalShift += abs(current - target)
                limit = max(0, target)
            } else {
                plan.moves.append((segment.mediaID, nil))
                plan.trayCount += 1
            }
            index -= 1
        }
        limit = anchor.end
        index = split
        while index < visible.count {
            let segment = visible[index]
            guard segment.start < limit - Self.epsilon, let current = deltas[segment.mediaID] else { break }
            let target = current + (limit - segment.start)
            if songDurationS.map({ target + duration(for: segment.mediaID) <= $0 + Self.epsilon }) ?? true {
                plan.moves.append((segment.mediaID, target))
                plan.totalShift += abs(target - current)
                limit = target + duration(for: segment.mediaID)
            } else {
                plan.moves.append((segment.mediaID, nil))
                plan.trayCount += 1
            }
            index += 1
        }
        return plan
    }

    // MARK: tiling

    /// The server's tiling: sort by start (longer first on a tie), drop a block contained in an earlier one,
    /// and drop one that extends the previous visible block by less than a second.
    private func tile(_ placed: [String: Double]) -> [Segment] {
        let index = Dictionary(uniqueKeysWithValues: captureOrder.enumerated().map { ($1, $0) })
        let raw = placed.map { id, delta -> (id: String, start: Double, end: Double) in
            let range = range(of: id, delta: delta)
            return (id, range.start, range.end)
        }.sorted {
            ($0.start, -$0.end, index[$0.id] ?? .max) < ($1.start, -$1.end, index[$1.id] ?? .max)
        }
        var result: [Segment] = []
        var maxEnd = -Double.infinity
        var visibleEnd: Double?
        for block in raw {
            var hidden = block.end - block.start <= Self.epsilon        // entirely outside the song
            if !hidden {
                if block.end <= maxEnd + Self.epsilon {
                    hidden = true                                        // inside an earlier block
                } else if let visibleEnd, block.end - visibleEnd < Self.minExtension - Self.epsilon {
                    hidden = true                                        // adds under a second
                }
                maxEnd = max(maxEnd, block.end)
                if !hidden { visibleEnd = block.end }
            }
            result.append(Segment(mediaID: block.id, start: block.start, end: block.end, hidden: hidden))
        }
        return result
    }

    private static func span(of segments: [Segment]) -> Double {
        let visible = segments.filter { !$0.hidden }
        guard let first = visible.first, let last = visible.map(\.end).max() else { return 0 }
        return last - first.start
    }

    // MARK: answering

    /// Placed takes by song time, then the tray in capture order. `includePlacements` is the server capability.
    func submission(includePlacements: Bool) -> SongOrderSubmission {
        let order = captureOrder.enumerated().reduce(into: [String: Int]()) { $0[$1.element] = $1.offset }
        let placedByTime = placed.sorted { ($0.value, order[$0.key] ?? .max) < ($1.value, order[$1.key] ?? .max) }
        return SongOrderSubmission(
            questionID: question.questionID,
            orderedMediaIDs: placedByTime.map(\.key) + tray,
            placements: includePlacements ? placedByTime.map { .init(mediaID: $0.key, deltaS: $0.value) } : nil)
    }
}

/// Clip numbers shown on the order card: position among the thread's non-audio media, in attach order.
/// Matches the numbers the clip picker uses, so "Clip 3" means the same take everywhere in the chat.
enum SongOrderPositions {
    static func map(media: [CreationAttachedMedia], question: SongOrderQuestion) -> [String: Int] {
        var result: [String: Int] = [:]
        for (index, clip) in media.filter({ $0.kind != "audio" }).enumerated() { result[clip.id] = index + 1 }
        // A take the thread state doesn't list (stale state) still gets a stable number.
        var fallback = result.count
        for id in question.normalizedOrder where result[id] == nil {
            fallback += 1
            result[id] = fallback
        }
        return result
    }
}

/// How a question in the transcript is presented.
enum SongOrderPhase: Equatable {
    /// Still open: the newest question that no `song_order` has answered. Interactive.
    case active
    /// A later user message carried a `song_order` for this question: read-only. The submission is what
    /// that message sent, so the card can say what was confirmed even after a relaunch.
    case answered(SongOrderSubmission)
    /// A newer question replaced it and nobody answered this one: hidden.
    case superseded
}

/// Folds the transcript into per-question phases. The server keeps a question open until a `song_order` for
/// that question arrives (`latest_open_song_order_question`), so a plain reply, a reaction, or a message
/// about something else never closes the card: only a user message carrying a matching `song_order` does.
enum SongOrderFold {
    struct Entry: Equatable {
        let messageID: String
        let question: SongOrderQuestion?
        let isUser: Bool
        /// The `song_order` this message carried (user messages only).
        var answer: SongOrderSubmission? = nil
    }

    static func phases(_ entries: [Entry]) -> [String: SongOrderPhase] {
        var result: [String: SongOrderPhase] = [:]
        for (index, entry) in entries.enumerated() {
            guard let question = entry.question else { continue }
            let later = entries[(index + 1)...]
            if let answer = later.lazy.compactMap({ $0.isUser ? $0.answer : nil }).first(where: { $0.questionID == question.questionID }) {
                result[entry.messageID] = .answered(answer)
            } else if later.contains(where: { $0.question != nil }) {
                result[entry.messageID] = .superseded
            } else {
                result[entry.messageID] = .active
            }
        }
        return result
    }
}
