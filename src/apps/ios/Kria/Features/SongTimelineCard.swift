import AVFoundation
import SwiftUI
import UIKit

/// What the song timeline needs from the chat that the vertical order list does not (KRI-561).
struct SongTimelineConfiguration {
    /// Downloads (or reuses) the creator's song and returns a LOCAL file; nil when it can't be had.
    /// The argument is the question's `song_generation`.
    var loadSong: @MainActor (Int?) async -> SongAudioFile?
}

/// The song-order question as an editor-style timeline (KRI-561, reworked in KRI-575): a fixed playhead over the
/// ruler, the clips and the song's waveform. Slide the lanes to move the playhead, press play to hear the song from
/// there, tap a clip or an empty spot to edit it, press and hold a clip to lift and move it. Used instead of
/// `SongOrderCard` when the server advertises `song_order_placements`.
struct SongTimelineCard: View {
    let question: SongOrderQuestion
    let media: [CreationAttachedMedia]
    let projectID: UUID
    let mode: SongOrderCardMode
    let configuration: SongTimelineConfiguration
    /// Resolves a take's local original for the preview. Injected so tests can supply a fixture file.
    var previewURL: @MainActor (String) -> URL?

    @State private var state: SongTimelineState
    @State private var sheet: Sheet?
    @State private var notice: Notice?
    @State private var audio: AudioStatus = .loading
    @State private var bars: [Float] = []
    @State private var selection: SongTimelineSelection?
    @State private var trayDrag: TrayDrag?
    @State private var surfaceFrame: CGRect = .zero
    @State private var cardFrame: CGRect = .zero
    @State private var freezer = SongTimelineScrollFreezer()
    @StateObject private var playback: SongTimelinePlayback

    /// An inline line under the timeline: a refusal (error) or a plain "moved over to make room" (information).
    struct Notice: Equatable {
        let text: String
        let isError: Bool
    }

    /// A tray clip being dragged: its floating copy follows `point` (global coordinates).
    struct TrayDrag: Equatable {
        let mediaID: String
        var point: CGPoint
    }

