import AVFoundation
import SwiftUI
import UIKit

/// What the song timeline needs from the chat that the vertical order list does not (KRI-561).
struct SongTimelineConfiguration {
    /// Downloads (or reuses) the creator's song and returns a LOCAL file; nil when it can't be had.
    /// The argument is the question's `song_generation`.
    var loadSong: @MainActor (Int?) async -> SongAudioFile?
}

/// The song-order question as a horizontal timeline: the song's waveform with each take sitting where it
/// matches, empty spots between takes, and a tray for takes left off the song. Drag a take to move it, tap an
/// empty spot to fill it. Used instead of `SongOrderCard` when the server advertises `song_order_placements`.
struct SongTimelineCard: View {
    static let pointsPerSecond: CGFloat = 14
    private static let rulerHeight: CGFloat = 20
    private static let waveHeight: CGFloat = 44
    private static let blockHeight: CGFloat = 64
    private static let laneTop: CGFloat = rulerHeight + 4 + waveHeight + 8
    private static let totalHeight: CGFloat = laneTop + blockHeight + 4
    private static let space = "song-timeline-space"

    let question: SongOrderQuestion
    let media: [CreationAttachedMedia]
    let projectID: UUID
    let mode: SongOrderCardMode
    let configuration: SongTimelineConfiguration
    /// Resolves a take's local original for the preview. Injected so tests can supply a fixture file.
    var previewURL: @MainActor (String) -> URL?

    @State private var state: SongTimelineState
    @State private var sheet: Sheet?
    @State private var notice: String?
    @State private var audio: AudioStatus = .loading
    @State private var bars: [Float] = []
    @State private var drag: DragInfo?
    @State private var scrollPosition = ScrollPosition(edge: .leading)
    @StateObject private var audition = NativeSongAuditionController(
        engine: AVAudioSongAuditionEngine(),
        host: .init(pauseVideo: { false }, resumeVideo: {}, activateAudioSession: {
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
            try? AVAudioSession.sharedInstance().setActive(true)
        }))

    enum AudioStatus: Equatable { case loading, ready(SongAudioFile), unavailable }
    struct DragInfo: Equatable { let mediaID: String; let startDelta: Double; var raw: Double; var lastSnap: Double? }
    enum Sheet: Identifiable, Equatable {
        case block(String), tray(String), gap(Int)
        var id: String {
            switch self {
            case .block(let id): "block-\(id)"
            case .tray(let id): "tray-\(id)"
            case .gap(let index): "gap-\(index)"
            }
        }
    }

    init(question: SongOrderQuestion, media: [CreationAttachedMedia], projectID: UUID, mode: SongOrderCardMode,
         configuration: SongTimelineConfiguration, songDurationS: Double? = nil, previewURL: (@MainActor (String) -> URL?)? = nil) {
        self.question = question
        self.media = media
        self.projectID = projectID
        self.mode = mode
        self.configuration = configuration
        self.previewURL = previewURL ?? { SongOrderPreviewSource.localURL(projectID: projectID, mediaID: $0) }
        _state = State(initialValue: SongTimelineState(question: question, media: media, songDurationS: songDurationS))
    }

    private var positions: [String: Int] { SongOrderPositions.map(media: media, question: question) }
    private func number(_ mediaID: String) -> Int { positions[mediaID] ?? 0 }
    private func attached(_ mediaID: String) -> CreationAttachedMedia {
        media.first { $0.id == mediaID } ?? CreationAttachedMedia(id: mediaID, filename: "Clip \(number(mediaID))", kind: "video", previewURL: nil)
    }

