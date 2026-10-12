@preconcurrency import AVFoundation
import SwiftUI
import UIKit

/// Owns one short-lived, front-camera capture session. The caller owns the destination URL and
/// retains a finalized take until it has either attached it or explicitly discarded it.
///
/// `onFinalized` is called exactly once for each file-output callback. A non-nil URL means the
/// movie contains playable video and non-empty audio, including a valid interrupted partial take.
@MainActor final class TeleprompterCamera: NSObject, ObservableObject {
    enum Phase: Equatable {
        case idle
        case preparing
        case ready
        case recording
        case finalizing
        case failed
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var error: String?
    @Published private(set) var recordingStartedAt: TimeInterval?
    @Published private(set) var finalizedURL: URL?
    @Published private(set) var interrupted = false

    /// Read by `TeleprompterCameraPreview`; configuration always happens on `captureQueue`.
    nonisolated(unsafe) let session = AVCaptureSession()
    var onFinalized: ((URL?, String?) -> Void)?
    /// Includes the file-output startup gap, so UI cannot offer Retake/Edit while a take may begin.
    var isBusy: Bool { phase == .preparing || phase == .recording || phase == .finalizing || startRequested }

    private let captureQueue = DispatchQueue(label: "app.kria.teleprompter-camera.capture")
    private nonisolated(unsafe) var movieOutput: AVCaptureMovieFileOutput?
    private nonisolated(unsafe) var isConfigured = false
    private nonisolated(unsafe) var recordingRequested = false
    private nonisolated(unsafe) var stopRequested = false
    private nonisolated(unsafe) var captureInterrupted = false
    /// Main-actor side of the narrow gap between the button tap and the file-output callback.
    private var startRequested = false
    private var stopSessionAfterFinalization = false
    private var priorIdleTimerDisabled: Bool?
    private var observers: [NSObjectProtocol] = []
    private var operationGeneration = 0

    override init() {
        super.init()
        observeLifecycle()
    }

    deinit { observers.forEach(NotificationCenter.default.removeObserver) }

    /// Rechecks both privacy grants every time, since a creator may return from Settings.
    func prepare() async {
        guard phase != .recording, phase != .finalizing, !startRequested else { return }
        let generation = operationGeneration
        phase = .preparing
        error = nil
        interrupted = false
        finalizedURL = nil

        if Self.usesUnavailableUITestFixture {
            fail(message: "The front camera is unavailable in this test environment. Use a physical iPhone to record with your script.")
            return
        }

        guard await requestPermissionsIfNeeded(generation: generation) else { return }
        guard generation == operationGeneration else { return }
        // The preview graph includes microphone input. With automatic audio configuration
        // disabled, it needs a recording-capable audio session before startRunning, not just
        // before the movie output starts writing. Stop auditions before acquiring that route.
        NotificationCenter.default.post(name: .kriaAudioCaptureWillStart, object: nil)
        do {
            try await configureSession()
            guard generation == operationGeneration else { return }
            phase = .ready
        } catch {
            guard generation == operationGeneration else { return }
            fail(message: error.localizedDescription)
        }
    }

    /// Begins writing directly to the caller's durable project-local URL. State becomes `.recording`
    /// only after `AVCaptureMovieFileOutput` confirms that it actually started.
    func startRecording(to url: URL) {
        guard phase == .ready, !startRequested else { return }
        error = nil
        finalizedURL = nil
        interrupted = false
        startRequested = true
        NotificationCenter.default.post(name: .kriaAudioCaptureWillStart, object: nil)

        captureQueue.async { [weak self] in
            guard let self else { return }
            guard self.isConfigured, let output = self.movieOutput else {
                Task { @MainActor [weak self] in
                    self?.finishStartupFailure("The camera is no longer ready. Prepare it again before recording.")
                }
                return
            }
            guard !self.recordingRequested else {
                Task { @MainActor [weak self] in
                    self?.finishStartupFailure("A camera recording is already starting. Try again after it finishes.")
                }
                return
            }
            do {
                let audio = AVAudioSession.sharedInstance()
                try audio.setCategory(.playAndRecord, mode: .videoRecording, options: [.defaultToSpeaker, .allowBluetoothHFP])
                try audio.setActive(true)
                self.recordingRequested = true
                self.captureInterrupted = false
                output.startRecording(to: url, recordingDelegate: self)
            } catch {
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let message = "Couldn’t start the camera recording. \(error.localizedDescription)"
                    self.finishStartupFailure(message)
                }
            }
        }
    }

