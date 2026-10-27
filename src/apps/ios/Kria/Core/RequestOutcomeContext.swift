import Foundation

struct RequestOutcomeContext: Codable, Equatable, Sendable {
    enum Relation: String, Codable, Sendable {
        case original, continuation, retry, revision, recovery, unclassified
    }
    let relation: Relation
    let parentTurnID: UUID?
    var clientBuild: String? = nil
    var clientVersion: String? = nil

    enum CodingKeys: String, CodingKey {
        case relation, parentTurnID = "parent_turn_id"
        case clientBuild = "client_build", clientVersion = "client_version"
    }

    static func forSubmission(events: [ThreadEvent], continuation: Bool,
                              choice: ChoiceSelectionSubmission? = nil) -> Self {
        let users = events.filter { $0.role == "user" && $0.eventType == "user_message" }
        let parent = users.last?.payload?["turn_id"]?.stringValue.flatMap(UUID.init(uuidString:))
        let relation: Relation
        if users.isEmpty {
            relation = .original
        } else if parent == nil {
            relation = .unclassified
        } else if let choice,
                  let question = events.last(where: { $0.payload?["choice_question"] != nil })?
                    .payload?["choice_question"]?.objectValue,
                  question["question_id"]?.stringValue == choice.questionID,
                  question["conflict"]?.stringValue == "simplification_requires_choice",
                  choice.optionKey == "simpler_version" {
            relation = .recovery
        } else {
            relation = continuation ? .continuation : .revision
        }
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return Self(relation: relation, parentTurnID: parent,
                    clientBuild: build?.range(of: #"^[0-9]{1,10}$"#, options: .regularExpression) != nil ? build : nil,
                    clientVersion: version?.range(of: #"^[0-9]+(?:\.[0-9]+){0,3}$"#, options: .regularExpression) != nil ? version : nil)
    }
}
