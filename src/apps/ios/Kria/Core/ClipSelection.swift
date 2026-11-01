import Foundation

// KRI-282: "which clips belong to this category?" asked as a picker instead of free text.
//
// Hand-rolled Codable on purpose: the generated OpenAPI client gains these fields only after the
// backend PR merges. Everything wire-shaped lives in this file (`ClipQuestion` in, `ClipSelectionSubmission`
// out, plus the capability flag in `CreationCapabilities`), so swapping to generated types later means
// changing only the `parse(payload:)` / `Encodable` conformances here.

/// The optional `clip_question` on an assistant question event payload.
struct ClipQuestion: Equatable, Sendable {
    struct Category: Equatable, Sendable, Identifiable {
        let key: String
        let label: String
        let op: String
        let candidateMediaIDs: [String]
        let suggestedMediaIDs: [String]
        var id: String { key }
    }

    /// KRI-693: how the server describes one candidate that is not a plain video (`clip_question.candidate_media`).
    /// A candidate id says nothing about its kind, so a Visuals photo (`asset-…`) used to read as "Clip 9".
    struct CandidateMedia: Equatable, Sendable {
        let kind: String
        /// The server's name for it in the chat's language ("Photo 1" / "Fotoğraf 1"); nil when blank.
        let label: String?
    }

    let version: Int
    let questionID: String
    let categories: [Category]
    let allowNone: Bool
    /// Empty on an older server and whenever the question offers no photo.
    var candidateMedia: [String: CandidateMedia] = [:]

    func isPhoto(_ mediaID: String) -> Bool { candidateMedia[mediaID]?.kind == "image" }

    /// Highest `clip_question.version` this build understands; newer payloads degrade to the text question.
    static let supportedVersion = 1

    /// The server sends the creator's own wording ("dodgeball"); capitalise the first letter for display.
    /// Wording with Turkish letters keeps Turkish capitals ("ilk gün" -> "İlk gün", not "Ilk gün").
    /// Decided by the label's own letters, never the device language: "ice cream" stays "Ice cream".
    static func displayLabel(_ raw: String) -> String {
        guard let first = raw.first else { return raw }
        let turkishLetters = CharacterSet(charactersIn: "çğıöşüÇĞİÖŞÜ")
        let turkish = raw.unicodeScalars.contains { turkishLetters.contains($0) }
        let capital = turkish ? String(first).uppercased(with: Locale(identifier: "tr")) : String(first).uppercased()
        return capital + raw.dropFirst()
    }

