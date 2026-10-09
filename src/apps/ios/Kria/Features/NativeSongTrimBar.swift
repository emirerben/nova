import SwiftUI

/// Where each handle of the song trim bar may sit (KRI-561). Pure so the limits are testable.
enum NativeSongTrimRange {
    /// Lip-sync trims cut the video; the server refuses under this much (`NativeLipsyncSongTrim.minimumTotalS`).
    static let lipsyncMinimumS = NativeLipsyncSongTrim.minimumTotalS

    /// The start handle, given where the end handle is.
    /// Background: anywhere from the song's start to a second before the end (and no later than the song's
    /// own limit). Lip-sync: only inward from the current start, leaving the minimum video.
    static func start(_ controls: NativeEditorYourSongControls, end: Double) -> ClosedRange<Double> {
        switch controls.mode {
        case .background:
            let upper = min(controls.maxStartS ?? .infinity, end - NativeUserSong.minPlayableS)
            return controls.barStartS...max(controls.barStartS, upper)
        case .lipsync:
            return controls.startS...max(controls.startS, end - lipsyncMinimumS)
        }
    }

    /// The end handle, given where the start handle is.
    /// Background: a second after the start, up to where the video or the song would stop anyway.
    /// Lip-sync: only inward from the current end, leaving the minimum video.
    static func end(_ controls: NativeEditorYourSongControls, start: Double) -> ClosedRange<Double> {
        switch controls.mode {
        case .background:
            let upper = min(start + controls.videoLengthS, controls.songDurationS ?? .infinity)
            return min(start + NativeUserSong.minPlayableS, upper)...max(upper, start + NativeUserSong.minPlayableS)
        case .lipsync:
            return min(start + lipsyncMinimumS, controls.endS)...max(controls.endS, start + lipsyncMinimumS)
        }
    }

    static func clamp(_ value: Double, to range: ClosedRange<Double>) -> Double {
        min(max(value, range.lowerBound), range.upperBound)
    }
}

/// What the trim bar does with a handle. Background handles edit the song live (one undo step per drag);
/// a lip-sync trim cuts the video, so it applies once, on release (`commit`), and only previews the audio
/// while a handle is held.
struct NativeSongTrimActions {
    let begin: () -> Void
    let moveStart: (Double) -> Void
    let moveEnd: (Double) -> Void
    let preview: (_ start: Double, _ end: Double) -> Void
    let commit: (_ start: Double, _ end: Double) -> Void
    let end: () -> Void
}

/// The two-handle song trim bar: the song (or, for lip-sync, the window it was filmed to) as a waveform with
/// a highlighted range between a start and an end handle. Each handle keeps a 44pt hit target and is a
/// VoiceOver adjustable element (1 second per step).
struct NativeSongTrimBar: View {
    let controls: NativeEditorYourSongControls
    let audioURL: URL?
    let actions: NativeSongTrimActions

    @StateObject private var waveforms = NativeSfxWaveformStore()
    private enum Handle { case start, end }
    @State private var active: Handle?
    @State private var origin: Double = 0
    /// Lip-sync only: the range being dragged. Nothing is applied until release.
    @State private var draft: (start: Double, end: Double)?
    @GestureState private var isDragging = false

    private let barHeight: CGFloat = 52
    private let handleWidth: CGFloat = 16
    private static let waveformKey = "user-song"

