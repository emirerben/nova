import AVFoundation
import SwiftUI

/// A local take stays local until the creator explicitly chooses "Use recording".
/// The sheet owns the file after a Files import; this object only deletes it after
/// the coordinator accepts the upload or the creator discards it.
@MainActor final class CreationVoiceRecorder: NSObject, ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var hasRecording = false
    @Published private(set) var isImported = false
    @Published private(set) var isPlaying = false
    @Published private(set) var duration: TimeInterval = 0
    @Published private(set) var elapsed: TimeInterval = 0
    @Published private(set) var playbackTime: TimeInterval = 0
    @Published private(set) var meter: CGFloat = 0
    @Published private(set) var error: String?
    @Published private(set) var isImporting = false

    private var audioRecorder: AVAudioRecorder?
    private var audioPlayer: AVAudioPlayer?
    private var meterTask: Task<Void, Never>?
    private var playbackTask: Task<Void, Never>?
    private var fileURL: URL?
    private var ownsFile = false
    private var permissionRequestInFlight = false
    private var operationGeneration = 0
    private var importTask: Task<Void, Never>?

    var recordingURL: URL? { fileURL }
    var displayName: String {
        isImported ? BackgroundUploadCoordinator.displayFilename(fileURL?.lastPathComponent ?? "Voiceover") : "Recorded voiceover"
    }
    var playbackProgress: CGFloat { duration > 0 ? min(1, max(0, playbackTime / duration)) : 0 }

    override init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(audioInterrupted(_:)), name: AVAudioSession.interruptionNotification, object: nil)
    }

    deinit {
        meterTask?.cancel()
        playbackTask?.cancel()
        NotificationCenter.default.removeObserver(self)
    }

    func start() async {
        guard !isRecording, !permissionRequestInFlight, !isImporting else { return }
        let generation = operationGeneration
        permissionRequestInFlight = true
        let permitted = await AVAudioApplication.requestRecordPermission()
        permissionRequestInFlight = false
        guard generation == operationGeneration else { return }
        guard permitted else {
            error = "Microphone access is off. Allow it in Settings to record, or upload an audio file instead."
            return
        }
        do {
            removeLocalTake()
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker])
            try session.setActive(true)
            let url = FileManager.default.temporaryDirectory.appending(path: "voiceover-\(UUID().uuidString).m4a")
            let recorder = try AVAudioRecorder(url: url, settings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 44_100,
                AVNumberOfChannelsKey: 1,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
            ])
            recorder.isMeteringEnabled = true
            guard recorder.record() else { throw RecorderError.couldNotStart }
            audioRecorder = recorder
            fileURL = url
            ownsFile = true
            isImported = false
            isRecording = true
            hasRecording = false
            elapsed = 0
            duration = 0
            meter = 0
            error = nil
            startMetering()
        } catch {
            removeLocalTake()
            self.error = "Couldn’t start recording. \(error.localizedDescription)"
        }
    }

    @discardableResult func stop() -> URL? {
        guard isRecording else { return fileURL }
        duration = audioRecorder?.currentTime ?? duration
        audioRecorder?.stop()
        elapsed = duration
        audioRecorder = nil
        isRecording = false
        hasRecording = fileURL != nil
        stopMetering()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        return fileURL
    }

    func stopForInterruption() {
        guard isRecording else { pausePlayback(); return }
        _ = stop()
        error = "Recording stopped because audio was interrupted. You can listen and use it, record again, or choose a file."
    }

    /// Takes ownership of the Files picker’s copied temporary URL.
    func importFile(_ url: URL) {
        discard()
        let generation = operationGeneration
        isImporting = true
        importTask = Task { [weak self] in
            let asset = AVURLAsset(url: url)
            let loadedDuration = try? await asset.load(.duration).seconds
            guard let self else { return }
            guard generation == self.operationGeneration else {
                try? FileManager.default.removeItem(at: url)
                return
            }
            self.isImporting = false
            guard let loadedDuration, loadedDuration.isFinite, loadedDuration > 0 else {
                self.error = "This audio file couldn’t be opened. Choose another file."
                try? FileManager.default.removeItem(at: url)
                return
            }
            self.fileURL = url
            self.ownsFile = true
            self.isImported = true
            self.hasRecording = true
            self.duration = loadedDuration
            self.elapsed = 0
            self.error = nil
        }
    }

    func togglePlayback() {
        guard let fileURL else { return }
        if isPlaying { pausePlayback(); return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio)
            try session.setActive(true)
            let player = try AVAudioPlayer(contentsOf: fileURL)
            player.delegate = self
            player.currentTime = min(playbackTime, max(0, player.duration - 0.01))
            player.prepareToPlay()
            guard player.play() else { throw RecorderError.couldNotPlay }
            audioPlayer = player
            isPlaying = true
            startPlaybackTimer()
        } catch { self.error = "This recording couldn’t be played. Choose another file." }
    }

    func pausePlayback() {
        playbackTime = audioPlayer?.currentTime ?? playbackTime
        audioPlayer?.pause()
        audioPlayer = nil
        isPlaying = false
        playbackTask?.cancel()
        playbackTask = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func discard() {
        operationGeneration += 1
        importTask?.cancel()
        importTask = nil
        isImporting = false
        removeLocalTake()
        error = nil
    }

    /// Called when the sheet is hidden while a permission/import request is in
    /// flight. It prevents a late callback from enabling audio in the background
    /// while preserving a take that was already recoverable in review.
    func cancelPendingWorkAndStop() {
        operationGeneration += 1
        importTask?.cancel()
        importTask = nil
        isImporting = false
        if isRecording { _ = stop() }
    }

    private func removeLocalTake() {
        pausePlayback()
        if isRecording { _ = stop() }
        if ownsFile, let fileURL { try? FileManager.default.removeItem(at: fileURL) }
        fileURL = nil
        ownsFile = false
        hasRecording = false
        isImported = false
        duration = 0
        elapsed = 0
        playbackTime = 0
        meter = 0
    }

    private func startMetering() {
        stopMetering()
        meterTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(80))
                guard !Task.isCancelled, let self else { return }
                self.updateMeter()
            }
        }
    }

    private func stopMetering() { meterTask?.cancel(); meterTask = nil }
    private func startPlaybackTimer() {
        playbackTask?.cancel()
        playbackTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(100))
                guard !Task.isCancelled, let self else { return }
                self.updatePlaybackTime()
            }
        }
    }
    private func updatePlaybackTime() { if let audioPlayer { playbackTime = audioPlayer.currentTime } }
    private func updateMeter() {
        guard let audioRecorder, isRecording else { return }
        audioRecorder.updateMeters()
        elapsed = audioRecorder.currentTime
        duration = elapsed
        // -60 dB is quiet; retain a little movement so a real quiet room does
        // not look like a broken recorder, without inventing a decorative wave.
        meter = max(0.03, min(1, CGFloat((audioRecorder.averagePower(forChannel: 0) + 60) / 60)))
    }

    @objc private func audioInterrupted(_ notification: Notification) { stopForInterruption() }

    enum RecorderError: LocalizedError { case couldNotStart, couldNotPlay, invalidFile
        var errorDescription: String? {
            switch self { case .couldNotStart: "The microphone did not start."; case .couldNotPlay: "The recording could not be played."; case .invalidFile: "The file has no usable duration." }
        }
    }
}

