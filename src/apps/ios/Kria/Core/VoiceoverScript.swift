import Foundation

// KRI-815: a voiceover script Kria wrote from the attached clips, before any recording exists.
//
// Hand-rolled like `ChoiceQuestion.swift`: the script rides in the free-form `assistant_response` payload
// (`voiceover_script`). The reply's text holds the same script, so an older build (or this build with the
// capability off) still reads it; this card only adds the Record / Import continuation.

/// The optional `voiceover_script` on an assistant reply payload.
struct VoiceoverScript: Equatable, Sendable {
    struct Check: Equatable, Sendable, Identifiable {
        let phrase: String
        let reason: String?
        var id: String { phrase }
    }

    enum Status: String, Sendable { case draft, failed }

    let version: Int
    let scriptID: String
    let revision: Int
    let status: Status
    /// The plan item the script was written for. A card for another item never opens a recorder here.
    let itemID: String
    let text: String
    let lines: [String]
    let needsCheck: [Check]
    let language: String

    /// Highest `voiceover_script.version` this build understands; newer payloads degrade to the reply text.
    static let supportedVersion = 1

    /// nil when there is no usable script record (absent, newer version, unknown continuation, malformed).
    static func parse(payload: [String: JSONValue]?) -> VoiceoverScript? {
        guard let fields = payload?["voiceover_script"]?.objectValue else { return nil }
        let version = fields["version"]?.numberValue.map(Int.init) ?? 0
        guard version == supportedVersion,
              fields["continuation"]?.stringValue == "record_or_import",
              let scriptID = fields["script_id"]?.stringValue, !scriptID.isEmpty,
              let itemID = fields["item_id"]?.stringValue, !itemID.isEmpty,
              let status = fields["status"]?.stringValue.flatMap(Status.init(rawValue:)) else { return nil }
        let text = fields["text"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        // A draft without words is not a script.
        if status == .draft && text.isEmpty { return nil }
        let lines = (fields["lines"]?.arrayValue ?? []).compactMap { $0.stringValue }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        let checks: [Check] = (fields["needs_check"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let phrase = object["phrase"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !phrase.isEmpty else { return nil }
            let reason = object["reason"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
            return Check(phrase: phrase, reason: reason?.isEmpty == false ? reason : nil)
        }
        return VoiceoverScript(
            version: version, scriptID: scriptID, revision: fields["revision"]?.numberValue.map(Int.init) ?? 0,
            status: status, itemID: itemID, text: status == .draft ? text : "",
            lines: status == .draft ? (lines.isEmpty ? [text] : lines) : [], needsCheck: checks,
            language: fields["language"]?.stringValue ?? "en"
        )
    }

    var isDraft: Bool { status == .draft }
    var turkish: Bool { language == "tr" }

    /// The reply text without the script that follows it (the card shows the script itself).
    func reply(from content: String) -> String {
        guard isDraft, content.hasSuffix(text) else { return content }
        return String(content.dropLast(text.count)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Whether `other` is a newer draft of the same script; an older card never replaces a newer one.
    func isSuperseded(by other: VoiceoverScript) -> Bool {
        other.scriptID == scriptID && other.revision > revision
    }

    /// The message sent once the voiceover is attached, in the chat's language.
    var recordedMessage: String {
        turkish ? "Seslendirmeyi bu metinle kaydettim. Videoyu buna göre hazırla."
            : "I recorded the voiceover with this script. Make the video around it."
    }
}

/// What the script card can do right now.
enum VoiceoverScriptCardMode {
    /// The newest script for this project: Record / Import open the voiceover step seeded with it. `voiceoverAttached`
    /// switches the primary action to sending the recorded-voiceover message.
    case active(voiceoverAttached: Bool, isSending: Bool, record: (VoiceoverScript) -> Void,
                importVoiceover: (VoiceoverScript) -> Void, planWithVoiceover: (VoiceoverScript) -> Void,
                generateNarration: ((VoiceoverScript) -> Void)? = nil)
    /// An earlier revision, another project's card, or a failed draft: read-only.
    case readOnly
}
