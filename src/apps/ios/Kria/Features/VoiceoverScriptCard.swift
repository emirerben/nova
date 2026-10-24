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
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(script.lines.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(KriaFont.body(15))
                            .foregroundStyle(KriaColor.ink)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                .copyableMessage(script.text, previewShape: RoundedRectangle(cornerRadius: 14, style: .continuous))
                .accessibilityElement(children: .combine)
                .accessibilityLabel(script.text)
                .accessibilityIdentifier("voiceover-script-text")
                if !script.needsCheck.isEmpty { checks }
            }
            actions
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("voiceover-script-card")
    }

    private var checks: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(script.turkish ? "Kaydetmeden önce kontrol et" : "Check before recording")
                .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
            ForEach(script.needsCheck) { check in
                Text("“\(check.phrase)”" + (check.reason.map { ": \($0)" } ?? ""))
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.butterPale, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("voiceover-script-checks")
    }

    @ViewBuilder private var actions: some View {
        switch mode {
        case .readOnly:
            EmptyView()
        case .active(let voiceoverAttached, let isSending, let record, let importVoiceover, let planWithVoiceover):
            VStack(alignment: .leading, spacing: 8) {
                if voiceoverAttached {
                    Button { planWithVoiceover(script) } label: {
                        Label(script.turkish ? "Bu seslendirmeyle videoyu planla" : "Plan my video with this voiceover",
                              systemImage: "sparkles")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(isSending)
                    .accessibilityIdentifier("voiceover-script-plan")
                } else if script.isDraft {
                    Button { record(script) } label: {
                        Label(script.turkish ? "Bu metinle kaydet" : "Record with this script", systemImage: "mic.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(isSending)
                    .accessibilityIdentifier("voiceover-script-record")
                }
                if !voiceoverAttached {
                    Button { importVoiceover(script) } label: {
                        Text(script.turkish ? "Seslendirme içe aktar" : "Import a voiceover").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(KriaSecondaryButtonStyle())
                    .disabled(isSending)
                    .accessibilityIdentifier("voiceover-script-import")
                }
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
                .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(script.lines.enumerated()), id: \.offset) { _, line in
                        Text(line).font(KriaFont.body(18).weight(.medium)).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)
        }
        .padding(14)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel((script.turkish ? "Metnin: " : "Your script: ") + script.text)
        .accessibilityIdentifier("voiceover-script-readalong")
    }
}
