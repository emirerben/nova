import SwiftUI

enum AttachmentStep: String, CaseIterable, Identifiable {
    case footage, voiceover, song, overlays
    var id: String { rawValue }
    var title: String { switch self { case .footage: "Footage"; case .voiceover: "Voiceover"; case .song: "Song"; case .overlays: "Overlays" } }
    var role: CreationMediaRole { switch self { case .footage: .clip; case .voiceover: .voiceover; case .song: .song; case .overlays: .visual } }
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
        VStack(alignment: .leading, spacing: 8) {
            Text(title).kriaType(.title).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
            Text(subtitle).kriaType(.body).foregroundStyle(KriaColor.mutedInk)
        }
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

enum DurationFormatter {
    static func clock(_ seconds: TimeInterval) -> String {
        let rounded = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", rounded / 60, rounded % 60)
    }
    static func seconds(_ seconds: TimeInterval) -> String { "\(max(1, Int(seconds.rounded(.up)))) seconds" }
}

struct RecordingVoiceoverView: View {
    @ObservedObject var recorder: CreationVoiceRecorder
    let stop: () -> Void
    var body: some View {
        VStack(spacing: 16) {
            Text(DurationFormatter.clock(recorder.elapsed)).kriaType(.title).monospacedDigit().foregroundStyle(KriaColor.ink)
                .accessibilityIdentifier("voiceover-duration")
            LiveMeterWaveform(level: recorder.meter).frame(height: 42).accessibilityLabel("Live microphone level")
            Button(action: stop) {
                RoundedRectangle(cornerRadius: 6, style: .continuous).fill(KriaColor.paper).frame(width: 22, height: 22)
                    .frame(width: 64, height: 64).background(KriaColor.ink, in: Circle())
            }
            .buttonStyle(.plain).accessibilityLabel("Stop recording").accessibilityIdentifier("voiceover-stop")
            Text("Stop recording").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 24)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct VoiceoverReviewView: View {
    @ObservedObject var recorder: CreationVoiceRecorder
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 14) {
                Button(action: recorder.togglePlayback) {
                    Image(systemName: recorder.isPlaying ? "pause.fill" : "play.fill").foregroundStyle(KriaColor.paper)
                        .frame(width: 44, height: 44).background(KriaColor.ink, in: Circle())
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
        .padding(16).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
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
        VStack(spacing: 8) {
            Button(recorder.isImported ? "Use voiceover" : "Use recording", action: onUse)
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: isUsing))
                .accessibilityIdentifier("voiceover-use").disabled(!canUse || isUsing)
            if recorder.isImported {
                Button("Choose another audio file", action: onChooseAnother)
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true)).disabled(isUsing).accessibilityIdentifier("voiceover-choose-another")
            } else {
                Button("Record again", action: onRecordAgain)
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true)).accessibilityIdentifier("voiceover-rerecord").disabled(isUsing)
            }
        }
    }
}

/// KRI-374: playback card for a song file the creator picked, before it is added to the project.
/// Reuses the voiceover recorder purely as a local audio player: nothing uploads until "Use this song".
struct SongReviewView: View {
    @ObservedObject var player: CreationVoiceRecorder
    var body: some View {
        HStack(spacing: 14) {
            Button(action: player.togglePlayback) {
                Image(systemName: player.isPlaying ? "pause.fill" : "play.fill").foregroundStyle(KriaColor.paper)
                    .frame(width: 44, height: 44).background(KriaColor.ink, in: Circle())
            }
            .accessibilityIdentifier("song-play").accessibilityLabel(player.isPlaying ? "Pause" : "Play")
            VStack(alignment: .leading, spacing: 3) {
                Text(player.displayName).font(KriaFont.body(15).weight(.semibold)).lineLimit(1)
                Text("\(DurationFormatter.clock(player.playbackTime)) / \(DurationFormatter.clock(player.duration))")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
            }
            Spacer()
        }
        .padding(16).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-review")
    }
}

struct SongReviewActions: View {
    let onUse: () -> Void
    let onChooseAnother: () -> Void
    let canUse: Bool
    let isUsing: Bool
    var body: some View {
        VStack(spacing: 8) {
            Button("Use this song", action: onUse).buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: isUsing))
                .accessibilityIdentifier("song-use").disabled(!canUse || isUsing)
            Button("Choose another song", action: onChooseAnother)
                .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true)).disabled(isUsing).accessibilityIdentifier("song-choose-another")
        }
    }
}

struct VoiceoverDurationWarning: View {
    let message: String
    let addFootage: () -> Void
    var body: some View {
        KriaNoticeCard(tone: .warning, title: message, action: KriaNoticeAction(title: "Add more footage", handler: addFootage))
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
                    Capsule().fill(KriaColor.sky).frame(width: max(2, (proxy.size.width - 72) / 19), height: height)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
