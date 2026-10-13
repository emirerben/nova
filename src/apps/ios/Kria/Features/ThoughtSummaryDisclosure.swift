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

/// Derives the one-line "what Kria is doing" from raw Gemini thought text.
/// Gemini streams `**Short title**` headings followed by prose, so the latest
/// complete heading is the current step. Code, JSON and half-streamed headings
/// never become the summary; `nil` means "use the generic copy".
enum ThoughtHeadline {
    static let maxLength = 90

    static func extract(from text: String) -> String? {
        let lines = text.components(separatedBy: .newlines)
        for line in lines.reversed() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.count > 4, trimmed.hasPrefix("**"), trimmed.hasSuffix("**") else { continue }
            if let heading = clean(String(trimmed.dropFirst(2).dropLast(2))) { return capped(heading) }
        }
        for paragraph in proseParagraphs(lines).reversed() where !looksLikeCode(paragraph) {
            if let sentence = clean(firstSentence(of: paragraph)) { return capped(sentence) }
        }
        return nil
    }

    /// Blank-line separated paragraphs with fenced code (including a fence that has
    /// not closed yet while streaming) removed.
    private static func proseParagraphs(_ lines: [String]) -> [String] {
        var paragraphs: [String] = []
        var current: [String] = []
        var inFence = false
        func flush() {
            if !current.isEmpty { paragraphs.append(current.joined(separator: " ")) }
            current = []
        }
        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") { inFence.toggle(); flush(); continue }
            if inFence { continue }
            if trimmed.isEmpty { flush() } else { current.append(trimmed) }
        }
        flush()
        return paragraphs
    }

    private static func looksLikeCode(_ paragraph: String) -> Bool {
        guard let first = paragraph.first else { return true }
        if "{[<".contains(first) { return true }
        let letters = paragraph.filter(\.isLetter).count
        return Double(letters) / Double(paragraph.count) < 0.5
    }

    private static func firstSentence(of paragraph: String) -> String {
        let chars = Array(paragraph)
        for (index, char) in chars.enumerated() where ".!?".contains(char) {
            if index + 1 == chars.count || chars[index + 1].isWhitespace {
                return String(chars[...index])
            }
        }
        return paragraph
    }

    private static func clean(_ raw: String) -> String? {
        var value = raw
        for marker in ["**", "__", "`"] { value = value.replacingOccurrences(of: marker, with: "") }
        value = value.trimmingCharacters(in: CharacterSet(charactersIn: "#-* \t"))
        let collapsed = value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return collapsed.isEmpty ? nil : collapsed
    }

    private static func capped(_ value: String) -> String {
        guard value.count > maxLength else { return value }
        return String(value.prefix(maxLength)).trimmingCharacters(in: .whitespaces) + "\u{2026}"
    }
}

extension KriaThoughtSummary {
    /// One-line summary of the latest step, or nil when the text has no usable line.
    var headline: String? { ThoughtHeadline.extract(from: text) }

    /// The live row and the finished timeline row come from the same server record, so they share `id`.
    /// Inside the chat's lazy stack that collision left the finished row showing the old streaming
    /// content; the status keeps their identities apart.
    fileprivate var rowID: String { "\(id)-\(status.rawValue)" }
}

/// One collapsed line per thought: the current step while Kria is thinking and
/// "Thought for Ns" afterwards. Tapping reveals the full text, so long reasoning
/// (and any code in it) stays out of the chat until the creator asks for it.
struct ThoughtSummaryDisclosure: View {
    let summaries: [KriaThoughtSummary]
    private var visible: [KriaThoughtSummary] {
        summaries.filter { $0.hasText && ($0.status == .streaming || ($0.status == .completed && $0.durationLabel != nil)) }
    }

    var body: some View {
        ForEach(visible, id: \.rowID) { summary in
            ThoughtSummaryRow(summary: summary)
        }
    }
}

private struct ThoughtSummaryRow: View {
    let summary: KriaThoughtSummary
    @State private var isExpanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var isStreaming: Bool { summary.status == .streaming }
    private var title: String {
        isStreaming ? (summary.headline ?? "Kria is thinking") : (summary.durationLabel ?? "Thought")
    }
    private var detail: AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: summary.text, options: options)) ?? AttributedString(summary.text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                if reduceMotion { isExpanded.toggle() } else { withAnimation(.easeOut(duration: 0.2)) { isExpanded.toggle() } }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "sparkles")
                    Text(title)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .contentTransition(.opacity)
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: title)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(KriaFont.body(13).weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("thought-summary-toggle")
            .accessibilityLabel(isStreaming ? "Kria is thinking: \(title)" : title)
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            .accessibilityHint("Shows Kria's full thinking")
            if isExpanded {
                Text(detail)
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.zinc)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("thought-summary-detail")
            }
        }
    }
}
