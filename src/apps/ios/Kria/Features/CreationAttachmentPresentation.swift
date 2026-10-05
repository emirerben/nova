import SwiftUI

enum AttachmentStep: String, CaseIterable, Identifiable {
    case footage, voiceover, overlays
    var id: String { rawValue }
    var title: String { switch self { case .footage: "Footage"; case .voiceover: "Voiceover"; case .overlays: "Overlays" } }
    var role: CreationMediaRole { switch self { case .footage: .clip; case .voiceover: .voiceover; case .overlays: .visual } }
    var accessibilityID: String { "attachment-step-\(rawValue)" }
}

/// Slide posts have ONE media pool, the photos and videos of the post. The server and the video
/// editor call it "visuals"/"overlays"; a person making a slideshow never should. Display copy only:
/// role identifiers (`CreationMediaRole.visual`, `AttachmentStep.overlays`) stay as they are.
/// Pinned by `SlidePostCopyTests`.
enum SlideMediaCopy {
    static let poolTitle = "Photos & videos"
    static let addHeading = "Add photos & videos"
    static let subtitle = "Add the photos and videos for your slides."
    static let loading = "Loading photos & videos…"
    static let retryLoading = "Retry loading photos & videos"
    static let removeFallback = "photo or video"
    static let preparingNoun = "photos & videos"
}

struct AttachmentHeading: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(KriaFont.body(28).weight(.semibold)).lineSpacing(1)
            Text(subtitle).font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk)
        }
    }
}

struct AttachmentStepProgress: View {
    let steps: [AttachmentStep]
    let current: AttachmentStep
    /// Replaces the "Overlays" step name (slide posts call that pool "Photos & videos").
    var overlaysTitle: String? = nil
    var body: some View {
        HStack(spacing: 12) {
            ForEach(Array(steps.enumerated()), id: \.element) { index, step in
                VStack(alignment: .leading, spacing: 9) {
                    Text("\(index + 1)  \(step == .overlays ? (overlaysTitle ?? step.title) : step.title)")
                        .font(KriaFont.body(13).weight(step == current ? .semibold : .regular))
                        .foregroundStyle(step == current ? KriaColor.ink : KriaColor.mutedInk)
                    Capsule().fill(step == current ? KriaColor.sky : index < (steps.firstIndex(of: current) ?? 0) ? KriaColor.sage : KriaColor.line).frame(height: 3)
                }
            }
        }.padding(.horizontal, 24).padding(.vertical, 14)
    }
}

struct AttachmentPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(KriaFont.body(16).weight(.semibold)).foregroundStyle(KriaColor.paper)
            .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.ink, in: Capsule())
            .opacity(configuration.isPressed ? 0.78 : 1)
    }
}

struct FootageDurationSummary {
    let count: Int
    let duration: TimeInterval
    let hasPendingMetadata: Bool
    let hasUnavailableDuration: Bool
    var exactDuration: TimeInterval? { count > 0 && !hasPendingMetadata && !hasUnavailableDuration ? duration : nil }
    var label: String {
        if count == 0 { return "Your footage" }
        return "Your footage · \(count) \(count == 1 ? "clip" : "clips")"
    }
    var value: String {
        if count == 0 { return "Add footage" }
        if hasPendingMetadata { return "Checking duration" }
        if hasUnavailableDuration { return "Duration unavailable" }
        return DurationFormatter.clock(duration)
    }
}

struct FootageDurationCard: View {
    let summary: FootageDurationSummary
    var body: some View {
        HStack { Text(summary.label).font(KriaFont.body(14)); Spacer(); Text(summary.value).font(KriaFont.body(20).weight(.semibold)) }
            .padding(16).background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 12))
            .accessibilityLabel("\(summary.label), \(summary.value)")
    }
}

enum DurationFormatter {
    static func clock(_ seconds: TimeInterval) -> String {
        let rounded = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", rounded / 60, rounded % 60)
    }
    static func seconds(_ seconds: TimeInterval) -> String { "\(max(1, Int(seconds.rounded(.up)))) seconds" }
}

struct VoiceoverStartView: View {
    let canRecord: Bool
    let start: () -> Void
    var body: some View {
        VStack(spacing: 16) {
            Button(action: start) {
                Image(systemName: "mic.fill").font(.system(size: 40, weight: .medium)).foregroundStyle(KriaColor.ink)
                    .frame(width: 112, height: 112).background(KriaColor.sky, in: Circle())
            }.disabled(!canRecord).accessibilityIdentifier("voiceover-record").accessibilityLabel("Record voiceover")
            Text("Tap to record").font(KriaFont.body(17).weight(.semibold))
            Text("You can listen before you use it.").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
        }.frame(maxWidth: .infinity).padding(.vertical, 16)
    }
}

