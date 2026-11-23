import Foundation
import KriaMediaEngine

// KRI-282: "your instructions conflict, which do you want?" asked as tappable options.
//
// Hand-rolled Codable like `ClipSelection.swift`: the generated OpenAPI client only models the request
// body (`choice_selection`); the question payload rides in the free-form event payload. Everything
// wire-shaped lives here (`ChoiceQuestion` in, `ChoiceSelectionSubmission` out, the capability flag in
// `CreationCapabilities`).

/// The optional `choice_question` on an assistant question event payload.
struct ChoiceQuestion: Equatable, Sendable {
    struct Option: Equatable, Sendable, Identifiable {
        let key: String
        let label: String
        let detail: String?
        let recommended: Bool
        /// Version 2 (KRI-1582): what this option looks like over the creator's own clip, drawn on this iPhone.
        var preview: Preview? = nil
        var id: String { key }
    }

    /// Server-compiled text layers and the frame of the creator's clip they are drawn over
    /// (`app/kria/text_design_ask.py`). Never more than a still: no audio, no timeline.
    struct Preview: Equatable, Sendable {
        let mediaID: String
        let isPhoto: Bool
        /// Second of her clip under the text.
        let atSeconds: Double
        /// Second of the layers' own clock the still samples.
        let timeSeconds: Double
        let layers: [PortableTextLayer]
        /// Font asset id → the bundled font file the layers draw with.
        let fonts: [String: String]
    }

    let version: Int
    let questionID: String
    let options: [Option]
    let allowFreeText: Bool

    /// Highest `choice_question.version` this build understands; newer payloads degrade to the text question.
    /// Version 2 adds an optional `preview` per option; a version 1 question parses exactly as before.
    static let supportedVersion = 2

    /// Options with a preview are shown as a strip of stills; a question with none (or a preview that did not parse)
    /// keeps the text blocks.
    var hasPreviews: Bool { options.contains { $0.preview != nil } }

    /// nil when the payload has no usable question (absent, newer version, fewer than two options).
    static func parse(payload: [String: JSONValue]?) -> ChoiceQuestion? {
        guard let fields = payload?["choice_question"]?.objectValue else { return nil }
        let version = fields["version"]?.numberValue.map(Int.init) ?? 1
        guard (1...supportedVersion).contains(version),
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        var seen = Set<String>()
        let options: [Option] = (fields["options"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let key = object["key"]?.stringValue, !key.isEmpty, seen.insert(key).inserted,
                  let label = object["label"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !label.isEmpty else { return nil }
            let detail = object["description"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
            return Option(key: key, label: label, detail: detail?.isEmpty == false ? detail : nil,
                          recommended: object["recommended"]?.boolValue ?? false,
                          preview: version >= 2 ? Preview.parse(object["preview"]) : nil)
        }
        // One option is not a choice: keep the plain text question.
        guard options.count >= 2 else { return nil }
        return ChoiceQuestion(version: version, questionID: questionID, options: options,
                              allowFreeText: fields["allow_free_text"]?.boolValue ?? true)
    }

    func option(key: String) -> Option? { options.first { $0.key == key } }

    /// The structured answer for tapping `option`.
    func submission(for option: Option) -> ChoiceSelectionSubmission {
        ChoiceSelectionSubmission(questionID: questionID, optionKey: option.key)
    }

    /// The readable user message sent alongside the structured answer: the option's own label, so the
    /// transcript (and an old server that ignores `choice_selection`) reads as the creator's decision.
    func message(for option: Option) -> String { option.label }
}

extension ChoiceQuestion.Preview {
    /// nil for anything this build cannot draw exactly: no clip, no layers, a layer that does not decode, or a run
    /// whose font is not a bundled font file. The option then reads as a text block.
    static func parse(_ value: JSONValue?) -> Self? {
        guard let fields = value?.objectValue,
              let mediaID = fields["media_id"]?.stringValue, !mediaID.isEmpty,
              let encoded = fields["text_layers"].flatMap({ try? JSONEncoder().encode($0) }),
              let layers = try? RecipeJSON.decoder().decode([PortableTextLayer].self, from: encoded), !layers.isEmpty
        else { return nil }
        let fonts = (fields["fonts"]?.objectValue ?? [:]).compactMapValues { value -> String? in
            guard let name = value.stringValue, !name.contains("/"), !name.contains("\\"),
                  ["ttf", "otf"].contains((name as NSString).pathExtension.lowercased()) else { return nil }
            return name
        }
        guard layers.allSatisfy({ $0.runs.allSatisfy { fonts[$0.fontAssetID] != nil } }) else { return nil }
        func seconds(_ key: String) -> Double? {
            guard let value = fields[key]?.numberValue, value.isFinite, value >= 0 else { return nil }
            return value
        }
        guard let time = seconds("time_s") else { return nil }
        return Self(mediaID: mediaID, isPhoto: fields["media_kind"]?.stringValue == "image",
                    atSeconds: seconds("at_s") ?? 0, timeSeconds: time, layers: layers, fonts: fonts)
    }
}

/// `choice_selection` on the submit-turn body, exactly as the server contract names it.
struct ChoiceSelectionSubmission: Encodable, Equatable, Sendable {
    let questionID: String
    let optionKey: String
    enum CodingKeys: String, CodingKey {
        case questionID = "question_id"
        case optionKey = "option_key"
    }
}
