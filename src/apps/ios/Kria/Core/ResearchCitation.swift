import Foundation

/// Read-only provenance rides the existing raw text record through native Save.
struct ResearchCitation: Identifiable {
    struct Source: Identifiable {
        let title: String
        let url: URL
        var id: String { url.absoluteString }
    }
    let id: String
    let originalText: String
    let startS: Double
    let endS: Double
    let mediaIDs: [String]
    let timelineBinding: [[String: JSONValue]]
    /// KRI-816: a spoken (Talking) edit has no editable clips; its explanation is
    /// bound to the spoken lines around the pause it sits in.
    var speechBinding: [[String: JSONValue]] = []
    let sources: [Source]
    let searchEntryHTML: String
    let retrievedAt: String

    static func from(_ text: EditorTextElement) -> ResearchCitation? {
        guard case .object(let params) = text.raw["source_params"],
              case .object(let value) = params["research"],
              case .string(let original) = value["original_text"],
              case .number(let start) = value["start_s"],
              case .number(let end) = value["end_s"],
              case .array(let links) = value["sources"] else { return nil }
        let sources = links.compactMap { item -> Source? in
            guard case .object(let link) = item, case .string(let address) = link["url"],
                  let url = publicURL(address) else { return nil }
            let title: String
            if case .string(let name) = link["title"] { title = name } else { title = url.host ?? "Source" }
            return Source(title: title, url: url)
        }
        guard !sources.isEmpty else { return nil }
        let mediaIDs: [String]
        if case .array(let ids) = value["media_ids"] {
            mediaIDs = ids.compactMap { if case .string(let id) = $0 { id } else { nil } }
        } else { mediaIDs = [] }
        let html: String
        if case .string(let content) = value["search_entry_html"] { html = content } else { html = "" }
        let retrieved: String
        if case .string(let date) = value["retrieved_at"] { retrieved = date } else { retrieved = "" }
        let binding: [[String: JSONValue]]
        if case .array(let values) = value["timeline_binding"] {
            binding = values.compactMap { if case .object(let row) = $0 { row } else { nil } }
        } else { binding = [] }
        let speech: [[String: JSONValue]]
        if case .array(let values) = value["speech_binding"] {
            speech = values.compactMap { if case .object(let row) = $0 { row } else { nil } }
        } else { speech = [] }
        return ResearchCitation(id: text.id, originalText: original, startS: start, endS: end,
                                mediaIDs: mediaIDs, timelineBinding: binding, speechBinding: speech,
                                sources: sources, searchEntryHTML: html, retrievedAt: retrieved)
    }

    func matches(_ text: EditorTextElement, clips: [EditorTimelineSlot], captions: [EditorCaptionCue] = []) -> Bool {
        guard text.text == originalText, abs(text.startS - startS) < 0.001,
              abs(text.endS - endS) < 0.001 else { return false }
        // Compare the persisted canonical cuts rather than inventing another timeline clock.
        let active = clips.filter { !$0.removed }
        if active.isEmpty, !speechBinding.isEmpty {
            // Every bound spoken line must still be said, unchanged, at the same time.
            return speechBinding.allSatisfy { row in
                guard case .string(let said) = row["text"], case .number(let from) = row["start_s"],
                      case .number(let to) = row["end_s"] else { return false }
                return captions.contains { cue in
                    cue.text == said && abs(cue.startS - from) < 0.001 && abs(cue.endS - to) < 0.001
                }
            }
        }
        guard !timelineBinding.isEmpty, timelineBinding.count == active.count else { return false }
        return zip(timelineBinding, active).allSatisfy { row, clip in
            guard case .number(let index) = row["clip_index"], Int(index) == clip.clipIndex,
                  case .number(let sourceStart) = row["in_s"], abs(sourceStart - clip.inS) < 0.001,
                  case .number(let duration) = row["duration_s"],
                  abs(duration - (clip.durationS ?? -1)) < 0.001 else { return false }
            let transition: String
            if case .string(let value) = row["transition_after"] { transition = value } else { transition = "cut" }
            let overlap: Double
            if case .number(let value) = row["transition_duration_s"] { overlap = value } else { overlap = 0 }
            return transition == clip.transitionAfter && abs(overlap - (clip.transitionDurationS ?? 0)) < 0.001
        }
    }

    static func publicURL(_ value: String) -> URL? {
        guard let url = URL(string: value), let host = url.host?.lowercased(),
              ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              url.user == nil, url.password == nil, host.contains("."),
              !host.hasSuffix(".local"), !host.hasSuffix(".internal"), !host.hasSuffix(".localhost"),
              // Citations are domains, not IP literals or browser numeric aliases.
              host.contains(where: { $0.isLetter }), !host.contains(":") else { return nil }
        return url
    }
}