    private var shownStart: Double { draft?.start ?? controls.startS }
    private var shownEnd: Double { draft?.end ?? controls.endS }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Trim").font(KriaFont.body(14))
                Spacer()
                Text(NativeEditorYourSong.windowLabel(start: shownStart, end: shownEnd))
                    .font(KriaFont.body(14).weight(.bold))
                    .accessibilityIdentifier("native-editor-your-song-range")
            }
            if controls.barEndS > controls.barStartS {
                bar
                HStack {
                    Text("Starts at \(NativeEditorYourSong.timecode(shownStart))")
                        .accessibilityIdentifier("native-editor-your-song-start-label")
                    Spacer()
                    Text("Ends at \(NativeEditorYourSong.timecode(shownEnd))")
                        .accessibilityIdentifier("native-editor-your-song-end-label")
                }
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if controls.mode == .lipsync {
                Label(NativeEditorYourSong.lipSyncTrimHintCopy, systemImage: "scissors")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("native-editor-your-song-trim-hint")
            } else if controls.songEndsBeforeVideo {
                Text(controls.hasCreatorEnd ? NativeEditorYourSong.songStoppedCopy : NativeEditorYourSong.songEndsEarlyCopy)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("native-editor-your-song-ends-early")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-your-song-start")
        .task(id: audioURL) { await waveforms.load(key: Self.waveformKey, url: audioURL) }
    }

    private var span: Double { max(0.0001, controls.barEndS - controls.barStartS) }

    /// The waveform of just the stretch the bar covers (the whole song, or a lip-sync window).
    private var visibleBars: [Float]? {
        guard let bars = waveforms.bars[Self.waveformKey], !bars.isEmpty,
              let duration = controls.songDurationS, duration > 0 else { return nil }
        let low = Int((controls.barStartS / duration * Double(bars.count)).rounded(.down))
        let high = Int((controls.barEndS / duration * Double(bars.count)).rounded(.up))
        let slice = bars[min(max(0, low), bars.count - 1)..<min(max(low + 1, high), bars.count)]
        return slice.isEmpty ? nil : Array(slice)
    }

    private var bar: some View {
        GeometryReader { geo in
            let usable = max(1, geo.size.width - handleWidth * 2)
            let x: (Double) -> CGFloat = { handleWidth + CGFloat(($0 - controls.barStartS) / span) * usable }
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 10).fill(KriaColor.softZinc)
                Group {
                    if let bars = visibleBars { NativeSfxWaveformBars(bars: bars) }
                    else { Capsule().fill(KriaColor.line).frame(height: 4) }
                }
                .padding(.horizontal, handleWidth)
                RoundedRectangle(cornerRadius: 8).fill(KriaColor.sky.opacity(0.22))
                    .frame(width: max(0, x(shownEnd) - x(shownStart) + handleWidth * 2))
                    .offset(x: x(shownStart) - handleWidth)
                    .allowsHitTesting(false)
                // Close handles must not steal each other's touches: past the midpoint between them, each
                // hands over to the other (the start owns the left of it, the end the right).
                let mid = (x(shownStart) + x(shownEnd)) / 2
                handle(.start, center: x(shownStart), box: min(x(shownStart) - 22, mid - 44)...min(x(shownStart) + 22, mid), usable: usable)
                handle(.end, center: x(shownEnd), box: max(x(shownEnd) - 22, mid)...max(x(shownEnd) + 22, mid + 44), usable: usable)
            }
        }
        .frame(height: barHeight)
        .onChange(of: isDragging) { _, dragging in if !dragging { finish() } }
        .onDisappear { finish() }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-your-song-start-bar")
    }

    private func value(of handle: Handle) -> Double { handle == .start ? shownStart : shownEnd }

    private func handle(_ which: Handle, center: CGFloat, box: ClosedRange<CGFloat>, usable: CGFloat) -> some View {
        let isStart = which == .start
        return Color.clear
            .overlay {
                RoundedRectangle(cornerRadius: 5).fill(KriaColor.sky)
                    .overlay(Capsule().fill(.white).frame(width: 2, height: 18))
                    .frame(width: handleWidth, height: barHeight)
                    .offset(x: center - (box.lowerBound + box.upperBound) / 2)
            }
            .frame(width: box.upperBound - box.lowerBound, height: barHeight)
            .contentShape(Rectangle())
            .offset(x: box.lowerBound)
            // Global space: the handle moves under the finger, so a local translation would shrink as it goes.
            .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
                .updating($isDragging) { _, state, _ in state = true }
                .onChanged { gesture in
                    if active == nil { begin(which) }
                    guard active == which else { return }
                    move(which, to: origin + Double(gesture.translation.width / usable) * span)
                }
                .onEnded { _ in finish() })
            .accessibilityElement()
            .accessibilityLabel(isStart ? "Song start" : "Song end")
            .accessibilityValue(isStart ? "Starts at \(NativeEditorYourSong.timecode(shownStart))"
                                        : "Ends at \(NativeEditorYourSong.timecode(shownEnd))")
            .accessibilityHint(controls.mode == .lipsync ? "Adjust to cut your video to this point. You can undo it before saving."
                                                        : "Adjust to move this point one second at a time.")
            .accessibilityIdentifier(isStart ? "native-editor-your-song-trim-start" : "native-editor-your-song-trim-end")
            .accessibilityAdjustableAction { direction in
                let step = direction == .increment ? 1.0 : -1.0
                actions.begin()
                let next = NativeSongTrimRange.clamp(value(of: which) + step, to: range(of: which, start: controls.startS, end: controls.endS))
                switch controls.mode {
                case .background: which == .start ? actions.moveStart(next) : actions.moveEnd(next)
                case .lipsync: which == .start ? actions.commit(next, controls.endS) : actions.commit(controls.startS, next)
                }
                actions.end()
            }
    }

    private func range(of handle: Handle, start: Double, end: Double) -> ClosedRange<Double> {
        handle == .start ? NativeSongTrimRange.start(controls, end: end) : NativeSongTrimRange.end(controls, start: start)
    }

    private func begin(_ which: Handle) {
        active = which
        origin = value(of: which)
        if controls.mode == .lipsync { draft = (controls.startS, controls.endS) }
        actions.begin()
    }

    private func move(_ which: Handle, to raw: Double) {
        let start = shownStart, end = shownEnd
        let next = NativeSongTrimRange.clamp(raw, to: range(of: which, start: start, end: end))
        switch controls.mode {
        case .background:
            which == .start ? actions.moveStart(next) : actions.moveEnd(next)
        case .lipsync:
            let range = which == .start ? (next, end) : (start, next)
            draft = range
            actions.preview(range.0, range.1)
        }
    }

    /// Closes the drag once, however it stopped (release, system cancel, the view going away).
    private func finish() {
        guard active != nil else { return }
        active = nil
        if controls.mode == .lipsync, let range = draft { actions.commit(range.start, range.end) }
        draft = nil
        actions.end()
    }
}