extension CreationVoiceRecorder: AVAudioPlayerDelegate {
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            isPlaying = false
            playbackTime = 0
            playbackTask?.cancel()
            playbackTask = nil
            audioPlayer = nil
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }
}

#if DEBUG
/// A deterministic six-second PCM file for UI review tests. It avoids relying
/// on the simulator microphone and never exists in release builds.
enum VoiceoverReviewFixture {
    static func make() -> URL? {
        let url = FileManager.default.temporaryDirectory.appending(path: "voiceover-fixture.wav")
        let sampleRate: UInt32 = 8_000
        let seconds: UInt32 = 6
        let samples = Int(sampleRate * seconds)
        var data = Data()
        data.append("RIFF".data(using: .ascii)!)
        append(UInt32(36 + samples * 2), to: &data)
        data.append("WAVEfmt ".data(using: .ascii)!)
        append(UInt32(16), to: &data); append(UInt16(1), to: &data); append(UInt16(1), to: &data)
        append(sampleRate, to: &data); append(sampleRate * 2, to: &data); append(UInt16(2), to: &data); append(UInt16(16), to: &data)
        data.append("data".data(using: .ascii)!); append(UInt32(samples * 2), to: &data)
        for index in 0..<samples {
            let phase = Double(index % Int(sampleRate)) / Double(sampleRate)
            append(Int16((sin(phase * .pi * 2 * 440) * 0.18 * Double(Int16.max)).rounded()), to: &data)
        }
        do { try data.write(to: url, options: .atomic); return url } catch { return nil }
    }

    private static func append<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var littleEndian = value.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }
}
#endif
