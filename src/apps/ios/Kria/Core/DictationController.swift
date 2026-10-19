import AVFoundation
import Observation
import Speech
import SwiftUI

// KRI-742: talk to Kria instead of typing.
//
// Hybrid by design. While the creator talks, Apple's on-device recognizer streams words into the
// composer so they see it working. On stop, the same recording goes to the server's Whisper pass,
// which handles Turkish/English mixing and the creator's own clip and song names, and its text
// replaces the live words. If that fails the live words stay: dictation never loses what was said.

struct DictationRecording: Sendable, Equatable {
    let url: URL
    let duration: TimeInterval
}

struct DictationTranscript: Sendable, Equatable {
    let text: String
    /// The server heard no speech in the recording (it returns no text rather than invented words).
    let isNoSpeech: Bool
}

enum DictationEngineError: Error, Equatable {
    case microphoneDenied
    case couldNotStart(String)
}

/// Captures the microphone: live partial text for the screen and a file for the server pass.
@MainActor protocol DictationEngine: AnyObject {
    /// Begins capture. Throws before anything is recording. `onPartial` is Apple's best transcript so
    /// far, `onLevel` a 0...1 loudness for the meter, `onInterrupted` fires if the system takes the
    /// microphone away (a call, Siri).
    func start(
        contextualStrings: [String],
        onPartial: @escaping @MainActor (String) -> Void,
        onLevel: @escaping @MainActor (CGFloat) -> Void,
        onInterrupted: @escaping @MainActor () -> Void
    ) async throws
    /// Ends capture and hands back the recording, or nil when nothing usable was captured.
    func stop() async -> DictationRecording?
    func cancel()
}

protocol DictationTranscriber: Sendable {
    func transcribe(_ recording: DictationRecording, contextTerms: [String]) async throws -> DictationTranscript
}

@MainActor @Observable final class DictationController {
    enum Phase: Equatable { case idle, requestingPermission, listening, finalizing }

    private(set) var phase: Phase = .idle
    /// 0...1, smoothed, for the mic button's ring.
    private(set) var level: CGFloat = 0
    /// A short line under the composer ("Didn't catch that"); clears itself.
    private(set) var notice: String?

    var isActive: Bool { phase != .idle }
    var isListening: Bool { phase == .listening }

    @ObservationIgnored private let engine: any DictationEngine
    @ObservationIgnored private let transcriber: (any DictationTranscriber)?
    @ObservationIgnored private let maxDuration: Duration
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var base = ""
    @ObservationIgnored private var partial = ""
    @ObservationIgnored private var contextTerms: [String] = []
    @ObservationIgnored private var onText: (String) -> Void = { _ in }
    @ObservationIgnored private var limitTask: Task<Void, Never>?
    @ObservationIgnored private var noticeTask: Task<Void, Never>?

    /// `maxDuration` stays under the server's 120 s ceiling so a long ramble still transcribes.
    init(engine: any DictationEngine, transcriber: (any DictationTranscriber)?, maxDuration: Duration = .seconds(110)) {
        self.engine = engine
        self.transcriber = transcriber
        self.maxDuration = maxDuration
    }

    static func make(api: any KriaAPIClient) -> DictationController {
        #if DEBUG
        if ProcessInfo.processInfo.environment["KRIA_VOICE_DICTATION"] == "1" {
            return DictationController(engine: ScriptedDictationEngine(), transcriber: ScriptedDictationTranscriber())
        }
        #endif
        return DictationController(engine: SystemDictationEngine(), transcriber: APIDictationTranscriber(api: api))
    }

    /// Joins what was already in the field with what was just said.
    static func join(_ base: String, _ spoken: String) -> String {
        let spoken = spoken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !spoken.isEmpty else { return base }
        let trimmedBase = base.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedBase.isEmpty ? spoken : trimmedBase + " " + spoken
    }

