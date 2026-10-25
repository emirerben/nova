import Foundation

// KRI-723: "Six moments from it, in this order: Caro arriving with the black tray, her hello wave, ...". The
// server only trusts a moment it can place from the clip record. For the rest it asks, before approval, "put your
// moments in order and show me where each one is" (`moment_order_question`). The creator answers with a card: put
// the moments in order, and for each one point at a window in the take, say it isn't in their clips, or let Kria
// pick. The answer travels back as `moment_order` on the next turn; the server checks it (409 stale, 404 foreign
// clip, 422 bad bounds / overlap) and pins it in the v2 length plan.
//
// Same shape as the mark question (`ProtectedRegionQuestion.swift`): hand-rolled tolerant parsing, because an
// absent, newer or malformed payload must mean "no card" (the text question still shows), never a failed decode.

/// A stretch of the take, on the ORIGINAL's clock (the server validates against `take.duration_s`).
struct MomentWindow: Equatable, Sendable {
    var start: Double
    var end: Double

    var length: Double { end - start }

    func overlaps(_ other: MomentWindow) -> Bool {
        // The server's own tolerance (`validate_moment_order_answer`).
        start < other.end - 0.001 && end > other.start + 0.001
    }

    /// "0:03.2–0:05.0": tenths, because two moments a second apart must not read the same.
    var label: String { "\(Self.clock(start))–\(Self.clock(end))" }

    /// Exact seconds, for accessibility values and UI tests.
    var exact: String { String(format: "%.2f-%.2f", start, end) }

    static func clock(_ seconds: Double) -> String {
        let tenths = max(0, Int((seconds * 10).rounded()))
        return String(format: "%d:%02d.%d", tenths / 600, (tenths / 10) % 60, tenths % 10)
    }
}

/// The optional `moment_order_question` on an assistant event payload.
struct MomentOrderQuestion: Equatable, Sendable {
    enum Status: String, Sendable { case grounded, ambiguous, unlocated, answered }

    struct Moment: Equatable, Sendable, Identifiable {
        let key: String
        /// The creator's own words for it ("her hello wave").
        let words: String
        let status: Status
        /// Where the clip record (or an earlier answer) places it; nil when nobody knows.
        let found: MomentWindow?
        /// Equal candidates for an ambiguous moment, best first.
        let suggestions: [MomentWindow]
        var id: String { key }
    }

    let questionID: String
    /// The take every moment is in, and its storage generation the question was asked about.
    let mediaID: String
    let durationS: Double
    /// How long a moment plays when nobody said (the server's default window).
    let defaultWindowS: Double
    let moments: [Moment]

    /// The server refuses a pointed window shorter than this (`MIN_POINTED_S`).
    static let minimumWindowS = 0.3

