import SwiftUI

/// KRI-815: the voiceover script Kria wrote from the creator's clips, with the way to record it.
/// The reply text above it says what this is; the card holds the words to read, the claims to check
/// first, and Record / Import, which open the voiceover step with this script on screen.
struct VoiceoverScriptCard: View {
    let script: VoiceoverScript
    let mode: VoiceoverScriptCardMode

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if script.isDraft {
                VStack(alignment: .leading, spacing: 8) {
                    Text(script.turkish ? "Metnin" : "Your script").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(script.lines.enumerated()), id: \.offset) { _, line in
                            Text(line).kriaType(.body).foregroundStyle(KriaColor.ink).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .copyableMessage(script.text, previewShape: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(script.text)
                    .accessibilityIdentifier("voiceover-script-text")
                }
                if !script.needsCheck.isEmpty { checks }
            }
            actions
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("voiceover-script-card")
    }

    private var checks: some View {
        KriaNoticeCard(
            tone: .warning,
            title: script.turkish ? "Kaydetmeden önce kontrol et" : "Check before recording",
            message: script.needsCheck.map { "“\($0.phrase)”" + ($0.reason.map { ": \($0)" } ?? "") }.joined(separator: "\n"))
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("voiceover-script-checks")
    }

    @ViewBuilder private var actions: some View {
        switch mode {
        case .readOnly:
            EmptyView()
        case .active(let voiceoverAttached, let isSending, let record, let importVoiceover, let planWithVoiceover, let generateNarration):
            if voiceoverAttached {
                Button { planWithVoiceover(script) } label: {
                    Text(script.turkish ? "Bu seslendirmeyle videoyu planla" : "Plan my video with this voiceover")
                }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .disabled(isSending)
                .accessibilityIdentifier("voiceover-script-plan")
            } else {
                KriaSourceRow(options: [
                    script.isDraft ? .record(caption: "Use your mic", identifier: "voiceover-script-record", isEnabled: !isSending) { record(script) } : nil,
                    .upload(caption: "Use an audio file", identifier: "voiceover-script-import", isEnabled: !isSending) { importVoiceover(script) },
                    script.isDraft ? generateNarration.map { generate in
                        KriaSourceOption(id: "ai-voice", symbol: "waveform", title: "AI voice", caption: "Read by AI",
                                         identifier: "voiceover-script-ai-voice", isEnabled: !isSending) { generate(script) }
                    } : nil
                ].compactMap { $0 }, onFill: true)
            }
        }
    }
}

/// The script, read along while recording in the voiceover step. Keyed by script + revision by the caller, so
/// an older card can never put a stale script on screen.
struct VoiceoverScriptReadAlong: View {
    let script: VoiceoverScript

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(script.turkish ? "Metnin" : "Your script")
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(script.lines.enumerated()), id: \.offset) { _, line in
                        Text(line).kriaType(.body).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)
        }
        .padding(16)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel((script.turkish ? "Metnin: " : "Your script: ") + script.text)
        .accessibilityIdentifier("voiceover-script-readalong")
    }
}