    enum AudioStatus: Equatable { case loading, ready(SongAudioFile), unavailable }
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
        self.previewURL = previewURL ?? { mediaID in
            #if DEBUG
            if let fixture = SongTimelineFixture.clipURL(mediaID: mediaID) { return fixture }
            #endif
            return SongOrderPreviewSource.localURL(projectID: projectID, mediaID: mediaID)
        }
        let initial = SongTimelineState(question: question, media: media, songDurationS: songDurationS)
        _state = State(initialValue: initial)
        // First paint shows content, not blank song: the playhead starts on the first clip.
        let segments = initial.segments()
        let clock = SongTimelineClock(duration: Self.axisDuration(state: initial, audioSeconds: nil),
                                      time: SongTimelineMath.initialTime(segments: segments))
        _playback = StateObject(wrappedValue: SongTimelinePlayback(clock: clock))
    }

    private var clock: SongTimelineClock { playback.clock }
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
                timelineBlock(isSending: isSending)
                notes
                footer(isSending: isSending, submit: submit)
            }
            .padding(14)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(alignment: .topLeading) { trayGhost }
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { cardFrame = $0 }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("song-timeline-card")
            .task { await prepareAudio() }
            .onDisappear { playback.pause(); endTrayDrag() }
            .onReceive(NotificationCenter.default.publisher(for: .kriaAudioCaptureWillStart)) { _ in playback.pause(); endTrayDrag() }
            .onChange(of: sheet) { _, opened in if opened != nil { playback.pause() } }
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
            Text("Slide the timeline and press play to hear the song. Tap a clip to edit it, or press and hold a clip to move it.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Everything that scrolls or drags sideways: no drag in here may open the projects drawer.
    private func timelineBlock(isSending: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            SongTimelinePreview(
                clock: clock, state: state, clipNumber: { number($0) }, media: { attached($0) },
                localURL: { previewURL($0) }, onToggle: { playback.togglePlayback() })
            SongTimelineSurface(
                state: state, question: question, clock: clock, selection: selection, bars: bars,
                songSeconds: songSeconds, isSending: isSending, externalDrag: externalDrag(),
                clipNumber: { number($0) }, media: { attached($0) }, actions: surfaceActions(isSending: isSending))
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { surfaceFrame = $0 }
            SongTimelineTransport(
                clock: clock, stops: stops, canPlay: playback.canPlay && !isSending,
                onPlayPause: { playback.togglePlayback() }, onPrevious: { jump(forward: false) }, onNext: { jump(forward: true) })
            SongTimelineActionBar(
                selection: selection, clipNumber: selectedNumber, isSending: isSending, actions: barActions)
            if !state.tray.isEmpty { trayRow(isSending: isSending) }
        }
        .excludesDrawerGesture()
    }

    private var stops: [SongTimelineMath.Stop] { SongTimelineMath.stops(segments: state.segments(), gaps: state.gaps()) }

    private var selectedNumber: Int {
        if case .block(let id)? = selection { return number(id) }
        return 0
    }

    // MARK: audio

    private func prepareAudio() async {
        guard case .loading = audio else { return }
        guard let file = await configuration.loadSong(question.songGeneration) else { audio = .unavailable; return }
        audio = .ready(file)
        playback.load(file)
        clock.setDuration(max(clock.duration, Self.axisDuration(state: state, audioSeconds: file.durationS)))
        let seconds = file.durationS ?? question.songDurationS ?? 60
        let peaks = await NativeSfxWaveformPeaks.load(url: file.url, buckets: min(4000, max(16, Int(seconds * 10))))
        bars = peaks ?? []
    }

    /// Length of the song axis in seconds: the song itself, else whatever the clips and the audio need.
    static func axisDuration(state: SongTimelineState, audioSeconds: Double?) -> Double {
        var seconds = state.songDurationS ?? 0
        if let audioSeconds { seconds = max(seconds, audioSeconds) }
        seconds = max(seconds, state.segments().map(\.end).max() ?? 0)
        return max(seconds, 20)
    }

    /// How much song the waveform lane covers: the audio file, else the whole axis.
    private var songSeconds: Double {
        if case .ready(let file) = audio, let seconds = file.durationS { return max(seconds, 1) }
        return max(clock.duration, 1)
    }

    // MARK: lane actions

    private func surfaceActions(isSending: Bool) -> SongTimelineSurfaceActions {
        SongTimelineSurfaceActions(
            selectBlock: { selectBlock($0) },
            selectGap: { selectGap($0) },
            clearSelection: { selection = nil },
            scrubBegan: { playback.scrubBegan() },
            scrubEnded: { playback.scrubEnded(resume: $0) },
            liftBegan: { id in notice = nil; playback.pause(); selection = .block(id) },
            commitMove: { id, raw in commitMove(id, toDelta: raw) },
            nudge: { id, seconds in nudge(id, by: seconds) },
            moveHere: { moveHere($0) },
            seek: { playback.seek(to: $0) })
    }

    private var barActions: SongTimelineActionBar.Actions {
        SongTimelineActionBar.Actions(
            moveToTray: { moveSelectionToTray() },
            nudgeEarlier: { if case .block(let id)? = selection { nudge(id, by: -0.5) } },
            nudgeLater: { if case .block(let id)? = selection { nudge(id, by: 0.5) } },
            here: { moveHereSelected() },
            preview: { previewSelected() },
            fill: { if case .gap(let index)? = selection { sheet = .gap(index) } })
    }

    private func selectBlock(_ id: String) {
        notice = nil
        selection = .block(id)
        if let segment = state.segments().first(where: { $0.mediaID == id }) { playback.seek(to: segment.start) }
    }

    private func selectGap(_ index: Int) {
        notice = nil
        selection = .gap(index)
        if let gap = state.gaps().first(where: { $0.index == index }) { playback.seek(to: gap.start) }
    }

    /// Previous / next clip or empty spot: the playhead jumps there and it is selected.
    private func jump(forward: Bool) {
        let all = stops
        let now = clock.currentTime
        guard let stop = forward ? SongTimelineMath.nextStop(after: now, in: all) : SongTimelineMath.previousStop(before: now, in: all) else { return }
        switch stop.kind {
        case .block(let id): selectBlock(id)
        case .gap(let index): selectGap(index)
        }
    }

    // MARK: moving clips

    /// Says what a move did: a refusal in red, "moved over to make room" in grey, nothing when it was a plain move.
    private func report(_ outcome: SongTimelineState.MoveOutcome, for id: String) {
        if let refusal = outcome.refusal {
            notice = Notice(text: refusal.message, isError: true)
            UIAccessibility.post(notification: .announcement, argument: refusal.message)
        } else if let text = outcome.message(clipNumber: { number($0) }) {
            notice = Notice(text: text, isError: false)
            UIAccessibility.post(notification: .announcement, argument: text)
        } else {
            notice = nil
            announceMoved(id)
        }
    }

    /// A clip was dropped (or sent to the playhead) at `raw`. Clips it lands on are pushed aside, never refused.
    private func commitMove(_ id: String, toDelta raw: Double, followWithPlayhead: Bool = false) {
        report(state.move(id, toDelta: raw), for: id)
        reconcileSelection()
        finishPlacing(id, followWithPlayhead: followWithPlayhead)
    }

    private func nudge(_ id: String, by seconds: Double) {
        guard let current = state.delta(of: id) else { return }
        report(state.move(id, toDelta: current + seconds), for: id)
        reconcileSelection()
    }

    /// "Move here" / "Add here": the clip starts at the playhead, whether it was on the song or in the tray.
    private func moveHere(_ id: String) {
        notice = nil
        commitMove(id, toDelta: clock.currentTime, followWithPlayhead: true)
    }

    private func moveHereSelected() {
        switch selection {
        case .block(let id)?, .tray(let id)?: moveHere(id)
        default: break
        }
    }

    private func previewSelected() {
        switch selection {
        case .block(let id)?: sheet = .block(id)
        case .tray(let id)?: sheet = .tray(id)
        default: break
        }
    }

    private func moveSelectionToTray() {
        guard case .block(let id)? = selection else { return }
        state.moveToTray(id)
        notice = nil
        selection = .tray(id)
    }

    /// Empty spots renumber whenever the arrangement changes, so a selected spot never survives an edit; a clip
    /// that left the song cannot stay selected as a placed one, and a tray clip that was placed is no longer in the tray.
    private func reconcileSelection() {
        switch selection {
        case .block(let id)?: if !state.isPlaced(id) { selection = .tray(id) }
        case .tray(let id)?: if state.isPlaced(id) { selection = .block(id) }
        case .gap?: selection = nil
        case nil: break
        }
    }

    private func announceMoved(_ id: String) {
        let start = max(0, state.delta(of: id) ?? 0)
        UIAccessibility.post(notification: .announcement, argument: "Clip \(number(id)) now starts at \(DurationFormatter.clock(start))")
    }

    // MARK: tray

    private func trayRow(isSending: Bool) -> some View {
        SongTimelineTray(
            clips: state.tray.map { ($0, attached($0), number($0)) }, isSending: isSending, selectedID: selectedTrayID,
            drag: trayDragCallbacks, onTap: { id in notice = nil; selection = .tray(id) }, onMoveHere: { moveHere($0) })
            .background(SongTimelineScrollFreezeProbe(freezer: freezer))
    }

    private var selectedTrayID: String? {
        if case .tray(let id)? = selection { return id }
        return nil
    }

    private var trayDragCallbacks: SongTimelineTray.Drag {
        SongTimelineTray.Drag(
            began: { id in
                notice = nil; playback.pause(); freezer.freeze(); selection = .tray(id)
                UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            },
            moved: { id, point in trayDrag = TrayDrag(mediaID: id, point: point) },
            ended: { id, point in dropTrayClip(id, at: point) },
            cancelled: { endTrayDrag() })
    }

    private func endTrayDrag() {
        freezer.thaw()
        trayDrag = nil
    }

    /// Released over the lanes: the clip starts at the song time under the finger. Anywhere else cancels.
    private func dropTrayClip(_ id: String, at point: CGPoint) {
        endTrayDrag()
        guard SongTimelineMath.isDropOverLanes(point, lanes: laneDropRect) else { return }
        commitMove(id, toDelta: songTime(atGlobalX: point.x), followWithPlayhead: true)
    }

    /// The lanes plus the ruler above them: the generous target for a drop.
    private var laneDropRect: CGRect {
        CGRect(x: surfaceFrame.minX, y: surfaceFrame.minY + SongTimelineSurface.Layout.rulerTop,
               width: surfaceFrame.width, height: SongTimelineSurface.Layout.total - SongTimelineSurface.Layout.rulerTop)
    }

    private func songTime(atGlobalX x: CGFloat) -> Double {
        SongTimelineMath.time(atX: x - surfaceFrame.minX, now: clock.currentTime, playheadX: surfaceFrame.width / 2,
                              pixelsPerSecond: clock.pixelsPerSecond)
    }

    /// While a tray clip is over the lanes the timeline previews where it would land.
    private func externalDrag() -> SongTimelineExternalDrag? {
        guard let trayDrag, SongTimelineMath.isDropOverLanes(trayDrag.point, lanes: laneDropRect) else { return nil }
        return SongTimelineExternalDrag(mediaID: trayDrag.mediaID, rawDelta: songTime(atGlobalX: trayDrag.point.x))
    }

    /// The floating copy of the tray clip under the finger.
    @ViewBuilder private var trayGhost: some View {
        if let trayDrag {
            CreationAttachmentThumbnail(media: attached(trayDrag.mediaID))
                .background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .shadow(color: KriaColor.ink.opacity(0.25), radius: 6, y: 3)
                .position(x: trayDrag.point.x - cardFrame.minX, y: trayDrag.point.y - cardFrame.minY - 44)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
                .accessibilityIdentifier("song-timeline-tray-ghost")
        }
    }

    /// After a clip lands on the song: select it and put the playhead on it.
    private func finishPlacing(_ id: String, followWithPlayhead: Bool = true) {
        if state.isPlaced(id) {
            selection = .block(id)
            if followWithPlayhead, let segment = state.segments().first(where: { $0.mediaID == id }) { playback.seek(to: segment.start) }
        } else if case .block? = selection {
            selection = .tray(id)
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
            Text(notice.text)
                .font(KriaFont.body(12).weight(.semibold)).foregroundStyle(notice.isError ? KriaColor.failureText : KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("song-timeline-notice")
        }
    }

    private func footer(isSending: Bool, submit: @escaping (SongOrderSubmission, String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button("Use this arrangement") {
                playback.pause()
                let submission = state.submission(includePlacements: true)
                submit(submission, submission.message(positions: positions))
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
            .disabled(isSending)
            .accessibilityIdentifier("song-timeline-use")
            if state.isChanged {
                Button("Reset") { notice = nil; selection = nil; state.reset() }
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
                    if let gap { report(state.place(id, inGap: gap), for: id) }
                    finishPlacing(id)
                    self.sheet = nil
                },
                onClose: { self.sheet = nil })
        case .block(let id):
            SongTimelineTakeSheet(
                mediaID: id, title: "Clip \(number(id))", detail: Self.detail(for: question.item(for: id), start: state.delta(of: id)),
                previewURL: previewURL(id), primaryTitle: "Move to tray", primaryID: "song-timeline-to-tray",
                primary: { state.moveToTray(id); notice = nil; selection = .tray(id); self.sheet = nil },
                onClose: { self.sheet = nil })
        case .tray(let id):
            SongTimelineTakeSheet(
                mediaID: id, title: "Clip \(number(id))", detail: Self.detail(for: question.item(for: id), start: nil),
                previewURL: previewURL(id), primaryTitle: "Add to the song", primaryID: "song-timeline-add-\(id)",
                primary: { report(state.placeAnywhere(id), for: id); finishPlacing(id); self.sheet = nil },
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
