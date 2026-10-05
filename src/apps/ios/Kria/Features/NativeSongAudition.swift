import AVFoundation
import UIKit

/// KRI-432: hearing the song while choosing where it starts.
///
/// Sliding the start-point bar plays the creator's song from the chosen start to the end of the window
/// (start + video length) on a dedicated light player, so the video preview is never rebuilt per drag
/// tick. The video pauses while this plays and is restored afterwards.

/// What to play: the local song file from `start`, `length` seconds, at `volume` (0...1).
struct SongAuditionRequest: Equatable, Sendable {
    let url: URL
    let start: TimeInterval
    let length: TimeInterval
    let volume: Float
}

/// The audio side of an audition. A real engine plays; tests record.
@MainActor protocol SongAuditionEngine: AnyObject {
    /// Called when a `play` ran to the end of its window (never after `stop` or a restart).
    var onFinished: (() -> Void)? { get set }
    /// Starts (or restarts) from `request.start`; a restart fades the old position out first.
    func play(_ request: SongAuditionRequest)
    func stop()
}

/// Drives one audition session: opens when the creator starts dragging, restarts from the new start
/// when the finger pauses (debounced) and on release, plays to the end of the window, and closes when
/// the window finished and the drag is over, or on `cancel`.
@MainActor final class NativeSongAuditionController: ObservableObject {
    /// The hooks into the editor's video preview.
    struct Host {
        /// Pauses the video preview; returns whether it was playing.
        var pauseVideo: () -> Bool
        /// Resumes the video preview after an audition that paused it.
        var resumeVideo: () -> Void
        /// Same audio session as the preview (playback / moviePlayback).
        var activateAudioSession: () -> Void
    }

    /// True from the first drag sample until the audition is over.
    @Published private(set) var isActive = false

    static let defaultDebounce: TimeInterval = 0.15

    private let engine: SongAuditionEngine
    private let host: Host
    private let debounce: TimeInterval
    private let sleeper: (TimeInterval) async -> Void
    private var videoWasPlaying = false
    private var dragging = false
    private var pending: SongAuditionRequest?
    private var lastStarted: SongAuditionRequest?
    private var engineRunning = false
    private var debounceTask: Task<Void, Never>?
    private nonisolated(unsafe) var backgroundObserver: NSObjectProtocol?

    deinit { if let backgroundObserver { NotificationCenter.default.removeObserver(backgroundObserver) } }

    init(engine: SongAuditionEngine, host: Host, debounce: TimeInterval = defaultDebounce,
         sleeper: @escaping (TimeInterval) async -> Void = { try? await Task.sleep(for: .seconds($0)) }) {
        self.engine = engine
        self.host = host
        self.debounce = debounce
        self.sleeper = sleeper
        engine.onFinished = { [weak self] in self?.windowFinished() }
        // Backgrounding ends the audition; the video must not resume behind the user's back.
        backgroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in Task { @MainActor [weak self] in self?.cancel(restoreVideo: false) } }
    }

    /// A drag started: pause the video (remembering whether it played) and claim the audio session.
    func begin() {
        dragging = true
        guard !isActive else { return }
        isActive = true
        videoWasPlaying = host.pauseVideo()
        host.activateAudioSession()
    }

    /// The start moved. Restarts from it once the finger has paused for the debounce interval.
    func update(_ request: SongAuditionRequest) {
        guard isActive else { return }
        pending = request
        debounceTask?.cancel()
        let delay = debounce
        debounceTask = Task { [weak self] in
            await self?.sleeper(delay)
            guard !Task.isCancelled else { return }
            self?.startEngine()
        }
    }

    /// The finger lifted: play from the final start now (unless it is already playing from there).
    func end() {
        dragging = false
        debounceTask?.cancel()
        guard isActive else { return }
        if let pending, !(engineRunning && lastStarted == pending) {
            startEngine()
        } else if !engineRunning {
            close(restoreVideo: true)
        }
    }

    /// Stops now. `restoreVideo: false` when something else (the user pressing play, the app going to
    /// the background) owns the video's play state.
    func cancel(restoreVideo: Bool = true) {
        debounceTask?.cancel()
        guard isActive || engineRunning else { return }
        engine.stop()
        engineRunning = false
        close(restoreVideo: restoreVideo)
    }

    private func startEngine() {
        guard isActive, let request = pending else { return }
        lastStarted = request
        engineRunning = true
        engine.play(request)
    }

    private func windowFinished() {
        engineRunning = false
        if !dragging { close(restoreVideo: true) }
    }

    private func close(restoreVideo: Bool) {
        let resume = restoreVideo && videoWasPlaying
        isActive = false
        dragging = false
        pending = nil
        lastStarted = nil
        videoWasPlaying = false
        if resume { host.resumeVideo() }
    }
}

/// Plays the window with `AVAudioPlayer` on the local file: seeks are instant, volume fades are built in
/// (30ms in and out, so a restart or the end of the window never clicks), and it needs no video item.
@MainActor final class AVAudioSongAuditionEngine: SongAuditionEngine {
    var onFinished: (() -> Void)?
    static let fade: TimeInterval = 0.03
    private var player: AVAudioPlayer?
    private var loadedURL: URL?
    private var task: Task<Void, Never>?

    func play(_ request: SongAuditionRequest) {
        task?.cancel()
        if loadedURL != request.url || player == nil {
            player = try? AVAudioPlayer(contentsOf: request.url)
            player?.prepareToPlay()
            loadedURL = request.url
        }
        guard let player else { onFinished?(); return }
        let start = min(max(0, request.start), player.duration)
        let length = min(request.length, player.duration - start)
        guard length > Self.fade * 2 else { onFinished?(); return }
        let wasPlaying = player.isPlaying
        let fade = Self.fade
        task = Task { [weak self, weak player] in
            guard let player else { return }
            if wasPlaying {
                player.setVolume(0, fadeDuration: fade)
                try? await Task.sleep(for: .seconds(fade))
                guard !Task.isCancelled else { return }
            }
            player.currentTime = start
            player.volume = 0
            player.play()
            player.setVolume(request.volume, fadeDuration: fade)
            try? await Task.sleep(for: .seconds(length - fade))
            guard !Task.isCancelled else { return }
            player.setVolume(0, fadeDuration: fade)
            try? await Task.sleep(for: .seconds(fade))
            guard !Task.isCancelled else { return }
            player.pause()
            self?.onFinished?()
        }
    }

    func stop() {
        task?.cancel()
        guard let player, player.isPlaying else { return }
        player.setVolume(0, fadeDuration: Self.fade)
        task = Task { [weak player] in
            try? await Task.sleep(for: .seconds(Self.fade))
            player?.pause()
        }
    }
}
