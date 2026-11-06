import AVFoundation
import SwiftUI
import UIKit

enum FeaturedEdit: String, CaseIterable, Identifiable {
    case bosphorus, lisbon, sunset, office, talking, lipsync

    static let onboarding: [FeaturedEdit] = [.bosphorus, .lisbon, .sunset, .office, .talking]
    var id: String { rawValue }
    var resource: String { "featured-\(rawValue)" }
    var aspectRatio: CGFloat { self == .bosphorus || self == .talking || self == .lipsync ? 9 / 16 : 16 / 9 }
}

extension CreationFormat {
    /// The bundled preview video a format card plays; nil falls back to the still poster.
    var previewEdit: FeaturedEdit? {
        switch self {
        case .montage: .bosphorus
        case .lipsync: .lipsync
        case .talkingToCamera: .talking
        case .narrated, .slides: nil
        }
    }
}

@MainActor final class FeaturedEditPlayback: ObservableObject {
    let player = AVPlayer()
    @Published private(set) var isPlaying = false
    private var active = false
    private var allowed = false
    private var statusObservation: NSKeyValueObservation?
    nonisolated(unsafe) private var endObserver: NSObjectProtocol?
    var ended: () -> Void = {}

    init(edit: FeaturedEdit) {
        player.isMuted = true
        player.preventsDisplaySleepDuringVideoPlayback = false
        player.actionAtItemEnd = .pause
        guard let url = Bundle.main.url(forResource: edit.resource, withExtension: "mp4") else { return }
        let item = AVPlayerItem(url: url)
        player.replaceCurrentItem(with: item)
        statusObservation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] player, _ in
            let playing = player.timeControlStatus == .playing
            Task { @MainActor [weak self] in self?.isPlaying = playing }
        }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.active else { return }
                self.ended()
            }
        }
    }

    deinit {
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    }

    func update(active: Bool, allowed: Bool) {
        let starts = active && !self.active
        self.active = active
        self.allowed = allowed
        if starts { restart() } else { apply() }
    }

    func restart() {
        player.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            Task { @MainActor [weak self] in self?.apply() }
        }
    }

    private func apply() {
        if active && allowed { player.play() } else { player.pause() }
    }
}

struct FeaturedEditPreview: View {
    let edit: FeaturedEdit
    let active: Bool
    let allowed: Bool
    var loops = false
    var completed: () -> Void = {}
    var playingChanged: (Bool) -> Void = { _ in }
    @StateObject private var playback: FeaturedEditPlayback

    init(edit: FeaturedEdit, active: Bool, allowed: Bool, loops: Bool = false,
         completed: @escaping () -> Void = {}, playingChanged: @escaping (Bool) -> Void = { _ in }) {
        self.edit = edit
        self.active = active
        self.allowed = allowed
        self.loops = loops
        self.completed = completed
        self.playingChanged = playingChanged
        _playback = StateObject(wrappedValue: FeaturedEditPlayback(edit: edit))
    }

    var body: some View {
        ZStack {
            BundledPosterImage(name: edit.resource).scaledToFill()
            if active { SlidePostVideoSurface(player: playback.player) }
        }
        .clipped()
        .allowsHitTesting(false)
        .onAppear { update() }
        .onChange(of: active) { _, _ in update() }
        .onChange(of: allowed) { _, _ in update() }
        .onChange(of: playback.isPlaying) { _, playing in playingChanged(playing) }
        .onDisappear { playback.update(active: false, allowed: false) }
    }

    private func update() {
        let controller = playback
        let shouldLoop = loops
        let onEnd = completed
        controller.ended = { [weak controller] in
            if shouldLoop { controller?.restart() } else { onEnd() }
        }
        controller.update(active: active, allowed: allowed)
    }
}