    /// Stops at the movie-output boundary. The completion callback validates the actual file before
    /// exposing it, so callers never attach an empty or silent movie.
    func stopRecording(interrupted: Bool = false) {
        guard phase == .recording || (phase == .ready && startRequested) else { return }
        phase = .finalizing
        self.interrupted = self.interrupted || interrupted
        captureQueue.async { [weak self] in
            guard let self, let output = self.movieOutput else { return }
            self.stopRequested = true
            self.captureInterrupted = self.captureInterrupted || interrupted
            if self.recordingRequested, output.isRecording { output.stopRecording() }
        }
    }

    /// Stops preview and audio. If a movie is in flight, its partial result is finalized first.
    func stopSession() {
        guard phase != .idle else { return }
        operationGeneration += 1
        if phase == .recording || phase == .finalizing || startRequested {
            stopSessionAfterFinalization = true
            stopRecording(interrupted: true)
            return
        }
        tearDownSession()
    }

    private func requestPermissionsIfNeeded(generation: Int) async -> Bool {
        guard generation == operationGeneration else { return false }
        let camera = AVCaptureDevice.authorizationStatus(for: .video)
        let microphone = AVCaptureDevice.authorizationStatus(for: .audio)
        guard camera != .restricted, microphone != .restricted else {
            guard generation == operationGeneration else { return false }
            fail(message: "Camera or microphone access is restricted on this iPhone. Choose a video instead.")
            return false
        }
        let cameraAllowed: Bool
        switch camera {
        case .authorized: cameraAllowed = true
        case .notDetermined: cameraAllowed = await requestAccess(for: .video)
        default: cameraAllowed = false
        }
        guard generation == operationGeneration else { return false }
        guard cameraAllowed else {
            fail(message: "Camera access is off. Allow it in Settings to record with your script, or choose a video instead.")
            return false
        }
        let microphoneAllowed: Bool
        switch microphone {
        case .authorized: microphoneAllowed = true
        case .notDetermined: microphoneAllowed = await requestAccess(for: .audio)
        default: microphoneAllowed = false
        }
        guard generation == operationGeneration else { return false }
        guard microphoneAllowed else {
            fail(message: "Microphone access is off. Allow it in Settings to record a video with sound, or choose a video instead.")
            return false
        }
        return true
    }

