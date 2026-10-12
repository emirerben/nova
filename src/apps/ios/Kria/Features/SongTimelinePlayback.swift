import AVFoundation
import SwiftUI

/// The song timeline's single source of time (KRI-575), like the editor's playback clock: every lane is positioned
/// from `currentTime`, panning the lanes seeks it, and playing the song advances it. Only the views that draw from
/// it observe it, so the rest of the card is not re-evaluated every frame.
@MainActor final class SongTimelineClock: ObservableObject {
    /// Song seconds, always inside 0...duration.
    @Published private(set) var currentTime: TimeInterval = 0
    @Published private(set) var isPlaying = false
    @Published private(set) var pixelsPerSecond: CGFloat = SongTimelineMath.defaultPixelsPerSecond(viewportWidth: 0)
    private(set) var duration: TimeInterval
    /// Once the creator has zoomed, a layout change no longer resets the zoom.
    private var userZoomed = false

    init(duration: TimeInterval = 0, time: TimeInterval = 0) {
        self.duration = max(0, duration)
        currentTime = SongTimelineMath.clamp(time, duration: duration)
    }

    func setDuration(_ seconds: TimeInterval) {
        duration = seconds.isFinite ? max(0, seconds) : 0
        if currentTime > duration { currentTime = duration }
    }

    func seek(to time: TimeInterval) {
        let target = SongTimelineMath.clamp(time, duration: duration)
        if target != currentTime { currentTime = target }
    }

    func setPlaying(_ playing: Bool) { if isPlaying != playing { isPlaying = playing } }

    /// Applies the default zoom for this viewport unless the creator has chosen one.
    func fit(viewportWidth: CGFloat) {
        guard !userZoomed else { return }
        let fitted = SongTimelineMath.defaultPixelsPerSecond(viewportWidth: viewportWidth)
        if fitted != pixelsPerSecond { pixelsPerSecond = fitted }
    }

    func setPixelsPerSecond(_ value: CGFloat) {
        userZoomed = true
        let clamped = SongTimelineMath.clampedPixelsPerSecond(value)
        if clamped != pixelsPerSecond { pixelsPerSecond = clamped }
    }

    func zoom(by factor: CGFloat) { setPixelsPerSecond(pixelsPerSecond * factor) }
}

/// Plays the song continuously from the playhead and keeps the clock on the audio. Reuses the start-point
/// audition's player (`AVAudioSongAuditionEngine`): one request from the playhead to the end of the song.
/// An `ObservableObject` only so the card can own it as a `@StateObject`; it publishes nothing, the clock does.
@MainActor final class SongTimelinePlayback: ObservableObject {
    let clock: SongTimelineClock
    private let audition: NativeSongAuditionController
    private let ticker = NativeTimelineEdgeScroller()
    private(set) var file: SongAudioFile?

    var canPlay: Bool { file != nil }

    init(clock: SongTimelineClock, engine: SongAuditionEngine = AVAudioSongAuditionEngine(),
         activateAudioSession: @escaping () -> Void = {
             try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
             try? AVAudioSession.sharedInstance().setActive(true)
         }) {
        self.clock = clock
        audition = NativeSongAuditionController(
            engine: engine, host: .init(pauseVideo: { false }, resumeVideo: {}, activateAudioSession: activateAudioSession))
    }

    func load(_ file: SongAudioFile?) { self.file = file }

    func togglePlayback() { clock.isPlaying ? pause() : play() }

    /// Plays from the playhead to the end of the song (from the top when the playhead is already at the end).
    func play() {
        guard file != nil else { return }
        if clock.currentTime >= clock.duration - 0.05 { clock.seek(to: 0) }
        startAudio()
        clock.setPlaying(true)
        ticker.onTick = { [weak self] _ in self?.tick() }
        ticker.start()
    }

    func pause() {
        audition.cancel()
        ticker.stop()
        ticker.onTick = nil
        clock.setPlaying(false)
    }

    /// Moves the playhead; audio that is playing follows it.
    func seek(to time: TimeInterval) {
        clock.seek(to: time)
        guard clock.isPlaying else { return }
        audition.cancel()
        startAudio()
    }

    /// The creator's finger took the lanes: stop the audio and say whether to resume afterwards.
    func scrubBegan() -> Bool {
        let wasPlaying = clock.isPlaying
        if wasPlaying { pause() }
        return wasPlaying
    }

    func scrubEnded(resume: Bool) { if resume { play() } }

    private func startAudio() {
        guard let file else { return }
        let end = max(file.durationS ?? clock.duration, clock.currentTime)
        audition.begin()
        audition.update(SongAuditionRequest(url: file.url, start: clock.currentTime, length: max(0.1, end - clock.currentTime), volume: 1))
        audition.end()
    }

    private func tick() {
        guard clock.isPlaying else { ticker.stop(); return }
        if let position = audition.playbackPosition { clock.seek(to: position) }
        // The song ran out (or the player stopped for another reason): leave the playhead where it is.
        if !audition.isActive { pause() }
    }
}
