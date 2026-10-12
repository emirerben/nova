import AVFoundation
import SwiftUI

/// A queue receipt is different from a completed attachment. The screen keeps
/// the local manifest until the existing intake proves the take was attached.
enum TeleprompterIntakeResult: Equatable { case queued, attached, failed(String) }

@MainActor final class TeleprompterRecordingFlow: ObservableObject {
    enum Stage: Equatable { case script, camera, countdown, starting, recording, finalizing, review }
    let store: TeleprompterStore
    let script: TeleprompterScript
    let camera = TeleprompterCamera()
    @Published var stage: Stage = .script
    @Published var error: String?
    @Published var countdown = 3
    @Published var selectedTake: TeleprompterTakeManifest?
    @Published var pendingTake: TeleprompterTakeManifest?
    @Published var usingTake = false
    @Published var queueAccepted = false
    @Published var attachmentFinished = false
    private var countdownTask: Task<Void, Never>?
    private var activeTake: TeleprompterTakeManifest?
    private var finalizedTake: TeleprompterTakeManifest?
    private var loaded = false
    private var generation = UUID()

    init(accountID: String, projectID: UUID, root: URL? = nil) {
        store = TeleprompterStore(accountID: accountID, projectID: projectID, root: root)
        script = TeleprompterScript()
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
            let recovery = try store.loadManifestsRecoveringCorruption()
            if let warning = recovery.recoveryWarning { self.error = warning }
            var takes = recovery.manifests.filter { $0.status != .accepted }
            for index in takes.indices where takes[index].status == .capturing {
                let valid = await Self.playable(store.fileURL(for: takes[index]))
                takes[index].status = valid ? .review : .invalid
                takes[index].interrupted = true
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
                let take = TeleprompterTakeManifest(script: self.script.draft,
                                                  slot: self.pendingTake == nil ? .pending : .candidate)
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
            if autoScrollAfterStart { script.resume() }
        } else if phase == .finalizing {
            script.pause(); stage = .finalizing; persistDraft()
        }
    }

    func stop(interrupted: Bool = false) {
        if stage == .countdown { cancelCountdown(); return }
        guard stage == .starting || stage == .recording else { return }
        script.pause(); persistDraft(); stage = .finalizing
        camera.stopRecording(interrupted: interrupted)
    }

    func leaveCapture() {
        cancelCountdown(); script.pause(); persistDraft()
        if stage == .starting || stage == .recording { stop(interrupted: true) }
        camera.stopSession()
    }

    private func finalized(url: URL?, message: String?) {
        guard var take = activeTake else { return }
        activeTake = nil
        take.status = url == nil ? .invalid : .review
        take.interrupted = camera.interrupted
        finalizedTake = take
        script.pause(); stage = .review
        error = message
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
            if let take = selectedTake, take.slot == .candidate { try store.remove(manifest: take, removingFile: true) }
            else if let take = selectedTake, take.status == .invalid {
                try store.remove(manifest: take, removingFile: true); pendingTake = nil
            }
            selectedTake = nil; error = nil; script.reset()
            await openCamera()
        } catch { self.error = "Couldn’t discard this candidate. Your previous take is still kept." }
    }

    func previousTake() {
        guard !usingTake, !queueAccepted, let previous = pendingTake,
              let current = selectedTake, current.id != previous.id else { return }
        do {
            try store.remove(manifest: current, removingFile: true)
            selectedTake = previous; error = nil; attachmentFinished = false
        } catch { self.error = "Couldn’t discard this candidate. Both takes are still kept." }
    }

    func discardSelected() {
        guard !usingTake, !queueAccepted, let take = selectedTake else { return }
        do {
            try store.remove(manifest: take, removingFile: true)
            if pendingTake?.id == take.id { pendingTake = nil }
            selectedTake = pendingTake; stage = selectedTake == nil ? .script : .review
            error = nil
        } catch { self.error = "Couldn’t discard this recording. It is still kept on this iPhone." }
    }

    func useTake(_ intake: (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult) async {
        guard !usingTake, !queueAccepted, var take = selectedTake, take.status == .review else { return }
        usingTake = true; defer { usingTake = false }
        do {
            let request = take
            let previous = pendingTake
            take.slot = .pending; take.status = .attaching; take.intakeAttempted = true
            try store.save(manifest: take) // Preserve the stable ID BEFORE enqueue.
            pendingTake = take; selectedTake = take
            if let previous, previous.id != take.id, previous.status != .accepted {
                try store.remove(manifest: previous, removingFile: true)
            }
            apply(await intake(request, store.fileURL(for: take)))
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
                try? FileManager.default.removeItem(at: store.fileURL(for: take))
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
