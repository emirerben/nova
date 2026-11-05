#if DEBUG
import AVFoundation
import SwiftUI

/// Deterministic Generate image states for screenshot review (`-ui-testing-brand` with
/// `KRIA_BRAND_STATE=generate-<state>`); never enters release navigation.
struct GenerateImagePreviewHost: View {
    let state: String
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @StateObject private var overlays = GeneratedOverlaySet()
    @StateObject private var voiceDraft = GeneratedMediaDraft(capability: .narration)

    private static let prompts = [
        "A flat lay of fresh ingredients on a wooden table, soft morning light",
        "A flat lay of fresh ingredients on a wooden table, golden hour, long shadows",
        "A flat lay of fresh ingredients on a wooden table, close on the herbs, window light",
    ]

    private static func image(_ index: Int) -> URL? {
        let names = ["featured-sunset", "featured-lisbon", "featured-bosphorus"]
        return Bundle.main.url(forResource: names[index % names.count], withExtension: "jpg")
            ?? Bundle.main.url(forResource: names[index % names.count], withExtension: "jpg", subdirectory: "FeaturedEdits")
    }

    private func card(_ index: Int, _ state: String?, stalled: Bool = false, prompt: String? = nil) -> OverlayCard {
        var value = OverlayCard(prompt: prompt ?? Self.prompts[index])
        if let state {
            value.generation = .preview(prompt ?? Self.prompts[index], state: state, url: state == "ready" ? Self.image(index) : nil,
                                        stalled: stalled, retryable: state == "provider_complete")
        }
        return value
    }

    var body: some View {
        Group {
            if state.hasPrefix("generate-voice") { voice } else { overlaySheet }
        }
        .background(KriaColor.paper)
        .environment(\.dynamicTypeSize, ProcessInfo.processInfo.environment["UI_TEST_DYNAMIC_TYPE_SIZE"] == "accessibility5" ? .accessibility5 : dynamicTypeSize)
        .task { install() }
    }

    private var overlaySheet: some View {
        GeneratedOverlaysSheet(itemID: "preview", overlays: overlays, ideaTitle: "Saturday coffee", accepted: {})
    }

    private var voice: some View {
        GeneratedMediaSheet(itemID: "preview", draft: voiceDraft, accepted: {}, restoresOnAppear: false)
    }

    private func install() {
        let screenState = state.replacingOccurrences(of: "generate-", with: "")
        switch screenState {
        case "choose":
            overlays.installPreview(screen: .choose, cards: [])
        case "describe":
            overlays.installPreview(screen: .compose, cards: [OverlayCard(prompt: Self.prompts[0])])
        case "decide":
            overlays.installPreview(screen: .compose, origin: .decide, cards: (0..<3).map { OverlayCard(prompt: Self.prompts[$0].replacingOccurrences(of: "A flat lay of fresh ingredients on a wooden table", with: "Fresh ingredients flat lay")) })
        case "add":
            var third = OverlayCard(prompt: Self.prompts[1], copiedFrom: 2)
            third.copiedPrompt = Self.prompts[1]
            overlays.installPreview(screen: .compose, cards: [OverlayCard(prompt: Self.prompts[0]), OverlayCard(prompt: Self.prompts[1]), third])
        case "making":
            overlays.installPreview(screen: .making, cards: [card(0, "ready"), card(1, "pending"), card(2, "pending")])
        case "stalled":
            overlays.installPreview(screen: .making, cards: [card(0, "ready"), card(1, "ready"), card(2, "pending", stalled: true)])
        case "results":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready"), card(2, "ready")])
        case "edit":
            let cards = [card(0, "ready"), card(1, "ready"), card(2, "ready")]
            overlays.installPreview(screen: .edit(cards[0].id), cards: cards)
        case "remade":
            var first = card(0, "ready")
            first.remake = .preview(Self.prompts[0] + ", a little more steam from the coffee", state: "ready", url: Self.image(0))
            overlays.installPreview(screen: .remade(first.id), cards: [first, card(1, "ready"), card(2, "ready")])
        case "remaking":
            var first = card(0, "ready")
            first.remake = .preview(Self.prompts[0], state: "pending", url: nil)
            overlays.installPreview(screen: .remade(first.id), cards: [first, card(1, "ready"), card(2, "ready")])
        case "problem-failed":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready"), card(2, "failed")])
        case "problem-unsure":
            overlays.installPreview(screen: .making, cards: [card(0, "ready"), card(1, "ready"), card(2, "unknown")])
        case "problem-unsaved":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready"), card(2, "provider_complete")])
        case "problem-session":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready")], setProblem: .sessionEnded)
        case "problem-update":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready")], setProblem: .updateRequired)
        case "problem-offline":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready")], setProblem: .offline)
        case "problem-add":
            overlays.installPreview(screen: .results, cards: [card(0, "ready"), card(1, "ready")], setProblem: .couldNotAdd)
        case "voice-review":
            voiceDraft.script = Self.script
        case "voice-making":
            voiceDraft.script = Self.script
            voiceDraft.recovery = GeneratedMediaRecovery(clientEventID: UUID().uuidString, operationID: nil)
            voiceDraft.operation = Self.operation(state: "pending", url: nil)
        case "voice-ready":
            voiceDraft.script = Self.script
            voiceDraft.recovery = GeneratedMediaRecovery(clientEventID: UUID().uuidString, operationID: nil)
            voiceDraft.operation = Self.operation(state: "ready", url: try? Self.voiceFile())
        default: break
        }
    }

    private static var script: GeneratedNarrationScript? {
        let text = "Okay, so this is my Saturday morning routine. First I grind the beans, which honestly is the best sound of the week. Then I let the water cool for about thirty seconds, because boiling water burns the coffee. While it blooms, I get the milk going. Nothing fancy, just a small jug and a steady hand. When the shot is ready I pour slowly and try to draw a heart. Sometimes it works. Today it kind of looks like a leaf, and I am fine with that."
        let json: [String: Any] = ["script_id": "preview", "script_version": 1, "text": text, "approved_script_sha256": "preview"]
        return (try? JSONSerialization.data(withJSONObject: json)).flatMap { try? JSONDecoder().decode(GeneratedNarrationScript.self, from: $0) }
    }

    private static func operation(state: String, url: URL?) -> GeneratedMediaOperation? {
        var json: [String: Any] = ["id": UUID().uuidString, "state": state, "capability": "narration", "retryable": false,
                                   "script_id": "preview", "script_version": 1, "approved_script_sha256": "preview"]
        if let url { json["preview_url"] = url.absoluteString; json["source_url"] = url.absoluteString }
        return (try? JSONSerialization.data(withJSONObject: json)).flatMap { try? JSONDecoder().decode(GeneratedMediaOperation.self, from: $0) }
    }

    private static func voiceFile() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("generate-preview-voice.wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let rate = 8_000.0
        let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(rate * 38))!
        buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = 0 }
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        try file.write(from: buffer)
        return url
    }
}
#endif