    private func requestAccess(for mediaType: AVMediaType) async -> Bool {
        await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: mediaType) { continuation.resume(returning: $0) }
        }
    }

    private func configureSession() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            captureQueue.async { [weak self] in
                guard let self else {
                    continuation.resume(throwing: CameraError.cancelled)
                    return
                }
                var configurationOpen = false
                var committed = false
                do {
                    let audio = AVAudioSession.sharedInstance()
                    try audio.setCategory(.playAndRecord, mode: .videoRecording, options: [.defaultToSpeaker, .allowBluetoothHFP])
                    try audio.setActive(true)
                    if self.isConfigured {
                        if !self.session.isRunning { self.session.startRunning() }
                        continuation.resume()
                        return
                    }
                    guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .front) else {
                        throw CameraError.frontCameraUnavailable
                    }
                    guard let microphone = AVCaptureDevice.default(for: .audio) else {
                        throw CameraError.microphoneUnavailable
                    }
                    let videoInput = try AVCaptureDeviceInput(device: camera)
                    let audioInput = try AVCaptureDeviceInput(device: microphone)
                    let output = AVCaptureMovieFileOutput()
                    self.session.beginConfiguration()
                    configurationOpen = true
                    // This is a controller-owned session. Remove every prior attempt before adding
                    // a new graph, so a failed permission/device configuration cannot accumulate
                    // duplicate inputs or outputs on a retry.
                    self.session.inputs.forEach(self.session.removeInput)
                    self.session.outputs.forEach(self.session.removeOutput)
                    self.session.sessionPreset = .high
                    self.session.automaticallyConfiguresApplicationAudioSession = false
                    guard self.session.canAddInput(videoInput), self.session.canAddInput(audioInput), self.session.canAddOutput(output) else {
                        throw CameraError.couldNotConfigure
                    }
                    self.session.addInput(videoInput)
                    self.session.addInput(audioInput)
                    self.session.addOutput(output)
                    self.configure1080p30IfSupported(camera)
                    if let connection = output.connection(with: .video) {
                        if connection.isVideoRotationAngleSupported(90) { connection.videoRotationAngle = 90 }
                        // Selfie comfort belongs only to the preview layer. Keep the source non-mirrored.
                        if connection.isVideoMirroringSupported {
                            connection.automaticallyAdjustsVideoMirroring = false
                            connection.isVideoMirrored = false
                        }
                    }
                    // AVFoundation rejects startRunning while a beginConfiguration transaction is
                    // open. Commit exactly once before starting hardware on the capture queue.
                    self.session.commitConfiguration()
                    committed = true
                    self.movieOutput = output
                    self.isConfigured = true
                    self.session.startRunning()
                    continuation.resume()
                } catch {
                    // Commit the transaction after removing partial graph entries. This leaves a
                    // retry with an empty, known-safe session instead of stale camera/mic inputs.
                    if configurationOpen {
                        self.session.inputs.forEach(self.session.removeInput)
                        self.session.outputs.forEach(self.session.removeOutput)
                        if !committed { self.session.commitConfiguration() }
                    }
                    self.session.stopRunning()
                    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
                    self.movieOutput = nil
                    self.isConfigured = false
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    /// Selects 1080p/30 only where the active device advertises it; other devices retain their
    /// safe `.high` configuration instead of applying an unsupported frame duration.
    private nonisolated func configure1080p30IfSupported(_ device: AVCaptureDevice) {
        guard let format = device.formats.first(where: { format in
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            return dimensions.width == 1_920 && dimensions.height == 1_080
                && format.videoSupportedFrameRateRanges.contains { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 }
        }) else { return }
        do {
            try device.lockForConfiguration()
            device.activeFormat = format
            let duration = CMTime(value: 1, timescale: 30)
            device.activeVideoMinFrameDuration = duration
            device.activeVideoMaxFrameDuration = duration
            device.unlockForConfiguration()
        } catch { }
    }

    private func observeLifecycle() {
        let center = NotificationCenter.default
        observers = [
            center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.handleInterruption("Audio was interrupted.") }
            },
            center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] notification in
                guard (notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue else { return }
                Task { @MainActor in self?.handleInterruption("The microphone connection changed.") }
            },
            center.addObserver(forName: AVCaptureSession.wasInterruptedNotification, object: session, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.handleInterruption("Camera capture was interrupted.") }
            },
            center.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: .main) { [weak self] notification in
                let detail = (notification.userInfo?[AVCaptureSessionErrorKey] as? NSError)?.localizedDescription ?? "Camera capture failed."
                Task { @MainActor in self?.handleInterruption(detail) }
            },
            center.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.handleInterruption("Recording stopped when the app went to the background.") }
            },
        ]
    }

    private func handleInterruption(_ message: String) {
        guard phase == .preparing || phase == .ready || phase == .recording || phase == .finalizing else { return }
        interrupted = true
        if phase == .recording || phase == .finalizing || startRequested {
            error = "\(message) A playable partial take will be kept."
            stopRecording(interrupted: true)
        } else {
            stopSession()
            error = message
            phase = .failed
        }
    }

    private func activateIdleTimerProtection() {
        guard priorIdleTimerDisabled == nil else { return }
        priorIdleTimerDisabled = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true
    }

    private func restoreIdleTimer() {
        guard let priorIdleTimerDisabled else { return }
        UIApplication.shared.isIdleTimerDisabled = priorIdleTimerDisabled
        self.priorIdleTimerDisabled = nil
    }

    private func tearDownSession() {
        captureQueue.async { [weak self] in
            guard let self else { return }
            self.session.stopRunning()
            self.movieOutput = nil
            self.isConfigured = false
            self.recordingRequested = false
            self.stopRequested = false
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
        recordingStartedAt = nil
        startRequested = false
        restoreIdleTimer()
        phase = .idle
    }

    private func fail(message: String) {
        error = message
        phase = .failed
        recordingStartedAt = nil
        startRequested = false
        restoreIdleTimer()
    }

    /// Completes an accepted record request that could not reach AVFoundation's didStart callback.
    /// This prevents the UI from waiting forever in the narrow start-requested state.
    private func finishStartupFailure(_ message: String) {
        guard startRequested else { return }
        stopSessionAfterFinalization = false
        tearDownSession()
        error = message
        phase = .failed
        onFinalized?(nil, message)
    }

    private static var usesUnavailableUITestFixture: Bool {
        let process = ProcessInfo.processInfo
        return process.arguments.contains("-ui-testing-chat")
            && process.environment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] == "1"
    }
}

