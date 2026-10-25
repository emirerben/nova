import Foundation

/// KRI-881: the sourced wording a research draft will put on screen, with its sources, as the server sends it on
/// `draft_applied.payload.research_preview` (`research_preview.preview_payload`) before the creator approves.
///
/// Optional and tolerant: no payload, a payload of the wrong shape, or one whose rows all fail to parse gives `nil`
/// and the approval card reads exactly as before. Unknown keys are ignored. A source is kept only with a public
/// http(s) link (the same rule as the editor's citations, `ResearchCitation.publicURL`); a row with no usable
/// source is dropped rather than shown as unsourced.
struct ResearchPreview: Equatable {
    struct Source: Identifiable, Equatable {
        let title: String
        let url: URL
        /// Where the source lives ("britannica.com"). Grounding redirect links carry the real domain in the
        /// provider-owned title, like the server's `_source_label`.
        let domain: String
        var id: String { url.absoluteString }
    }

    struct Explanation: Identifiable, Equatable {
        let id: Int
        let text: String
        let startS: Double?
        let endS: Double?
        let sources: [Source]
    }

    let explanations: [Explanation]
    let retrievedAt: String

    static func parse(_ value: JSONValue?) -> ResearchPreview? {
        guard case .object(let payload) = value, let rows = payload["explanations"]?.arrayValue else { return nil }
        let explanations = rows.enumerated().compactMap { index, row -> Explanation? in
            guard case .object(let item) = row,
                  let text = item["text"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
            else { return nil }
            var seen = Set<String>()
            let sources = (item["sources"]?.arrayValue ?? []).compactMap(source).filter { seen.insert($0.id).inserted }
            guard !sources.isEmpty else { return nil }
            return Explanation(id: index, text: text, startS: number(item["start_s"]), endS: number(item["end_s"]), sources: sources)
        }
        guard !explanations.isEmpty else { return nil }
        return ResearchPreview(explanations: explanations, retrievedAt: payload["retrieved_at"]?.stringValue ?? "")
    }

    private static func source(_ value: JSONValue) -> Source? {
        guard case .object(let link) = value, let address = link["url"]?.stringValue,
              let url = ResearchCitation.publicURL(address), let host = url.host?.lowercased() else { return nil }
        let title = link["title"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        var domain = host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
        if host == "vertexaisearch.cloud.google.com", !title.isEmpty { domain = title }
        return Source(title: title.isEmpty ? domain : title, url: url, domain: domain)
    }

    private static func number(_ value: JSONValue?) -> Double? {
        if case .number(let value) = value, value.isFinite, value >= 0 { return value }
        return nil
    }

    /// "0:03–0:07", or nil when the server sent no usable window.
    static func window(_ explanation: Explanation) -> String? {
        guard let start = explanation.startS, let end = explanation.endS, end > start else { return nil }
        func clock(_ seconds: Double) -> String {
            let whole = Int(seconds.rounded(.down))
            return String(format: "%d:%02d", whole / 60, whole % 60)
        }
        return "\(clock(start))–\(clock(end))"
    }
}

/// The card's own words in the chat's reply language (`ClipAnswerLanguage`, from `thread.state.reply_language`).
struct ResearchPreviewStrings {
    let language: ClipAnswerLanguage

    var eyebrow: String { language == .turkish ? "KAYNAKLAR" : "SOURCES" }
    var intro: String {
        language == .turkish
            ? "Bu kaynaklı metin yalnızca onaylarsan videoya eklenir."
            : "This sourced text is added only if you approve."
    }
    func openLabel(_ source: ResearchPreview.Source) -> String {
        let name = source.title == source.domain ? source.domain : "\(source.title), \(source.domain)"
        return language == .turkish ? "Kaynağı aç: \(name)" : "Open source: \(name)"
    }
    func window(_ text: String) -> String {
        language == .turkish ? "Ekranda \(text)" : "On screen \(text)"
    }
}
