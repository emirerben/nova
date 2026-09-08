import AVFoundation
import SwiftUI

enum NativeTrimEdge: Sendable { case leading, trailing }

enum NativeEditorSaveState: Equatable, Sendable {
    case idle
    case saving
    case saved
    case previewPending
    case conflict
    case failed(String)
}

/// Local-first state for the Paper native editor. Every edit is a synchronous
/// value transaction; persistence happens only from the explicit Save action.
@MainActor final class NativeEditorSession: ObservableObject {
    @Published var draft: EditorDraft
    @Published var selectedClipID: UUID?
    @Published var currentTime: TimeInterval = 0
    @Published var duration: TimeInterval = 0
    @Published var isPlaying = false
    @Published var isSaving = false
    @Published var hasUnsavedChanges = false
    @Published var saveState: NativeEditorSaveState = .idle
    @Published var player: AVPlayer?
    @Published private(set) var canEditTimeline = true
    @Published private(set) var canEditText = true
    @Published private(set) var canEditCaptions = false
    @Published private(set) var canEditMix = false
    let operations: any EditorOperations

    private let minimumClipDuration: TimeInterval = 0.1
    private var undoStack: [EditorDraft] = []
    private var redoStack: [EditorDraft] = []
    private var cleanDraft: EditorDraft
    private var api: (any KriaAPIClient)?
    private var threadID: UUID?
    private var itemID: String?
    private var variantKey: String?
    private var jobID: UUID?
    private var previewRefreshTask: Task<Void, Never>?
    private var changedSections: Set<NativeEditorSection> = []
    private var activeTrim: ActiveTrim?
    nonisolated(unsafe) private var timeObserver: Any?
    nonisolated(unsafe) private var observingPlayer: AVPlayer?

    private enum NativeEditorSection: Hashable { case timeline, text, captions, mix }
    private struct ActiveTrim {
        let clipID: UUID
        let edge: NativeTrimEdge
        let baseline: EditorDraft
        let redoBaseline: [EditorDraft]
        var recordedUndo = false
    }

