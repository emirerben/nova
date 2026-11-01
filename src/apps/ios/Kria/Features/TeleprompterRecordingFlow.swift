import AVFoundation
import SwiftUI

/// A queue receipt is different from a completed attachment. The screen keeps
/// the local manifest until the existing intake proves the take was attached.
enum TeleprompterIntakeResult: Equatable { case queued, attached, failed(String) }

/// KRI-1119: what the client knows about a background it handed to the visual upload next to a take. This is the seam
/// the server work consumes: nothing tells the server WHICH visual is the speaker background yet.
/// The visual's identity is `recordID`: the upload reserves it as `client_upload_id = "ios-<recordID>"` (the
/// reservation id is `UploadRecoveryRecord.visualReservationID` once reserved, and the pool asset `CreationVisual.id`
/// once analysed). Resolve it with `mediaID(in:)`.
struct CameraBackgroundSelection: Equatable, Sendable {
    let projectID: UUID
    let itemID: String
    let recordID: UUID
    let kind: SpeakerBackgroundKind
    let fileName: String

    var clientUploadID: String { "ios-\(recordID.uuidString)" }

    /// The reservation id the server minted for this upload, once the upload has reserved. Nil before that.
    func mediaID(in records: [UploadRecoveryRecord]) -> String? {
        records.first { $0.id == recordID }?.visualReservationID
    }
}

/// States (or clears) which visual is the background on the server. Returns how it ended.
typealias BackgroundStatement = (SpeakerBackgroundIntent) async -> SpeakerBackgroundOutcome

enum BackgroundIntakeResult: Equatable, Sendable { case queued(CameraBackgroundSelection), failed(String) }
/// Uploads a take's background file as a visual. Called after the take itself was handed to intake, never before.
typealias BackgroundIntake = (TeleprompterTakeManifest, SpeakerBackgroundReference, URL) async -> BackgroundIntakeResult

@MainActor final class TeleprompterRecordingFlow: ObservableObject {
    enum Stage: Equatable { case script, camera, countdown, starting, recording, finalizing, review }
    static let silentTakeMessage = "This take has no sound. Your microphone didn’t record anything, so it can’t be used for a talking video."
    static let quietTakeMessage = "We could barely hear you in this take."
    static let photosNoteMessage = "Couldn’t save this take to Photos. Allow access in Settings to keep a copy."
    let store: TeleprompterStore
    let script: TeleprompterScript
    let camera = TeleprompterCamera()
    private let viewRemovalGrace: Duration
    private var pendingViewRemoval: Task<Void, Never>?
    @Published var stage: Stage = .script
    @Published var error: String?
    @Published var countdown = 3
    @Published var selectedTake: TeleprompterTakeManifest?
    @Published var pendingTake: TeleprompterTakeManifest?
    @Published var usingTake = false
    @Published var queueAccepted = false
    @Published var attachmentFinished = false
    /// A non-blocking note when the raw take could not be copied to Photos (KRI-653).
    @Published var photosNote: String?
    /// KRI-1119: the background the creator is filming in front of (nil = none). Always nil while the capability is off.
    @Published private(set) var background: TeleprompterBackgroundChoice?
    /// A non-blocking note about the background (it could not be saved, or it could not be uploaded). Never blocks the take.
    @Published var backgroundNote: String?
    let speakerBackgroundEnabled: Bool
    /// The live matted preview, created the first time a background is shown. Nil without a background.
    @Published private(set) var livePreview: LiveBackgroundPreview?
    private let makeLivePreview: () -> LiveBackgroundPreview
    typealias BackgroundProviderFactory = @Sendable (SpeakerBackgroundReference, URL) async throws -> any LiveBackgroundProviding
    private let makeBackgroundProvider: BackgroundProviderFactory
    private let backgroundIntake: BackgroundIntake?
    private let backgroundStatement: BackgroundStatement?
    private var uploadingBackground = false
    private var statingBackground = false
    static let statementFailureNote = "Couldn’t tell Kria which picture is your background. Your take is saved."

    /// Set when the background reached the visual upload. The seam for the server to learn which visual it is.
    @Published private(set) var queuedBackground: CameraBackgroundSelection?
    private var backgroundLoad: Task<Void, Never>?
    private var countdownTask: Task<Void, Never>?
    private var activeTake: TeleprompterTakeManifest?
    private var finalizedTake: TeleprompterTakeManifest?
    private var loaded = false
    private var generation = UUID()
    private let photoSaver: PhotoLibrarySaving
    /// Photos copies the file during `performChanges`, so a take's movie must outlive its save.
    private var photoSaves: [UUID: Task<Bool, Never>] = [:]