    // MARK: body

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: summary == nil ? "circle.dashed" : "checkmark.circle.fill")
                    .foregroundStyle(summary == nil ? KriaColor.mutedInk : KriaColor.success)
                Text(Self.answeredText(summary: summary))
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("song-timeline-answered")
        case .active(let isSending, let submit):
            VStack(alignment: .leading, spacing: 12) {
                header
                timeline(isSending: isSending)
                if !state.tray.isEmpty { trayRow(isSending: isSending) }
                notes
                footer(isSending: isSending, submit: submit)
            }
            .padding(14)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("song-timeline-card")
            .task { await prepareAudio() }
            .onDisappear { audition.cancel() }
            .onReceive(NotificationCenter.default.publisher(for: .kriaAudioCaptureWillStart)) { _ in audition.cancel() }
            .sheet(item: $sheet) { sheetContent($0, isSending: isSending) }
        }
    }

    static func answeredText(summary: String?) -> String {
        summary.map { "Arrangement confirmed: \($0)" } ?? "Arrangement question closed"
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Place your clips on the song")
                .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                .accessibilityAddTraits(.isHeader)
            Text("Drag a clip to move it along the song, or tap an empty spot to fill it. Tap a clip to hear that part of the song.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: audio

    private func prepareAudio() async {
        guard case .loading = audio else { return }
        guard let file = await configuration.loadSong(question.songGeneration) else { audio = .unavailable; return }
        audio = .ready(file)
        let seconds = file.durationS ?? question.songDurationS ?? 60
        let peaks = await NativeSfxWaveformPeaks.load(url: file.url, buckets: max(16, Int(seconds * 3)))
        bars = peaks ?? []
    }

    private func playSong(start: Double, length: Double) {
        guard case .ready(let file) = audio, length > 0 else { return }
        audition.begin()
        audition.update(SongAuditionRequest(url: file.url, start: start, length: length, volume: 1))
        audition.end()
    }

    /// Length of the song axis in seconds: the song itself, else whatever the clips and the audio need.
    private func axisDuration(_ segments: [SongTimelineState.Segment]) -> Double {
        var seconds = state.songDurationS ?? 0
        if case .ready(let file) = audio, let duration = file.durationS { seconds = max(seconds, duration) }
        seconds = max(seconds, segments.map(\.end).max() ?? 0)
        return max(seconds, 20)
    }

    // MARK: timeline

    private func timeline(isSending: Bool) -> some View {
        let pps = Self.pointsPerSecond
        let segments = state.segments()
        let gaps = state.gaps()
        let axis = axisDuration(segments)
        let width = CGFloat(axis) * pps
        let visible = segments.filter { !$0.hidden }
        let firstStart = visible.first?.start
        let lastEnd = visible.map(\.end).max()
        return ScrollView(.horizontal, showsIndicators: false) {
            ZStack(alignment: .topLeading) {
                ruler(axis: axis)
                waveform(axis: axis)
                if let firstStart, let lastEnd { outsideRegions(firstStart: firstStart, lastEnd: lastEnd, width: width) }
                ForEach(gaps) { gap in gapView(gap, isSending: isSending) }
                ForEach(segments) { segment in blockView(segment, isSending: isSending) }
            }
            .frame(width: width, height: Self.totalHeight, alignment: .topLeading)
            .coordinateSpace(name: Self.space)
            .padding(.horizontal, 4)
        }
        .scrollPosition($scrollPosition)
        .scrollDisabled(drag != nil)
        .frame(height: Self.totalHeight)
        .onAppear {
            if let firstStart { scrollPosition.scrollTo(x: max(0, CGFloat(firstStart) * pps - 24)) }
        }
        .accessibilityIdentifier("song-timeline-scroll")
    }

    private func ruler(axis: Double) -> some View {
        let pps = Self.pointsPerSecond
        return ZStack(alignment: .topLeading) {
            ForEach(0...Int(axis / 10), id: \.self) { index in
                Text(DurationFormatter.clock(Double(index * 10)))
                    .font(KriaFont.body(10).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize()
                    .offset(x: CGFloat(index * 10) * pps + 3, y: 0)
            }
        }
        .frame(height: Self.rulerHeight, alignment: .topLeading)
        .accessibilityHidden(true)
    }

    private func waveform(axis: Double) -> some View {
        let pps = Self.pointsPerSecond
        let audioSeconds: Double = {
            if case .ready(let file) = audio, let seconds = file.durationS { return seconds }
            return axis
        }()
        return ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(KriaColor.fill)
                .frame(width: CGFloat(axis) * pps, height: Self.waveHeight)
            if bars.isEmpty {
                // No audio (yet, or at all): a flat track keeps the same shape.
                RoundedRectangle(cornerRadius: 1.5).fill(KriaColor.line)
                    .frame(width: CGFloat(axis) * pps - 8, height: 3)
                    .offset(x: 4, y: Self.waveHeight / 2 - 1.5)
            } else {
                let drawn = bars
                Canvas { context, size in
                    let barWidth = size.width / CGFloat(drawn.count)
                    for (index, value) in drawn.enumerated() {
                        let height = max(2, CGFloat(value) * size.height)
                        let rect = CGRect(x: CGFloat(index) * barWidth + 0.5, y: (size.height - height) / 2,
                                          width: max(1, barWidth - 1.5), height: height)
                        context.fill(Path(roundedRect: rect, cornerRadius: 1), with: .color(KriaColor.zinc.opacity(0.55)))
                    }
                }
                .frame(width: CGFloat(audioSeconds) * pps, height: Self.waveHeight - 6)
                .offset(y: 3)
            }
        }
        .offset(y: Self.rulerHeight + 4)
        .accessibilityHidden(true)
    }

    /// The song before the first clip and after the last one is not in the video: dim it.
    private func outsideRegions(firstStart: Double, lastEnd: Double, width: CGFloat) -> some View {
        let pps = Self.pointsPerSecond
        let top = Self.rulerHeight + 4
        let height = Self.totalHeight - top
        let leading = CGFloat(firstStart) * pps
        let trailingX = CGFloat(lastEnd) * pps
        return ZStack(alignment: .topLeading) {
            if leading > 1 {
                Rectangle().fill(KriaColor.ink.opacity(0.10)).frame(width: leading, height: height).offset(y: top)
                if leading > 100 { outsideLabel.offset(x: 8, y: top + 4) }
            }
            if width - trailingX > 1 {
                Rectangle().fill(KriaColor.ink.opacity(0.10)).frame(width: width - trailingX, height: height).offset(x: trailingX, y: top)
                if width - trailingX > 100 { outsideLabel.offset(x: trailingX + 8, y: top + 4) }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private var outsideLabel: some View {
        Text("outside the video").font(KriaFont.body(10).weight(.medium)).foregroundStyle(KriaColor.mutedInk).fixedSize()
    }

    // MARK: blocks and gaps

    private func liveRange(_ segment: SongTimelineState.Segment) -> (start: Double, end: Double) {
        guard let drag, drag.mediaID == segment.mediaID else { return (segment.start, segment.end) }
        return state.range(of: segment.mediaID, delta: state.snapped(segment.mediaID, drag.raw))
    }

    @ViewBuilder
    private func blockView(_ segment: SongTimelineState.Segment, isSending: Bool) -> some View {
        let id = segment.mediaID
        let range = liveRange(segment)
        let pps = Self.pointsPerSecond
        let width = max(44, CGFloat(range.end - range.start) * pps)
        let item = question.item(for: id)
        let uncertain = item?.status.isUncertain ?? true
        let label = blockLabel(segment)
        // Not a `Button`: its tap is starved by the high-priority drag below. A tap gesture + the button trait
        // is the same for VoiceOver and XCUITest.
        blockFace(id: id, width: width, uncertain: uncertain, hidden: segment.hidden, dragging: drag?.mediaID == id)
        .onTapGesture { tapBlock(id, segment: segment) }
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .frame(width: width, height: Self.blockHeight)
        .offset(x: CGFloat(range.start) * pps, y: Self.laneTop)
        .zIndex(drag?.mediaID == id ? 2 : 1)
        .highPriorityGesture(dragGesture(id, isSending: isSending))
        .accessibilityLabel(label)
        .accessibilityHint("Double tap to hear the song here. Swipe up or down to move by half a second.")
        .accessibilityAdjustableAction { direction in
            guard !isSending else { return }
            switch direction {
            case .increment: nudge(id, by: 0.5)
            case .decrement: nudge(id, by: -0.5)
            @unknown default: break
            }
        }
        .accessibilityIdentifier("song-timeline-block-\(id)")
    }

    private func blockLabel(_ segment: SongTimelineState.Segment) -> String {
        let delta = state.delta(of: segment.mediaID) ?? segment.start
        var text = "Clip \(number(segment.mediaID)), starts at \(DurationFormatter.clock(max(0, delta))), drag to move"
        if segment.hidden { text += ", hidden behind another clip" }
        return text
    }

    private func blockFace(id: String, width: CGFloat, uncertain: Bool, hidden: Bool, dragging: Bool) -> some View {
        ZStack(alignment: .bottomLeading) {
            TimelineThumbnail(media: attached(id))
                .frame(width: width, height: Self.blockHeight)
                .background(dragging ? KriaColor.butter : KriaColor.sky)
            Text(hidden ? "Clip \(number(id)) · hidden" : "Clip \(number(id))")
                .font(KriaFont.body(11).weight(.bold)).foregroundStyle(Color.white)
                .lineLimit(1)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(KriaColor.ink.opacity(0.7), in: Capsule())
                .padding(4)
        }
        .frame(width: width, height: Self.blockHeight)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(alignment: .topTrailing) { if uncertain { UncertainBadge().offset(x: -3, y: 3) } }
        .opacity(hidden ? 0.5 : 1)
        .shadow(color: dragging ? KriaColor.ink.opacity(0.25) : .clear, radius: 6, y: 3)
        .contentShape(Rectangle())
    }

    private func gapView(_ gap: SongTimelineState.Gap, isSending: Bool) -> some View {
        let pps = Self.pointsPerSecond
        let width = max(8, CGFloat(gap.length) * pps)
        let hit = max(44, width)
        let label = "Empty spot \(DurationFormatter.clock(gap.start)) to \(DurationFormatter.clock(gap.end)), double tap to fill"
        return Button {
            guard !isSending else { return }
            sheet = .gap(gap.index)
            playSong(start: gap.start, length: gap.length)
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(KriaColor.zinc, style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                    .frame(width: width, height: Self.blockHeight)
                if width >= 44 {
                    VStack(spacing: 2) {
                        Image(systemName: "plus").font(.system(size: 14, weight: .semibold))
                        Text("Empty").font(KriaFont.body(10).weight(.medium))
                    }
                    .foregroundStyle(KriaColor.mutedInk)
                }
            }
            .frame(width: hit, height: Self.blockHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .frame(width: hit, height: Self.blockHeight)
        .offset(x: CGFloat(gap.start) * pps - (hit - width) / 2, y: Self.laneTop)
        .accessibilityLabel(label)
        .accessibilityIdentifier("song-timeline-gap-\(gap.index)")
    }

    // MARK: interaction

    private func tapBlock(_ id: String, segment: SongTimelineState.Segment) {
        notice = nil
        sheet = .block(id)
        playSong(start: segment.start, length: max(1, segment.end - segment.start))
    }

    private func dragGesture(_ id: String, isSending: Bool) -> some Gesture {
        DragGesture(minimumDistance: 8, coordinateSpace: .named(Self.space))
            .onChanged { value in
                guard !isSending, let startDelta = state.delta(of: id) else { return }
                if drag == nil {
                    drag = DragInfo(mediaID: id, startDelta: startDelta, raw: startDelta, lastSnap: nil)
                    notice = nil
                    audition.cancel()
                }
                guard var current = drag, current.mediaID == id else { return }
                current.raw = current.startDelta + Double(value.translation.width / Self.pointsPerSecond)
                // A light tick each time the block settles onto one of its candidate positions.
                let candidates = question.item(for: id)?.candidates.map(\.deltaS) ?? []
                let snapped = state.snapped(id, current.raw)
                if candidates.contains(snapped), current.lastSnap != snapped {
                    UIImpactFeedbackGenerator(style: .light).impactOccurred()
                }
                current.lastSnap = candidates.contains(snapped) ? snapped : nil
                drag = current
            }
            .onEnded { _ in
                guard let info = drag, info.mediaID == id else { return }
                drag = nil
                let refusal = state.move(id, toDelta: info.raw)
                notice = refusal?.message
                if refusal == nil { announceMoved(id) }
            }
    }

    private func nudge(_ id: String, by seconds: Double) {
        guard let current = state.delta(of: id) else { return }
        let refusal = state.move(id, toDelta: current + seconds)
        notice = refusal?.message
        if let refusal {
            UIAccessibility.post(notification: .announcement, argument: refusal.message)
        } else {
            announceMoved(id)
        }
    }

    private func announceMoved(_ id: String) {
        let start = max(0, state.delta(of: id) ?? 0)
        UIAccessibility.post(notification: .announcement, argument: "Clip \(number(id)) now starts at \(DurationFormatter.clock(start))")
    }

    // MARK: tray, notes, footer

    private func trayRow(isSending: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Tray").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(state.tray, id: \.self) { id in
                        Button { if !isSending { notice = nil; sheet = .tray(id) } } label: {
                            VStack(spacing: 4) {
                                CreationAttachmentThumbnail(media: attached(id))
                                    .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
                                Text("Clip \(number(id))").font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            }
                            .frame(minWidth: 52, minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Clip \(number(id)), in the tray, double tap for options")
                        .accessibilityIdentifier("song-timeline-tray-\(id)")
                    }
                }
            }
        }
    }

    @ViewBuilder private var notes: some View {
        let footer = state.footerNotice
        if case .unavailable = audio {
            Text("Song preview isn't available right now")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("song-timeline-audio-note")
        }
        if let footer {
            Text(footer.message)
                .font(KriaFont.body(12)).foregroundStyle(footer.isWarning ? KriaColor.failureText : KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("song-timeline-footer-note")
        }
        if let notice {
            Text(notice)
                .font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("song-timeline-notice")
        }
    }

    private func footer(isSending: Bool, submit: @escaping (SongOrderSubmission, String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button("Use this arrangement") {
                audition.cancel()
                let submission = state.submission(includePlacements: true)
                submit(submission, submission.message(positions: positions))
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
            .disabled(isSending)
            .accessibilityIdentifier("song-timeline-use")
            if state.isChanged {
                Button("Reset") { notice = nil; state.reset() }
                    .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .disabled(isSending)
                    .accessibilityIdentifier("song-timeline-reset")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: sheets

    @ViewBuilder private func sheetContent(_ sheet: Sheet, isSending: Bool) -> some View {
        switch sheet {
        case .gap(let index):
            let gap = state.gaps().first { $0.index == index }
            let clips = state.tray
            SongTimelineClipPicker(
                title: gap.map { "Fill \(DurationFormatter.clock($0.start)) to \(DurationFormatter.clock($0.end))" } ?? "Fill this spot",
                clips: clips.map { ($0, attached($0), number($0)) },
                onPick: { id in
                    if let gap { notice = state.place(id, inGap: gap)?.message }
                    audition.cancel()
                    self.sheet = nil
                },
                onClose: { audition.cancel(); self.sheet = nil })
        case .block(let id):
            SongTimelineTakeSheet(
                mediaID: id, title: "Clip \(number(id))", detail: Self.detail(for: question.item(for: id), start: state.delta(of: id)),
                previewURL: previewURL(id), primaryTitle: "Move to tray", primaryID: "song-timeline-to-tray",
                primary: { state.moveToTray(id); notice = nil; audition.cancel(); self.sheet = nil },
                onClose: { audition.cancel(); self.sheet = nil })
        case .tray(let id):
            SongTimelineTakeSheet(
                mediaID: id, title: "Clip \(number(id))", detail: Self.detail(for: question.item(for: id), start: nil),
                previewURL: previewURL(id), primaryTitle: "Add to the song", primaryID: "song-timeline-add-\(id)",
                primary: { notice = state.placeAnywhere(id)?.message; self.sheet = nil },
                onClose: { self.sheet = nil })
        }
    }

    /// One line about a take: where it sits, or why Kria was unsure.
    static func detail(for item: SongOrderQuestion.Item?, start: Double?) -> String {
        if let start { return "Starts at \(DurationFormatter.clock(max(0, start))) in the song" }
        switch item?.reason {
        case .tie: return "Kria wasn’t sure: two places in the song fit about equally well."
        case .weak: return "Kria found only a weak match for this clip."
        case .noEvidence: return "Kria couldn’t find this clip in your song."
        case nil: return "Not placed on the song"
        }
    }
}

// MARK: - Sheets

private struct SongTimelineClipPicker: View {
    let title: String
    let clips: [(id: String, media: CreationAttachedMedia, number: Int)]
    let onPick: (String) -> Void
    let onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(title).font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Close", action: onClose)
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("song-timeline-picker-close")
            }
            if clips.isEmpty {
                Text("There are no clips in the tray. Open a clip on the timeline and move it to the tray first.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("song-timeline-picker-empty")
            } else {
                Text("Pick a clip from the tray for this spot.").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 12) {
                        ForEach(clips, id: \.id) { clip in
                            Button { onPick(clip.id) } label: {
                                VStack(spacing: 6) {
                                    CreationAttachmentThumbnail(media: clip.media)
                                        .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
                                    Text("Clip \(clip.number)").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                }
                                .frame(minWidth: 64, minHeight: 44)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Clip \(clip.number)")
                            .accessibilityHint("Places this clip in the empty spot")
                            .accessibilityIdentifier("song-timeline-pick-\(clip.id)")
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .presentationDetents([.medium])
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-timeline-picker")
    }
}

private struct SongTimelineTakeSheet: View {
    let mediaID: String
    let title: String
    let detail: String
    let previewURL: URL?
    let primaryTitle: String
    let primaryID: String
    let primary: () -> Void
    let onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .accessibilityAddTraits(.isHeader)
                    Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button("Done", action: onClose)
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("song-timeline-sheet-done")
            }
            SongOrderPreview(mediaID: mediaID, url: previewURL, title: title)
            Button(primaryTitle, action: primary)
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
                .accessibilityIdentifier(primaryID)
            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .presentationDetents([.medium, .large])
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-timeline-take-sheet")
    }
}

/// A take's still, filling whatever frame it is given (the shared thumbnail is a fixed 52x64).
private struct TimelineThumbnail: View {
    let media: CreationAttachedMedia
    var body: some View {
        Group {
            if let image = UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path) {
                Image(uiImage: image).resizable().scaledToFill()
            } else if let url = media.previewURL {
                AsyncImage(url: url) { $0.resizable().scaledToFill() } placeholder: { Image(systemName: "video").foregroundStyle(KriaColor.mutedInk) }
            } else {
                Image(systemName: "video").foregroundStyle(KriaColor.mutedInk)
            }
        }
        .clipped()
        .accessibilityHidden(true)
    }
}
