import Foundation
import KriaMediaEngine

// KRI-901: "No text on her face or on the plate". The server keeps text off an object only where the creator
// marked it, so before approval it asks in the chat "Mark the plate on one frame" and the creator answers with a
// card: pick a clip, pick a moment, draw a box around the plate. The answer travels back as `protected_region` on
// the next turn; the server checks the clip is the creator's own, stamps its generation and pins the mark at
// approval.
//
// Same shape as the song-range question (`SongRangeQuestion.swift`): hand-rolled tolerant parsing, because an
// absent, newer or malformed payload must mean "no card" (the text question still shows), never a failed decode.

/// The optional `protected_region_question` on an assistant event payload.
struct ProtectedRegionQuestion: Equatable, Sendable {
    let questionID: String
    /// The object in the creator's own words ("plate"), as the server will match the mark to it.
    let label: String
    /// The clips the creator may mark it on, in the edit's order.
    let mediaIDs: [String]

    static func parse(payload: [String: JSONValue]?) -> ProtectedRegionQuestion? {
        guard let fields = payload?["protected_region_question"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty,
              let label = fields["label"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty,
              case .array(let rows)? = fields["media_ids"] else { return nil }
        let mediaIDs = rows.compactMap { $0.stringValue }.filter { !$0.isEmpty }
        guard !mediaIDs.isEmpty else { return nil }
        return ProtectedRegionQuestion(questionID: questionID, label: label, mediaIDs: mediaIDs)
    }

    /// "Mark the plate".
    var actionTitle: String { "Mark the \(label)" }
}

/// `protected_region` on the submit-turn body, exactly as the server's `ProtectedRegionIn` names it.
struct ProtectedRegionAnswer: Encodable, Equatable, Sendable {
    let label: String
    let mediaID: String
    let sourceTimeS: Double
    /// `[left, top, right, bottom]`, normalized to the UPRIGHT source frame.
    let box: [Double]

    /// The server refuses a mark whose box is under 2% of the frame on a side (`protected_region_invalid`).
    static let minimumSide = 0.02
    static let mediaNotFoundCode = "protected_region_media_not_found"
    static let invalidCode = "protected_region_invalid"

    enum CodingKeys: String, CodingKey { case label, box; case mediaID = "media_id"; case sourceTimeS = "source_t_s" }

    /// The one conversion from the editor's rectangle (x, y, width, height, top-left origin, upright frame) to the
    /// server's corners. Clamped to the frame with every side at least `minimumSide`, so a box the card allowed is
    /// never refused as too small.
    static func box(from rect: NormalizedSourceRect) -> [Double] {
        func finite(_ value: Double, _ fallback: Double) -> Double { value.isFinite ? value : fallback }
        let width = min(1, max(minimumSide, finite(rect.width, 1)))
        let height = min(1, max(minimumSide, finite(rect.height, 1)))
        let left = min(max(0, finite(rect.x, 0)), 1 - width)
        let top = min(max(0, finite(rect.y, 0)), 1 - height)
        return [left, top, left + width, top + height].map { ($0 * 10_000).rounded() / 10_000 }
    }

    init(label: String, mediaID: String, sourceTimeS: Double, rect: NormalizedSourceRect) {
        self.label = label
        self.mediaID = mediaID
        self.sourceTimeS = min(3600, max(0, sourceTimeS.isFinite ? sourceTimeS : 0))
        self.box = Self.box(from: rect)
    }

    private init(label: String, mediaID: String, sourceTimeS: Double, box: [Double]) {
        self.label = label; self.mediaID = mediaID; self.sourceTimeS = sourceTimeS; self.box = box
    }

    /// The mark a user message carried (the server stores the stamped mark on the event), or nil.
    static func parse(payload: [String: JSONValue]?) -> ProtectedRegionAnswer? {
        guard let fields = payload?["protected_region"]?.objectValue,
              let label = fields["label"]?.stringValue, !label.isEmpty,
              let mediaID = fields["media_id"]?.stringValue, !mediaID.isEmpty else { return nil }
        var box: [Double] = []
        if case .array(let values)? = fields["box"] { box = values.compactMap { $0.numberValue } }
        return ProtectedRegionAnswer(label: label, mediaID: mediaID, sourceTimeS: fields["source_t_s"]?.numberValue ?? 0, box: box)
    }

    /// The readable chat message sent alongside the structured payload. The server never reads it as an
    /// instruction (the planner sees a neutral stand-in).
    var message: String { "Marked the \(label)" }

    /// Labels match the way the server folds them: case and spacing do not matter.
    static func sameLabel(_ a: String, _ b: String) -> Bool {
        let fold: (String) -> String = { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
        return fold(a) == fold(b)
    }
}

/// How a mark question in the transcript is presented.
enum ProtectedRegionPhase: Equatable {
    /// The newest question no mark has answered: interactive.
    case active
    /// A later user message carried a mark of this object: read-only.
    case answered(ProtectedRegionAnswer)
    /// A newer question replaced it unanswered: hidden.
    case superseded
}

/// Folds the transcript into per-question phases, like `SongRangeFold`. Only a user message carrying a mark of the
/// SAME object closes a card, so a plain reply about something else leaves it open.
enum ProtectedRegionFold {
    struct Entry: Equatable {
        let messageID: String
        let question: ProtectedRegionQuestion?
        let isUser: Bool
        var answer: ProtectedRegionAnswer? = nil
    }

    static func phases(_ entries: [Entry]) -> [String: ProtectedRegionPhase] {
        var result: [String: ProtectedRegionPhase] = [:]
        for (index, entry) in entries.enumerated() {
            guard let question = entry.question else { continue }
            let later = entries[(index + 1)...]
            if let answer = later.lazy.compactMap({ $0.isUser ? $0.answer : nil })
                .first(where: { ProtectedRegionAnswer.sameLabel($0.label, question.label) }) {
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

/// A 404 / 422 the server answered a mark with, shown inline on its card (the card stays open, the box kept).
struct ProtectedRegionProblem: Equatable {
    let questionID: String
    let message: String

    static func isMarkProblem(_ error: APIError) -> Bool {
        guard case .requestFailed(let status, let detail) = error else { return false }
        return (status == 404 && detail.code == ProtectedRegionAnswer.mediaNotFoundCode)
            || (status == 422 && detail.code == ProtectedRegionAnswer.invalidCode)
    }

    init(questionID: String, error: APIError) {
        self.questionID = questionID
        if case .requestFailed(_, let detail) = error, let message = detail.problemMessage ?? detail.message {
            self.message = message
        } else {
            self.message = "That mark didn't go through. Draw a box around the object and try again."
        }
    }
}
