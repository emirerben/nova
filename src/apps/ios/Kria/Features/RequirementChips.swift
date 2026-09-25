import SwiftUI

// KRI-207: what Kria understood, what it did, and what it guessed.
//
// The server judges every requirement in the creator's brief (met / partial / couldn't) and lists
// the names it guessed for clips (a landmark, a place). This file is the native side of that:
// one chip per requirement under the reply that carries the receipts, and a "Guessed names" row
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

    /// `nil` for anything that isn't a receipt this build understands (a future status, a missing id).
    init?(json: JSONValue) {
        guard case .object(let object) = json,
              let id = object["requirement_id"]?.stringValue, !id.isEmpty,
              let outcome = object["status"]?.stringValue.flatMap(RequirementOutcome.init(rawValue:)) else { return nil }
        requirementID = id
        self.outcome = outcome
        reason = object["reason"]?.stringValue.flatMap { $0.isEmpty ? nil : $0 }
        let labels: [InferredLabel] = (object["inferred_labels"]?.arrayValue ?? []).compactMap { raw in
            guard case .object(let label) = raw, let text = label["text"]?.stringValue, !text.isEmpty else { return nil }
            return InferredLabel(
                text: text,
                mediaID: label["media_id"]?.stringValue,
                clipIndex: label["clip_index"]?.numberValue.map { Int($0) }
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

struct CreativeBriefRequirement: Decodable, Equatable, Sendable, Identifiable {
    let id: String
    let kind: String
    let scope: String
    let literal: String?
    let description: String?

    /// The creator-facing words for one chip: what they asked for, shortest form first.
    var chipTitle: String {
        [description, literal, scope].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty } ?? scope
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
        threadID = try container.decode(String.self, forKey: .threadID)
        version = try container.decodeIfPresent(Int.self, forKey: .version) ?? 0
        requirements = try container.decodeIfPresent([CreativeBriefRequirement].self, forKey: .requirements) ?? []
        receipts = (try container.decodeIfPresent([JSONValue].self, forKey: .receipts) ?? [])
            .compactMap(RequirementReceiptItem.init(json:))
    }

    var requirementsByID: [String: CreativeBriefRequirement] {
        Dictionary(requirements.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
    }
}

/// Under an assistant reply that carries receipts: one chip per requirement, then the guessed names.
/// Type is Inter (`KriaFont.body`) throughout — never the display serif on new UI.
struct RequirementChipsView: View {
    let receipts: [RequirementReceiptItem]
    let requirements: [String: CreativeBriefRequirement]
    /// Nil where correcting isn't possible (no composer): the guessed-names row is then omitted.
    let correct: ((InferredLabel) -> Void)?
    @State private var expandedID: String?

    private var guessedNames: [InferredLabel] {
        var seen = Set<String>()
        return receipts.flatMap(\.inferredLabels)
            .filter { seen.insert($0.id).inserted }
            .sorted { ($0.clipIndex ?? .max) < ($1.clipIndex ?? .max) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(receipts, id: \.requirementID) { receipt in
                RequirementChip(
                    receipt: receipt,
                    title: requirements[receipt.requirementID]?.chipTitle,
                    isExpanded: expandedID == receipt.requirementID,
                    toggle: { expandedID = expandedID == receipt.requirementID ? nil : receipt.requirementID }
                )
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

private struct RequirementChip: View {
    let receipt: RequirementReceiptItem
    let title: String?
    let isExpanded: Bool
    let toggle: () -> Void

    private var treatment: (icon: String, foreground: Color, background: Color) {
        switch receipt.outcome {
        case .met: ("checkmark", KriaColor.success, KriaColor.successSoft)
        case .partial: ("circle.lefthalf.filled", KriaColor.ink, KriaColor.butter)
        case .notPossible: ("xmark", KriaColor.failureText, KriaColor.failureSoft)
        }
    }

    private var label: String { title ?? receipt.outcome.word }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button(action: toggle) {
                HStack(spacing: 6) {
                    Image(systemName: treatment.icon).font(.system(size: 11, weight: .bold))
                    Text(label)
                        .font(KriaFont.body(12).weight(.semibold))
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(treatment.foreground)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(treatment.background)
                .clipShape(Capsule())
                .frame(minHeight: 44)
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(receipt.outcome.word): \(label)")
            .accessibilityHint(isExpanded ? "Hides the reason" : "Shows the reason")
            .accessibilityIdentifier("requirement-chip-\(receipt.requirementID)")
            if isExpanded {
                Text(receipt.reason ?? receipt.outcome.defaultReason)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 4)
                    .accessibilityIdentifier("requirement-reason-\(receipt.requirementID)")
            }
        }
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
            ForEach(labels) { label in
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
                .accessibilityIdentifier("guessed-name-\(label.clipIndex.map(String.init) ?? label.text)")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("guessed-names")
    }
}
