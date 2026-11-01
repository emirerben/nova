import AVFoundation
import SwiftUI
import UIKit

/// KRI-644: the selected video slide plays by itself, looping, like part of the slide: no native transport over the
/// canvas, only a small pause/play control. The workspace owns ONE of these, so only the selected slide's video can
/// play, and every load carries a token so a load that finishes after its slide lost the selection is dropped.
@MainActor final class SlidePostVideoPlayback: ObservableObject {
    enum Phase: Equatable { case idle, loading, ready, failed }

    /// Made on first use: the workspace keeps this object in `@State` (so only the video views observe it), and that
    /// initial value is rebuilt and dropped on every parent render, so constructing one must stay free.
    var player: AVPlayer {
        if let made { return made }
        let player = makePlayer()
        made = player
        return player
    }
    private var made: AVPlayer?
    /// The asset the player is loading or showing; nil when nothing is.
    @Published private(set) var assetID: String?
    @Published private(set) var phase: Phase = .idle
    /// The viewer paused it (or has Auto-Play Video Previews off), so it waits for the play control.
    @Published private(set) var paused = false
    /// What the player is actually doing, for the control's accessibility value.
    @Published private(set) var timeControlStatus: AVPlayer.TimeControlStatus = .paused
    /// The decoded picture's size once the item is ready (KRI-1138): a turned video is laid out at its own cover size so the
    /// pixels beyond the frame rotate into view, as they do in the export. nil until then.
    @Published private(set) var videoSize: CGSize?
    /// The workspace is on screen, the app is active and nothing covers the preview.
    private var allowed = false
    private var token = 0
    private var statusObservation: NSKeyValueObservation?
    private var timeControlObservation: NSKeyValueObservation?
    nonisolated(unsafe) private var endObserver: NSObjectProtocol?
    nonisolated(unsafe) private var failureObserver: NSObjectProtocol?

    private func makePlayer() -> AVPlayer {
        let player = AVPlayer()
        player.actionAtItemEnd = .none
        // A looping preview must not keep the phone awake on its own.
        player.preventsDisplaySleepDuringVideoPlayback = false
        timeControlObservation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] observed, _ in
            let status = observed.timeControlStatus
            Task { @MainActor [weak self] in self?.timeControlStatus = status }
        }
        return player
    }

    deinit {
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        if let failureObserver { NotificationCenter.default.removeObserver(failureObserver) }
    }

    /// Stops whatever was playing and marks `assetID` as loading. Pass the returned token to `attach`.
    func begin(assetID: String) -> Int {
        stop()
        self.assetID = assetID
        phase = .loading
        paused = !UIAccessibility.isVideoAutoplayEnabled
        return token
    }

    /// Plays `item` once it is ready, unless the selection moved on (or the workspace went away) since `begin`.
    func attach(_ item: AVPlayerItem, token: Int) {
        guard token == self.token, assetID != nil else { return }
        statusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] observed, _ in
            let status = observed.status
            Task { @MainActor [weak self] in self?.itemStatusChanged(status, token: token) }
        }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.loop(token: token) }
        }
        failureObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemFailedToPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.itemStatusChanged(.failed, token: token) }
        }
        player.replaceCurrentItem(with: item)
    }

    /// Stops the load `begin` returned `token` for, unless a newer one has replaced it.
    func stop(token: Int) {
        if token == self.token { stop() }
    }

    /// Detaches the item (not just pauses it) so nothing from the previous slide can keep playing or reappear.
    func stop() {
        token += 1
        statusObservation?.invalidate(); statusObservation = nil
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        if let failureObserver { NotificationCenter.default.removeObserver(failureObserver) }
        endObserver = nil; failureObserver = nil
        made?.pause()
        made?.replaceCurrentItem(with: nil)
        // A player that failed itself (not just its item, e.g. after a media-services reset) never plays again.
        if made?.status == .failed { made = nil; timeControlObservation = nil }
        assetID = nil
        videoSize = nil
        phase = .idle
    }

    func setAllowed(_ allowed: Bool) {
        self.allowed = allowed
        apply()
    }

    func togglePaused() {
        guard phase == .ready else { return }
        paused.toggle()
        apply()
    }

    private func apply() {
        if allowed, phase == .ready, !paused { player.play() } else { made?.pause() }
    }

    private func itemStatusChanged(_ status: AVPlayerItem.Status, token: Int) {
        guard token == self.token else { return }
        switch status {
        case .readyToPlay:
            guard phase == .loading else { return }
            let size = player.currentItem?.presentationSize ?? .zero
            videoSize = size.width > 0 && size.height > 0 ? size : nil
            phase = .ready
            apply()
        case .failed:
            phase = .failed
            made?.pause()
        default: break
        }
    }

    private func loop(token: Int) {
        guard token == self.token else { return }
        player.seek(to: .zero)
        apply()
    }
}

