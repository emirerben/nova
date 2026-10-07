import SwiftUI

/// The footage's own ("original" / camera) sound in the Sounds tab: one level for the whole video.
///
/// A creator-song video plays the song alone by default, which makes a take's own audio impossible to
/// check while debugging a lip-sync cut. This row turns the camera audio up (it then plays WITH the song,
/// each at its own level) or back down to 0. It edits `mix.original_level` in the editor document, so Undo,
/// Save and the live preview behave like every other Sounds control, and it is gated by the server's
/// `original_audio` capability (phone-rendered guided edits), so it never shows where the render would ignore it.
struct NativeEditorOriginalAudioRow: View {
    @ObservedObject var session: NativeEditorSession

    private var levelBinding: Binding<Double> {
        Binding(get: { session.originalAudioLevel }, set: { session.setOriginalAudioLevel($0) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: "waveform").frame(width: 44, height: 44)
                    .background(KriaColor.sage, in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Original audio").font(KriaFont.body(15).weight(.semibold))
                    Text("The sound recorded with your clips").font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44)
            VStack(spacing: 4) {
                HStack {
                    Text("Volume").font(KriaFont.body(14))
                    Spacer()
                    Text("\(Int((session.originalAudioLevel * 100).rounded()))%").font(KriaFont.body(14).weight(.bold))
                        .accessibilityIdentifier("native-editor-original-audio-percent")
                }
                HStack(spacing: 10) {
                    Text("0%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    NativePaperSlider(session: session, value: levelBinding, bounds: 0...1, step: 0.01, label: "Original audio volume")
                        .frame(height: 44)
                    Text("100%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }
                .disabled(!session.canEditOriginalAudio)
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("native-editor-original-audio-volume")
            Label(note, systemImage: session.canEditOriginalAudio ? "speaker.wave.2" : "lock")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("native-editor-original-audio-note")
        }
    }

    private var note: String {
        guard session.canEditOriginalAudio else { return "Original audio can't be changed for this edit." }
        return "Plays together with your song. Use the Audio button on a clip to hear just that clip."
    }
}

/// The per-clip audio switch shown in the clip inspectors: on = this clip's own sound is heard.
struct NativeClipAudioSection: View {
    let clipID: String
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        let isOn = session.isClipAudioOn(clipID: clipID)
        Section("Clip audio") {
            Button { session.toggleClipAudio(clipID: clipID) } label: {
                Label(isOn ? "Original audio on" : "Original audio off", systemImage: isOn ? "speaker.wave.2" : "speaker.slash")
                    .frame(minHeight: 44, alignment: .leading)
            }
            .disabled(!session.canEditClipAudio || !session.canEditOriginalAudio)
            .accessibilityIdentifier("native-editor-clip-audio-toggle")
            .accessibilityValue(isOn ? "On" : "Off")
            if session.canEditClipAudio, session.originalAudioLevel <= 0 {
                Text("Original audio is off for the whole video. Turning this clip on also turns the others off, so you hear just this one.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if !session.canEditClipAudio {
                Text("Per-clip audio isn't available for this edit.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
        }
    }
}