    init(accountID: String, projectID: UUID, root: URL? = nil,
         script: TeleprompterScript = TeleprompterScript(),
         viewRemovalGrace: Duration = .seconds(1),
         photoSaver: PhotoLibrarySaving = PhotoLibrarySaver(),
         speakerBackgroundEnabled: Bool = false,
         makeLivePreview: @escaping () -> LiveBackgroundPreview = { LiveBackgroundPreview() },
         backgroundIntake: BackgroundIntake? = nil,
         backgroundStatement: BackgroundStatement? = nil,
         makeBackgroundProvider: @escaping BackgroundProviderFactory = TeleprompterRecordingFlow.liveBackgroundProvider,
         reportDiagnostic: @escaping (RecorderDiagnosticReport) -> Void = { _ in }) {
        self.speakerBackgroundEnabled = speakerBackgroundEnabled
        self.makeLivePreview = makeLivePreview
        self.backgroundIntake = backgroundIntake
        self.backgroundStatement = backgroundStatement
        self.makeBackgroundProvider = makeBackgroundProvider
        store = TeleprompterStore(accountID: accountID, projectID: projectID, root: root)
        self.script = script
        self.viewRemovalGrace = viewRemovalGrace
        self.photoSaver = photoSaver
        camera.reportDiagnostic = reportDiagnostic
        camera.onFinalized = { [weak self] url, message in self?.finalized(url: url, message: message) }
    }

    var captureBusy: Bool { [.countdown, .starting, .recording, .finalizing].contains(stage) }
    var editableText: String {
        get { script.text }
        set { script.updateText(newValue); persistDraft() }
    }

    func load() async {
        guard !loaded else { return }; loaded = true
        do {
            #if DEBUG
            try await TeleprompterReviewFixture.seedIfRequested(store: store)
            #endif
            do { if let draft = try store.loadDraft() { script.draft = draft } }
            catch { self.error = "Couldn’t restore the script. Your saved takes are still available." }
            restoreBackground()
            let recovery = try store.loadManifestsRecoveringCorruption()
            if let warning = recovery.recoveryWarning { self.error = warning }
            var takes = recovery.manifests.filter { $0.status != .accepted }
            for index in takes.indices where takes[index].status == .capturing {
                let valid = await Self.playable(store.fileURL(for: takes[index]))
                takes[index].status = valid ? .review : .invalid
                takes[index].interrupted = true
                if valid { takes[index].audioCheck = await TakeAudioMeasurement.measure(url: store.fileURL(for: takes[index])) }
                do { try store.save(manifest: takes[index]) }
                catch {
                    // A storage problem with one recovered candidate must not
                    // hide other reviewed takes. Use writes durable intent
                    // successfully before it can submit any recovered movie.
                    self.error = "Your recovered take is kept, but its details couldn’t be saved. Free some storage and try again."
                }
            }
            pendingTake = takes.last { $0.slot == .pending }
            selectedTake = takes.last { $0.slot == .candidate } ?? pendingTake
            queueAccepted = selectedTake?.status == .attaching
            if selectedTake != nil { stage = .review }
        } catch { self.error = "Couldn’t restore the saved script or take. Your local files are still kept. \(error.localizedDescription)" }
    }

    @discardableResult func persistDraft() -> Bool {
        do { try store.save(draft: script.draft); return true }
        catch { self.error = "Couldn’t save your script. Free some storage and try again. \(error.localizedDescription)"; return false }
    }

    func openCamera() async {
        guard !script.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, persistDraft() else { return }
        error = nil; stage = .camera
        await camera.prepare()
    }

    func editScript() {
        guard !captureBusy, !usingTake else { return }
        script.pause(); camera.stopSession(); persistDraft(); stage = .script
    }