/// The selected video's picture, shown only once its own item is ready so a previous slide's frame never lingers.
struct SlidePostVideoPicture: View {
    @ObservedObject var playback: SlidePostVideoPlayback
    let assetID: String
    /// KRI-1138: the slide's turn. The poster beneath draws the backdrop; this turns the picture about the canvas centre.
    var tilt: SlidePostTilt? = nil

    var body: some View {
        if playback.assetID == assetID, playback.phase == .ready {
            if let tilt, !tilt.isUpright {
                GeometryReader { proxy in
                    let media = Self.coverSize(video: playback.videoSize, canvas: proxy.size)
                    SlidePostVideoSurface(player: playback.player, gravity: playback.videoSize == nil ? .resizeAspectFill : .resizeAspect)
                        .frame(width: media.width, height: media.height)
                        .rotationEffect(.degrees(tilt.rotationDeg))
                        .position(x: proxy.size.width / 2, y: proxy.size.height / 2)
                }
                .clipped()
            } else {
                SlidePostVideoSurface(player: playback.player)
            }
        }
    }

    /// The video scaled to cover `canvas` (the cover scale `SlidePostTiltGeometry.mediaSize` uses), or the canvas itself
    /// while its size is unknown.
    static func coverSize(video: CGSize?, canvas: CGSize) -> CGSize {
        guard let video else { return canvas }
        return SlidePostTiltGeometry.mediaSize(imageSize: video, crop: nil, canvas: canvas, fit: false)
    }
}

/// The player's picture with no controls. It fills the slide like the poster under it and the exported MP4 (cover
/// crop), and draws nothing until a frame is decoded, so the poster shows while a video loads.
struct SlidePostVideoSurface: UIViewRepresentable {
    let player: AVPlayer
    /// Aspect-fill by default; a turned video is laid out at its own cover size and shows whole (`resizeAspect`).
    var gravity: AVLayerVideoGravity = .resizeAspectFill

    func makeUIView(context: Context) -> Surface {
        let view = Surface()
        view.playerLayer.player = player
        view.playerLayer.videoGravity = gravity
        return view
    }

    func updateUIView(_ view: Surface, context: Context) {
        if view.playerLayer.player !== player { view.playerLayer.player = player }
        if view.playerLayer.videoGravity != gravity { view.playerLayer.videoGravity = gravity }
    }

    final class Surface: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }

        override init(frame: CGRect) {
            super.init(frame: frame)
            playerLayer.videoGravity = .resizeAspectFill
            isUserInteractionEnabled = false
            isAccessibilityElement = false
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    }
}

/// Loading spinner, failure notice with Try again, and the small pause/play control for the selected video slide.
/// The control sits in the slide's bottom-leading corner, clear of the slide counter and the AI button.
struct SlidePostVideoControls: View {
    @ObservedObject var playback: SlidePostVideoPlayback
    let assetID: String
    /// False while the text or draw tools own the canvas; the video keeps playing but the control steps aside.
    let showsToggle: Bool
    let onRetry: () -> Void
    @State private var showsSpinner = false

    private var current: Bool { playback.assetID == assetID }
    private var loading: Bool { current && playback.phase == .loading }

    var body: some View {
        ZStack {
            if loading, showsSpinner {
                ProgressView()
                    .tint(.white)
                    .padding(10)
                    .background(KriaColor.mutedInk.opacity(0.7), in: Circle())
                    .allowsHitTesting(false)
                    .accessibilityLabel("Loading video")
                    .accessibilityIdentifier("slidepost-video-loading")
            } else if current, playback.phase == .failed {
                VStack(spacing: 8) {
                    Text("Couldn’t play this video")
                        .font(KriaFont.body(13).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .accessibilityIdentifier("slidepost-video-failed")
                    Button("Try again", action: onRetry)
                        .font(KriaFont.body(13))
                        .buttonStyle(KriaSecondaryButtonStyle())
                        .accessibilityIdentifier("slidepost-video-retry")
                }
                .padding(12)
                .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottomLeading) {
            if current, playback.phase == .ready, showsToggle { toggle }
        }
        // Fast local loads never flash a spinner over the poster.
        .task(id: loading) {
            showsSpinner = false
            guard loading else { return }
            try? await Task.sleep(for: .milliseconds(400))
            if !Task.isCancelled { showsSpinner = true }
        }
    }

    private var toggle: some View {
        Button { playback.togglePaused() } label: {
            Image(systemName: playback.paused ? "play.fill" : "pause.fill")
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 32, height: 32)
                .background(KriaColor.mutedInk.opacity(0.85), in: Circle())
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(4)
        .accessibilityLabel(playback.paused ? "Play video" : "Pause video")
        .accessibilityValue(statusValue)
        .accessibilityIdentifier("slidepost-video-toggle")
    }

    private var statusValue: String {
        switch playback.timeControlStatus {
        case .playing: "Playing"
        case .waitingToPlayAtSpecifiedRate: "Loading"
        default: "Paused"
        }
    }
}