    static func parse(payload: [String: JSONValue]?) -> MomentOrderQuestion? {
        guard let fields = payload?["moment_order_question"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty,
              let take = fields["take"]?.objectValue,
              let mediaID = take["media_id"]?.stringValue, !mediaID.isEmpty,
              let duration = take["duration_s"]?.numberValue, duration.isFinite, duration > 0,
              let rows = fields["moments"]?.arrayValue else { return nil }
        func window(_ value: JSONValue?) -> MomentWindow? {
            guard let object = value?.objectValue,
                  let start = object["source_start_s"]?.numberValue, let end = object["source_end_s"]?.numberValue,
                  start.isFinite, end.isFinite, end > start else { return nil }
            return MomentWindow(start: start, end: end)
        }
        var seen = Set<String>()
        let moments: [Moment] = rows.compactMap { row in
            guard let object = row.objectValue,
                  let key = object["moment_key"]?.stringValue, !key.isEmpty, seen.insert(key).inserted,
                  let words = object["words"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !words.isEmpty
            else { return nil }
            let status = object["status"]?.stringValue.flatMap(Status.init(rawValue:)) ?? .unlocated
            let suggestions = (object["suggestions"]?.arrayValue ?? []).compactMap { window($0) }
            return Moment(key: key, words: words, status: status, found: window(object["found"]), suggestions: suggestions)
        }
        // A row this build cannot read would make the answer incomplete (422): show no card instead.
        guard !moments.isEmpty, moments.count == rows.count else { return nil }
        let defaultWindow = fields["default_window_s"]?.numberValue ?? 2
        return MomentOrderQuestion(questionID: questionID, mediaID: mediaID, durationS: duration,
                                   defaultWindowS: defaultWindow.isFinite && defaultWindow > 0 ? defaultWindow : 2,
                                   moments: moments)
    }

    func moment(_ key: String) -> Moment? { moments.first { $0.key == key } }

    /// A window the server accepts: inside the take, at least `minimumWindowS` long, rounded to milliseconds.
    func clamped(_ window: MomentWindow) -> MomentWindow {
        let minimum = min(Self.minimumWindowS, durationS)
        var start = window.start.isFinite ? max(0, window.start) : 0
        var end = window.end.isFinite ? min(durationS, window.end) : start + defaultWindowS
        if end - start < minimum { end = start + minimum }
        if end > durationS { end = durationS; start = max(0, end - minimum) }
        func ms(_ value: Double) -> Double { (value * 1000).rounded() / 1000 }
        return MomentWindow(start: ms(start), end: ms(min(end, durationS)))
    }

    /// Where the picker opens for a moment: the creator's last window, the evidence, the best suggestion, else a
    /// default-length window at the start of the take.
    func initialWindow(for key: String, current: MomentWindow?) -> MomentWindow {
        if let current { return clamped(current) }
        if let moment = moment(key), let known = moment.found ?? moment.suggestions.first { return clamped(known) }
        return clamped(MomentWindow(start: 0, end: defaultWindowS))
    }
}

/// What the creator chose for one moment.
enum MomentChoice: Equatable, Sendable {
    case point(MomentWindow)
    case skip
    case delegate
}

/// The card's working state: the creator's order and a choice per moment.
struct MomentOrderDraft: Equatable {
    private(set) var order: [String]
    private(set) var choices: [String: MomentChoice]

    /// Kria's order, with each moment the clip record placed already pointed at its evidence (the creator can
    /// change it). The rest start unchosen: nothing is guessed for them.
    init(question: MomentOrderQuestion) {
        order = question.moments.map(\.key)
        var choices: [String: MomentChoice] = [:]
        for moment in question.moments where moment.status == .grounded || moment.status == .answered {
            if let found = moment.found { choices[moment.key] = .point(question.clamped(found)) }
        }
        self.choices = choices
    }

    func position(of key: String) -> Int? { order.firstIndex(of: key).map { $0 + 1 } }
    func canMoveUp(_ key: String) -> Bool { (order.firstIndex(of: key) ?? 0) > 0 }
    func canMoveDown(_ key: String) -> Bool { order.firstIndex(of: key).map { $0 < order.count - 1 } ?? false }

    mutating func moveUp(_ key: String) {
        guard let index = order.firstIndex(of: key), index > 0 else { return }
        order.swapAt(index, index - 1)
    }

    mutating func moveDown(_ key: String) {
        guard let index = order.firstIndex(of: key), index < order.count - 1 else { return }
        order.swapAt(index, index + 1)
    }

    mutating func choose(_ choice: MomentChoice, for key: String) {
        guard order.contains(key) else { return }
        choices[key] = choice
    }

    func choice(for key: String) -> MomentChoice? { choices[key] }

    /// Every moment pointed at, skipped or left to Kria: the server refuses anything less.
    var isComplete: Bool { order.allSatisfy { choices[$0] != nil } }

    /// Two pointed windows that overlap (the server answers 422 `moment_order_overlap`): their keys.
    var overlapping: Set<String> {
        let pointed = order.compactMap { key -> (String, MomentWindow)? in
            if case .point(let window)? = choices[key] { return (key, window) }
            return nil
        }
        var result = Set<String>()
        for (i, a) in pointed.enumerated() {
            for b in pointed[(i + 1)...] where a.1.overlaps(b.1) { result.insert(a.0); result.insert(b.0) }
        }
        return result
    }

    func answer(question: MomentOrderQuestion) -> MomentOrderAnswer? {
        guard isComplete else { return nil }
        let rows: [MomentOrderAnswer.Row] = order.compactMap { key in
            switch choices[key] {
            case .point(let window)?:
                let window = question.clamped(window)
                return .init(momentKey: key, action: "point", mediaID: question.mediaID,
                             sourceStartS: window.start, sourceEndS: window.end)
            case .skip?: return .init(momentKey: key, action: "skip")
            case .delegate?: return .init(momentKey: key, action: "delegate")
            case nil: return nil
            }
        }
        return MomentOrderAnswer(questionID: question.questionID, answers: rows)
    }
}

/// `moment_order` on the submit-turn body, exactly as the server's `MomentOrderAnswerIn` names it.
struct MomentOrderAnswer: Encodable, Equatable, Sendable {
    struct Row: Encodable, Equatable, Sendable {
        let momentKey: String
        /// `point` / `skip` / `delegate`.
        let action: String
        var mediaID: String? = nil
        var sourceStartS: Double? = nil
        var sourceEndS: Double? = nil

        enum CodingKeys: String, CodingKey {
            case action
            case momentKey = "moment_key", mediaID = "media_id"
            case sourceStartS = "source_start_s", sourceEndS = "source_end_s"
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(momentKey, forKey: .momentKey)
            try c.encode(action, forKey: .action)
            try c.encodeIfPresent(mediaID, forKey: .mediaID)
            try c.encodeIfPresent(sourceStartS, forKey: .sourceStartS)
            try c.encodeIfPresent(sourceEndS, forKey: .sourceEndS)
        }
    }

    let questionID: String
    let answers: [Row]

    enum CodingKeys: String, CodingKey { case answers; case questionID = "question_id" }

    static let staleCode = "moment_order_stale"
    static let mediaNotFoundCode = "moment_order_media_not_found"
    static let invalidCode = "moment_order_invalid"
    static let overlapCode = "moment_order_overlap"

    /// The answer a stored user message carried, or nil.
    static func parse(payload: [String: JSONValue]?) -> MomentOrderAnswer? {
        guard let fields = payload?["moment_order"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        let rows: [Row] = (fields["answers"]?.arrayValue ?? []).compactMap { value in
            guard let row = value.objectValue, let key = row["moment_key"]?.stringValue,
                  let action = row["action"]?.stringValue else { return nil }
            return Row(momentKey: key, action: action, mediaID: row["media_id"]?.stringValue,
                       sourceStartS: row["source_start_s"]?.numberValue, sourceEndS: row["source_end_s"]?.numberValue)
        }
        return MomentOrderAnswer(questionID: questionID, answers: rows)
    }

    /// The chat message sent with the structured answer: the server's own neutral echo, in the chat's language,
    /// so the reply language never flips and no model reads times or digits as new asks.
    static func message(language: ClipAnswerLanguage) -> String {
        language == .turkish ? "Anlarımın her birinin nerede olduğunu gösterdim." : "I showed you where each of my moments is."
    }
}

/// How a moment question in the transcript is presented.
enum MomentOrderPhase: Equatable {
    /// The newest question nothing has answered: interactive.
    case active
    /// A later user message carried an answer to THIS question: read-only.
    case answered(MomentOrderAnswer)
    /// A newer moment question replaced it unanswered: hidden.
    case superseded
}

/// Folds the transcript into per-question phases, like `ProtectedRegionFold`. Only an answer naming the same
/// `question_id` closes a card, so a free-text reply leaves it open (the server asks again anyway).
enum MomentOrderFold {
    struct Entry: Equatable {
        let messageID: String
        let question: MomentOrderQuestion?
        let isUser: Bool
        var answer: MomentOrderAnswer? = nil
    }

    static func phases(_ entries: [Entry]) -> [String: MomentOrderPhase] {
        var result: [String: MomentOrderPhase] = [:]
        for (index, entry) in entries.enumerated() {
            guard let question = entry.question else { continue }
            let later = entries[(index + 1)...]
            if let answer = later.lazy.compactMap({ $0.isUser ? $0.answer : nil })
                .first(where: { $0.questionID == question.questionID }) {
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

/// A 404 / 422 the server answered with, shown on its card (the card stays open, the creator's choices kept).
struct MomentOrderProblem: Equatable {
    let questionID: String
    let message: String

    static func isAnswerProblem(_ error: APIError) -> Bool {
        guard case .requestFailed(let status, let detail) = error else { return false }
        return (status == 404 && detail.code == MomentOrderAnswer.mediaNotFoundCode)
            || (status == 422 && [MomentOrderAnswer.invalidCode, MomentOrderAnswer.overlapCode].contains(detail.code))
    }

    init(questionID: String, error: APIError, language: ClipAnswerLanguage) {
        self.questionID = questionID
        if case .requestFailed(_, let detail) = error, let message = detail.problemMessage ?? detail.message {
            self.message = message
        } else {
            self.message = MomentOrderStrings(language).refused
        }
    }
}

/// Every string on the card, in the chat's reply language (never the device language), like the clip picker.
struct MomentOrderStrings {
    let turkish: Bool
    init(_ language: ClipAnswerLanguage) { turkish = language == .turkish }

    private func say(_ en: String, _ tr: String) -> String { turkish ? tr : en }

    var title: String { say("Show me your moments", "Anlarını göster") }
    var hint: String {
        say("Put them in the order you want. For each one, show me where it is, tell me it isn’t in your clips, or let me pick.",
            "İstediğin sıraya koy. Her biri için yerini göster, kliplerinde olmadığını söyle ya da benim seçmeme izin ver.")
    }
    var point: String { say("Show where", "Yerini göster") }
    var skip: String { say("Not in my clips", "Kliplerimde yok") }
    var delegate: String { say("You pick", "Sen seç") }
    var send: String { say("Use these moments", "Bu anları kullan") }
    var chooseEach: String { say("Choose one option for each moment.", "Her an için bir seçenek seç.") }
    var overlap: String { say("Two of your moments overlap. Move one of them.", "İki anın üst üste biniyor. Birini kaydır.") }
    var refused: String { say("Those moments didn’t go through. Check them and try again.", "Bu anlar gönderilemedi. Kontrol edip tekrar dene.") }
    var notOnThisPhone: String {
        say("This clip isn’t on this iPhone, so you can’t point at it here. Tell me it isn’t in your clips or let me pick.",
            "Bu klip bu iPhone’da değil, burada yerini gösteremezsin. Kliplerinde olmadığını söyle ya da benim seçmeme izin ver.")
    }
    var stale: String {
        say("Kria checked your clip again, so that answer is out of date. Answer the newest moments card.",
            "Kria klibine yeniden baktı, bu cevap artık geçerli değil. En yeni anlar kartını cevapla.")
    }
    var sheetTitle: String { say("Where is it?", "Nerede?") }
    var done: String { say("Use this part", "Bu kısmı kullan") }
    var cancel: String { say("Cancel", "Vazgeç") }
    var dragHint: String {
        say("Drag the box to the moment. Drag an edge to make it longer or shorter.",
            "Kutuyu ana sürükle. Uzatmak ya da kısaltmak için bir kenarını sürükle.")
    }
    var couldNotLoad: String { say("Couldn’t load this clip.", "Bu klip yüklenemedi.") }
    func moveUp(_ words: String) -> String { say("Move \(words) up", "\(words) yukarı taşı") }
    func moveDown(_ words: String) -> String { say("Move \(words) down", "\(words) aşağı taşı") }

    func status(_ moment: MomentOrderQuestion.Moment, choice: MomentChoice?) -> String {
        switch choice {
        case .point(let window)?:
            if moment.status == .grounded || moment.status == .answered, let found = moment.found,
               abs(found.start - window.start) < 0.01, abs(found.end - window.end) < 0.01 {
                return say("Found it at \(window.label)", "\(window.label) arasında buldum")
            }
            return say("You picked \(window.label)", "Seçtiğin yer: \(window.label)")
        case .skip?: return say("Not in your clips: left out", "Kliplerinde yok: çıkarıldı")
        case .delegate?: return say("Kria picks a part for this one", "Bunun için bir kısmı Kria seçecek")
        case nil:
            return moment.status == .ambiguous
                ? say("Could be a few places. Show me which one.", "Birkaç yerde olabilir. Hangisi olduğunu göster.")
                : say("I couldn’t find this one in the clip.", "Bunu klipte bulamadım.")
        }
    }

    func answered(count: Int) -> String {
        say(count == 1 ? "Moment set" : "\(count) moments set, in your order",
            count == 1 ? "An ayarlandı" : "\(count) an senin sıranla ayarlandı")
    }
}