    /// Names worth teaching the recognizers: the project title and the footage names, minus camera-roll
    /// filenames like `IMG_4021` that nobody says out loud.
    static func contextTerms(title: String?, mediaNames: [String]) -> [String] {
        var seen = Set<String>()
        var terms: [String] = []
        for raw in [title ?? ""] + mediaNames {
            var name = (raw as NSString).deletingPathExtension.replacingOccurrences(of: "_", with: " ")
            name = name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard name.count >= 2, name.range(of: #"^(img|mov|vid|pxl|dsc|screenshot)\b[\s\-]*\d*"#, options: [.regularExpression, .caseInsensitive]) == nil,
                  name.rangeOfCharacter(from: .letters) != nil, seen.insert(name.lowercased()).inserted else { continue }
            terms.append(String(name.prefix(60)))
            if terms.count >= 30 { break }
        }
        return terms
    }

    // MARK: - Control

    /// Starts listening. Whatever is in the field now stays; dictated words are appended to it.
    func start(base: String, contextTerms: [String], onText: @escaping (String) -> Void) {
        guard phase == .idle else { return }
        generation += 1
        let current = generation
        self.base = base
        self.contextTerms = contextTerms
        self.onText = onText
        partial = ""
        level = 0
        clearNotice()
        phase = .requestingPermission
        Task { [weak self] in
            guard let self else { return }
            do {
                try await engine.start(
                    contextualStrings: contextTerms,
                    onPartial: { [weak self] text in self?.receivePartial(text, generation: current) },
                    onLevel: { [weak self] value in self?.receiveLevel(value, generation: current) },
                    onInterrupted: { [weak self] in
                        guard let self, generation == current, phase == .listening else { return }
                        Task { await self.stop() }
                    }
                )
            } catch {
                guard generation == current else { return }
                phase = .idle
                showNotice(Self.message(for: error))
                return
            }
            // Cancelled while the permission sheet was up: the engine is already capturing, so stop it.
            guard generation == current, phase == .requestingPermission else {
                engine.cancel()
                return
            }
            phase = .listening
            limitTask = Task { [weak self] in
                try? await Task.sleep(for: self?.maxDuration ?? .seconds(110))
                guard !Task.isCancelled else { return }
                // stop() cancels `limitTask`; run it in its own task so the server pass is not cancelled with it.
                Task { await self?.stop() }
            }
        }
    }

    /// Stops listening and swaps the live words for the server's text. Safe to call in any phase.
    func stop() async {
        switch phase {
        case .idle, .finalizing:
            return
        case .requestingPermission:
            cancel()
            return
        case .listening:
            break
        }
        let current = generation
        phase = .finalizing
        level = 0
        limitTask?.cancel()
        let recording = await engine.stop()
        guard generation == current else {
            Self.discard(recording)
            return
        }
        await finalize(recording, generation: current)
    }

    /// Drops the session without a server pass. The live words already in the field stay.
    func cancel() {
        guard phase != .idle else { return }
        generation += 1
        limitTask?.cancel()
        engine.cancel()
        level = 0
        phase = .idle
    }

    // MARK: - Internals

    private func finalize(_ recording: DictationRecording?, generation current: Int) async {
        defer {
            if generation == current { phase = .idle }
            Self.discard(recording)
        }
        if let recording, let transcriber {
            do {
                let transcript = try await transcriber.transcribe(recording, contextTerms: contextTerms)
                guard generation == current else { return }
                if !transcript.text.isEmpty {
                    onText(Self.join(base, transcript.text))
                    return
                }
            } catch {
                guard generation == current else { return }
                if partial.isEmpty { onText(base); showNotice("Couldn’t transcribe that. Try again.") }
                return
            }
        }
        // The server heard nothing (or there was no server pass): the live words, if any, are all we have.
        if partial.isEmpty {
            onText(base)
            showNotice("Didn’t catch that. Try again.")
        }
    }

    private func receivePartial(_ text: String, generation current: Int) {
        guard generation == current, phase == .listening || phase == .finalizing else { return }
        partial = text
        onText(Self.join(base, text))
    }

    private func receiveLevel(_ value: CGFloat, generation current: Int) {
        guard generation == current, phase == .listening else { return }
        level = max(value, level * 0.6)
    }

    private func showNotice(_ text: String) {
        notice = text
        noticeTask?.cancel()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            self?.notice = nil
        }
    }

    private func clearNotice() {
        noticeTask?.cancel()
        notice = nil
    }

    private static func message(for error: Error) -> String {
        switch error as? DictationEngineError {
        case .microphoneDenied: "Microphone access is off. Allow it in Settings to talk to Kria."
        case .couldNotStart: "Couldn’t start the microphone. Try again."
        case nil: "Couldn’t start dictation. Try again."
        }
    }

