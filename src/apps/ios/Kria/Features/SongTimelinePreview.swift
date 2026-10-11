import AVFoundation
import SwiftUI

/// Drives the timeline's video preview (KRI-575): the take under the playhead plays muted, in step with the song,
/// so the creator can CHECK a lip-sync placement by eye. The song clock is the master; this only follows it.
///
/// Paused or scrubbing, it shows the exact frame for the song time (zero-tolerance seeks, the item swapped only when
/// the active take changes). Playing, it plays the take at rate 1 from the right offset, re-seeks when it drifts more
/// than `driftTolerance` from `clock - delta`, and moves to the next take at the boundary. It is always muted and
/// never touches the audio session: the song player owns that.
@MainActor final class SongTimelinePreviewModel: ObservableObject {
    static let driftTolerance = 0.15
    /// A re-seek is not repeated sooner than this, so a slow seek can never start a chase.
    private static let reseekCooldown: TimeInterval = 0.6
    /// Seeking takes a moment; aim this far ahead so the video lands in step instead of just behind.
    private static let seekLatencyAllowance = 0.08

    let player = AVPlayer()
    private var activeID: String?
    private var urls: [String: URL?] = [:]
    private var assets: [String: AVURLAsset] = [:]
    private var wantsToPlay = false
    private var isSeeking = false
    private var generation = 0
    private var lastExactSeek = -1.0
    private var lastReseekAt = Date.distantPast

    init() {
        player.isMuted = true
        player.volume = 0
        player.actionAtItemEnd = .pause
        player.automaticallyWaitsToMinimizeStalling = false
    }

    /// The take's original on this iPhone, looked up once (it hits the source-asset store and the file system).
    func localURL(for mediaID: String, resolve: (String) -> URL?) -> URL? {
        if let known = urls[mediaID] { return known }
        let found = resolve(mediaID)
        urls[mediaID] = found
        return found
    }

    /// Where in the active take the video is, for the view's accessibility value; nil with no item.
    var videoTime: Double? {
        let seconds = player.currentTime().seconds
        return player.currentItem != nil && seconds.isFinite ? seconds : nil
    }

    func sync(slot: SongTimelineMath.PreviewSlot, playing: Bool, resolve: (String) -> URL?, upcoming: String?) {
        guard case .clip(let id, let takeTime) = slot, let url = localURL(for: id, resolve: resolve) else {
            holdStill()
            return
        }
        if activeID != id { load(id, url: url) }
        if playing { follow(takeTime) } else { showFrame(takeTime) }
        if playing, let upcoming, let next = localURL(for: upcoming, resolve: resolve) { prefetch(upcoming, url: next) }
    }

    /// Stops the video and forgets the playing state (the view went away, or the creator started recording).
    func stop() {
        holdStill()
        generation += 1
    }

    // MARK: following the clock

    private func load(_ id: String, url: URL) {
        generation += 1
        wantsToPlay = false
        isSeeking = false
        lastExactSeek = -1
        activeID = id
        let asset = assets[id] ?? AVURLAsset(url: url)
        assets[id] = asset
        player.replaceCurrentItem(with: AVPlayerItem(asset: asset))
    }

    /// Paused or scrubbing: the exact frame for this time.
    private func showFrame(_ takeTime: Double) {
        if wantsToPlay { player.pause(); wantsToPlay = false; generation += 1; isSeeking = false }
        guard abs(takeTime - lastExactSeek) > 0.02 else { return }
        lastExactSeek = takeTime
        player.seek(to: CMTime(seconds: takeTime, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func follow(_ takeTime: Double) {
        guard wantsToPlay else { startPlaying(at: takeTime); return }
        guard !isSeeking else { return }
        let drifted = SongTimelineMath.shouldReseek(videoTime: player.currentTime().seconds, targetTime: takeTime, tolerance: Self.driftTolerance)
        if drifted, Date().timeIntervalSince(lastReseekAt) >= Self.reseekCooldown { startPlaying(at: takeTime) }
    }

    private func startPlaying(at takeTime: Double) {
        wantsToPlay = true
        isSeeking = true
        lastExactSeek = -1
        lastReseekAt = Date()
        generation += 1
        let token = generation
        let target = CMTime(seconds: takeTime + Self.seekLatencyAllowance, preferredTimescale: 600)
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.generation == token else { return }
                self.isSeeking = false
                self.player.play()
            }
        }
    }

    private func holdStill() {
        player.pause()
        wantsToPlay = false
        isSeeking = false
    }

    /// Warms the next take's file so the switch at the boundary does not wait on it.
    private func prefetch(_ id: String, url: URL) {
        guard assets[id] == nil else { return }
        let asset = AVURLAsset(url: url)
        assets[id] = asset
        Task { _ = try? await asset.load(.isPlayable) }
    }
}

/// The video preview at the top of the song timeline: the take under the playhead, or a calm tile when nothing plays.
struct SongTimelinePreview: View {
    static let height: CGFloat = 200

