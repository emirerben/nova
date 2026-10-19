import SwiftUI

// KRI-207: what Kria understood, what it did, and what it guessed.
//
// The server judges every requirement in the creator's brief (met / partial / couldn't) and lists
// the names it guessed for clips (a landmark, a place). This file is the native side of that:
// one static row per requirement under the reply that carries the receipts, and a "Guessed names" row
// whose tap starts a one-line correction in the composer.
//
// Decoded by hand (not the generated client) so an additive server field can never fail a whole
// chat refresh: anything unrecognised is skipped, never thrown.

enum RequirementOutcome: String, Equatable, Sendable {
    case met
    case partial
    case notPossible = "not_possible"

    /// Plain words for VoiceOver and for a chip whose requirement text hasn't loaded yet.
    var word: String {
        switch self {
        case .met: "Done"
        case .partial: "Partly done"
        case .notPossible: "Couldn’t do"
        }
    }

    /// Shown when the tapped chip has no reason of its own.
    var defaultReason: String {
        switch self {
        case .met: "Done as you asked."
        case .partial: "Only part of this was possible."
        case .notPossible: "This wasn’t possible with the footage you gave me."
        }
    }
}

/// One name the server guessed for one clip. `clipIndex` is zero-based (server order); the creator
/// sees "clip 4" for index 3.
struct InferredLabel: Equatable, Sendable, Identifiable {
    let text: String
    let mediaID: String?
    let clipIndex: Int?

    var id: String { "\(mediaID ?? "-")|\(clipIndex.map(String.init) ?? "-")|\(text)" }
    var clipNumber: Int? { clipIndex.map { $0 + 1 } }

    /// "I guessed Harbor Point for clip 4"; without a clip number, just the guess.
    var guessSentence: String {
        clipNumber.map { "I guessed \(text) for clip \($0)" } ?? "I guessed \(text)"
    }

    /// The opening of the correction, left for the creator to finish with the right name.
    var correctionPrompt: String {
        clipNumber.map { "Clip \($0) isn't \(text), it's " } ?? "That isn't \(text), it's "
    }
}

struct RequirementReceiptItem: Equatable, Sendable {
    let requirementID: String
    let outcome: RequirementOutcome
    let reason: String?
    let inferredLabels: [InferredLabel]
    /// KRI-529: the server could not check this requirement against the output. That is "not
    /// checked yet", not "partly done": it must never read as a half-finished result.
    let isUnchecked: Bool

    /// `nil` for anything that isn't a receipt this build understands (a future status, a missing id).
    init?(json: JSONValue) {
        guard case .object(let object) = json,
              let id = object["requirement_id"]?.stringValue, !id.isEmpty,
              let outcome = object["status"]?.stringValue.flatMap(RequirementOutcome.init(rawValue:)) else { return nil }
        requirementID = id
        self.outcome = outcome
        // A receipt that was refused as impossible stays a failure even if nothing was checked.
        isUnchecked = object["verification"]?.stringValue == "unchecked" && outcome != .notPossible
        reason = object["reason"]?.stringValue.flatMap { $0.isEmpty ? nil : $0 }
        let labels: [InferredLabel] = (object["inferred_labels"]?.arrayValue ?? []).compactMap { raw in
            guard case .object(let label) = raw, let text = label["text"]?.stringValue, !text.isEmpty else { return nil }
            return InferredLabel(
                text: text,
                mediaID: label["media_id"]?.stringValue,
                // A fractional, negative or out-of-range number is no clip: drop the number, keep the name.
                clipIndex: label["clip_index"]?.numberValue.flatMap { Int(exactly: $0) }.flatMap { $0 >= 0 ? $0 : nil }
            )
        }
        // A server without `inferred_labels` still sends the plain names: show them without a clip number.
        inferredLabels = labels.isEmpty
            ? (object["inferred"]?.arrayValue ?? []).compactMap { $0.stringValue }.filter { !$0.isEmpty }
                .map { InferredLabel(text: $0, mediaID: nil, clipIndex: nil) }
            : labels
    }

    static func parse(payload: [String: JSONValue]?) -> [Self] {
        (payload?["requirement_receipts"]?.arrayValue ?? []).compactMap(Self.init(json:))
    }
}

struct CreativeBriefRequirement: Equatable, Sendable, Identifiable {
    let id: String
    let kind: String?
    let scope: String?
    let literal: String?
    let description: String?

    init(id: String, kind: String? = nil, scope: String? = nil, literal: String? = nil, description: String? = nil) {
        self.id = id; self.kind = kind; self.scope = scope; self.literal = literal; self.description = description
    }