    private static func discard(_ recording: DictationRecording?) {
        if let recording { try? FileManager.default.removeItem(at: recording.url) }
    }
}

// MARK: - Server pass

struct APIDictationTranscriber: DictationTranscriber {
    let api: any KriaAPIClient

    func transcribe(_ recording: DictationRecording, contextTerms: [String]) async throws -> DictationTranscript {
        let audio = try Data(contentsOf: recording.url)
        let response = try await api.transcribeDictation(audio: audio, contextTerms: contextTerms)
        return DictationTranscript(text: response.text, isNoSpeech: response.reason == "no_speech")
    }
}

// MARK: - Microphone + Apple recognizer

/// Feeds each microphone buffer to the recognizer and the file. Runs on the audio thread, so it holds
/// no UI state and everything it touches is behind a lock.
private final class DictationTapSink: @unchecked Sendable {
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var file: AVAudioFile?
    private var lastLevelAt: CFAbsoluteTime = 0
    private let onLevel: @Sendable (CGFloat) -> Void

    init(request: SFSpeechAudioBufferRecognitionRequest?, file: AVAudioFile?, onLevel: @escaping @Sendable (CGFloat) -> Void) {
        self.request = request
        self.file = file
        self.onLevel = onLevel
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let request = request, file = file
        let now = CFAbsoluteTimeGetCurrent()
        let emitLevel = now - lastLevelAt > 0.04
        if emitLevel { lastLevelAt = now }
        lock.unlock()
        request?.append(buffer)
        try? file?.write(from: buffer)
        if emitLevel { onLevel(Self.level(of: buffer)) }
    }

    /// Ends the recognizer's audio and closes the file so it can be uploaded.
    func close() {
        lock.lock()
        let request = request
        self.request = nil
        file = nil
        lock.unlock()
        request?.endAudio()
    }

    private static func level(of buffer: AVAudioPCMBuffer) -> CGFloat {
        guard let samples = buffer.floatChannelData?[0] else { return 0 }
        let count = Int(buffer.frameLength)
        guard count > 0 else { return 0 }
        var sum: Float = 0
        for index in 0..<count { sum += samples[index] * samples[index] }
        let rms = (sum / Float(count)).squareRoot()
        let decibels = 20 * log10(max(rms, 0.000_01))
        return CGFloat(max(0, min(1, (decibels + 50) / 50)))
    }
}

@MainActor final class SystemDictationEngine: DictationEngine {
    private static let claimToken = "dictation"

    private var audioEngine: AVAudioEngine?
    private var sink: DictationTapSink?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var observers: [NSObjectProtocol] = []
    private var fileURL: URL?
    private var startedAt: Date?