extension TeleprompterCamera: AVCaptureFileOutputRecordingDelegate {
    nonisolated func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        Task { @MainActor [weak self] in
            guard let self, self.phase == .ready || self.phase == .finalizing else { return }
            self.recordingStartedAt = ProcessInfo.processInfo.systemUptime
            self.phase = self.phase == .finalizing ? .finalizing : .recording
            self.activateIdleTimerProtection()
            self.captureQueue.async { [weak self] in
                guard let self, self.stopRequested, let output = self.movieOutput, output.isRecording else { return }
                output.stopRecording()
            }
        }
    }

    nonisolated func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL, from connections: [AVCaptureConnection], error: (any Error)?) {
        Task { @MainActor [weak self] in
            await self?.finishRecording(outputFileURL, delegateError: error)
        }
    }
}

private extension TeleprompterCamera {
    func finishRecording(_ url: URL, delegateError: (any Error)?) async {
        phase = .finalizing
        let wasInterrupted = interrupted
        let validation = await Self.validateMovie(at: url)
        captureQueue.async { [weak self] in
            guard let self else { return }
            self.recordingRequested = false
            self.stopRequested = false
            // The session still has microphone input until the review flow tears it down.
            // Deactivate only after stopRunning in tearDownSession.
        }
        recordingStartedAt = nil
        startRequested = false
        restoreIdleTimer()

        switch validation {
        case .valid:
            finalizedURL = url
            phase = .ready
            // AVCapture sometimes supplies AVErrorRecordingSuccessfullyFinishedKey alongside a
            // successful file. The validated file, rather than that text, is authoritative.
            if wasInterrupted { error = "Recording was interrupted. This partial take is playable." }
            onFinalized?(url, wasInterrupted ? error : nil)
        case let .invalid(reason):
            let delegateDetail = delegateError.map { ": \($0.localizedDescription)" } ?? ""
            let message = "The recorded video couldn’t be finalized (\(reason))\(delegateDetail)"
            error = message
            phase = .failed
            onFinalized?(nil, message)
        }

        if stopSessionAfterFinalization {
            stopSessionAfterFinalization = false
            tearDownSession()
        }
    }

    enum MovieValidation {
        case valid
        case invalid(String)
    }

    static func validateMovie(at url: URL) async -> MovieValidation {
        let asset = AVURLAsset(url: url)
        do {
            let playable = try await asset.load(.isPlayable)
            let tracks = try await asset.load(.tracks)
            guard playable, tracks.contains(where: { $0.mediaType == .video }) else { return .invalid("no playable video track") }
            guard let audio = tracks.first(where: { $0.mediaType == .audio }) else { return .invalid("no audio track") }
            let audioDuration = try await audio.load(.timeRange).duration.seconds
            guard audioDuration.isFinite, audioDuration > 0 else { return .invalid("empty audio track") }
            let duration = try await asset.load(.duration).seconds
            guard duration.isFinite, duration > 0 else { return .invalid("empty movie") }
            return .valid
        } catch { return .invalid(error.localizedDescription) }
    }

    enum CameraError: LocalizedError {
        case frontCameraUnavailable
        case microphoneUnavailable
        case couldNotConfigure
        case cancelled

        var errorDescription: String? {
            switch self {
            case .frontCameraUnavailable: "The front camera is unavailable here. Recording with a script needs a physical iPhone."
            case .microphoneUnavailable: "The microphone is unavailable. Choose a video instead."
            case .couldNotConfigure: "The camera could not be configured. Try closing another app that is using the camera."
            case .cancelled: "Camera setup was cancelled."
            }
        }
    }
}

/// A preview-only surface: mirroring is deliberately confined here and never affects the movie output.
struct TeleprompterCameraPreview: UIViewRepresentable {
    @ObservedObject var camera: TeleprompterCamera

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = camera.session
        view.previewLayer.videoGravity = .resizeAspect
        view.configurePreviewConnection()
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        if uiView.previewLayer.session !== camera.session { uiView.previewLayer.session = camera.session }
        // `makeUIView` often precedes async preparation. This runs again after the controller
        // publishes its prepared phase, when the layer finally has a video connection.
        uiView.configurePreviewConnection()
    }

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }

        override func layoutSubviews() {
            super.layoutSubviews()
            configurePreviewConnection()
        }

        func configurePreviewConnection() {
            guard let connection = previewLayer.connection else { return }
            if connection.isVideoRotationAngleSupported(90) { connection.videoRotationAngle = 90 }
            if connection.isVideoMirroringSupported {
                connection.automaticallyAdjustsVideoMirroring = false
                connection.isVideoMirrored = true
            }
        }
    }
}
