import Foundation

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
        var id: String { key }
    }

    let version: Int
    let questionID: String
    let options: [Option]
    let allowFreeText: Bool

    /// Highest `choice_question.version` this build understands; newer payloads degrade to the text question.
    static let supportedVersion = 1

    /// nil when the payload has no usable question (absent, newer version, fewer than two options).
    static func parse(payload: [String: JSONValue]?) -> ChoiceQuestion? {
        guard let fields = payload?["choice_question"]?.objectValue else { return nil }
        let version = fields["version"]?.numberValue.map(Int.init) ?? 1
        guard version == supportedVersion,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        var seen = Set<String>()
        let options: [Option] = (fields["options"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let key = object["key"]?.stringValue, !key.isEmpty, seen.insert(key).inserted,
                  let label = object["label"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !label.isEmpty else { return nil }
            let detail = object["description"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
            return Option(key: key, label: label, detail: detail?.isEmpty == false ? detail : nil,
                          recommended: object["recommended"]?.boolValue ?? false)
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

/// `choice_selection` on the submit-turn body, exactly as the server contract names it.
struct ChoiceSelectionSubmission: Encodable, Equatable, Sendable {
    let questionID: String
    let optionKey: String
    enum CodingKeys: String, CodingKey {
        case questionID = "question_id"
        case optionKey = "option_key"
    }
}