    func start(
        contextualStrings: [String],
        onPartial: @escaping @MainActor (String) -> Void,
        onLevel: @escaping @MainActor (CGFloat) -> Void,
        onInterrupted: @escaping @MainActor () -> Void
    ) async throws {
        guard await AVAudioApplication.requestRecordPermission() else { throw DictationEngineError.microphoneDenied }
        // Speech permission is optional: without it the recording still goes to the server pass.
        let speechAllowed = await Self.speechAuthorized()
        teardown()
        NotificationCenter.default.post(name: .kriaAudioCaptureWillStart, object: nil)
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker])
            try session.setActive(true)
            AudioSessionArbiter.shared.claimCapture(Self.claimToken)

            let engine = AVAudioEngine()
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw DictationEngineError.couldNotStart("No microphone input")
            }

            let url = FileManager.default.temporaryDirectory.appending(path: "dictation-\(UUID().uuidString).m4a")
            let file = try AVAudioFile(
                forWriting: url,
                settings: [
                    AVFormatIDKey: kAudioFormatMPEG4AAC,
                    AVSampleRateKey: format.sampleRate,
                    AVNumberOfChannelsKey: min(2, Int(format.channelCount)),
                    AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
                ],
                commonFormat: format.commonFormat,
                interleaved: format.isInterleaved
            )

            var request: SFSpeechAudioBufferRecognitionRequest?
            if speechAllowed, let recognizer = Self.makeRecognizer(), recognizer.isAvailable {
                let live = SFSpeechAudioBufferRecognitionRequest()
                live.shouldReportPartialResults = true
                live.addsPunctuation = true
                live.contextualStrings = contextualStrings
                if recognizer.supportsOnDeviceRecognition { live.requiresOnDeviceRecognition = true }
                request = live
                // @Sendable keeps this off the main actor: Speech calls it on its own queue.
                recognitionTask = recognizer.recognitionTask(with: live) { @Sendable result, _ in
                    guard let text = result?.bestTranscription.formattedString else { return }
                    Task { @MainActor in onPartial(text) }
                }
            }

            let sink = DictationTapSink(request: request, file: file, onLevel: { value in
                Task { @MainActor in onLevel(value) }
            })
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { @Sendable buffer, _ in
                sink.append(buffer)
            }
            engine.prepare()
            try engine.start()

            self.audioEngine = engine
            self.sink = sink
            fileURL = url
            startedAt = Date()
            observeInterruptions(engine: engine, onInterrupted)
        } catch {
            teardown()
            if let error = error as? DictationEngineError { throw error }
            throw DictationEngineError.couldNotStart(error.localizedDescription)
        }
    }

    func stop() async -> DictationRecording? {
        guard audioEngine != nil else { return nil }
        let url = fileURL
        let duration = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        sink?.close()
        // The recognizer needs a beat to deliver its last words once the audio ends.
        if recognitionTask != nil { try? await Task.sleep(for: .milliseconds(350)) }
        teardown()
        guard let url, duration >= 0.3 else {
            if let url { try? FileManager.default.removeItem(at: url) }
            return nil
        }
        return DictationRecording(url: url, duration: duration)
    }

    func cancel() {
        let url = fileURL
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        sink?.close()
        teardown()
        if let url { try? FileManager.default.removeItem(at: url) }
    }

    private func teardown() {
        recognitionTask?.cancel()
        recognitionTask = nil
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        let hadSession = audioEngine != nil || sink != nil
        audioEngine = nil
        sink = nil
        fileURL = nil
        startedAt = nil
        AudioSessionArbiter.shared.releaseCapture(Self.claimToken)
        if hadSession { AudioSessionArbiter.shared.deactivate(requester: "dictation.stop") }
    }

    private func observeInterruptions(engine: AVAudioEngine, _ onInterrupted: @escaping @MainActor () -> Void) {
        let interruption = NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { @Sendable note in
            let began = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
            guard began else { return }
            Task { @MainActor in onInterrupted() }
        }
        let route = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { @Sendable _ in
            Task { @MainActor in onInterrupted() }
        }
        observers = [interruption, route]
    }

    private static func speechAuthorized() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { @Sendable status in continuation.resume(returning: status == .authorized) }
            }
        default: return false
        }
    }

    private static func makeRecognizer() -> SFSpeechRecognizer? {
        let preferred = Locale.preferredLanguages.first.map { Locale(identifier: $0) } ?? Locale.current
        return SFSpeechRecognizer(locale: preferred) ?? SFSpeechRecognizer()
    }
}

#if DEBUG
// UI tests (`KRIA_VOICE_DICTATION=1`): a simulator has no usable microphone or recognizer, so the
// run is scripted. The controller, composer and send path under test are the real ones.

@MainActor final class ScriptedDictationEngine: DictationEngine {
    static let partialText = "make the intro shorter"
    private var onPartial: (@MainActor (String) -> Void)?

    func start(
        contextualStrings: [String],
        onPartial: @escaping @MainActor (String) -> Void,
        onLevel: @escaping @MainActor (CGFloat) -> Void,
        onInterrupted: @escaping @MainActor () -> Void
    ) async throws {
        self.onPartial = onPartial
        onLevel(0.6)
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            self?.onPartial?(Self.partialText)
        }
    }

    func stop() async -> DictationRecording? {
        onPartial = nil
        let url = FileManager.default.temporaryDirectory.appending(path: "dictation-scripted-\(UUID().uuidString).m4a")
        try? Data([0]).write(to: url)
        return DictationRecording(url: url, duration: 1.5)
    }

    func cancel() { onPartial = nil }
}

struct ScriptedDictationTranscriber: DictationTranscriber {
    static let finalText = "Make the intro shorter and add the beach clip first."

    func transcribe(_ recording: DictationRecording, contextTerms: [String]) async throws -> DictationTranscript {
        try await Task.sleep(for: .milliseconds(300))
        return DictationTranscript(text: Self.finalText, isNoSpeech: false)
    }
}
#endif
