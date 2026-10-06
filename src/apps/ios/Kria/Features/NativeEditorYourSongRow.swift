import SwiftUI

/// The creator's own song in the Sounds tab (KRI-374). It is a project asset the
/// server pinned to this edit, not a catalog track, so there is no track ID or
/// alignment to offer. KRI-428 adds what the creator can change: song volume,
/// where the song starts (background mode only; lip-sync keeps it where the
/// takes were filmed) and removing the song (camera audio plays instead).
/// Every control is gated by the server's `user_song.*` capabilities and edits
/// the editor document, so Undo and Save work like any other lane.
struct NativeEditorYourSongRow: View {
    let song: NativeEditorYourSong
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: "music.note").frame(width: 44, height: 44)
                    .background(KriaColor.sage, in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(song.title).font(KriaFont.body(15).weight(.semibold))
                    Text(song.window).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    if let mode = song.mode {
                        Text(mode).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(song.accessibilitySummary)
            .accessibilityIdentifier("native-editor-your-song")
            if let controls = session.yourSongControls {
                volume(controls)
                start(controls)
                if controls.canRemove { removeButton }
            }
            Label(NativeEditorYourSong.helperCopy, systemImage: "speaker.slash")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("native-editor-your-song-note")
        }
        // Leaving the Sounds panel (or removing the song) ends any audition in progress.
        .onDisappear { session.songAudition.cancel() }
    }

    private var volumeBinding: Binding<Double> {
        Binding(get: { session.yourSongControls?.volume ?? 1 }, set: { session.setUserSongVolume($0) })
    }

    private func volume(_ controls: NativeEditorYourSongControls) -> some View {
        VStack(spacing: 4) {
            HStack {
                Text("Volume").font(KriaFont.body(14))
                Spacer()
                Text("\(Int((controls.volume * 100).rounded()))%").font(KriaFont.body(14).weight(.bold))
            }
            HStack(spacing: 10) {
                Text("0%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                NativePaperSlider(session: session, value: volumeBinding, bounds: 0...1, step: 0.01, label: "Song volume")
                    .frame(height: 44)
                Text("100%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
            .disabled(!controls.canEditVolume)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-your-song-volume")
    }

    @ViewBuilder
    private func start(_ controls: NativeEditorYourSongControls) -> some View {
        switch controls.mode {
        case .background:
            NativeSongWindowBar(controls: controls, audioURL: session.userSongAudioURL,
                                onChange: { session.moveSongStart($0) },
                                onBegin: { session.beginSongStartDrag() },
                                onEnd: { session.endSongStartDrag() })
                .disabled(!controls.canEditStart)
        case .lipsync:
            Label(NativeEditorYourSong.lipSyncLockCopy, systemImage: "lock")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("native-editor-your-song-start-locked")
        }
    }

    private var removeButton: some View {
        Button(role: .destructive) { session.removeUserSong() } label: {
            Label("Remove song", systemImage: "trash").frame(maxWidth: .infinity, minHeight: 44)
        }
        .buttonStyle(KriaSecondaryButtonStyle())
        .foregroundStyle(KriaColor.failureText)
        .accessibilityIdentifier("native-editor-your-song-remove")
        .accessibilityHint("Your camera audio plays instead. You can undo this before saving.")
    }
}

/// Start-point bar for a background song: the whole song as a waveform, and a window the
/// length of the video that slides over it. Drag the bar, or use VoiceOver adjust (1 second).
/// Until the song file resolves the strip stays plain; without a known song length the bar is
/// replaced by its label alone.
struct NativeSongWindowBar: View {
    let controls: NativeEditorYourSongControls
    let audioURL: URL?
    let onChange: (Double) -> Void
    let onBegin: () -> Void
    let onEnd: () -> Void

    @StateObject private var waveforms = NativeSfxWaveformStore()
    @State private var dragOrigin: Double?
    /// Resets to false when the drag ends OR is cancelled (a system interruption never calls `onEnded`).
    @GestureState private var isDragging = false

    private let barHeight: CGFloat = 52
    private let minimumWindowWidth: CGFloat = 28
    private static let waveformKey = "user-song"

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Start").font(KriaFont.body(14))
                Spacer()
                Text(controls.startLabel).font(KriaFont.body(14).weight(.bold))
                    .accessibilityIdentifier("native-editor-your-song-start-label")
            }
            if let duration = controls.songDurationS, duration > 0 {
                bar(duration: duration)
            }
            if controls.songEndsBeforeVideo {
                Text(NativeEditorYourSong.songEndsEarlyCopy)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("native-editor-your-song-ends-early")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-your-song-start")
        .task(id: audioURL) { await waveforms.load(key: Self.waveformKey, url: audioURL) }
    }

    private func bar(duration: Double) -> some View {
        GeometryReader { geo in
            let width = max(1, geo.size.width)
            // The bar is the whole song. The window starts where the song does and is as long as the video,
            // or what is left of the song when that is shorter (so it shrinks as the start moves right).
            let windowWidth = min(width, max(minimumWindowWidth, CGFloat(controls.windowLengthS / duration) * width))
            let x = min(CGFloat(controls.startS / duration) * width, width - windowWidth)
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 10).fill(KriaColor.softZinc)
                Group {
                    if let bars = waveforms.bars[Self.waveformKey], !bars.isEmpty { NativeSfxWaveformBars(bars: bars) }
                    else { Capsule().fill(KriaColor.line).frame(height: 4) }
                }
                .padding(.horizontal, 4)
                RoundedRectangle(cornerRadius: 8).stroke(KriaColor.sky, lineWidth: 2)
                    .background(KriaColor.sky.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
                    .frame(width: windowWidth)
                    .offset(x: x)
            }
            .contentShape(Rectangle())
            .accessibilityElement()
            .accessibilityLabel("Song start")
            .accessibilityValue(controls.startLabel)
            .accessibilityIdentifier("native-editor-your-song-start-bar")
            .accessibilityAdjustableAction { direction in
                onBegin()
                onChange(controls.startS + (direction == .increment ? 1 : -1))
                onEnd()
            }
            .gesture(DragGesture(minimumDistance: 0)
                .updating($isDragging) { _, state, _ in state = true }
                .onChanged { gesture in
                    if dragOrigin == nil { dragOrigin = controls.startS; onBegin() }
                    // One bar width is the whole song.
                    let seconds = Double(gesture.translation.width / width) * duration
                    onChange((dragOrigin ?? controls.startS) + seconds)
                }
                .onEnded { _ in finishDrag(if: true) })
        }
        .frame(height: barHeight)
        .onChange(of: isDragging) { _, dragging in finishDrag(if: !dragging) }
        .onDisappear { finishDrag(if: true) }
    }

    /// Closes the undo transaction a drag opened, once, however the drag stopped.
    private func finishDrag(if condition: Bool) {
        guard condition, dragOrigin != nil else { return }
        dragOrigin = nil
        onEnd()
    }
}
