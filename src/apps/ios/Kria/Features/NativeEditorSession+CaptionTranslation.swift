import CoreGraphics
import Foundation

/// The translate call's state, shown in the Translation panel.
enum CaptionTranslationStatus: Equatable {
    case idle
    case translating(language: String)
    /// The route answered. `dropped` lines came back without a translation (`outcome` `partial`).
    case finished(language: String, dropped: Int)
    /// Nothing usable came back. The document is unchanged.
    case failed(language: String, reason: CaptionTranslationFailure = .unmet)
}

/// Why a translate call changed nothing, each with its own honest message in the panel.
enum CaptionTranslationFailure: Equatable {
    /// `outcome: "unmet"` (the translator failed or every line came back in the wrong language), or an unknown error.
    case unmet
    /// 426 `client_update_required` (`caption_translation`): this build is too old.
    case updateRequired
    /// 422 `caption_translation_disabled`: the server turned the feature off.
    case disabled
    /// 422 `caption_translation_same_language`.
    case sameLanguage
    /// 422 `caption_translation_no_captions`, or nothing to send.
    case noCaptions
    /// 422 `caption_translation_too_long`: more text than the route takes in one call (20,000 characters).
    case tooLong
    /// 429: the route's rate limit (10 a minute, 60 an hour).
    case rateLimited
    case offline

    static func from(_ error: Error) -> CaptionTranslationFailure {
        guard let error = error as? APIError else { return .unmet }
        switch error {
        case .offline: return .offline
        case .nativeUpdateRequired: return .updateRequired
        case .requestFailed(let status, let detail):
            if status == 426 { return .updateRequired }
            if status == 429 { return .rateLimited }
            switch detail.object?["code"]?.stringValue ?? detail.code {
            case "client_update_required": return .updateRequired
            case "caption_translation_disabled": return .disabled
            case "caption_translation_same_language": return .sameLanguage
            case "caption_translation_no_captions": return .noCaptions
            case "caption_translation_too_long": return .tooLong
            default: return .unmet
            }
        default: return .unmet
        }
    }
}

// KRI-1501 two-language captions: the translated-line track's editor model. Contract: docs/pipelines/dual-captions.md.
extension NativeEditorSession {
    /// `editor_capabilities.caption_translation = {"languages": [...]}`. Present only where the server will keep and
    /// draw the track for this build (gate `dual_captions_planned_for`).
    var offersCaptionTranslation: Bool {
        guard let capability = document.capabilities["caption_translation"], capability.editable else { return false }
        return !(capability.languages ?? []).isEmpty
    }

    /// The language the spoken captions are in: the track's `source_language`, else the variant's `caption_language`.
    var spokenCaptionLanguage: String? { document.captionTranslation?.sourceLanguage ?? captionLanguage }

    /// The capability's languages minus the spoken one (a translated line is never in the spoken language).
    var captionTranslationLanguages: [String] {
        let spoken = spokenCaptionLanguage
        return (document.capabilities["caption_translation"]?.languages ?? []).filter { $0 != spoken }
    }

    /// The track writes `caption_meta.translation` and each cue's `translation`, so both lanes must be open.
    var canEditCaptionTranslation: Bool { offersCaptionTranslation && canEditCaptionMeta && canEditCaptionLines }

    var captionTranslationEnabled: Bool { document.captionTranslation?.enabled ?? false }

    /// The language's name in the reader's language ("English", "Englisch", "İngilizce").
    static func captionLanguageName(_ code: String) -> String {
        Locale.current.localizedString(forLanguageCode: code)?.capitalized(with: Locale.current) ?? code.uppercased()
    }

