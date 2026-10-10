import SwiftUI

/// A provider-safe, human-readable account of a model's work. The server only
/// supplies marked thought summaries; prompts and hidden reasoning never enter
/// this type or the local transcript.
struct KriaThoughtSummary: Codable, Equatable, Identifiable, Sendable {
    enum Status: String, Codable, Sendable { case streaming, completed, failed }
    let id: String
    let clientRequestID: String?
    let status: Status
    let text: String
    let startedAt: Date
    let completedAt: Date?
    let durationMS: Int?
    enum CodingKeys: String, CodingKey { case id, status, text; case clientRequestID = "client_request_id"; case startedAt = "started_at"; case completedAt = "completed_at"; case durationMS = "duration_ms" }

    init(id: String, clientRequestID: String? = nil, status: Status, text: String, startedAt: Date, completedAt: Date? = nil, durationMS: Int? = nil) {
        self.id = id; self.clientRequestID = clientRequestID; self.status = status; self.text = text
        self.startedAt = startedAt; self.completedAt = completedAt; self.durationMS = durationMS
    }

    var hasText: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    var durationLabel: String? {
        guard let durationMS else { return nil }
        let seconds = max(1, Int((Double(durationMS) / 1000).rounded()))
        return "Thought for \(seconds)s"
    }
}

struct KriaThoughtSummaryResponse: Codable, Sendable {
    let clientRequestID: String?
    let summaries: [KriaThoughtSummary]
    enum CodingKeys: String, CodingKey { case summaries; case clientRequestID = "client_request_id" }
}

/// Live text stays visible while a request is in flight. Completed summaries
/// collapse into a VoiceOver-labelled disclosure and can be reopened later.
struct ThoughtSummaryDisclosure: View {
    let summaries: [KriaThoughtSummary]
    private var visible: [KriaThoughtSummary] { summaries.filter(\.hasText) }

    var body: some View {
        ForEach(visible) { summary in
            if summary.status == .streaming {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Kria is thinking", systemImage: "sparkles")
                        .font(KriaFont.body(13).weight(.semibold))
                    Text(summary.text).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Kria is thinking. \(summary.text)")
            } else if summary.status == .completed, let label = summary.durationLabel {
                DisclosureGroup(label) {
                    Text(summary.text).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                        .padding(.top, 4)
                }
                .font(KriaFont.body(13).weight(.semibold))
                .accessibilityLabel(label)
            }
        }
    }
}