    /// `nil` without an id; every other field is optional so one odd requirement never costs the rest.
    init?(json: JSONValue) {
        guard case .object(let object) = json, let id = object["id"]?.stringValue, !id.isEmpty else { return nil }
        self.init(id: id, kind: object["kind"]?.stringValue, scope: object["scope"]?.stringValue,
                  literal: object["literal"]?.stringValue, description: object["description"]?.stringValue)
    }

    /// The creator-facing words for one chip: what they asked for, shortest form first. Nil when the
    /// brief carries no words for it (the chip then falls back to a neutral title).
    var chipTitle: String? {
        [description, literal, scope].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty }
    }
}

/// `GET creation-threads/{id}/brief`. Receipts arrive on chat events too, so only the requirements
/// are needed to name the chips; the receipts are decoded for completeness and for a thread whose
/// events were trimmed.
struct CreativeBrief: Decodable, Equatable, Sendable {
    let threadID: String
    let version: Int
    let requirements: [CreativeBriefRequirement]
    let receipts: [RequirementReceiptItem]

    private enum CodingKeys: String, CodingKey {
        case threadID = "thread_id", version, requirements, receipts = "requirement_receipts"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        threadID = (try? container.decodeIfPresent(String.self, forKey: .threadID)) ?? ""
        version = (try? container.decodeIfPresent(Int.self, forKey: .version)) ?? 0
        // Element by element: a requirement or receipt this build can't read is skipped, never fatal.
        requirements = ((try? container.decodeIfPresent([JSONValue].self, forKey: .requirements)) ?? [])
            .compactMap(CreativeBriefRequirement.init(json:))
        receipts = ((try? container.decodeIfPresent([JSONValue].self, forKey: .receipts)) ?? [])
            .compactMap(RequirementReceiptItem.init(json:))
    }

    init(threadID: String = "", version: Int = 0, requirements: [CreativeBriefRequirement] = [], receipts: [RequirementReceiptItem] = []) {
        self.threadID = threadID; self.version = version; self.requirements = requirements; self.receipts = receipts
    }

    var requirementsByID: [String: CreativeBriefRequirement] {
        Dictionary(requirements.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
    }
}

/// The optional "say it once" keys an assistant event can carry next to its receipts or question.
/// Hand-decoded and tolerant like the receipts: a missing or oddly typed key is nil / empty, never a failure.
struct ReplyExtras: Equatable, Sendable {
    /// Short lead that replaces the long prose. Only honoured together with receipts.
    let lead: String?
    /// Extra deliberate changes no request claimed.
    let alsoChanged: [String]
    /// Chip labels for an `assistant_response` question (2-4 short strings).
    let options: [String]