    /// nil when the payload has no usable question (absent, newer version, no categories, no candidates).
    static func parse(payload: [String: JSONValue]?) -> ClipQuestion? {
        guard let fields = payload?["clip_question"]?.objectValue else { return nil }
        let version = fields["version"]?.numberValue.map(Int.init) ?? 1
        guard version == supportedVersion,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        var seen = Set<String>()
        let categories: [Category] = (fields["categories"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let key = object["key"]?.stringValue, !key.isEmpty, seen.insert(key).inserted else { return nil }
            func ids(_ name: String) -> [String] {
                var unique = Set<String>()
                return (object[name]?.arrayValue ?? []).compactMap(\.stringValue).filter { unique.insert($0).inserted }
            }
            let candidates = ids("candidate_media_ids")
            guard !candidates.isEmpty else { return nil }
            let suggested = ids("suggested_media_ids").filter(candidates.contains)
            let rawLabel = object["label"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
            let label = displayLabel(rawLabel?.isEmpty == false ? rawLabel! : key)
            return Category(key: key, label: label, op: object["op"]?.stringValue ?? "group",
                            candidateMediaIDs: candidates, suggestedMediaIDs: suggested)
        }
        guard !categories.isEmpty else { return nil }
        return ClipQuestion(version: version, questionID: questionID, categories: categories,
                            allowNone: fields["allow_none"]?.boolValue ?? true,
                            candidateMedia: parseCandidateMedia(fields["candidate_media"]))
    }

    /// Only photos are named; anything else (a video, junk) keeps its plain "Clip N" row.
    private static func parseCandidateMedia(_ value: JSONValue?) -> [String: CandidateMedia] {
        var result: [String: CandidateMedia] = [:]
        for entry in value?.arrayValue ?? [] {
            guard let object = entry.objectValue, let id = object["media_id"]?.stringValue, !id.isEmpty,
                  object["kind"]?.stringValue == "image" else { continue }
            let label = object["label"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
            result[id] = CandidateMedia(kind: "image", label: label?.isEmpty == false ? label : nil)
        }
        return result
    }
}

/// How one picker tile reads (KRI-693): what it is called and whether it is a photo. The server names photos
/// in the chat's language; every other tile is "Clip N", unchanged.
struct ClipTileInfo: Equatable {
    let name: String
    let isPhoto: Bool
    /// The placeholder glyph while no thumbnail is available.
    var symbol: String { isPhoto ? "photo" : "film" }

    init(mediaID: String, position: Int, question: ClipQuestion, media: CreationAttachedMedia?) {
        isPhoto = question.isPhoto(mediaID) || media?.kind == "image"
        name = question.candidateMedia[mediaID].flatMap { $0.kind == "image" ? $0.label : nil } ?? "Clip \(position)"
    }
}

/// Thumbnails for the Visuals-pool candidates of a clip question. Pool photos and videos are not chat media, so
/// the chat state never lists them; the iPhone looks them up in the plan item's Visuals instead.
enum ClipPoolMedia {
    /// The question's pool candidates (`asset-<uuid>`), in candidate order.
    static func poolIDs(in question: ClipQuestion) -> [String] {
        var seen = Set<String>()
        return question.categories.flatMap(\.candidateMediaIDs).filter { $0.hasPrefix("asset-") && seen.insert($0).inserted }
    }

    /// One entry per pool candidate that the Visuals read lists, in candidate order, so appending them to the
    /// chat media numbers them exactly as the picker already did. A candidate the read lacks is left out.
    static func media(for question: ClipQuestion, pool: [CreationVisual]) -> [CreationAttachedMedia] {
        let byID = Dictionary(pool.map { ($0.id.lowercased(), $0) }, uniquingKeysWith: { first, _ in first })
        return poolIDs(in: question).compactMap { candidateID in
            guard let visual = byID[String(candidateID.dropFirst("asset-".count)).lowercased()] else { return nil }
            // A video's `display_url` is the raw file, never a poster; a photo's is its signed preview.
            let thumbnail = visual.kind == "video" ? visual.previewURL : (visual.displayURL ?? visual.sourceURL)
            return CreationAttachedMedia(id: candidateID, filename: visual.sourceFilename ?? "Visual", kind: visual.kind, previewURL: thumbnail)
        }
    }
}

/// `clip_selection` on the submit-turn body, exactly as the server contract names it.
struct ClipSelectionSubmission: Encodable, Equatable, Sendable {
    struct Answer: Encodable, Equatable, Sendable {
        let key: String
        let mediaIDs: [String]
        enum CodingKeys: String, CodingKey { case key; case mediaIDs = "media_ids" }
    }

    let questionID: String
    let answers: [Answer]
    let noneKeys: [String]
    let skipped: Bool
    enum CodingKeys: String, CodingKey {
        case answers, skipped
        case questionID = "question_id"
        case noneKeys = "none_keys"
    }

    static func skip(_ question: ClipQuestion) -> Self {
        Self(questionID: question.questionID, answers: [], noneKeys: [], skipped: true)
    }

    /// Short read-only summary once answered, e.g. "Dodgeball: 2 clips · Football: 1 clip" or "Skipped".
    func summary(question: ClipQuestion) -> String {
        if skipped { return "Skipped, Kria decides" }
        let labels = Dictionary(question.categories.map { ($0.key, $0.label) }, uniquingKeysWith: { first, _ in first })
        var parts = answers.map { "\(labels[$0.key] ?? $0.key): \($0.mediaIDs.count) clip\($0.mediaIDs.count == 1 ? "" : "s")" }
        parts += noneKeys.map { "\(labels[$0] ?? $0): none" }
        return parts.joined(separator: " · ")
    }

    /// The readable message sent alongside the structured payload ("Dodgeball: clips 3, 7. Football: clips 1, 2").
    /// `positions` maps media id -> the 1-based clip number the picker showed. It is the creator's visible turn in the
    /// chat, so it follows the chat's reply language (KRI-694); the structured answer, not this text, is what binds.
    func message(question: ClipQuestion, positions: [String: Int], language: ClipAnswerLanguage = .english) -> String {
        let skip = language == .turkish ? "Atla, benim yerime sen karar ver" : "Skip, decide for me"
        if skipped { return skip }
        let labels = Dictionary(question.categories.map { ($0.key, $0.label) }, uniquingKeysWith: { first, _ in first })
        var pieces: [String] = []
        for category in question.categories {
            let label = labels[category.key] ?? category.key
            if let answer = answers.first(where: { $0.key == category.key }), !answer.mediaIDs.isEmpty {
                let numbers = answer.mediaIDs.compactMap { positions[$0] }.sorted()
                let list = numbers.map(String.init).joined(separator: ", ")
                switch language {
                case .turkish: pieces.append("\(label): klip \(list)")
                case .english: pieces.append("\(label): clip\(numbers.count == 1 ? "" : "s") \(list)")
                }
            } else if noneKeys.contains(category.key) {
                pieces.append(language == .turkish ? "\(label) için hiçbiri" : "None of these for \(label)")
            }
        }
        return pieces.isEmpty ? skip : pieces.joined(separator: ". ")
    }
}

/// The language the picker's posted answer is written in: the chat's reply language, which the server keeps on
/// `thread.state["reply_language"]` (KRI-520). Never the device language -- a Turkish chat on an English phone
/// still reads Turkish. Only Turkish has its own wording; a missing or unknown language stays English, exactly
/// like the server's `say(en:, tr:)`.
enum ClipAnswerLanguage: Equatable, Sendable {
    case english
    case turkish

    init(threadState: [String: JSONValue]) {
        self = threadState["reply_language"]?.stringValue == "tr" ? .turkish : .english
    }
}

/// Whether to offer the picker for a clip question the server sent. The payload itself only exists when the
/// server's `clip_selection_questions` flag is on, so a capability snapshot that is simply not loaded (nil: a
/// failed or cancelled read, never retried) must NOT hide it -- only an explicit "this server does not
/// advertise it" (an older server that still sends the payload) falls back to the plain text question.
enum ClipSelectionAvailability {
    static func isAvailable(capabilities: CreationCapabilities?) -> Bool {
        guard let capabilities else { return true }
        return capabilities.clipSelectionQuestionsEnabled
    }
}

/// Mutable picker state for one question. Pure value type so the interactions are unit-testable.
struct ClipSelectionState: Equatable {
    let question: ClipQuestion
    private(set) var selected: [String: Set<String>]
    private(set) var none: Set<String> = []
    /// Categories the picker has put on screen. Kept here, not in the sheet, so closing and reopening it remembers.
    private(set) var shown: Set<String> = []

    /// `suggested_media_ids` start ticked.
    init(question: ClipQuestion) {
        self.question = question
        selected = Dictionary(uniqueKeysWithValues: question.categories.map { ($0.key, Set($0.suggestedMediaIDs)) })
    }

    func isSelected(_ mediaID: String, in key: String) -> Bool { selected[key]?.contains(mediaID) == true }
    func count(in key: String) -> Int { selected[key]?.count ?? 0 }

    /// Ticks every candidate of one category (clears that category's "None of these").
    mutating func selectAll(in key: String) {
        guard let category = question.categories.first(where: { $0.key == key }) else { return }
        selected[key] = Set(category.candidateMediaIDs)
        none.remove(key)
    }

    /// Unticks every candidate of one category.
    mutating func clear(in key: String) {
        guard question.categories.contains(where: { $0.key == key }) else { return }
        selected[key] = []
    }
    func isNone(_ key: String) -> Bool { none.contains(key) }

    /// Tap a thumbnail; tapping a ticked one unticks it. Ticking clears that category's "None of these".
    mutating func toggle(_ mediaID: String, in key: String) {
        guard let category = question.categories.first(where: { $0.key == key }),
              category.candidateMediaIDs.contains(mediaID) else { return }
        if selected[key]?.contains(mediaID) == true {
            selected[key]?.remove(mediaID)
        } else {
            selected[key, default: []].insert(mediaID)
            none.remove(key)
        }
    }

    /// Slide-to-select: ticks or unticks exactly `mediaIDs` (candidates only). Ticking clears "None of these".
    mutating func set(_ mediaIDs: [String], selected isOn: Bool, in key: String) {
        guard let category = question.categories.first(where: { $0.key == key }) else { return }
        let valid = mediaIDs.filter(category.candidateMediaIDs.contains)
        guard !valid.isEmpty else { return }
        if isOn {
            selected[key, default: []].formUnion(valid)
            none.remove(key)
        } else {
            selected[key]?.subtract(valid)
        }
    }

    /// "None of these" is exclusive with ticks in the same category.
    mutating func toggleNone(_ key: String) {
        guard question.allowNone, question.categories.contains(where: { $0.key == key }) else { return }
        if none.contains(key) { none.remove(key) } else {
            none.insert(key)
            selected[key] = []
        }
    }

    /// Send needs at least one decision (a tick or a "None of these").
    var canSend: Bool { !submission.answers.isEmpty || !submission.noneKeys.isEmpty }

    /// A category is answered once it has a tick or "None of these".
    func isAnswered(_ key: String) -> Bool { count(in: key) > 0 || isNone(key) }

    mutating func markShown(_ key: String) {
        guard question.categories.contains(where: { $0.key == key }) else { return }
        shown.insert(key)
    }

    /// Done needs both: the creator has seen the category and it has a tick or "None of these". A suggestion is only
    /// the starting selection, so a pre-ticked category the creator never opened is not done (KRI-1101).
    func isDone(_ key: String) -> Bool { shown.contains(key) && isAnswered(key) }

    /// The first category after `key` (the one on screen) that is not done, wrapping round to the start; nil once
    /// every other category is done. The picker's main button moves here ("Next") instead of sending, so a question
    /// with two groups never goes out with a group the creator has not seen or answered.
    func nextPending(after key: String) -> ClipQuestion.Category? {
        let categories = question.categories
        guard !categories.isEmpty else { return nil }
        let start = categories.firstIndex { $0.key == key } ?? categories.count - 1
        for step in 1...categories.count {
            let category = categories[(start + step) % categories.count]
            if category.key != key, !isDone(category.key) { return category }
        }
        return nil
    }

    var selectedCount: Int { selected.values.reduce(0) { $0 + $1.count } }

    /// Candidate order is preserved so the message and payload are deterministic.
    var submission: ClipSelectionSubmission {
        let answers = question.categories.compactMap { category -> ClipSelectionSubmission.Answer? in
            let ids = category.candidateMediaIDs.filter { selected[category.key]?.contains($0) == true }
            return ids.isEmpty ? nil : .init(key: category.key, mediaIDs: ids)
        }
        let noneKeys = question.categories.map(\.key).filter(none.contains)
        return ClipSelectionSubmission(questionID: question.questionID, answers: answers, noneKeys: noneKeys, skipped: false)
    }
}

/// Clip numbers shown on the picker: position among the thread's non-audio media, in attach order.
enum ClipPositions {
    static func map(media: [CreationAttachedMedia], question: ClipQuestion) -> [String: Int] {
        var result: [String: Int] = [:]
        for (index, clip) in media.filter({ $0.kind != "audio" }).enumerated() { result[clip.id] = index + 1 }
        // A candidate the thread state doesn't list (stale state) still gets a stable number.
        var fallback = result.count
        for id in question.categories.flatMap(\.candidateMediaIDs) where result[id] == nil {
            fallback += 1
            result[id] = fallback
        }
        return result
    }
}