    /// Where a translate call goes: the UI-test stub in fixtures, else the API for this variant.
    private var captionTranslationTransport: ((CaptionTranslationRequest) async throws -> CaptionTranslationResponse)? {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-dual-captions") {
            return { NativeEditorUITestFixtures.dualCaptionTranslation(for: $0) }
        }
        #endif
        return captionTranslationAPI
    }

    /// Asks the server for the translated lines and applies them (the route persists nothing; Save keeps them).
    /// An `unmet` answer, an error, or no usable line leaves the document exactly as it was.
    func translateCaptions(to language: String) async {
        guard canEditCaptionTranslation, captionTranslationLanguages.contains(language) else { return }
        let cues = document.captionCues
            .filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .map { CaptionTranslationRequest.Cue(id: $0.id, text: $0.text, startS: $0.startS, endS: $0.endS) }
        guard !cues.isEmpty else {
            captionTranslationStatus = .failed(language: language, reason: .noCaptions)
            return
        }
        guard let transport = captionTranslationTransport else {
            captionTranslationStatus = .failed(language: language, reason: .offline)
            return
        }
        captionTranslationStatus = .translating(language: language)
        do {
            let response = try await transport(CaptionTranslationRequest(language: language, cues: cues))
            guard captionTranslationStatus == .translating(language: language) else { return }
            captionTranslationStatus = applyCaptionTranslation(response, requested: language,
                sources: Dictionary(cues.map { ($0.id, $0.text) }, uniquingKeysWith: { first, _ in first }))
        } catch {
            guard captionTranslationStatus == .translating(language: language) else { return }
            captionTranslationStatus = .failed(language: language, reason: .from(error))
        }
    }

    /// Where the preview parks for a translated line: once its pop-in (0.25 s) has finished, inside the cue.
    func captionTranslationParkTime(id: String) -> TimeInterval? {
        guard let cue = document.captionCues.first(where: { $0.id == id }) else { return nil }
        let latest = max(cue.startS, cue.endS - 1.0 / 30)
        return timelineProjection.projectBaseTime(min(cue.startS + 0.35, latest))
    }

    /// The spoken line was edited after it was translated: its translated line may no longer match.
    func captionTranslationIsStale(id: String) -> Bool {
        guard let cue = document.captionCues.first(where: { $0.id == id }), let line = cue.translation else { return false }
        let source = captionTranslationSourceTexts[id] ?? savedCaptionCue(id: id).flatMap { $0.translation == line ? $0.text : nil }
        return source.map { $0 != cue.text } ?? false
    }

    /// Translates just this line again (after its spoken text changed) and replaces only its translated line.
    func retranslateCaption(id: String) async {
        guard canEditCaptionTranslation, let track = document.captionTranslation,
              let cue = document.captionCues.first(where: { $0.id == id }),
              !cue.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let transport = captionTranslationTransport else { return }
        let language = track.language
        captionTranslationStatus = .translating(language: language)
        do {
            let response = try await transport(CaptionTranslationRequest(language: language,
                cues: [.init(id: cue.id, text: cue.text, startS: cue.startS, endS: cue.endS)]))
            guard captionTranslationStatus == .translating(language: language) else { return }
            let text = response.translations.first { $0.id == id }?.text?.trimmingCharacters(in: .whitespacesAndNewlines)
            guard response.language == language, let text, !text.isEmpty else {
                captionTranslationStatus = .failed(language: language, reason: .unmet)
                return
            }
            captionTranslationSourceTexts[id] = cue.text
            transactDocument(sections: [.captions, .captionMeta]) { doc in
                guard let index = doc.captionCues.firstIndex(where: { $0.id == id }), var track = doc.captionTranslation else { return }
                doc.captionCues[index].translation = String(text.prefix(CaptionTranslationLook.maximumCharacters))
                track.droppedCueIDs.removeAll { $0 == id }
                doc.captionTranslation = track
            }
            captionTranslationStatus = .finished(language: language, dropped: 0)
        } catch {
            guard captionTranslationStatus == .translating(language: language) else { return }
            captionTranslationStatus = .failed(language: language, reason: .from(error))
        }
    }

    /// One undo step: every cue's `translation` from the answer (a missing or null line removes the cue's old one) and
    /// the track's language, source and the server's `outcome` / `dropped_cue_ids`. The creator's look is kept.
    @discardableResult
    func applyCaptionTranslation(_ response: CaptionTranslationResponse, requested: String,
                                 sources: [String: String] = [:]) -> CaptionTranslationStatus {
        var texts: [String: String] = [:]
        for line in response.translations {
            if let text = line.text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty, texts[line.id] == nil {
                texts[line.id] = String(text.prefix(CaptionTranslationLook.maximumCharacters))
            }
        }
        guard response.language == requested, response.outcome != "unmet", !texts.isEmpty else { return .failed(language: requested) }
        for (id, text) in sources where texts[id] != nil { captionTranslationSourceTexts[id] = text }
        let spoken = response.sourceLanguage ?? spokenCaptionLanguage
        let attempted = document.captionCues.filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.count
        transactDocument(sections: [.captions, .captionMeta]) { doc in
            for index in doc.captionCues.indices { doc.captionCues[index].translation = texts[doc.captionCues[index].id] }
            var track = doc.captionTranslation ?? EditorCaptionTranslation(language: response.language)
            track.enabled = true
            track.language = response.language
            track.sourceLanguage = spoken
            track.outcome = response.outcome
            track.droppedCueIDs = response.droppedCueIDs
            doc.captionTranslation = track
        }
        return .finished(language: response.language, dropped: max(response.droppedCueIDs.count, attempted - texts.count))
    }

    /// The editor switch: off keeps the lines but draws nothing.
    func setCaptionTranslationEnabled(_ enabled: Bool) {
        guard canEditCaptionTranslation, var track = document.captionTranslation, track.enabled != enabled else { return }
        track.enabled = enabled
        transactDocument(section: .captionMeta) { $0.captionTranslation = track }
    }

    /// One cue's translated line, typed by the creator. Empty removes the line (the cue shows no translation).
    func setCaptionTranslationText(id: String, text: String) {
        let text = String(text.prefix(CaptionTranslationLook.maximumCharacters))
        guard canEditCaptionTranslation, let index = document.captionCues.firstIndex(where: { $0.id == id }),
              document.captionCues[index].raw["translation"]?.stringValue != text else { return }
        transactDocument(section: .captions) { doc in
            if text.isEmpty { doc.captionCues[index].raw.removeValue(forKey: "translation") }
            else { doc.captionCues[index].raw["translation"] = .string(text) }
        }
    }

    func setCaptionTranslationColor(_ hex: String?) {
        if let hex, hex.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) == nil { return }
        updateCaptionTranslationTrack { $0.color = hex }
    }

    func setCaptionTranslationFont(_ font: String?) {
        if let font, !NativeEditorWireContract.captionFonts.contains(font) { return }
        updateCaptionTranslationTrack { $0.font = font }
    }

    func setCaptionTranslationStroke(_ width: Double) {
        guard width.isFinite else { return }
        updateCaptionTranslationTrack { $0.strokeWidth = min(12, max(0, width.rounded())) }
    }

    private func updateCaptionTranslationTrack(_ body: (inout EditorCaptionTranslation) -> Void) {
        guard canEditCaptionTranslation, var track = document.captionTranslation else { return }
        body(&track)
        transactDocument(section: .captionMeta) { $0.captionTranslation = track }
    }

    /// The size the line is drawn at before any shrink-to-fit: its own, the track's, else 0.6 × the spoken size.
    func captionTranslationSize(_ scope: CaptionStyleScope) -> Double {
        let track = document.captionTranslation
        if case .line(let id) = scope, let size = track?.lineOverrides[id]?["size_px"]?.numberValue { return size }
        if let size = track?.sizePx { return size }
        return CaptionTranslationLook.defaultSize(spoken: captionStyleSize(scope))
    }

    func setCaptionTranslationSize(_ scope: CaptionStyleScope, size: Double) {
        guard size.isFinite else { return }
        writeTranslationStyle(scope, values: ["size_px": .number(CaptionTranslationLook.clampedSize(size))])
    }

    /// Nil puts the line back under the spoken caption (attached); a y places its centre there, horizontally centred.
    func placeCaptionTranslation(_ scope: CaptionStyleScope, y: Double?) {
        guard let y else {
            writeTranslationStyle(scope, values: ["position_x_frac": .null, "position_y_frac": .null])
            return
        }
        writeTranslationStyle(scope, values: ["position_x_frac": .number(0.5),
                                              "position_y_frac": .number(min(0.90, max(0.10, y)))])
    }

    /// Whether the line (or every line) follows the spoken caption.
    func captionTranslationIsAttached(_ scope: CaptionStyleScope) -> Bool {
        let track = document.captionTranslation
        if case .line(let id) = scope, track?.lineOverrides[id]?["position_y_frac"]?.numberValue != nil { return false }
        return track?.positionYFrac == nil
    }

    /// The line's centre on the canvas now: where the preview drew it, else its stored place, else under the captions.
    func captionTranslationPosition(id: String) -> CGPoint {
        if let bounds = previewSelectionBounds(for: EditorSelection(kind: .captionTranslation, id: id), at: currentTime) {
            return CGPoint(x: bounds.centerX, y: bounds.centerY)
        }
        let track = document.captionTranslation
        let own = track?.lineOverrides[id] ?? [:]
        return CGPoint(x: own["position_x_frac"]?.numberValue ?? track?.positionXFrac ?? 0.5,
                       y: own["position_y_frac"]?.numberValue ?? track?.positionYFrac ?? 0.88)
    }

    func translationStill(id: String) -> CaptionStill {
        CaptionStill(id: id, position: captionTranslationPosition(id: id), sizePx: captionTranslationSize(.line(id)), translation: true)
    }

    /// A drag of the translated line on the preview: only that line moves (detached), never the spoken caption.
    func setCaptionTranslationGesturePosition(id: String, position: CGPoint) {
        guard position.x.isFinite, position.y.isFinite else { return }
        let values: [String: JSONValue] = [
            "position_x_frac": .number(min(CaptionTranslationLook.xRange.upperBound, max(CaptionTranslationLook.xRange.lowerBound, position.x))),
            "position_y_frac": .number(min(CaptionTranslationLook.yRange.upperBound, max(CaptionTranslationLook.yRange.lowerBound, position.y))),
        ]
        writeTranslationStyle(.line(id), values: values)
        rememberCaptionGesture(id: id, values: values, translation: true)
    }

    func setCaptionTranslationGestureSize(id: String, size: Double) {
        guard size.isFinite else { return }
        let values: [String: JSONValue] = ["size_px": .number(CaptionTranslationLook.clampedSize(size))]
        writeTranslationStyle(.line(id), values: values)
        rememberCaptionGesture(id: id, values: values, translation: true)
    }

    /// Size / position for every translated line (track-wide values; each line's own value for that key is dropped)
    /// or for one line (`line_overrides[cue id]`). `.null` clears a key.
    func writeTranslationStyle(_ scope: CaptionStyleScope, values: [String: JSONValue]) {
        guard canEditCaptionTranslation, var track = document.captionTranslation else { return }
        switch scope {
        case .all:
            for (key, value) in values {
                switch key {
                case "size_px": track.sizePx = value.numberValue
                case "position_x_frac": track.positionXFrac = value.numberValue
                case "position_y_frac": track.positionYFrac = value.numberValue
                default: continue
                }
                for id in Array(track.lineOverrides.keys) {
                    track.lineOverrides[id]?.removeValue(forKey: key)
                    if track.lineOverrides[id]?.isEmpty == true { track.lineOverrides[id] = nil }
                }
            }
        case .line(let id):
            guard document.captionCues.contains(where: { $0.id == id }) else { return }
            var own = track.lineOverrides[id] ?? [:]
            for (key, value) in values where ["size_px", "position_x_frac", "position_y_frac"].contains(key) {
                if value == .null { own.removeValue(forKey: key) } else { own[key] = value }
            }
            track.lineOverrides[id] = own.isEmpty ? nil : own
        }
        // A line override is keyed by the cue id, so a cue without a stored id gets one (as caption line overrides do).
        let materializeIDs = scope != .all && document.captionCues.contains { $0.raw["id"] == nil }
        transactDocument(sections: materializeIDs ? [.captionMeta, .captions] : [.captionMeta]) { doc in
            if materializeIDs {
                for index in doc.captionCues.indices { doc.captionCues[index].raw["id"] = .string(doc.captionCues[index].id) }
            }
            doc.captionTranslation = track
        }
    }
}