struct RecordingVoiceoverView: View {
    @ObservedObject var recorder: CreationVoiceRecorder
    let stop: () -> Void
    var body: some View {
        VStack(spacing: 18) {
            Text(DurationFormatter.clock(recorder.elapsed)).font(KriaFont.body(34).weight(.semibold)).monospacedDigit().accessibilityIdentifier("voiceover-duration")
            LiveMeterWaveform(level: recorder.meter).frame(height: 42).accessibilityLabel("Live microphone level")
            Button(action: stop) { Label("Stop recording", systemImage: "stop.fill") }.buttonStyle(AttachmentPrimaryButtonStyle()).accessibilityIdentifier("voiceover-stop")
        }.frame(maxWidth: .infinity).padding(.vertical, 20)
    }
}

struct VoiceoverReviewView: View {
    @ObservedObject var recorder: CreationVoiceRecorder
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 14) {
                Button(action: recorder.togglePlayback) {
                    Image(systemName: recorder.isPlaying ? "pause.fill" : "play.fill")
                        .frame(width: 44, height: 44).background(KriaColor.sky, in: Circle())
                }
                .accessibilityIdentifier("voiceover-play").accessibilityLabel(recorder.isPlaying ? "Pause" : "Play")
                VStack(alignment: .leading, spacing: 3) {
                    Text(recorder.displayName).font(KriaFont.body(15).weight(.semibold)).lineLimit(1)
                    Text("\(DurationFormatter.clock(recorder.playbackTime)) / \(DurationFormatter.clock(recorder.duration))")
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
                        .accessibilityIdentifier("voiceover-playback-time")
                }
                Spacer()
            }
            HStack(spacing: 9) {
                Image(systemName: "waveform").foregroundStyle(KriaColor.mutedInk).accessibilityHidden(true)
                GeometryReader { proxy in
                    Capsule().fill(KriaColor.line).overlay(alignment: .leading) {
                        Capsule().fill(KriaColor.ink).frame(width: proxy.size.width * recorder.playbackProgress)
                    }
                }
                .frame(height: 4)
            }.frame(height: 20)
            .accessibilityLabel("Voiceover playback \(DurationFormatter.clock(recorder.playbackTime)) of \(DurationFormatter.clock(recorder.duration))")
        }
        .padding(16).background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 12))
    }
}

/// Kept separate from the playback card so a parent can place the footage
/// comparison warning immediately before the irreversible "Use" action.
struct VoiceoverReviewActions: View {
    @ObservedObject var recorder: CreationVoiceRecorder
    let onUse: () -> Void
    let onRecordAgain: () -> Void
    let onChooseAnother: () -> Void
    let canUse: Bool
    let isUsing: Bool
    var body: some View {
        VStack(spacing: 14) {
            Button(recorder.isImported ? "Use voiceover" : "Use recording", action: onUse).buttonStyle(AttachmentPrimaryButtonStyle()).accessibilityIdentifier("voiceover-use").disabled(!canUse || isUsing)
            if recorder.isImported {
                Button("Choose another audio file", action: onChooseAnother).font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.mutedInk).frame(minHeight: 44).disabled(isUsing).accessibilityIdentifier("voiceover-choose-another")
            } else {
                Button("Record again", action: onRecordAgain).buttonStyle(KriaSecondaryButtonStyle()).accessibilityIdentifier("voiceover-rerecord").disabled(isUsing)
            }
        }
    }
}

struct VoiceoverDurationWarning: View {
    let message: String
    let addFootage: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(KriaColor.ink)
            VStack(alignment: .leading, spacing: 7) {
                Text(message).font(KriaFont.body(13))
                Button("Add more footage", action: addFootage).font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
            }
        }.padding(14).background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 12))
    }
}

struct LiveMeterWaveform: View {
    let level: CGFloat
    var body: some View {
        GeometryReader { proxy in
            HStack(alignment: .center, spacing: 4) {
                ForEach(0..<19, id: \.self) { index in
                    let distance = abs(CGFloat(index - 9)) / 9
                    let height = max(5, (1 - distance * 0.55) * (8 + level * 34))
                    Capsule().fill(KriaColor.ink).frame(width: max(2, (proxy.size.width - 72) / 19), height: height)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
