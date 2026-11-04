import SwiftUI

/// Speech-cleanup findings from the analysis (`CreationThread.speechCleanup`),
/// shared by the v1 confirmation card (`CreationConfirmationStage`) and the v2
/// approval card (`DirectionStage`) so identical server data reads the same
/// way on both. Every field is optional and `summarySentence` drops whatever
/// the server didn't send -- never a fabricated number.
struct SpeechCleanupStats: Equatable {
    let candidateCount: Int?
    let estimatedRemovedMs: Int?
    let fillerSounds: Int?
    let longPauses: Int?
    let retakes: Int?
    /// The creator already asked for the cleanup in chat (top-level `requested_in_chat`),
    /// so the v2 card shows "Clean up speech" already on. Old servers never send it.
    let requestedInChat: Bool
    /// The creator asked for TIGHT pauses (top-level `pause_style == "tight"`); the analysis
    /// numbers only count long pauses, so the card leaves them out.
    let tightPauses: Bool

    init(analysis: [String: JSONValue], requestedInChat: Bool = false, tightPauses: Bool = false) {
        self.requestedInChat = requestedInChat
        self.tightPauses = tightPauses
        candidateCount = analysis["candidate_count"]?.numberValue.map(Int.init)
        estimatedRemovedMs = analysis["estimated_removed_ms"]?.numberValue.map(Int.init)
        let counts = analysis["category_counts"]?.objectValue
        fillerSounds = counts?["filler_sounds"]?.numberValue.map(Int.init)
        longPauses = counts?["long_pauses"]?.numberValue.map(Int.init)
        retakes = counts?["retakes"]?.numberValue.map(Int.init)
    }

    init(candidateCount: Int? = nil, estimatedRemovedMs: Int? = nil, fillerSounds: Int? = nil, longPauses: Int? = nil, retakes: Int? = nil, requestedInChat: Bool = false, tightPauses: Bool = false) {
        self.requestedInChat = requestedInChat
        self.tightPauses = tightPauses
        self.candidateCount = candidateCount
        self.estimatedRemovedMs = estimatedRemovedMs
        self.fillerSounds = fillerSounds
        self.longPauses = longPauses
        self.retakes = retakes
    }

    var removesRetakes: Bool { (retakes ?? 0) > 0 }

    var cleanupConsentSentence: String {
        if removesRetakes {
            return "Clean up speech also removes the detected restarted sentence\(retakes == 1 ? "" : "s")."
        }
        return "Choose whether to remove the detected pauses and filler sounds. A sentence you restarted stays in."
    }

    /// e.g. "Found 3 pauses and filler sounds (2 filler sounds, 1 long pause) — about 4.2s shorter."
    var summarySentence: String? {
        var sentence = ""
        if let candidateCount, candidateCount > 0 {
            if removesRetakes {
                sentence += "Found \(candidateCount) speech cleanup \(candidateCount == 1 ? "point" : "points")"
            } else {
                sentence += "Found \(candidateCount) \(candidateCount == 1 ? "pause or filler sound" : "pauses and filler sounds")"
            }
            var categories: [String] = []
            if let fillerSounds, let longPauses, fillerSounds + longPauses > 0 {
                categories.append("\(fillerSounds) filler \(fillerSounds == 1 ? "sound" : "sounds")")
                categories.append("\(longPauses) long \(longPauses == 1 ? "pause" : "pauses")")
            }
            if let retakes, retakes > 0 {
                categories.append("\(retakes) restarted \(retakes == 1 ? "sentence" : "sentences")")
            }
            if !categories.isEmpty {
                sentence += " (\(categories.joined(separator: ", ")))"
            }
        }
        if let estimatedRemovedMs, estimatedRemovedMs > 0 {
            let seconds = (Double(estimatedRemovedMs) / 100).rounded() / 10
            let secondsText = seconds.truncatingRemainder(dividingBy: 1) == 0
                ? String(Int(seconds)) : String(format: "%.1f", seconds)
            sentence += sentence.isEmpty ? "About \(secondsText)s shorter" : " — about \(secondsText)s shorter"
        }
        return sentence.isEmpty ? nil : sentence + "."
    }
}

/// Shared "clean vs keep original" buttons for the requires-choice speech-
/// cleanup state (see `SpeechCleanupOffer`), so the v1 confirmation card and
/// the v2 approval card show identical copy for the same server projection.
struct SpeechCleanupChoiceButtons: View {
    let stats: SpeechCleanupStats
    let isDisabled: Bool
    let clean: () -> Void
    let keepOriginal: () -> Void

