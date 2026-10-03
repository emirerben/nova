import AVFoundation
import SwiftUI

/// One-at-a-time audition for sound effects (catalog rows, placed sounds, trimmed preview).
/// Loading lives on the tapped control (`loadingID`); a failed load is remembered in
/// `failedIDs` so the row can say "Preview unavailable · Retry".
@MainActor
final class NativeSfxAuditionPlayer: ObservableObject {
    @Published private(set) var playingID: String?
    @Published private(set) var loadingID: String?
    @Published private(set) var failedIDs: Set<String> = []

    private var player: AVPlayer?
    private var statusObservation: NSKeyValueObservation?
    private var endObserver: NSObjectProtocol?

    /// Starts `id`, or stops it when it is already playing/loading (tap-to-toggle).
    func toggle(id: String, url: URL?, range: ClosedRange<Double>? = nil, gain: Double = 1) {
        if playingID == id || loadingID == id { stop(); return }
        play(id: id, url: url, range: range, gain: gain)
    }

    func play(id: String, url: URL?, range: ClosedRange<Double>? = nil, gain: Double = 1) {
        stop()
        failedIDs.remove(id)
        guard let url else { failedIDs.insert(id); return }
        loadingID = id
        let item = AVPlayerItem(url: url)
        if let range { item.forwardPlaybackEndTime = CMTime(seconds: range.upperBound, preferredTimescale: 600) }
        let player = AVPlayer(playerItem: item)
        player.volume = Float(min(max(0, gain), 1))
        self.player = player
        statusObservation = item.observe(\.status, options: [.new]) { [weak self] item, _ in
            Task { @MainActor [weak self] in
                guard let self, self.loadingID == id else { return }
                switch item.status {
                case .readyToPlay:
                    self.loadingID = nil
                    self.playingID = id
                    if let range { await player.seek(to: CMTime(seconds: range.lowerBound, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero) }
                    guard self.playingID == id else { return }
                    player.play()
                case .failed:
                    self.loadingID = nil
                    self.failedIDs.insert(id)
                    self.release()
                default: break
                }
            }
        }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in if self?.playingID == id { self?.stop() } }
        }
    }

    func stop() {
        player?.pause()
        release()
        playingID = nil
        loadingID = nil
    }

    private func release() {
        statusObservation?.invalidate(); statusObservation = nil
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = nil
        player = nil
    }
}

/// Round play/pause control used by every sound row. 44pt hit target; the spinner replaces
/// the glyph while the preview loads.
struct NativeSfxPlayButton: View {
    @ObservedObject var player: NativeSfxAuditionPlayer
    let id: String
    let name: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().fill(.white)
                if player.loadingID == id {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: player.playingID == id ? "pause.fill" : "play.fill").font(.system(size: 13, weight: .bold))
                }
            }
            .frame(width: 36, height: 36)
            .frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(player.playingID == id ? "Pause \(name)" : "Play \(name)")
        .accessibilityIdentifier("native-editor-sfx-audition-\(id)")
    }
}