    init(
        draft: EditorDraft = EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0),
        operations: any EditorOperations = LocalEditorOperations(),
        initialPlaybackURL: URL? = nil
    ) {
        self.draft = draft
        self.operations = operations
        cleanDraft = draft
        duration = draft.clips.map(\.end).max() ?? 0
        // Deterministic UI fixtures exercise every implemented local control.
        // Real projects replace these optimistic defaults from status caps.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
            canEditCaptions = true
            canEditMix = draft.music != nil
        }
        if let initialPlaybackURL {
            installPlayer(url: initialPlaybackURL)
        }
    }

    convenience init(project: ProjectSummary, operations: any EditorOperations = LocalEditorOperations()) {
        self.init(draft: EditorDraft(projectID: project.id, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0), operations: operations)
        // A production editor starts fail-closed until the authoritative
        // variant advertises its renderer capabilities. The draft initializer
        // remains locally editable for deterministic fixtures and unit tests.
        canEditTimeline = false
        canEditText = false
        canEditCaptions = false
        canEditMix = false
    }

    deinit {
        previewRefreshTask?.cancel()
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
    }

    var canUndo: Bool { !undoStack.isEmpty }
    var canRedo: Bool { !redoStack.isEmpty }

    func load(api: any KriaAPIClient, threadID: UUID) async {
        self.api = api; self.threadID = threadID; isSaving = true; saveState = .saving
        defer { isSaving = false }
        do {
            let snapshot = try await api.draft(threadID: threadID)
            let jobID = snapshot.baseJobID.flatMap(UUID.init)
            self.jobID = jobID
            let authoritativeVariant: [String: JSONValue]?
            if let jobID { authoritativeVariant = try? await api.editorVariant(jobID: jobID, variantID: snapshot.variantKey) }
            else { authoritativeVariant = nil }
            draft = snapshot.editorDraft(projectID: threadID, authoritativeVariant: authoritativeVariant)
            configureCapabilities(from: authoritativeVariant)
            cleanDraft = draft; undoStack.removeAll(); redoStack.removeAll(); changedSections.removeAll(); hasUnsavedChanges = false; saveState = .idle
            itemID = snapshot.itemID; variantKey = snapshot.variantKey
            duration = draft.clips.map(\.end).max() ?? 0
            if let output = authoritativeVariant?["output_url"]?.stringValue, let url = URL(string: output) {
                installPlayer(url: url)
            } else if let jobID, let url = try? await api.playbackURL(jobID: jobID) {
                installPlayer(url: url)
            }
        } catch {
            saveState = .failed(error.localizedDescription)
        }
    }

    func load(project: ProjectSummary, api: any KriaAPIClient) async {
        await load(api: api, threadID: project.id)
        if player == nil, let jobID = project.activeJobID, let url = try? await api.playbackURL(jobID: jobID) { installPlayer(url: url) }
    }

    /// Gallery rows are render jobs, not creation-thread IDs. Promote the job
    /// through the server's idempotent editor route, then project its live
    /// variant into the same local draft model used by conversation projects.
    func load(libraryJobID: UUID, api: any KriaAPIClient) async {
        self.api = api
        threadID = nil
        jobID = libraryJobID
        isSaving = true
        saveState = .saving
        defer { isSaving = false }
        do {
            let receipt = try await api.openJobInEditor(jobID: libraryJobID)
            let variant = try await api.editorVariant(jobID: libraryJobID, variantID: receipt.variantID)
            let generation = variant["render_generation_id"]?.stringValue
                ?? variant["render_finished_at"]?.stringValue
                ?? ""
            let snapshot = DraftSnapshot(
                draftID: "gallery-\(libraryJobID.uuidString)",
                itemID: receipt.planItemID,
                variantKey: receipt.variantID,
                draftRevision: 0,
                snapshotHash: "",
                etag: "",
                baseJobID: libraryJobID.uuidString,
                baseGenerationID: generation,
                snapshot: [:],
                canUndo: false,
                createdAt: .now
            )
            draft = snapshot.editorDraft(projectID: draft.projectID, authoritativeVariant: variant)
            configureCapabilities(from: variant)
            cleanDraft = draft
            undoStack.removeAll()
            redoStack.removeAll()
            changedSections.removeAll()
            hasUnsavedChanges = false
            saveState = .idle
            itemID = receipt.planItemID
            variantKey = receipt.variantID
            refreshDuration()
            if let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
                installPlayer(url: url)
            } else if let url = try? await api.playbackURL(jobID: libraryJobID) {
                installPlayer(url: url)
            }
        } catch {
            saveState = .failed(error.localizedDescription)
        }
    }

    func togglePlayback() {
        guard let player else { isPlaying = false; return }
        if isPlaying { player.pause() } else { player.play() }
        isPlaying.toggle()
    }

    func seek(to time: TimeInterval) {
        let clamped = min(max(0, time), max(0, duration))
        currentTime = clamped
        player?.seek(to: CMTime(seconds: clamped, preferredTimescale: 600))
    }

    func selectClip(_ clipID: UUID?) { selectedClipID = clipID }
    func selectClip(_ clip: EditorClip?) { selectedClipID = clip?.id }

    func trimSelected(edge: NativeTrimEdge, to time: TimeInterval) {
        guard let id = selectedClipID, let clip = draft.clips.first(where: { $0.id == id }) else { return }
        let initialHandle: TimeInterval
        switch edge {
        case .leading: initialHandle = clip.start
        case .trailing: initialHandle = clip.end
        }
        beginTrim(clipID: id, edge: edge)
        updateTrim(by: time - initialHandle)
        endTrim()
    }

    /// Captures one immutable source window for the whole pointer gesture.
    /// Every preview update is derived from this baseline, so cumulative drag
    /// translations cannot be applied repeatedly to an already-trimmed clip.
    func beginTrim(clipID: UUID, edge: NativeTrimEdge) {
        guard canEditTimeline, draft.clips.contains(where: { $0.id == clipID }) else { return }
        activeTrim = ActiveTrim(
            clipID: clipID,
            edge: edge,
            baseline: draft,
            redoBaseline: redoStack
        )
    }

    func updateTrim(by translation: TimeInterval) {
        guard translation.isFinite, var active = activeTrim,
              let index = active.baseline.clips.firstIndex(where: { $0.id == active.clipID }) else { return }
        var next = active.baseline
        var clip = next.clips[index]
        switch active.edge {
        case .leading:
            // Left trim preserves the source Out point. A negative translation
            // restores earlier source frames until the file's beginning.
            let sourceOut = max(
                minimumClipDuration,
                min(clip.trimOut, clip.sourceDuration ?? clip.trimOut)
            )
            let nextIn = min(
                max(0, clip.trimIn + translation),
                max(0, sourceOut - minimumClipDuration)
            )
            clip.trimIn = nextIn
            clip.trimOut = sourceOut
        case .trailing:
            // Right trim preserves source In. Later clips are not a ceiling:
            // they ripple after this duration changes.
            let sourceIn = max(0, clip.trimIn)
            let currentOut = max(sourceIn + minimumClipDuration, clip.trimOut)
            let maximumOut = clip.sourceDuration.map {
                max(sourceIn + minimumClipDuration, $0)
            } ?? .greatestFiniteMagnitude
            clip.trimIn = sourceIn
            clip.trimOut = min(
                max(currentOut + translation, sourceIn + minimumClipDuration),
                maximumOut
            )
        }
        clip.end = clip.start + max(minimumClipDuration, clip.trimOut - clip.trimIn)
        next.clips[index] = clip
        reflow(&next.clips, from: index + 1)

        if next == active.baseline {
            if active.recordedUndo {
                undoStack.removeLast()
                redoStack = active.redoBaseline
                active.recordedUndo = false
            }
        } else if !active.recordedUndo {
            undoStack.append(active.baseline)
            redoStack.removeAll()
            active.recordedUndo = true
        }
        draft = next
        activeTrim = active
        refreshDuration()
        refreshDirtyState()
    }

    func endTrim() {
        activeTrim = nil
    }

    func moveSelected(by offset: TimeInterval) {
        guard canEditTimeline, let id = selectedClipID, let index = draft.clips.firstIndex(where: { $0.id == id }), abs(offset) > 0.000001 else { return }
        transact(section: .timeline) { draft in
            let destination = min(max(0, index + (offset < 0 ? -1 : 1)), draft.clips.count - 1)
            guard destination != index else { return }
            let clip = draft.clips.remove(at: index); draft.clips.insert(clip, at: destination); reflow(&draft.clips, from: 0)
        }
    }

    func slideSourceWindow(by offset: TimeInterval) {
        guard canEditTimeline, let id = selectedClipID, let index = draft.clips.firstIndex(where: { $0.id == id }) else { return }
        transact(section: .timeline) { draft in
            var clip = draft.clips[index]; let length = clip.trimOut - clip.trimIn
            let sourceEnd = clip.sourceDuration ?? max(clip.trimOut, length)
            let start = min(max(0, clip.trimIn + offset), max(0, sourceEnd - length))
            clip.trimIn = start; clip.trimOut = start + length; draft.clips[index] = clip
        }
    }

    func deleteSelectedClip() {
        guard canEditTimeline, draft.clips.count > 1, let id = selectedClipID, let index = draft.clips.firstIndex(where: { $0.id == id }) else { return }
        transact(section: .timeline) { draft in draft.clips.remove(at: index); reflow(&draft.clips, from: max(0, index)) }
        selectedClipID = draft.clips.indices.contains(index) ? draft.clips[index].id : draft.clips.last?.id
    }

    func addText(content: String? = nil) {
        guard canEditText else { return }
        let value = content?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty ?? "Your story"
        transact(section: .text) { $0.text.append(TextLayer(id: UUID(), content: value, position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")) }
    }

    func updateText(id: UUID, content: String) {
        guard canEditText else { return }
        transact(section: .text) { draft in if let index = draft.text.firstIndex(where: { $0.id == id }) { draft.text[index].content = content } }
    }

    func setTextStyle(id: UUID?, style: String) {
        guard canEditText else { return }
        transact(section: .text) { draft in
            let target = id ?? draft.text.last?.id
            if let target, let index = draft.text.firstIndex(where: { $0.id == target }) { draft.text[index].style = style }
        }
    }

    func toggleCaptions() { guard canEditCaptions else { return }; transact(section: .captions) { $0.captions.enabled.toggle() } }
    func setCaptionStyle(_ style: String) { guard canEditCaptions else { return }; transact(section: .captions) { $0.captions.style = style } }

    func setMusicVolume(_ volume: Double) {
        guard canEditMix, draft.music != nil else { return }
        transact(section: .mix) { $0.music?.volume = min(max(0, volume), 1) }
    }

    func undo() {
        guard let previous = undoStack.popLast() else { return }
        redoStack.append(draft); draft = previous; refreshDuration(); refreshDirtyState()
    }

    func redo() {
        guard let next = redoStack.popLast() else { return }
        undoStack.append(draft); draft = next; refreshDuration(); refreshDirtyState()
    }

    func save() async {
        guard !isSaving, hasUnsavedChanges else { return }
        guard let api, let itemID, let variantKey else { saveState = .failed("Load the project before saving edits."); return }
        isSaving = true; saveState = .saving
        defer { isSaving = false }
        let snapshot = draft.persistedSnapshot()
        let payload = Self.object(snapshot["editor_payload"]); let sections = Self.object(payload?["sections"])
        let request = EditorCommitRequest(
            timelineSlots: changedSections.contains(.timeline) ? Self.array(sections?["timeline_slots"]) : nil,
            textElements: changedSections.contains(.text) ? Self.array(sections?["text_elements"]) : nil,
            captionMeta: changedSections.contains(.captions) ? ["enabled": .bool(draft.captions.enabled), "style": .string(draft.captions.style)] : nil,
            mix: changedSections.contains(.mix) ? ["music_level": .number(draft.music?.volume ?? 0)] : nil,
            baseGeneration: payload?["base_generation"]?.stringValue ?? ""
        )
        do {
            let response = try await api.editorCommit(itemID: itemID, variantID: variantKey, request: request)
            var savedSnapshot = snapshot
            var payload = Self.object(savedSnapshot["editor_payload"]) ?? [:]
            payload["base_generation"] = .string(response.generation)
            savedSnapshot["editor_payload"] = .object(payload)
            draft.serverSnapshot = savedSnapshot
            cleanDraft = draft
            undoStack.removeAll()
            redoStack.removeAll()
            changedSections.removeAll()
            hasUnsavedChanges = false
            saveState = .previewPending
            startPreviewRefresh(generation: response.generation)
        } catch APIError.conflict { saveState = .conflict }
        catch { saveState = .failed(error.localizedDescription) }
    }

    private func transact(section: NativeEditorSection, _ body: (inout EditorDraft) -> Void) {
        var next = draft; body(&next); guard next != draft else { return }
        undoStack.append(draft); redoStack.removeAll(); draft = next; changedSections.insert(section); refreshDuration(); refreshDirtyState()
    }

    private func refreshDirtyState() {
        hasUnsavedChanges = draft != cleanDraft
        if draft.clips != cleanDraft.clips { changedSections.insert(.timeline) } else { changedSections.remove(.timeline) }
        if draft.text != cleanDraft.text { changedSections.insert(.text) } else { changedSections.remove(.text) }
        if draft.captions != cleanDraft.captions { changedSections.insert(.captions) } else { changedSections.remove(.captions) }
        if draft.music != cleanDraft.music { changedSections.insert(.mix) } else { changedSections.remove(.mix) }
    }
    private func reflow(_ clips: inout [EditorClip], from index: Int) {
        guard !clips.isEmpty else { return }
        let start = min(max(0, index), clips.count - 1)
        for i in start..<clips.count { let length = max(minimumClipDuration, clips[i].end - clips[i].start); let previousEnd = i == 0 ? 0 : clips[i - 1].end; clips[i].start = previousEnd; clips[i].end = previousEnd + length }
    }
    private func installPlayer(url: URL) {
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        let item = AVPlayerItem(url: url); let next = AVPlayer(playerItem: item); player = next
        observingPlayer = next
        let itemDuration = item.asset.duration.seconds
        if draft.clips.isEmpty, itemDuration.isFinite, itemDuration > 0 { duration = itemDuration }
        timeObserver = next.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.05, preferredTimescale: 600), queue: .main) { [weak self] time in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.currentTime = min(max(0, time.seconds), max(0, self.duration))
                self.isPlaying = next.timeControlStatus == .playing
            }
        }
    }
    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }

    private func refreshDuration() {
        duration = draft.clips.map(\.end).max() ?? 0
        if currentTime > duration { seek(to: duration) }
    }

    private func configureCapabilities(from variant: [String: JSONValue]?) {
        guard let variant else {
            // Preview fixtures remain interactive, but production never claims
            // renderer support without an authoritative status response.
            canEditTimeline = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor")
            canEditText = canEditTimeline
            canEditCaptions = canEditTimeline
            canEditMix = canEditTimeline && draft.music != nil
            return
        }
        let capabilities = Self.object(variant["editor_capabilities"])
        canEditTimeline = capabilities?["timeline"] == .bool(true)
        canEditText = capabilities?["text_elements"] == .bool(true)
        let archetype = variant["resolved_archetype"]?.stringValue
        canEditCaptions = ["subtitled", "narrated"].contains(archetype) && variant["base_video_path"]?.stringValue != nil
        canEditMix = capabilities?["mix"] == .bool(true) && draft.music != nil
    }

    private func startPreviewRefresh(generation: String) {
        previewRefreshTask?.cancel()
        guard let api, let jobID, let variantKey else { return }
        previewRefreshTask = Task { [weak self] in
            for _ in 0..<120 {
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled, let variant = try? await api.editorVariant(jobID: jobID, variantID: variantKey) else { continue }
                let currentGeneration = variant["render_generation_id"]?.stringValue ?? variant["render_finished_at"]?.stringValue
                guard currentGeneration == generation else { continue }
                let status = variant["render_status"]?.stringValue
                if status == "ready", let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
                    guard let self else { return }
                    self.rebaseCleanDraft(from: variant)
                    self.installPlayer(url: url)
                    self.saveState = .saved
                    return
                }
                if status == "failed" {
                    self?.saveState = .failed("The edit was saved, but its new preview could not be rendered.")
                    return
                }
            }
        }
    }

    /// The renderer may quantize requested durations to a 0.5-second or beat
    /// grid. Once its generation is ready, make those authoritative slots the
    /// new clean baseline so the handles and duration match the saved video.
    /// Never overwrite a follow-up edit made while the preview was rendering.
    @discardableResult
    func rebaseCleanDraft(from variant: [String: JSONValue]) -> Bool {
        guard !hasUnsavedChanges, let itemID, let variantKey else { return false }
        let generation = variant["render_generation_id"]?.stringValue
            ?? variant["render_finished_at"]?.stringValue
            ?? ""
        let snapshot = DraftSnapshot(
            draftID: "rendered-\(draft.projectID.uuidString)",
            itemID: itemID,
            variantKey: variantKey,
            draftRevision: draft.revision,
            snapshotHash: "",
            etag: draft.etag,
            baseJobID: jobID?.uuidString,
            baseGenerationID: generation,
            snapshot: draft.serverSnapshot,
            canUndo: false,
            createdAt: .now
        )
        let selected = selectedClipID
        draft = snapshot.editorDraft(projectID: draft.projectID, authoritativeVariant: variant)
        cleanDraft = draft
        undoStack.removeAll()
        redoStack.removeAll()
        changedSections.removeAll()
        hasUnsavedChanges = false
        if let selected, draft.clips.contains(where: { $0.id == selected }) {
            selectedClipID = selected
        } else {
            selectedClipID = nil
        }
        configureCapabilities(from: variant)
        refreshDuration()
        return true
    }
}

private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