    static func parse(payload: [String: JSONValue]?, eventType: String, hasReceipts: Bool) -> Self {
        func strings(_ key: String) -> [String] {
            (payload?[key]?.arrayValue ?? []).compactMap { $0.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
        let lead = hasReceipts
            ? payload?["reply_lead"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty : nil
        // Legacy `assistant_question` events keep their own exact-string `options` path (QuestionOptionsRow).
        let isReplyQuestion = eventType == "assistant_response" && payload?["turn_value"]?.stringValue == "question"
        return Self(lead: lead, alsoChanged: hasReceipts ? strings("also_changed") : [], options: isReplyQuestion ? strings("options") : [])
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}

/// Under an assistant reply that carries receipts: one static row per requirement (outcome icon + the
/// reason, always visible), an optional muted "Also:" line, then the guessed names.
/// Type is Inter (`KriaFont.body`) throughout — never the display serif on new UI.
struct RequirementChipsView: View {
    let receipts: [RequirementReceiptItem]
    let requirements: [String: CreativeBriefRequirement]
    /// False until the brief has loaded (or its fetch has definitively failed). Titles wait for it so
    /// they never flash a placeholder and then relabel themselves. A single receipt shows no title at
    /// all (the creator's own message is right above), so it never has to wait.
    let titlesReady: Bool
    /// Extra deliberate changes no request claimed, shown as one muted last line.
    var alsoChanged: [String] = []
    /// True when the message shows a short `reply_lead` instead of the long prose, so the rows carry
    /// each reason. False (old server, or a review turn whose prose still lists them): the rows are just
    /// outcome + request title, never a second copy of the reasons the prose already gives.
    var showsReasons: Bool = true
    /// Nil where correcting isn't possible (no composer): the guessed-names row is then omitted.
    let correct: ((InferredLabel) -> Void)?

    /// One row per requirement id, first receipt wins (a duplicate id would collide in the list).
    private var uniqueReceipts: [RequirementReceiptItem] {
        var seen = Set<String>()
        return receipts.filter { seen.insert($0.requirementID).inserted }
    }

    private var guessedNames: [InferredLabel] {
        var seen = Set<String>()
        return receipts.flatMap(\.inferredLabels)
            .filter { seen.insert($0.id).inserted }
            .sorted { ($0.clipIndex ?? .max) < ($1.clipIndex ?? .max) }
    }

    var body: some View {
        let rows = uniqueReceipts
        // A lone receipt under a lead needs no title, so it never waits for the brief.
        let showsTitles = !showsReasons || rows.count > 1
        if titlesReady || !showsTitles {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(rows.enumerated()), id: \.element.requirementID) { offset, receipt in
                    RequirementReceiptRow(
                        receipt: receipt,
                        title: showsTitles ? (requirements[receipt.requirementID]?.chipTitle ?? "Request \(offset + 1)") : nil,
                        showsReason: showsReasons
                    )
                }
                if !alsoChanged.isEmpty {
                    Text("Also: " + alsoChanged.joined(separator: "; "))
                        .font(KriaFont.body(12))
                        .foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("requirement-also-changed")
                }
                if let correct, !guessedNames.isEmpty {
                    GuessedNamesRow(labels: guessedNames, correct: correct)
                }
            }
            .padding(.top, 2)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("requirement-chips")
        }
    }
}

/// One fact, once: an outcome icon and the reason, always visible (no tap, no disclosure). With 2+ asks the
/// short request title leads in medium weight and the reason sits beneath it.
private struct RequirementReceiptRow: View {
    let receipt: RequirementReceiptItem
    /// Nil for a lone receipt: the creator's own message is right above it.
    let title: String?
    /// False when the prose above already states the reason (no `reply_lead`).
    var showsReason: Bool = true

    private var treatment: (icon: String, tint: Color, meaning: String) {
        // KRI-529: an unchecked requirement is neutral, never the yellow "partly done".
        if receipt.isUnchecked { return ("checkmark", KriaColor.zinc, "Have a look") }
        return switch receipt.outcome {
        case .met: ("checkmark", KriaColor.success, receipt.outcome.word)
        case .partial: ("circle.lefthalf.filled", KriaColor.ink, receipt.outcome.word)
        case .notPossible: ("xmark", KriaColor.failureText, receipt.outcome.word)
        }
    }

    private var reason: String {
        receipt.reason ?? (receipt.isUnchecked ? "Have a look at this one in the video." : receipt.outcome.defaultReason)
    }

    private var spokenLabel: String {
        switch (title, showsReason) {
        case let (title?, true): "\(treatment.meaning): \(title). \(reason)"
        case let (title?, false): "\(treatment.meaning): \(title)"
        case (nil, _): "\(treatment.meaning): \(reason)"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: treatment.icon)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(treatment.tint)
                .frame(width: 16, height: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                if let title {
                    Text(title)
                        .font(KriaFont.body(13).weight(.medium))
                        .foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if showsReason {
                    Text(reason)
                        .font(KriaFont.body(13))
                        .foregroundStyle(title == nil ? KriaColor.ink : KriaColor.mutedInk)
                        // Never truncated: the "Couldn't" / "Partly" explanation is what the creator needs.
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spokenLabel)
        .accessibilityIdentifier("requirement-chip-\(receipt.requirementID)")
    }
}

private struct GuessedNamesRow: View {
    let labels: [InferredLabel]
    let correct: (InferredLabel) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Guessed names")
                .font(KriaFont.body(12).weight(.semibold))
                .foregroundStyle(KriaColor.zinc)
            ForEach(Array(labels.enumerated()), id: \.offset) { offset, label in
                Button { correct(label) } label: {
                    HStack(spacing: 6) {
                        Text(label.guessSentence)
                            .font(KriaFont.body(13))
                            .foregroundStyle(KriaColor.ink)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Image(systemName: "pencil").font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(KriaColor.zinc)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityHint("Starts a correction in the message box")
                .accessibilityIdentifier("guessed-name-\(offset)")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("guessed-names")
    }
}

extension ChatSubmission {
    /// True while the message is only the opening of a guessed-name correction ("Clip 4 isn't X, it's ")
    /// with nothing after it: the creator is mid-sentence, so that is not yet something to send.
    static func isBareCorrectionStub(_ text: String) -> Bool {
        let last = text.split(whereSeparator: \.isNewline).last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
        return last.range(of: #"^(Clip [0-9]+|That) isn't .+, it's$"#, options: .regularExpression) != nil
    }
}

extension InferredLabel {
    /// Adds the correction opening to what the creator already typed instead of replacing it.
    func correctionDraft(appendingTo draft: String) -> String {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? correctionPrompt : draft.trimmingCharacters(in: .newlines) + "\n" + correctionPrompt
    }
}