    var body: some View {
        Group {
            Text(stats.cleanupConsentSentence)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            if let summary = stats.summarySentence {
                Text(summary)
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("speech-cleanup-stats")
            }
            Button(action: clean) {
                Text("Clean up speech and create")
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 12)
            }
            .buttonStyle(CanonicalPrimaryButtonStyle())
            .disabled(isDisabled)
            .accessibilityIdentifier("speech-cleanup-clean")
            Button(action: keepOriginal) {
                Text("Keep original speech and create")
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 12)
            }
            .buttonStyle(CanonicalSecondaryButtonStyle())
            .disabled(isDisabled)
            .accessibilityIdentifier("speech-cleanup-keep-original")
        }
    }
}

/// v2 approval-card variant for when the creator already asked for the cleanup in chat:
/// "Clean up speech" starts on, and one "Create this video" button applies the choice.
struct SpeechCleanupPreselectedChoice: View {
    let stats: SpeechCleanupStats
    let isDisabled: Bool
    let create: (Bool) -> Void
    @State private var cleanUp = true

    init(stats: SpeechCleanupStats, isDisabled: Bool, create: @escaping (Bool) -> Void) {
        self.stats = stats
        self.isDisabled = isDisabled
        self.create = create
    }

    var body: some View {
        Group {
            Toggle(isOn: $cleanUp) {
                Text("Clean up speech")
                    .font(KriaFont.body(14).weight(.medium))
                Text(stats.tightPauses
                    ? "You asked for tight pauses, so this is on. Every pause gets cut down to about 0.3 s."
                    : "You asked me to cut your pauses, so this is on.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
            .disabled(isDisabled)
            .accessibilityIdentifier("speech-cleanup-preselected-toggle")
            if stats.removesRetakes {
                Text(stats.cleanupConsentSentence)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if !stats.tightPauses, let summary = stats.summarySentence {
                Text(summary)
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("speech-cleanup-stats")
            }
            Button { create(cleanUp) } label: {
                Text("Create this video")
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 12)
            }
            .buttonStyle(CanonicalPrimaryButtonStyle())
            .disabled(isDisabled)
            .accessibilityIdentifier("speech-cleanup-create")
        }
    }
}

/// What the v2 approval card (`DirectionStage`) offers for speech cleanup,
/// derived purely from `CreationThread.speechCleanup` so the branching is
/// unit-testable without a live thread or view. The server's `analysis.status`
/// is a closed set (`queued|running|ready|no_findings|failed`); an absent
/// analysis (not yet created) reads the same as `queued`/`running`, and any
/// future status this client doesn't recognize falls back to `.plain` so a
/// server-side addition never leaves the creator stuck on a spinner.
enum SpeechCleanupOffer: Equatable {
    /// Not applicable, no findings, ready without a choice to make, or an
    /// unrecognized status: today's plain "Create this video" flow, unchanged.
    case plain
    /// No analysis yet, or it's still queued/running.
    case checking
    /// Ready, and the creator must choose whether to apply it.
    case choice(SpeechCleanupStats)
    /// The speech-check analysis itself failed.
    case failed
    /// The video has no audible speech (`no_speech_track`, KRI-653). Not retryable, and rendering
    /// "without cleanup" would only produce captions over silence, so no create action is offered.
    case noSpeech

    /// `analysisID`, when present, rides on every cleanup-related request
    /// (`speech_cleanup_analysis_id`) regardless of which offer it resolves to.
    static func resolve(_ speechCleanup: [String: JSONValue]?) -> (offer: Self, analysisID: String?) {
        let cleanup = speechCleanup ?? [:]
        let analysis = cleanup["analysis"]?.objectValue ?? [:]
        let analysisID = analysis["id"]?.stringValue
        guard cleanup["applicable"]?.booleanValue == true else { return (.plain, analysisID) }
        switch analysis["status"]?.stringValue {
        case nil, "queued", "running":
            return (.checking, analysisID)
        case "failed":
            if analysis["error"]?.objectValue?["code"]?.stringValue == "no_speech_track" {
                return (.noSpeech, analysisID)
            }
            return (.failed, analysisID)
        case "ready", "no_findings":
            if cleanup["requires_choice"]?.booleanValue == true {
                return (.choice(SpeechCleanupStats(analysis: analysis, requestedInChat: cleanup["requested_in_chat"]?.booleanValue == true, tightPauses: cleanup["pause_style"]?.stringValue == "tight")), analysisID)
            }
            return (.plain, analysisID)
        default:
            return (.plain, analysisID)
        }
    }
}
