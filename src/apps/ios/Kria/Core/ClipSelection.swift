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

    let version: Int
    let questionID: String
    let categories: [Category]
    let allowNone: Bool

    /// Highest `clip_question.version` this build understands; newer payloads degrade to the text question.
    static let supportedVersion = 1

    /// The server sends the creator's own wording ("dodgeball"); capitalise the first letter for display.
    static func displayLabel(_ raw: String) -> String {
        guard let first = raw.first else { return raw }
        return first.uppercased() + raw.dropFirst()
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
                            allowNone: fields["allow_none"]?.boolValue ?? true)
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
    /// `positions` maps media id -> the 1-based clip number the picker showed.
    func message(question: ClipQuestion, positions: [String: Int]) -> String {
        if skipped { return "Skip, decide for me" }
        let labels = Dictionary(question.categories.map { ($0.key, $0.label) }, uniquingKeysWith: { first, _ in first })
        var pieces: [String] = []
        for category in question.categories {
            if let answer = answers.first(where: { $0.key == category.key }), !answer.mediaIDs.isEmpty {
                let numbers = answer.mediaIDs.compactMap { positions[$0] }.sorted()
                let list = numbers.map(String.init).joined(separator: ", ")
                pieces.append("\(labels[category.key] ?? category.key): clip\(numbers.count == 1 ? "" : "s") \(list)")
            } else if noneKeys.contains(category.key) {
                pieces.append("None of these for \(labels[category.key] ?? category.key)")
            }
        }
        return pieces.isEmpty ? "Skip, decide for me" : pieces.joined(separator: ". ")
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