    func record(manual: Bool) {
        guard stage == .camera, camera.phase == .ready, persistDraft() else { return }
        script.pause(); error = nil; stage = .countdown; countdown = 3
        let token = UUID(); generation = token
        countdownTask?.cancel()
        countdownTask = Task { [weak self] in
            for value in (1...3).reversed() {
                guard let self, self.generation == token, !Task.isCancelled else { return }
                self.countdown = value
                do { try await Task.sleep(for: .seconds(1)) } catch { return }
            }
            guard let self, self.generation == token, !Task.isCancelled, self.stage == .countdown else { return }
            guard self.camera.phase == .ready else { self.stage = .camera; return }
            do {
                var take = TeleprompterTakeManifest(script: self.script.draft,
                                                  slot: self.pendingTake == nil ? .pending : .candidate)
                take.speakerBackground = self.speakerBackgroundEnabled ? self.background?.reference : nil
                try self.store.save(manifest: take)
                self.activeTake = take
                self.autoScrollAfterStart = !manual
                self.stage = .starting
                self.camera.startRecording(to: self.store.fileURL(for: take))
            } catch {
                self.error = "Couldn’t save this take before recording. Free some storage and try again."
                self.stage = .camera
            }
        }
    }
    private var autoScrollAfterStart = true

    func cancelCountdown() {
        generation = UUID(); countdownTask?.cancel(); countdownTask = nil
        if stage == .countdown { stage = .camera }
    }

    func cameraChanged(_ phase: TeleprompterCamera.Phase) {
        // Published delivers this phase before the camera's stored value changes.
        if phase == .failed, stage == .countdown {
            cancelCountdown()
        } else if phase == .recording, stage == .starting {
            stage = .recording
            // The movie output has really started: a video background plays from its first frame, as the edit will.
            livePreview?.restartBackground()
            if autoScrollAfterStart { script.resume() }
        } else if phase == .finalizing {
            script.pause(); stage = .finalizing; persistDraft()
        }
    }