    @ObservedObject var clock: SongTimelineClock
    let state: SongTimelineState
    let clipNumber: (String) -> Int
    let media: (String) -> CreationAttachedMedia
    /// The take's original on this iPhone (the same lookup the take sheet uses); nil when it is not here.
    let localURL: (String) -> URL?
    let onToggle: () -> Void
    @StateObject private var model = SongTimelinePreviewModel()

    var body: some View {
        let slot = currentSlot()
        ZStack(alignment: .bottomLeading) {
            content(slot)
            chip(slot)
        }
        .frame(maxWidth: .infinity)
        .frame(height: Self.height)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .contentShape(Rectangle())
        .onTapGesture(perform: onToggle)
        .onAppear { sync() }
        .onChange(of: clock.currentTime) { sync() }
        .onChange(of: clock.isPlaying) { sync() }
        .onChange(of: state) { sync() }
        .onDisappear { model.stop() }
        // The voiceover recorder takes the audio session; a video still running underneath would fight it.
        .onReceive(NotificationCenter.default.publisher(for: .kriaAudioCaptureWillStart)) { _ in model.stop() }
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(accessibilityLabel(slot))
        .accessibilityValue(accessibilityValue(slot))
        .accessibilityHint("Double tap to play or pause the song")
        .accessibilityIdentifier("song-timeline-preview")
    }

    // MARK: slot

    private func currentSlot() -> SongTimelineMath.PreviewSlot {
        SongTimelineMath.previewSlot(
            at: clock.currentTime, segments: state.segments(), delta: { state.delta(of: $0) }, clipDuration: { state.duration(for: $0) })
    }

    private func sync() {
        model.sync(slot: currentSlot(), playing: clock.isPlaying, resolve: localURL, upcoming: upcomingClipID())
    }

    /// The visible take that starts next after the playhead, to warm up before the boundary.
    private func upcomingClipID() -> String? {
        let now = clock.currentTime
        return state.segments().filter { !$0.hidden && $0.start > now }.min { $0.start < $1.start }?.mediaID
    }

    // MARK: content

    @ViewBuilder private func content(_ slot: SongTimelineMath.PreviewSlot) -> some View {
        switch slot {
        case .clip(let id, _):
            clipContent(id)
        case .gap:
            tile("Empty spot: nothing plays here", symbol: "plus.rectangle.on.rectangle")
        case .beforeFirst:
            tile("Before your first clip", symbol: "backward.end")
        case .afterLast:
            tile("After your last clip", symbol: "forward.end")
        case .noClips:
            tile("No clip is on the song yet", symbol: "film")
        }
    }

    @ViewBuilder private func clipContent(_ id: String) -> some View {
        if model.localURL(for: id, resolve: localURL) != nil {
            SongOrderPlayerSurface(player: model.player)
        } else {
            VStack(spacing: 8) {
                SongTimelinePreviewStill(media: media(id))
                Text("This clip isn't on this iPhone")
                    .font(KriaFont.body(12).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
            }
            .padding(12)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func tile(_ text: String, symbol: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: symbol).font(.system(size: 22, weight: .regular)).foregroundStyle(KriaColor.zinc)
            Text(text).font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                .multilineTextAlignment(.center)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder private func chip(_ slot: SongTimelineMath.PreviewSlot) -> some View {
        if case .clip(let id, let takeTime) = slot {
            Text("Clip \(clipNumber(id)) · \(SongTimelineMath.timecode(takeTime))")
                .font(KriaFont.body(11).weight(.bold)).foregroundStyle(Color.white)
                .lineLimit(1)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(KriaColor.ink.opacity(0.7), in: Capsule())
                .padding(8)
        }
    }

    // MARK: accessibility

    private func accessibilityLabel(_ slot: SongTimelineMath.PreviewSlot) -> String {
        switch slot {
        case .clip(let id, _):
            let here = model.localURL(for: id, resolve: localURL) != nil
            return here ? "Preview, clip \(clipNumber(id))" : "Preview, clip \(clipNumber(id)). This clip isn't on this iPhone"
        case .gap: return "Preview. Empty spot: nothing plays here"
        case .beforeFirst: return "Preview. Before your first clip"
        case .afterLast: return "Preview. After your last clip"
        case .noClips: return "Preview. No clip is on the song yet"
        }
    }

    /// The chip's text, then how far into the clip the picture really is: what the creator is checking against the song.
    private func accessibilityValue(_ slot: SongTimelineMath.PreviewSlot) -> String {
        guard case .clip(let id, let takeTime) = slot else { return "" }
        let shown = model.videoTime ?? takeTime
        return "Clip \(clipNumber(id)) · \(SongTimelineMath.timecode(takeTime)), video at \(SongTimelineMath.timecode(shown))"
    }
}

/// A take's still, fitted into the preview tile.
private struct SongTimelinePreviewStill: View {
    let media: CreationAttachedMedia
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                Image(systemName: "video").font(.system(size: 22)).foregroundStyle(KriaColor.zinc)
            }
        }
        .frame(maxHeight: 120)
        .task(id: media.id) { image = UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path) }
    }
}