    func stop(reason: TakeStopReason = .user) {
        if stage == .countdown { cancelCountdown(); return }
        guard stage == .starting || stage == .recording else { return }
        script.pause(); persistDraft(); stage = .finalizing
        camera.stopRecording(reason: reason)
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .inactive:
            // KRI-653: whatever happened to the audio session while we were away (Control Center, an
            // alert, the app switcher), the next prepare() must rebuild the capture graph. Inactive
            // never takes the camera away, so a running take keeps going (KRI-1149). A countdown is not a take: it
            // is cancelled so the take never starts on a graph this call just marked stale.
            camera.sceneBecameInactive()
            if stage == .countdown { cancelCountdown() }
            if !captureBusy { script.pause(); persistDraft() }
        case .background:
            leaveCapture(reason: .backgrounded)
        case .active:
            camera.sceneBecameActive()
            // Privacy prompts briefly make the scene inactive. They must not
            // cancel the prepare operation whose grant they are requesting.
            if stage == .camera, camera.phase != .preparing { Task { await camera.prepare() } }
        @unknown default:
            break
        }
    }

    /// The screen can disappear and come straight back (transitions, overlays). A running take only ends when
    /// it stays gone past the grace; anything not yet recording is released at once.
    func viewDisappeared() {
        guard stage == .starting || stage == .recording else { leaveCapture(reason: .viewRemoved); return }
        pendingViewRemoval?.cancel()
        let grace = viewRemovalGrace
        pendingViewRemoval = Task {
            try? await Task.sleep(for: grace)
            guard !Task.isCancelled else { return }
            pendingViewRemoval = nil
            leaveCapture(reason: TakeStopPolicy.reason(for: .viewDisappeared(reappeared: false)) ?? .viewRemoved)
        }
    }

    func viewAppeared() {
        pendingViewRemoval?.cancel()
        pendingViewRemoval = nil
    }

    func leaveCapture(reason: TakeStopReason) {
        cancelCountdown(); script.pause(); persistDraft()
        if stage == .starting || stage == .recording { stop(reason: reason) }
        camera.stopSession()
    }

    #if DEBUG
    /// Test seam: the real path sets this when the countdown ends and the file output starts.
    func seedActiveTakeForTesting(_ take: TeleprompterTakeManifest) { activeTake = take }
    func seedSelectedTakeForTesting(_ take: TeleprompterTakeManifest) { selectedTake = take }
    #endif

    private func finalized(url: URL?, message: String?) {
        guard var take = activeTake else { return }
        activeTake = nil
        take.status = url == nil ? .invalid : .review
        take.interrupted = camera.interrupted
        take.stopReason = camera.stopReason
        take.audioCheck = url == nil ? nil : camera.finalizedAudioCheck
        // The choice at the end of filming wins: the picker may change while recording.
        take.speakerBackground = speakerBackgroundEnabled ? background?.reference : nil
        finalizedTake = take
        script.pause(); stage = .review
        error = message
        // KRI-653: a silent take is never attached. Say why before the creator reaches for "Use take".
        if take.audioCheck?.verdict == .silent { error = Self.silentTakeMessage }
        do {
            try store.save(manifest: take)
            selectedTake = take
            if take.slot == .pending { pendingTake = take }
            finalizedTake = nil
        } catch {
            // Keep the movie and in-memory candidate. Use stays disabled until
            // this durable transition can be written successfully.
            selectedTake = nil
            self.error = "Your recording is kept, but its details couldn’t be saved. Retry saving before using it."
        }
        camera.stopSession()
        // Every playable take is copied to Photos (silent ones too: the footage is still the creator's),
        // after the session is stopped so a first-run permission prompt cannot interrupt a recording.
        if take.status == .review { savePhotoCopy(of: take) }
    }

    // MARK: Speaker background (KRI-1119)

    private func restoreBackground() {
        guard speakerBackgroundEnabled else { return }
        do {
            guard let choice = try store.loadBackgroundChoice() else { return }
            if FileManager.default.fileExists(atPath: store.backgroundFileURL(for: choice.reference).path) {
                background = choice
                showBackgroundLive()
            } else { try? store.removeBackgroundChoice() }
        } catch { backgroundNote = "Couldn’t restore your background. Pick it again to see yourself in front of it." }
    }

    /// Copies a picked file into the take store and makes it the project's background. Nothing is uploaded here.
    func chooseBackground(from url: URL, kind: SpeakerBackgroundKind) {
        // Not during a take: changing the capture graph under a running recording is not something we measured.
        guard speakerBackgroundEnabled, !usingTake, !captureBusy else { return }
        do {
            let reference = try store.importBackground(from: url, kind: kind)
            let choice = TeleprompterBackgroundChoice(reference: reference)
            try store.save(backgroundChoice: choice)
            background = choice; backgroundNote = nil
            pruneBackgroundFiles()
            showBackgroundLive()
        } catch { backgroundNote = "Couldn’t save this background. Free some storage and try again." }
    }

    func clearBackground() {
        guard let cleared = background, !usingTake, !captureBusy else { return }
        // The server already knows this pick: it must hear about the clear too.
        if cleared.stated == true { try? store.markServerClearPending(eventID: "speaker-bg-clear-\(UUID().uuidString)") }
        background = nil; backgroundNote = nil
        try? store.removeBackgroundChoice()
        pruneBackgroundFiles()
        showBackgroundLive()
    }

    /// A photo is decoded once off the main thread; a video is a muted, looping player.
    static let liveBackgroundProvider: BackgroundProviderFactory = { reference, url in
        switch reference.kind {
        case .image: try await Task.detached { try StillBackground(url: url) }.value
        case .video: try await LoopingVideoBackground(url: url)
        }
    }

    /// Points the camera's frame output and the live preview at the current choice (or away from it). Any failure leaves
    /// the plain camera preview, and nothing here is on the recording's path.
    private func showBackgroundLive() {
        backgroundLoad?.cancel()
        guard let choice = background else {
            camera.setLiveFrameConsumer(nil)
            livePreview?.setBackground(nil)
            livePreview = nil
            return
        }
        let preview = livePreview ?? makeLivePreview()
        guard preview.isAvailable else { return }
        let url = store.backgroundFileURL(for: choice.reference), reference = choice.reference, make = makeBackgroundProvider
        backgroundLoad = Task { [weak self] in
            do {
                let provider = try await make(reference, url)
                guard let self, !Task.isCancelled, self.background?.reference == choice.reference else { provider.stop(); return }
                preview.setBackground(provider)
                self.livePreview = preview
                self.camera.setLiveFrameConsumer(preview)
            } catch is CancellationError {
            } catch {
                guard let self, self.background?.reference == choice.reference else { return }
                self.camera.setLiveFrameConsumer(nil)
                self.livePreview = nil
                self.backgroundNote = "Couldn’t show this background in the preview. It is still saved with your take."
            }
        }
    }

    /// Hands the take's background file to the visual upload once, then (re)states which visual it is. Either failing is a
    /// note: the take stays attached.
    func uploadBackgroundIfNeeded() async {
        await handOverBackground()
        await syncBackgroundStatement()
    }

    /// Tells the server which visual is the background, or that it was cleared. Safe to call at any time: it sends only
    /// what the server has not heard yet, with idempotency keys that are stable across retries and relaunches. A failure
    /// keeps the choice and is tried again at the next opening.
    func syncBackgroundStatement() async {
        guard speakerBackgroundEnabled, let backgroundStatement, !statingBackground else { return }
        statingBackground = true; defer { statingBackground = false }
        let pendingSet = background.flatMap { $0.uploaded == true && $0.stated != true ? $0 : nil }
        if let clearID = store.pendingServerClearEventID {
            // A new pick replaces the old one on the server, so its set makes the clear unnecessary.
            if pendingSet == nil {
                switch await backgroundStatement(.clear(eventID: clearID)) {
                case .saved, .unavailable: store.removeServerClearPending()
                case .failed: backgroundNote = Self.statementFailureNote; return
                }
            } else { store.removeServerClearPending() }
        }
        guard let choice = pendingSet else { return }
        let intent = SpeakerBackgroundIntent.set(clientUploadID: "ios-\(choice.reference.id.uuidString)",
                                                 eventID: "speaker-bg-set-\(choice.reference.id.uuidString)")
        switch await backgroundStatement(intent) {
        case .saved, .unavailable:
            // Unavailable means the server turned it off: nothing more to say, and nothing to retry.
            markBackgroundStated(choice.reference.id)
        case .failed:
            backgroundNote = Self.statementFailureNote
        }
    }

    private func markBackgroundStated(_ id: UUID) {
        guard var choice = background, choice.reference.id == id else { return }
        choice.stated = true
        guard (try? store.save(backgroundChoice: choice)) != nil else { return }
        background = choice
    }

    private func handOverBackground() async {
        guard speakerBackgroundEnabled, let backgroundIntake, !uploadingBackground,
              let take = selectedTake ?? pendingTake, take.status == .attaching || take.status == .accepted,
              let reference = take.speakerBackground else { return }
        if background?.reference.id == reference.id, background?.uploaded == true { return }
        let file = store.backgroundFileURL(for: reference)
        guard FileManager.default.fileExists(atPath: file.path) else {
            backgroundNote = "Your take was added, but its background file is gone. Add the background again from your edit."
            return
        }
        uploadingBackground = true; defer { uploadingBackground = false }
        switch await backgroundIntake(take, reference, file) {
        case .queued(let selection):
            queuedBackground = selection; backgroundNote = nil
            markBackgroundUploaded(reference.id)
        case .failed(let message):
            backgroundNote = "Your take was added, but its background couldn’t be uploaded. \(message)"
        }
    }

    /// Records that the background file reached the visual upload, so later takes do not upload it again.
    private func markBackgroundUploaded(_ id: UUID) {
        guard var choice = background, choice.reference.id == id, choice.uploaded != true else { return }
        choice.uploaded = true
        guard (try? store.save(backgroundChoice: choice)) != nil else { return }
        background = choice
    }

    private func pruneBackgroundFiles() {
        var names = Set<String>()
        if let background { names.insert(background.reference.fileName) }
        for take in [selectedTake, pendingTake, activeTake, finalizedTake] {
            if let name = take?.speakerBackground?.fileName { names.insert(name) }
        }
        store.pruneBackgrounds(keeping: names)
    }

    // MARK: Photos copy of the raw take (KRI-653)

    private func savePhotoCopy(of take: TeleprompterTakeManifest) {
        let url = store.fileURL(for: take), id = take.id, saver = photoSaver
        photosNote = nil
        let save = Task<Bool, Never> { (try? await saver.saveVideo(at: url)) != nil }
        photoSaves[id] = save
        Task { [weak self] in
            let saved = await save.value
            guard let self else { return }
            self.photoSaves[id] = nil
            self.recordPhotoSave(id: id, saved: saved)
        }
    }

    private func recordPhotoSave(id: UUID, saved: Bool) {
        if !saved { photosNote = Self.photosNoteMessage }
        for candidate in [selectedTake, pendingTake] {
            guard var take = candidate, take.id == id else { continue }
            take.savedToPhotos = saved
            if (try? store.save(manifest: take)) != nil {
                if selectedTake?.id == id { selectedTake = take }
                if pendingTake?.id == id { pendingTake = take }
            }
        }
    }

    /// Deletes a take's movie, but never while Photos is still reading it.
    private func deleteMovie(of take: TeleprompterTakeManifest) {
        let url = store.fileURL(for: take)
        guard let pending = photoSaves[take.id] else { try? FileManager.default.removeItem(at: url); return }
        Task { _ = await pending.value; try? FileManager.default.removeItem(at: url) }
    }

    /// `store.remove(manifest:removingFile: true)`, with the movie removal deferred past a pending Photos save.
    private func removeTake(_ take: TeleprompterTakeManifest) throws {
        guard photoSaves[take.id] != nil else { try store.remove(manifest: take, removingFile: true); return }
        try store.remove(manifest: take, removingFile: false)
        deleteMovie(of: take)
    }

    var needsMetadataRetry: Bool { finalizedTake != nil }
    func retryMetadata() {
        guard let take = finalizedTake else { return }
        do {
            try store.save(manifest: take)
            selectedTake = take
            if take.slot == .pending { pendingTake = take }
            finalizedTake = nil; error = nil
        } catch { self.error = "Couldn’t save the recording details. Your video is still kept. \(error.localizedDescription)" }
    }

    func retake() async {
        guard !usingTake, !queueAccepted, selectedTake?.status != .attaching else { return }
        do {
            if let take = selectedTake, take.slot == .candidate { try removeTake(take) }
            else if let take = selectedTake, take.status == .invalid {
                try removeTake(take); pendingTake = nil
            }
            selectedTake = nil; error = nil; script.reset()
            await openCamera()
        } catch { self.error = "Couldn’t discard this candidate. Your previous take is still kept." }
    }

    func previousTake() {
        guard !usingTake, !queueAccepted, let previous = pendingTake,
              let current = selectedTake, current.id != previous.id else { return }
        do {
            try removeTake(current)
            selectedTake = previous; error = nil; attachmentFinished = false
        } catch { self.error = "Couldn’t discard this candidate. Both takes are still kept." }
    }

    func discardSelected() {
        guard !usingTake, !queueAccepted, let take = selectedTake else { return }
        do {
            try removeTake(take)
            if pendingTake?.id == take.id { pendingTake = nil }
            selectedTake = pendingTake; stage = selectedTake == nil ? .script : .review
            error = nil
        } catch { self.error = "Couldn’t discard this recording. It is still kept on this iPhone." }
    }

    func useTake(_ intake: (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult) async {
        guard !usingTake, !queueAccepted, var take = selectedTake, take.status == .review,
              take.audioCheck?.verdict != .silent else { return }
        usingTake = true; defer { usingTake = false }
        do {
            let request = take
            let previous = pendingTake
            take.slot = .pending; take.status = .attaching; take.intakeAttempted = true
            try store.save(manifest: take) // Preserve the stable ID BEFORE enqueue.
            pendingTake = take; selectedTake = take
            if let previous, previous.id != take.id, previous.status != .accepted {
                try removeTake(previous)
            }
            let result = await intake(request, store.fileURL(for: take))
            apply(result)
            // The take is handed over first and never waits on, or fails because of, its background.
            if case .failed = result { return }
            await uploadBackgroundIfNeeded()
        } catch { self.error = "Your video is kept, but its details couldn’t be saved. Try again." }
    }

    func apply(_ result: TeleprompterIntakeResult) {
        guard var take = selectedTake else { return }
        switch result {
        case .queued: queueAccepted = true; error = nil
        case .attached:
            take.status = .accepted
            do {
                try store.save(manifest: take)
                selectedTake = take; pendingTake = nil; queueAccepted = false; attachmentFinished = true; error = nil
                // Intake owns a separate durable project original now. Keep the
                // immutable script manifest, release this duplicate movie.
                deleteMovie(of: take)
            } catch { self.error = "The take was added. Couldn’t save that receipt yet; retry checking its status." }
        case .failed(let message):
            take.status = .review
            do { try store.save(manifest: take); selectedTake = take; pendingTake = take }
            catch { self.error = "Your take is kept. Couldn’t save its retry state."; return }
            queueAccepted = false; error = message
        }
    }

    static func playable(_ url: URL) async -> Bool {
        let asset = AVURLAsset(url: url)
        guard let playable = try? await asset.load(.isPlayable), playable,
              let duration = try? await asset.load(.duration).seconds, duration.isFinite, duration > 0,
              let video = try? await asset.loadTracks(withMediaType: .video), !video.isEmpty,
              let audio = try? await asset.loadTracks(withMediaType: .audio), !audio.isEmpty else { return false }
        return true
    }
}
