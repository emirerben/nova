import AVFoundation
import SwiftUI

enum NativeTrimEdge: Sendable { case leading, trailing }

enum NativeEditorSaveState: Equatable, Sendable {
    case idle
    case saving
    case saved
    case previewPending
    case renderRetryNeeded(String)
    case conflict
    case loadFailed(String)
    case previewFailed(String)
    case failed(String)
}

enum NativeEditorLoadState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case failed(String)
}

@MainActor final class NativeEditorPlaybackClock: ObservableObject {
    @Published var currentTime: TimeInterval = 0
}

/// Local-first state for the Paper native editor. Every edit is a synchronous
/// value transaction; persistence happens only from the explicit Save action.
@MainActor final class NativeEditorSession: ObservableObject {
    /// Canonical mutable editor state. The legacy `draft` accessor below is a
    /// compatibility projection for first-pass views and test fixtures.
    @Published private(set) var document: EditorDocument {
        didSet {
            draftProjectionCache = nil
            timelineItemsCache = nil
            timelineClipsCache = nil
            timelineProjectionCache = nil
            previewTextCache = nil
        }
    }
    /// Cross-kind selection is the source of truth. `selectedClipID` below is
    /// a source-compatible adapter for the first-pass clip views.
    @Published private(set) var selection: EditorSelection?
    let playbackClock = NativeEditorPlaybackClock()
    var currentTime: TimeInterval {
        get { playbackClock.currentTime }
        set { playbackClock.currentTime = newValue }
    }
    @Published var duration: TimeInterval = 0
    @Published var isPlaying = false
    @Published var isSaving = false
    @Published var hasUnsavedChanges = false
    @Published var saveState: NativeEditorSaveState = .idle
    @Published private(set) var loadState: NativeEditorLoadState = .loaded
    @Published var player: AVPlayer?
    @Published private(set) var isDirectManipulating = false
    @Published private(set) var canEditTimeline = true
    @Published private(set) var canEditText = true
    @Published private(set) var canEditCaptions = false
    @Published private(set) var canEditMix = false
    let operations: any EditorOperations
    @Published private(set) var rendersOnDevice = false
    private var deviceRenders: DeviceRenderSessions?
    private var guidedRevisionNumber: Int?
    var deviceRenderKey: DeviceRenderKey? {
        guard rendersOnDevice, let jobID, let variantKey else { return nil }
        return DeviceRenderKey(projectID: threadID ?? projectID, jobID: jobID, variantID: variantKey)
    }

    func useDeviceRendering(_ sessions: DeviceRenderSessions) { deviceRenders = sessions }

    func refreshDeviceRender(retry: Bool = false) async {
        guard let key = deviceRenderKey, let api, let deviceRenders else { return }
        let capabilities = (try? await api.creationCapabilities())?.phoneRendering ?? .disabled
        await deviceRenders.reconcile(key, capabilities: capabilities, retry: retry)
    }

    func showDeviceOutput(_ url: URL) {
        guard rendersOnDevice else { return }
        installPlayer(url: url, preferredDuration: authoritativeDuration)
    }

    private let projectID: UUID
    private var etag: String = ""

    var draft: EditorDraft {
        get {
            if let draftProjectionCache { return draftProjectionCache }
            var projected = projectedDraft(from: document)
            if api == nil, itemID == nil {
                for index in projected.clips.indices {
                    if let metadata = compatibilityClipMetadata[projected.clips[index].id] {
                        projected.clips[index].sourceClipIndex = metadata.sourceClipIndex
                        projected.clips[index].slotID = metadata.slotID
                    }
                }
                projected.serverSnapshot = compatibilitySnapshot
            }
            draftProjectionCache = projected
            return projected
        }
        set {
            etag = newValue.etag
            if !newValue.serverSnapshot.isEmpty { compatibilitySnapshot = newValue.serverSnapshot }
            var next = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(newValue))
            next.revision.number = newValue.revision
            rememberClipIDs(from: newValue, in: next)
            rememberCompatibilityMetadata(from: newValue)
            document = next
        }
    }

    private let minimumClipDuration: TimeInterval = 0.1
    private let historyLimit = 100
    private var undoStack: [EditorDocument] = []
    private var redoStack: [EditorDocument] = []
    private var cleanDocument: EditorDocument
    private var api: (any KriaAPIClient)?
    private var threadID: UUID?
    private var itemID: String?
    private var variantKey: String?
    private var jobID: UUID?
    private var previewRefreshTask: Task<Void, Never>?
    private var pendingPreviewGeneration: String?
    private var changedSections: Set<EditorSection> = []
    private var explicitlyDirtySections: Set<EditorSection> = []
    private var pendingRenderRetrySections: Set<EditorSection> = []
    private var clipIDsBySlot: [String: UUID] = [:]
    private var compatibilityClipMetadata: [UUID: (sourceClipIndex: Int?, slotID: String?)] = [:]
    private var compatibilitySnapshot: [String: JSONValue] = [:]
    private var activeTrim: ActiveTrim?
    private var transactionBaseline: EditorDocument?
    private var draftProjectionCache: EditorDraft?
    private var activeTimedEdit: ActiveTimedEdit?
    private var timelineItemsCache: [NativeEditorTimelineItem]?
    private var timelineClipsCache: [EditorClip]?
    private var timelineProjectionCache: NativeEditorTimelineProjection?
    private var previewTextCache: [TextLayer]?
    /// The local timeline is optimistic while the rendered variant and its
    /// AVPlayerItem arrive asynchronously. Keep those duration sources
    /// separate so a stale projection cannot hide the rendered tail.
    private var authoritativeDuration: TimeInterval?
    private var mediaDuration: TimeInterval?
    /// Duration-changing edits temporarily use the optimistic local
    /// projection. Keep the last rendered durations so undoing back to the
    /// clean document restores the exact playable boundary.
    private var durationSourcesInvalidated = false
    private(set) var timelineProjectionBuildCount = 0
    nonisolated(unsafe) private var timeObserver: Any?
    nonisolated(unsafe) private var observingPlayer: AVPlayer?
    nonisolated(unsafe) private var endObserver: NSObjectProtocol?
    private var durationLoadTask: Task<Void, Never>?
    private let playbackEndTolerance: TimeInterval = 0.05

    private struct ActiveTrim {
        let clipID: UUID
        let edge: NativeTrimEdge
        let baseline: EditorDocument
        let redoBaseline: [EditorDocument]
        var recordedUndo = false
    }

    private enum TimedEditKind { case move, trim }
    private struct ActiveTimedEdit {
        let selection: EditorSelection
        let edge: NativeTrimEdge?
        let kind: TimedEditKind
        let baseline: EditorDocument
        let redoBaseline: [EditorDocument]
        var recordedUndo = false
    }

    init(
        draft: EditorDraft = EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0),
        operations: any EditorOperations = LocalEditorOperations(),
        initialPlaybackURL: URL? = nil
    ) {
        var initialDocument = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(draft))
        initialDocument.revision.number = draft.revision
        self.projectID = draft.projectID
        self.etag = draft.etag
        self.compatibilitySnapshot = draft.serverSnapshot
        self.document = initialDocument
        self.cleanDocument = initialDocument
        self.operations = operations
        rememberClipIDs(from: draft, in: initialDocument)
        rememberCompatibilityMetadata(from: draft)
        duration = timelineProjection.totalDuration
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
        self.init(draft: EditorDraft(projectID: project.id, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0), operations: operations, initialPlaybackURL: project.outputURL)
        // A production editor starts fail-closed until the authoritative
        // variant advertises its renderer capabilities. The draft initializer
        // remains locally editable for deterministic fixtures and unit tests.
        canEditTimeline = false
        canEditText = false
        canEditCaptions = false
        canEditMix = false
        loadState = .idle
    }

    deinit {
        previewRefreshTask?.cancel()
        durationLoadTask?.cancel()
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    }

    var canUndo: Bool { !undoStack.isEmpty }
    var canRedo: Bool { !redoStack.isEmpty }
    var selectedClipID: UUID? {
        get { guard selection?.kind == .clip else { return nil }; return UUID(uuidString: selection?.id ?? "") }
        set { selection = newValue.map { EditorSelection(kind: .clip, id: $0.uuidString) } }
    }
    var selectedSelection: EditorSelection? { selection }
    var dirtySections: Set<EditorSection> { changedSections }
    func isDirty(_ section: EditorSection) -> Bool { changedSections.contains(section) }
    func markDirty(_ section: EditorSection) { explicitlyDirtySections.insert(section); changedSections.insert(section); hasUnsavedChanges = true }

    // Capability and inspector routing are intentionally read-only. Views can
    // explain a disabled control without duplicating server capability logic.
    func capability(_ key: String) -> EditorCapability? { document.capabilities[key] }
    func capabilityReason(_ key: String) -> String? { document.capabilities[key]?.reason }
    func canEdit(_ key: String) -> Bool { document.capabilities[key]?.editable ?? false }
    func canEdit(_ section: EditorSection) -> Bool { canEditSection(section) }
    func capability(for section: EditorSection) -> EditorCapability? {
        for key in sectionCapabilityKeys(section) { if let capability = document.capabilities[key] { return capability } }
        return nil
    }
    func selectedObject() -> EditorSelection? { selection }
    func inspectorSelection() -> EditorSelection? { selection }
    func inspectorRoute() -> EditorSelectionKind? { selection?.kind }

    /// Wrap a drag/pinch/typing gesture in one undo transaction. Calls to the
    /// typed mutation APIs while a transaction is open update the document but
    /// do not append additional undo entries.
    func beginTransaction() {
        guard transactionBaseline == nil else { return }
        transactionBaseline = document
    }
    func endTransaction() {
        if let baseline = transactionBaseline, document != baseline {
            appendUndo(baseline); redoStack.removeAll()
        }
        transactionBaseline = nil
    }

    private func appendUndo(_ value: EditorDocument) {
        undoStack.append(value)
        if undoStack.count > historyLimit {
            undoStack.removeFirst(undoStack.count - historyLimit)
        }
    }

    private func appendRedo(_ value: EditorDocument) {
        redoStack.append(value)
        if redoStack.count > historyLimit {
            redoStack.removeFirst(redoStack.count - historyLimit)
        }
    }
    func beginEditTransaction() { beginTransaction() }
    func endEditTransaction() { endTransaction() }
    func beginDirectManipulation() {
        isDirectManipulating = true
        beginTransaction()
    }
    func endDirectManipulation() {
        endTransaction()
        isDirectManipulating = false
    }

    /// Selects any timeline/preview object. Selection seeks without changing
    /// playback state; callers can opt out for compatibility with old clip
    /// buttons that only changed the inspector.
    func select(_ value: EditorSelection?, seekToStart: Bool = true) {
        selection = value
        guard seekToStart, let value, let start = startTime(for: value) else { return }
        seek(to: start)
    }

    func select(_ item: NativeEditorTimelineItem, seekToStart: Bool = true) {
        select(item.selection, seekToStart: seekToStart)
    }

    func cycleSelection(at time: TimeInterval = -1) {
        let time = time >= 0 ? time : currentTime
        let next = NativeEditorInteraction.cycleSelection(in: timelineItems, at: time, current: selection)
        select(next, seekToStart: false)
    }

    var timelineItems: [NativeEditorTimelineItem] {
        if let timelineItemsCache { return timelineItemsCache }
        timelineProjectionBuildCount += 1
        let projection = timelineProjection
        var items: [NativeEditorTimelineItem] = timelineClips.enumerated().map { index, clip in
            NativeEditorTimelineItem(
                selection: EditorSelection(kind: .clip, id: clip.id.uuidString),
                start: clip.start,
                end: clip.end,
                sourceIndex: index
            )
        }
        func projected(
            _ selection: EditorSelection,
            start: TimeInterval,
            end: TimeInterval,
            zIndex: Int,
            sourceIndex: Int
        ) -> NativeEditorTimelineItem {
            let range = projection.projectBaseInterval(start: start, end: end)
            return NativeEditorTimelineItem(
                selection: selection,
                start: range.start,
                end: range.end,
                zIndex: zIndex,
                sourceIndex: sourceIndex
            )
        }
        items += document.textElements.enumerated().map { index, item in projected(EditorSelection(kind: .text, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 300 + index), sourceIndex: index) }
        items += document.captionCues.enumerated().map { index, item in projected(EditorSelection(kind: .captionCue, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 400 + index), sourceIndex: index) }
        items += document.soundEffects.enumerated().map { index, item in projected(EditorSelection(kind: .soundEffect, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 100 + index), sourceIndex: index) }
        items += document.mediaOverlays.enumerated().map { index, item in projected(EditorSelection(kind: .mediaOverlay, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 200 + index), sourceIndex: index) }
        items += document.visualBlocks.enumerated().map { index, item in projected(EditorSelection(kind: .visualBlock, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 150 + index), sourceIndex: index) }
        items += document.motionScenes.enumerated().map { index, item in projected(EditorSelection(kind: .motionScene, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 250 + index), sourceIndex: index) }
        items += document.cameraEffects.enumerated().map { index, item in projected(EditorSelection(kind: .cameraEffect, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 260 + index), sourceIndex: index) }
        if let carousel = projection.carouselItem { items.append(carousel) }
        if let music = document.music {
            items.append(
                NativeEditorTimelineItem(
                    selection: EditorSelection(kind: .music, id: music.trackID),
                    start: 0,
                    end: max(minimumClipDuration, projection.totalDuration),
                    zIndex: 50,
                    sourceIndex: 0
                )
            )
        }
        timelineItemsCache = items
        return items
    }

    var timelineProjection: NativeEditorTimelineProjection {
        if let timelineProjectionCache { return timelineProjectionCache }
        let value = NativeEditorInteraction.timelineProjection(
            slots: document.clips,
            carousel: document.carouselMoment
        )
        timelineProjectionCache = value
        return value
    }

    /// Canonical clip projection for timeline/media views. This deliberately
    /// avoids the compatibility `draft` encoder on playback-clock ticks.
    var timelineClips: [EditorClip] {
        if let timelineClipsCache { return timelineClipsCache }
        let clips = timelineProjection.clipWindows.compactMap { window -> EditorClip? in
            guard document.clips.indices.contains(window.sourceIndex) else { return nil }
            let slot = document.clips[window.sourceIndex]
            let id = clipID(for: slot.id)
            let duration = max(minimumClipDuration, window.end - window.start)
            let sourceStart = max(0, slot.inS)
            let sourceDuration = Self.number(slot.raw["source_duration_s"] ?? slot.raw["source_duration"])
            let assetID = slot.raw["asset_id"]?.stringValue.flatMap(UUID.init(uuidString:)) ?? id
            return EditorClip(
                id: id,
                assetID: assetID,
                sourceClipIndex: slot.clipIndex,
                start: window.start,
                end: window.end,
                trimIn: sourceStart,
                trimOut: sourceStart + duration,
                sourceDuration: sourceDuration,
                muted: slot.raw["muted"] == .bool(true),
                slotID: slot.id
            )
        }
        timelineClipsCache = clips
        return clips
    }

    /// Text projection used by the preview without serializing the document.
    var previewTextLayers: [TextLayer] {
        if let previewTextCache { return previewTextCache }
        let layers = document.textElements.compactMap { item -> TextLayer? in
            guard let id = UUID(uuidString: item.id) else { return nil }
            let x = Self.number(item.raw["x_frac"] ?? item.raw["x"]) ?? 0.5
            let y = Self.number(item.raw["y_frac"] ?? item.raw["y"]) ?? 0.5
            let style = item.raw["font_family"]?.stringValue ?? item.raw["style"]?.stringValue ?? "Fraunces"
            return TextLayer(id: id, content: item.text, position: CGPoint(x: x, y: y), style: style)
        }
        previewTextCache = layers
        return layers
    }

    func load(api: any KriaAPIClient, threadID: UUID, variantID: String? = nil, allowPlaybackFallback: Bool = true) async {
        self.api = api; self.threadID = threadID; isSaving = true; saveState = .saving; loadState = .loading
        defer { isSaving = false }
        do {
            let snapshot = try await api.draft(threadID: threadID)
            let jobID = snapshot.baseJobID.flatMap(UUID.init)
            self.jobID = jobID
            let authoritativeVariant: [String: JSONValue]?
            let requestedVariantKey = variantID ?? snapshot.variantKey
            if let jobID { authoritativeVariant = try await api.editorVariant(jobID: jobID, variantID: requestedVariantKey) }
            else { authoritativeVariant = nil }
            draft = snapshot.editorDraft(projectID: threadID, authoritativeVariant: authoritativeVariant)
            configureCapabilities(from: authoritativeVariant)
            cleanDocument = document; undoStack.removeAll(); redoStack.removeAll(); changedSections.removeAll(); explicitlyDirtySections.removeAll(); pendingRenderRetrySections.removeAll(); hasUnsavedChanges = false; saveState = .idle
            itemID = snapshot.itemID; variantKey = requestedVariantKey
            durationSourcesInvalidated = false
            setAuthoritativeDuration(Self.number(authoritativeVariant?["duration_s"]))
            refreshDuration()
            if let output = authoritativeVariant?["output_url"]?.stringValue, let url = URL(string: output) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            } else if allowPlaybackFallback, let jobID, let url = try? await api.playbackURL(jobID: jobID) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            }
            loadState = .loaded
        } catch {
            saveState = .loadFailed(error.localizedDescription)
            loadState = .failed(error.localizedDescription)
        }
    }

    func load(project: ProjectSummary, api: any KriaAPIClient) async {
        loadState = .loading
        var resolvedProject = project
        if project.activeJobID != nil,
           (project.activePlanItemID == nil || project.outputVariantID == nil),
           let refreshed = try? await api.project(threadID: project.id) {
            resolvedProject = refreshed.summary
        }
        if let jobID = resolvedProject.activeJobID,
           let itemID = resolvedProject.activePlanItemID,
           let variantID = resolvedProject.outputVariantID {
            await load(
                editorJobID: jobID,
                planItemID: itemID,
                preferredVariantID: variantID,
                threadID: project.id,
                api: api
            )
        } else if let jobID = resolvedProject.activeJobID {
            await load(
                editorJobID: jobID,
                planItemID: resolvedProject.activePlanItemID,
                preferredVariantID: resolvedProject.outputVariantID,
                threadID: project.id,
                api: api
            )
        } else {
            await load(api: api, threadID: project.id, variantID: resolvedProject.outputVariantID, allowPlaybackFallback: resolvedProject.outputURL == nil)
        }
        if player == nil, let url = resolvedProject.outputURL {
            installPlayer(url: url, preferredDuration: authoritativeDuration)
        } else if player == nil, let jobID = resolvedProject.activeJobID, let url = try? await api.playbackURL(jobID: jobID) {
            installPlayer(url: url, preferredDuration: authoritativeDuration)
        }
    }

    /// Gallery rows are render jobs, not creation-thread IDs. Promote the job
    /// through the server's idempotent editor route, then project its live
    /// variant into the same local draft model used by conversation projects.
    func load(libraryJobID: UUID, api: any KriaAPIClient) async {
        await load(editorJobID: libraryJobID, planItemID: nil, preferredVariantID: nil, threadID: nil, api: api)
    }

    private func load(
        editorJobID: UUID,
        planItemID: String?,
        preferredVariantID: String?,
        threadID: UUID?,
        api: any KriaAPIClient
    ) async {
        self.api = api
        self.threadID = threadID
        jobID = editorJobID
        isSaving = true
        saveState = .saving
        loadState = .loading
        defer { isSaving = false }
        do {
            let receipt: OpenInEditorResponse? = if planItemID == nil {
                try await api.openJobInEditor(jobID: editorJobID)
            } else {
                nil
            }
            guard let resolvedPlanItemID = planItemID ?? receipt?.planItemID else {
                throw APIError.invalidResponse
            }
            let resolved: (variantID: String, variant: [String: JSONValue])
            if let requestedVariantID = preferredVariantID ?? receipt?.variantID {
                do {
                    resolved = (
                        requestedVariantID,
                        try await api.editorVariant(jobID: editorJobID, variantID: requestedVariantID)
                    )
                } catch where receipt != nil && requestedVariantID != receipt?.variantID {
                    resolved = (
                        receipt!.variantID,
                        try await api.editorVariant(jobID: editorJobID, variantID: receipt!.variantID)
                    )
                }
            } else {
                let variants = try await api.editorVariants(jobID: editorJobID)
                guard let variant = variants.first(where: {
                    $0["render_status"]?.stringValue == "ready" && $0["variant_id"]?.stringValue != nil
                }) ?? variants.first(where: { $0["variant_id"]?.stringValue != nil }),
                      let variantID = variant["variant_id"]?.stringValue else {
                    throw APIError.invalidResponse
                }
                resolved = (variantID, variant)
            }
            let variant = resolved.variant
            let generation = variant["render_generation_id"]?.stringValue
                ?? variant["render_finished_at"]?.stringValue
                ?? ""
            let snapshot = DraftSnapshot(
                draftID: "job-\(editorJobID.uuidString)",
                itemID: resolvedPlanItemID,
                variantKey: resolved.variantID,
                draftRevision: 0,
                snapshotHash: "",
                etag: "",
                baseJobID: editorJobID.uuidString,
                baseGenerationID: generation,
                snapshot: [:],
                canUndo: false,
                createdAt: .now
            )
            draft = snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant)
            configureCapabilities(from: variant)
            cleanDocument = document
            undoStack.removeAll()
            redoStack.removeAll()
            changedSections.removeAll()
            explicitlyDirtySections.removeAll()
            pendingRenderRetrySections.removeAll()
            hasUnsavedChanges = false
            saveState = .idle
            itemID = resolvedPlanItemID
            variantKey = resolved.variantID
            durationSourcesInvalidated = false
            setAuthoritativeDuration(Self.number(variant["duration_s"]))
            refreshDuration()
            if let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            } else if let url = try? await api.playbackURL(jobID: editorJobID) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            }
            loadState = .loaded
        } catch {
            saveState = .loadFailed(error.localizedDescription)
            loadState = .failed(error.localizedDescription)
        }
    }

    func togglePlayback() {
        guard let player else { isPlaying = false; return }
        if isPlaying {
            player.pause()
            isPlaying = false
            return
        }
        // AVPlayer does not automatically restart after an end notification.
        // Make replay deterministic and keep clock and transport in lockstep.
        if duration > 0, currentTime >= duration - playbackEndTolerance {
            player.seek(to: .zero)
            currentTime = 0
        }
        player.play()
        isPlaying = true
    }

    func seek(to time: TimeInterval) {
        let clamped = min(max(0, time), max(0, duration))
        currentTime = clamped
        player?.seek(to: CMTime(seconds: clamped, preferredTimescale: 600))
    }

    func selectClip(_ clipID: UUID?) { select(clipID.map { EditorSelection(kind: .clip, id: $0.uuidString) }, seekToStart: false) }
    func selectClip(_ clip: EditorClip?) { selectClip(clip?.id) }

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
            baseline: document,
            redoBaseline: redoStack
        )
    }

    func updateTrim(by translation: TimeInterval) {
        guard translation.isFinite, var active = activeTrim else { return }
        guard let index = clipSlotIndex(active.clipID.uuidString, in: active.baseline) else { return }
        var next = active.baseline
        var slot = next.clips[index]
        let sourceDuration = Self.number(slot.raw["source_duration_s"] ?? slot.raw["source_duration"])
        let sourceIn = max(0, slot.inS)
        let currentDuration = max(minimumClipDuration, slot.durationS ?? minimumClipDuration)
        switch active.edge {
        case .leading:
            // Left trim preserves the source Out point. A negative translation
            // restores earlier source frames until the file's beginning.
            let sourceOut = max(minimumClipDuration, min(sourceIn + currentDuration, sourceDuration ?? sourceIn + currentDuration))
            slot.inS = min(max(0, sourceIn + translation), max(0, sourceOut - minimumClipDuration))
            slot.durationS = max(minimumClipDuration, sourceOut - slot.inS)
        case .trailing:
            // Right trim preserves source In. Later clips are not a ceiling:
            // they ripple after this duration changes.
            let currentOut = sourceIn + currentDuration
            let maximumOut = sourceDuration.map { max(sourceIn + minimumClipDuration, $0) } ?? .greatestFiniteMagnitude
            let nextOut = min(max(currentOut + translation, sourceIn + minimumClipDuration), maximumOut)
            slot.durationS = nextOut - sourceIn
        }
        slot.durationBeats = nil; next.clips[index] = slot
        reflowSlots(&next.clips, from: index + 1)

        if next == active.baseline {
            if active.recordedUndo {
                undoStack.removeLast()
                redoStack = active.redoBaseline
                active.recordedUndo = false
            }
        } else if !active.recordedUndo {
            appendUndo(active.baseline)
            redoStack.removeAll()
            active.recordedUndo = true
        }
        document = next
        activeTrim = active
        refreshDirtyState()
        refreshDuration()
    }

    func endTrim() {
        activeTrim = nil
    }

    func moveSelected(by offset: TimeInterval) {
        guard canEditTimeline, let id = selectedClipID, let index = clipSlotIndex(id.uuidString), abs(offset) > 0.000001 else { return }
        let destination = min(max(0, index + (offset < 0 ? -1 : 1)), document.clips.count - 1)
        guard destination != index else { return }
        transactDocument(section: .timeline) { doc in let clip = doc.clips.remove(at: index); doc.clips.insert(clip, at: destination) }
    }

    func slideSourceWindow(by offset: TimeInterval) {
        guard canEditTimeline, let id = selectedClipID, let index = clipSlotIndex(id.uuidString), document.clips.indices.contains(index) else { return }
        let slot = document.clips[index]
        let length = max(minimumClipDuration, slot.durationS ?? minimumClipDuration)
        let sourceEnd = Self.number(slot.raw["source_duration_s"] ?? slot.raw["source_duration"]) ?? slot.inS + length
        let start = min(max(0, slot.inS + offset), max(0, sourceEnd - length))
        transactDocument(section: .timeline) { $0.clips[index].inS = start }
    }

    // MARK: - Typed clip inspector mutations

    func setClipLookPreset(clipID: String, preset: String?) {
        let value = preset ?? "none"
        guard NativeEditorWireContract.lookPresets.contains(value) else { return }
        mutateClip(clipID: clipID) { $0.lookPreset = value }
    }
    func setClipLookPreset(clipID: UUID, preset: String?) { setClipLookPreset(clipID: clipID.uuidString, preset: preset) }
    func updateClipLook(clipID: String, preset: String?, adjustments: [String: JSONValue]? = nil) {
        let value = preset ?? "none"
        guard NativeEditorWireContract.lookPresets.contains(value) else { return }
        mutateClip(clipID: clipID) { slot in slot.lookPreset = value; if let adjustments { slot.lookAdjustments = adjustments } }
    }
    func updateClipLook(clipID: UUID, preset: String?, adjustments: [String: JSONValue]? = nil) { updateClipLook(clipID: clipID.uuidString, preset: preset, adjustments: adjustments) }
    func setClipLookAdjustments(clipID: String, adjustments: [String: JSONValue]?) {
        mutateClip(clipID: clipID) { $0.lookAdjustments = adjustments }
    }
    func setClipLookAdjustments(clipID: UUID, adjustments: [String: JSONValue]?) { setClipLookAdjustments(clipID: clipID.uuidString, adjustments: adjustments) }
    func setClipTransition(clipID: String, transition: String, durationS: Double? = nil) {
        guard NativeEditorWireContract.transitions.contains(transition) else { return }
        let duration = transition == "cut" ? nil : durationS.map { min(max(0.1, $0), 1) }
        mutateClip(clipID: clipID) { $0.transitionAfter = transition; $0.transitionDurationS = duration }
    }
    func setClipTransition(clipID: UUID, transition: String, durationS: Double? = nil) { setClipTransition(clipID: clipID.uuidString, transition: transition, durationS: durationS) }
    func setClipTiming(clipID: String, inS: Double? = nil, durationS: Double? = nil, durationBeats: Int? = nil) {
        mutateClip(clipID: clipID) { slot in
            if let inS { slot.inS = max(0, inS) }
            if let durationS { slot.durationS = max(minimumClipDuration, durationS) }
            if let durationBeats { slot.durationBeats = durationBeats }
            if inS != nil || durationS != nil { slot.durationBeats = nil }
        }
    }
    func setClipSourceWindow(clipID: String, startS: Double, endS: Double) {
        let length = max(minimumClipDuration, endS - startS)
        setClipTiming(clipID: clipID, inS: startS, durationS: length)
    }
    func setClipTiming(clipID: UUID, inS: Double? = nil, durationS: Double? = nil, durationBeats: Int? = nil) { setClipTiming(clipID: clipID.uuidString, inS: inS, durationS: durationS, durationBeats: durationBeats) }
    func setClipSourceWindow(clipID: UUID, startS: Double, endS: Double) { setClipSourceWindow(clipID: clipID.uuidString, startS: startS, endS: endS) }
    func updateClipSourceWindow(clipID: String, startS: Double, durationS: Double) {
        setClipTiming(clipID: clipID, inS: startS, durationS: durationS)
    }
    func removeClip(clipID: String) {
        guard canEditSection(.timeline), document.clips.count > 1, let index = clipSlotIndex(clipID) else { return }
        transactDocument(section: .timeline) { doc in
            var removed = doc.clips.remove(at: index); removed.removed = true; doc.tombstones.append(removed)
        }
    }
    func restoreClip(clipID: String) {
        guard canEditSection(.timeline), let index = document.tombstones.firstIndex(where: { $0.id == clipID }) else { return }
        transactDocument(section: .timeline) { doc in var restored = doc.tombstones.remove(at: index); restored.removed = false; doc.clips.append(restored) }
    }

    private func mutateClip(clipID: String, _ body: (inout EditorTimelineSlot) -> Void) {
        guard canEditSection(.timeline), let index = clipSlotIndex(clipID) else { return }
        transactDocument(section: .timeline) { doc in var slot = doc.clips[index]; body(&slot); doc.clips[index] = slot }
    }
    private func clipSlotIndex(_ id: String) -> Int? { clipSlotIndex(id, in: document) }
    private func clipSlotIndex(_ id: String, in value: EditorDocument) -> Int? {
        if let index = value.clips.firstIndex(where: { $0.id == id }) { return index }
        guard let uuid = UUID(uuidString: id) else { return nil }
        if let index = value.clips.firstIndex(where: { $0.id == uuid.uuidString }) { return index }
        guard let slotID = clipIDsBySlot.first(where: { $0.value == uuid })?.key else { return nil }
        return value.clips.firstIndex { $0.id == slotID }
    }

    func deleteSelectedClip() {
        guard canEditTimeline, draft.clips.count > 1, let id = selectedClipID, let index = draft.clips.firstIndex(where: { $0.id == id }) else { return }
        transact(section: .timeline) { draft in draft.clips.remove(at: index); reflow(&draft.clips, from: max(0, index)) }
        selectedClipID = draft.clips.indices.contains(index) ? draft.clips[index].id : draft.clips.last?.id
    }

    func addText(content: String? = nil) {
        guard canEditSection(.text) else { return }
        let value = content?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty ?? "Your story"
        transact(section: .text) { $0.text.append(TextLayer(id: UUID(), content: value, position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")) }
    }

    func updateText(id: UUID, content: String) {
        updateTextContent(id: id.uuidString, content: content)
    }

    func setTextStyle(id: UUID?, style: String) {
        let target = id?.uuidString ?? document.textElements.last?.id
        if let target { setTextStyle(id: target, style: style) }
    }

    // MARK: - Authored text mutations

    func updateTextContent(id: String, content: String) {
        guard canEditSection(.text) else { return }
        mutateText(id: id) { $0.text = content }
    }
    func updateTextContent(id: UUID, content: String) { updateTextContent(id: id.uuidString, content: content) }
    func updateTextTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditSection(.text) else { return }
        mutateText(id: id) { if let startS { $0.startS = startS }; if let endS { $0.endS = max($0.startS, endS) } }
    }
    func updateTextTiming(id: UUID, startS: Double? = nil, endS: Double? = nil) { updateTextTiming(id: id.uuidString, startS: startS, endS: endS) }
    func setTextStyle(id: String, style: String) {
        guard canEditSection(.text) else { return }
        mutateText(id: id) { $0.raw["font_family"] = .string(style) }
    }
    func setTextPosition(id: String, x: Double, y: Double) {
        guard canEditSection(.text) else { return }
        mutateText(id: id) { $0.raw["x_frac"] = .number(min(max(0, x), 1)); $0.raw["y_frac"] = .number(min(max(0, y), 1)) }
    }
    func setTextSize(id: String, sizePX: Double?) { setTextRaw(id: id, key: "size_px", value: sizePX.map(JSONValue.number) ?? .null) }
    func setTextWidth(id: String, width: Double?) { setTextRaw(id: id, key: "max_width_frac", value: width.map { .number(min(max(0.2, $0), 1)) } ?? .null) }
    func setTextAlignment(id: String, alignment: String?) { setTextRaw(id: id, key: "alignment", value: alignment.map(JSONValue.string) ?? .null) }
    func setTextAnimation(id: String, animation: String?) {
        let value = animation ?? "none"
        guard NativeEditorWireContract.textAnimations.contains(value) else { return }
        setTextRaw(id: id, key: "effect", value: .string(value))
    }
    func setTextColor(id: String, color: String?) { setTextRaw(id: id, key: "color", value: color.map(JSONValue.string) ?? .null) }
    func setTextHighlightColor(id: String, color: String?) { setTextRaw(id: id, key: "highlight_color", value: color.map(JSONValue.string) ?? .null) }
    func setTextShadow(id: String, enabled: Bool) { setTextRaw(id: id, key: "shadow_enabled", value: .bool(enabled)) }
    func setTextStroke(id: String, width: Double?) { setTextRaw(id: id, key: "stroke_width", value: width.map(JSONValue.number) ?? .null) }
    func setTextBehindSubject(id: String, behind: Bool) { setTextRaw(id: id, key: "behind_subject", value: .bool(behind)) }
    func updateTextRaw(id: String, key: String, value: JSONValue?) { setTextRaw(id: id, key: key, value: value ?? .null) }
    func setTextRaw(id: String, key: String, value: JSONValue) {
        guard canEditSection(.text) else { return }
        mutateText(id: id) { $0.raw[key] = value }
    }
    func setTextPosition(id: UUID, x: Double, y: Double) { setTextPosition(id: id.uuidString, x: x, y: y) }
    func setTextTiming(id: UUID, startS: Double? = nil, endS: Double? = nil) { updateTextTiming(id: id.uuidString, startS: startS, endS: endS) }

    private func mutateText(id: String, _ body: (inout EditorTextElement) -> Void) {
        guard let index = document.textElements.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .text) { doc in var item = doc.textElements[index]; body(&item); doc.textElements[index] = item }
    }

    func toggleCaptions() {
        guard canEditSection(.captionMeta) else { return }
        let enabled = Self.bool(document.captionMeta["enabled"]) ?? draft.captions.enabled
        transactDocument(section: .captionMeta) { $0.captionMeta["enabled"] = .bool(!enabled) }
    }
    func setCaptionStyle(_ style: String) {
        guard ["sentence", "word"].contains(style) else { return }
        guard canEditSection(.captionMeta) else { return }
        transactDocument(section: .captionMeta) { $0.captionMeta["style"] = .string(style) }
    }
    func setCaptionEnabled(_ enabled: Bool) {
        guard canEditSection(.captionMeta) else { return }
        transactDocument(section: .captionMeta) { $0.captionMeta["enabled"] = .bool(enabled) }
    }
    func setCaptionMeta(key: String, value: JSONValue?) {
        guard canEditSection(.captionMeta) else { return }
        transactDocument(section: .captionMeta) { $0.captionMeta[key] = value ?? .null }
    }
    func setCaptionFont(_ font: String?) {
        if let font, !NativeEditorWireContract.captionFonts.contains(font) { return }
        guard canEditSection(.captionMeta) else { return }
        transactDocument(section: .captionMeta) {
            $0.captionMeta["font"] = font.map(JSONValue.string) ?? .null
            $0.captionMeta["font_set"] = .bool(true)
        }
    }
    func setCaptionSize(_ sizePX: Double?) { setCaptionMeta(key: "size_px", value: sizePX.map { .number(min(max(36, $0.rounded()), 160)) }) }
    func setCaptionColor(_ color: String?) { setCaptionMeta(key: "color", value: color.map(JSONValue.string)) }
    func setCaptionHighlightColor(_ color: String?) { setCaptionMeta(key: "highlight_color", value: color.map(JSONValue.string)) }
    func setCaptionStrokeWidth(_ width: Double?) { setCaptionMeta(key: "stroke_width", value: width.map(JSONValue.number)) }
    func setCaptionShadowEnabled(_ enabled: Bool) { setCaptionMeta(key: "shadow_enabled", value: .bool(enabled)) }
    func setCaptionPositionY(_ y: Double?) { setCaptionMeta(key: "y_frac", value: y.map { .number(min(max(0.30, $0), 0.90)) }) }

    func updateCaptionCue(id: String, text: String? = nil, startS: Double? = nil, endS: Double? = nil) {
        guard canEditSection(.captions) else { return }
        guard let index = document.captionCues.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .captions) { doc in
            var cue = doc.captionCues[index]
            if let text { cue.text = text }; if let startS { cue.startS = startS }; if let endS { cue.endS = max(cue.startS, endS) }
            doc.captionCues[index] = cue
        }
    }
    func updateCaptionCue(id: UUID, text: String? = nil, startS: Double? = nil, endS: Double? = nil) { updateCaptionCue(id: id.uuidString, text: text, startS: startS, endS: endS) }

    // MARK: - Timed visual and sound lanes

    func setSoundEffectTiming(id: String, atS: Double? = nil, startS: Double? = nil, endS: Double? = nil) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.timing", "sfx.timing", "sound_effects.timing", "lanes.sfx"]) { effect in
            if let atS { effect.pointS = max(0, atS); effect.startS = max(0, atS) }
            if let startS { effect.startS = max(0, startS); if effect.pointS != nil { effect.pointS = effect.startS } }
            if let endS { effect.endS = max(effect.startS + minimumClipDuration, endS) }
            effect.endS = max(effect.startS + minimumClipDuration, effect.endS)
        }
    }
    func setSoundEffectTrim(id: String, trimStartS: Double? = nil, trimEndS: Double? = nil) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.trim", "sfx.trim", "sound_effects.trim", "lanes.sfx"]) { effect in
            if let trimStartS { effect.raw["trim_start_s"] = .number(max(0, trimStartS)) }
            if let trimEndS { effect.raw["trim_end_s"] = .number(max(0, trimEndS)) }
        }
    }
    func setSoundEffectGain(id: String, gain: Double) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.gain", "sfx.gain", "sound_effects.gain", "lanes.sfx"]) { $0.raw["gain"] = .number(min(max(0, gain), 2)) }
    }
    func removeSoundEffect(id: String) {
        guard canEditOperation(["lanes.sfx.remove", "sfx.remove", "sound_effects.remove", "lanes.sfx"], section: .soundEffects) else { return }
        transactDocument(section: .soundEffects) { $0.soundEffects.removeAll { $0.id == id } }
    }

    func setMediaOverlayTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        mutateTimedEffect(kind: .mediaOverlay, id: id, section: .mediaOverlays, operationKeys: ["lanes.overlays.timing", "overlays.timing", "media_overlays.timing", "lanes.overlays"]) { effect in
            if let startS { effect.startS = max(0, startS) }
            if let endS { effect.endS = max(effect.startS + minimumClipDuration, endS) }
        }
    }
    func setMediaOverlayPosition(id: String, x: Double, y: Double) {
        mutateTimedEffect(kind: .mediaOverlay, id: id, section: .mediaOverlays, operationKeys: ["lanes.overlays.position", "overlays.position", "media_overlays.position", "lanes.overlays"]) { effect in
            effect.raw["x_frac"] = .number(min(max(0, x), 1)); effect.raw["y_frac"] = .number(min(max(0, y), 1))
        }
    }
    func setMediaOverlayScale(id: String, scale: Double) {
        mutateTimedEffect(kind: .mediaOverlay, id: id, section: .mediaOverlays, operationKeys: ["lanes.overlays.scale", "overlays.scale", "media_overlays.scale", "lanes.overlays"]) { $0.raw["scale"] = .number(min(max(0.05, scale), 1)) }
    }
    func setMediaOverlayDisplayMode(id: String, mode: String) {
        mutateTimedEffect(kind: .mediaOverlay, id: id, section: .mediaOverlays, operationKeys: ["lanes.overlays.display_mode", "overlays.display_mode", "media_overlays.display_mode", "lanes.overlays"]) { $0.raw["display_mode"] = .string(mode) }
    }
    func setMediaOverlayZIndex(id: String, z: Double) {
        mutateTimedEffect(kind: .mediaOverlay, id: id, section: .mediaOverlays, operationKeys: ["layers.reorder", "layer_order", "lanes.overlays.z_order", "lanes.overlays"]) { $0.raw["z"] = .number(z) }
    }
    func removeMediaOverlay(id: String) {
        guard canEditOperation(["lanes.overlays.remove", "overlays.remove", "media_overlays.remove", "lanes.overlays"], section: .mediaOverlays) else { return }
        transactDocument(section: .mediaOverlays) { $0.mediaOverlays.removeAll { $0.id == id } }
    }

    func setVisualBlockTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditOperation(["lanes.visual_blocks.timing", "visual_blocks.timing", "lanes.visual_blocks"], section: .visualBlocks),
              let index = document.visualBlocks.firstIndex(where: { $0.id == id }),
              document.visualBlocks[index].kind != "montage" else { return }
        transactDocument(section: .visualBlocks) { document in
            var value = document.visualBlocks[index]
            if let startS { value.startS = max(0, startS) }
            if let endS { value.endS = max(value.startS + minimumClipDuration, endS) }
            if value.kind == "text_card" {
                value.endS = min(value.endS, value.startS + 10)
            } else if value.kind == "media",
                      value.raw["media_kind"]?.stringValue == "video" {
                let sourceDuration = Self.number(value.raw["source_duration_s"])
                let trimStart = Self.number(value.raw["trim_start_s"]) ?? 0
                let trimEnd = Self.number(value.raw["trim_end_s"]) ?? sourceDuration
                if let trimEnd {
                    value.endS = min(value.endS, value.startS + max(minimumClipDuration, trimEnd - trimStart))
                }
            }
            document.visualBlocks[index] = value
        }
    }
    func setVisualBlockPreset(id: String, preset: String?) {
        guard document.visualBlocks.first(where: { $0.id == id })?.kind == "text_card" else { return }
        mutateVisualBlock(id: id, operationKeys: ["lanes.visual_blocks.style_preset_id", "visual_blocks.style_preset_id", "lanes.visual_blocks"]) { block in
            block.raw["style_preset_id"] = preset.map(JSONValue.string) ?? .null
            block.raw.removeValue(forKey: "preset")
        }
    }
    func setVisualBlockTransform(
        id: String,
        fitMode: String? = nil,
        focalX: Double? = nil,
        focalY: Double? = nil,
        zoom: Double? = nil
    ) {
        guard document.visualBlocks.first(where: { $0.id == id })?.kind == "media" else { return }
        mutateVisualBlock(id: id, operationKeys: ["lanes.visual_blocks.transform", "visual_blocks.transform", "lanes.visual_blocks"]) { block in
            var transform = Self.object(block.raw["transform"]) ?? [:]
            if let fitMode, ["contain", "cover"].contains(fitMode) { transform["fit_mode"] = .string(fitMode) }
            if let focalX { transform["focal_x"] = .number(min(max(0, focalX), 1)) }
            if let focalY { transform["focal_y"] = .number(min(max(0, focalY), 1)) }
            if let zoom { transform["zoom"] = .number(min(max(1, zoom), 4)) }
            block.raw["transform"] = .object(transform)
            block.raw.removeValue(forKey: "rotation")
        }
    }
    func setVisualBlockOverlayLayout(id: String, x: Double? = nil, y: Double? = nil, scale: Double? = nil) {
        guard document.visualBlocks.first(where: { $0.id == id })?.kind == "media" else { return }
        mutateVisualBlock(id: id, operationKeys: ["lanes.visual_blocks.transform", "visual_blocks.transform", "lanes.visual_blocks"]) { block in
            if let x { block.raw["x_frac"] = .number(min(max(0, x), 1)) }
            if let y { block.raw["y_frac"] = .number(min(max(0, y), 1)) }
            if let scale { block.raw["scale"] = .number(min(max(0.05, scale), 1)) }
        }
    }
    func setVisualBlockDisplayMode(id: String, mode: String) {
        guard document.visualBlocks.first(where: { $0.id == id })?.kind == "media",
              ["fullscreen", "overlay"].contains(mode) else { return }
        mutateVisualBlock(id: id, operationKeys: ["lanes.visual_blocks.transform", "visual_blocks.transform", "lanes.visual_blocks"]) {
            $0.raw["display_mode"] = .string(mode)
        }
    }
    func removeVisualBlock(id: String) {
        guard canEditOperation(["lanes.visual_blocks.remove", "visual_blocks.remove", "lanes.visual_blocks"], section: .visualBlocks) else { return }
        transactDocument(section: .visualBlocks) { $0.visualBlocks.removeAll { $0.id == id } }
    }

    func setMotionSceneTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditMotionScene(id: id, keys: ["lanes.motion_scenes.timing", "motion_scenes.timing", "lanes.motion_scenes"]), let index = document.motionScenes.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .motionScenes) { document in
            var value = document.motionScenes[index]
            if let startS { value.startS = Self.roundToMotionFrame(max(0, startS)) }
            if let endS { value.endS = Self.roundToMotionFrame(max(value.startS + 1.0 / 30.0, endS)) }
            value.endS = min(value.endS, value.startS + 8)
            document.motionScenes[index] = value
        }
    }
    func motionRuntimeMismatchReason(id: String) -> String? {
        guard document.motionScenes.contains(where: { $0.id == id }), runtimeMismatch() else { return nil }
        return document.capabilities["motion_scenes"]?.reason ?? "motion_runtime_mismatch"
    }
    func isMotionSceneReadOnly(id: String) -> Bool {
        document.motionScenes.contains(where: { $0.id == id }) && runtimeMismatch()
    }

    func setCameraEffectTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditOperation(["camera_effects.timing", "lanes.camera_effects.timing", "camera_effects"], section: .cameraEffects), let index = document.cameraEffects.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .cameraEffects) { document in
            var value = document.cameraEffects[index]
            if let startS { value.startS = max(0, startS) }
            if let endS { value.endS = endS }
            value.endS = min(value.startS + 2, max(value.startS + 0.4, value.endS))
            document.cameraEffects[index] = value
        }
    }
    func setCameraEffectIntensity(id: String, intensity: Double) {
        mutateCameraEffect(id: id, keys: ["camera_effects.intensity", "lanes.camera_effects.intensity", "camera_effects"]) { $0.raw["intensity"] = .number(min(max(0, intensity), 0.08)) }
    }
    func setCameraEffectEasing(id: String, easing: String) { mutateCameraEffect(id: id, keys: ["camera_effects.easing", "lanes.camera_effects.easing", "camera_effects"]) { $0.raw["easing"] = .string(easing) } }

    func setCarouselMomentPosition(_ position: String) {
        guard canEditOperation(["carousel.position", "carousel", "carousel_moment"], section: .carouselMoment) else { return }
        transactDocument(section: .carouselMoment) { doc in var value = doc.carouselMoment ?? [:]; value["position"] = .string(position); doc.carouselMoment = value }
    }
    func setCarouselPosition(_ position: String) { setCarouselMomentPosition(position) }
    func removeCarouselMoment() {
        guard canEditOperation(["carousel.remove", "carousel", "carousel_moment"], section: .carouselMoment) else { return }
        transactDocument(section: .carouselMoment) { $0.carouselMoment = nil }
    }

    // A single baseline is shared by all timed-lane body and edge gestures.
    func beginTimedBodyMove(kind: EditorSelectionKind, id: String) { beginTimedEdit(selection: EditorSelection(kind: kind, id: id), kind: .move, edge: nil) }
    func updateTimedBodyMove(by translation: TimeInterval) { updateTimedEdit(by: translation) }
    func endTimedBodyMove() { endTimedEdit() }
    func beginTimedEdgeTrim(kind: EditorSelectionKind, id: String, edge: NativeTrimEdge) { beginTimedEdit(selection: EditorSelection(kind: kind, id: id), kind: .trim, edge: edge) }
    func beginTimedEdgeTrim(selection: EditorSelection, edge: NativeTrimEdge) { beginTimedEdit(selection: selection, kind: .trim, edge: edge) }
    func updateTimedEdgeTrim(by translation: TimeInterval) { updateTimedEdit(by: translation) }
    func endTimedEdgeTrim() { endTimedEdit() }

    func reorderLayer(selection: EditorSelection, to destination: Int) {
        guard canEditOperation(["layers.reorder", "layer_order", "lanes.layer_order"], section: .timeline) else { return }
        let layers = orderedLayers(); guard let current = layers.firstIndex(where: { $0.selection == selection }) else { return }
        let target = min(max(0, destination), layers.count - 1); guard target != current else { return }
        var reordered = layers; let item = reordered.remove(at: current); reordered.insert(item, at: target)
        let touched = Set(reordered.compactMap { section(for: $0.selection.kind) })
        transactDocument(sections: touched) { doc in
            for (z, item) in reordered.enumerated() { setLayerZ(item.selection, z: Double(z), in: &doc) }
        }
    }
    func moveLayer(selection: EditorSelection, by offset: Int) { guard let index = orderedLayers().firstIndex(where: { $0.selection == selection }) else { return }; reorderLayer(selection: selection, to: index + offset) }

    func setMusicVolume(_ volume: Double) {
        setMusicLevel(volume)
    }
    func setMusicLevel(_ level: Double) {
        guard canEditSection(.mix), document.music != nil else { return }
        transactDocument(section: .mix) { $0.mix["music_level"] = .number(min(max(0, level), 1)) }
    }
    func setOriginalMixLevel(_ level: Double) {
        guard canEditSection(.mix) else { return }
        transactDocument(section: .mix) { $0.mix["original_level"] = .number(min(max(0, level), 1)) }
    }
    func setMusicWindow(startS: Double? = nil, alignment: String? = nil) {
        guard canEditSection(.music), document.music != nil else { return }
        if let alignment, !NativeEditorWireContract.musicAlignments.contains(alignment) { return }
        transactDocument(section: .music) { music in
            guard var value = music.music else { return }
            if let startS { value.startS = max(0, startS) }; if let alignment { value.alignment = alignment }; music.music = value
        }
    }
    func setMusic(trackID: String, startS: Double = 0, alignment: String? = nil) {
        guard canEditSection(.music) else { return }
        let canonicalAlignment = alignment.flatMap(EditorMusicAlignment.init(rawValue:))?.rawValue ?? EditorMusicAlignment.preserveCuts.rawValue
        transactDocument(section: .music) { $0.music = EditorMusic(trackID: trackID, startS: max(0, startS), alignment: canonicalAlignment) }
    }
    func setMusic(trackID: UUID, title: String = "Music", startS: Double = 0) {
        guard canEditSection(.music) else { return }
        transactDocument(section: .music) { $0.music = EditorMusic(trackID: trackID.uuidString, startS: max(0, startS), raw: ["title": .string(title)]) }
    }
    func removeMusic() {
        guard canEditSection(.music) else { return }
        transactDocument(section: .music) { $0.music = nil }
    }
    func setBackgroundMusic(_ value: EditorBackgroundMusic?) {
        guard canEditSection(.backgroundMusic) else { return }
        transactDocument(section: .backgroundMusic) { $0.backgroundMusic = value }
    }
    func setBackgroundMusicLevel(_ levelDB: Double?) {
        guard canEditSection(.backgroundMusic) else { return }
        transactDocument(section: .backgroundMusic) { $0.backgroundMusic?.gainDB = levelDB }
    }

    func undo() {
        guard let previous = undoStack.popLast() else { return }
        appendRedo(document); document = previous; refreshDirtyState(); refreshDuration()
    }

    func redo() {
        guard let next = redoStack.popLast() else { return }
        appendUndo(document); document = next; refreshDirtyState(); refreshDuration()
    }

    func save() async {
        guard !isSaving, hasUnsavedChanges else { return }
        guard let api, let itemID, let variantKey else { saveState = .failed("Load the project before saving edits."); return }
        isSaving = true; saveState = .saving
        defer { isSaving = false }
        let submittedDocument = document
        let submittedSections = changedSections
        let submittedUndoCount = undoStack.count
        let snapshot = document.encodeSnapshot()
        let payload = Self.object(snapshot["editor_payload"]); let sections = Self.object(payload?["sections"])
        let request = commitRequest(sections: sections, baseGeneration: payload?["base_generation"]?.stringValue ?? document.revision.baseGeneration)
        do {
            let response = try await api.editorCommit(itemID: itemID, variantID: variantKey, request: request)
            let acknowledged = acknowledgedSections(response.sections, submittedSections: submittedSections)
            let postSubmitUndo = Array(undoStack.dropFirst(submittedUndoCount))
            let hasPostSubmitEdits = document != submittedDocument
            guidedRevisionNumber = response.revisionNumber ?? guidedRevisionNumber
            document.revision.number = response.revisionNumber ?? document.revision.number
            document.revision.hash = response.revisionHash ?? document.revision.hash
            if response.ok, let expectedDuration = response.expectedDuration {
                durationSourcesInvalidated = false
                setAuthoritativeDuration(expectedDuration)
                refreshDuration()
            }
            acknowledge(acknowledged, generation: response.generation, submittedDocument: submittedDocument)
            if hasPostSubmitEdits {
                let acknowledgedRevision = document.revision
                undoStack = postSubmitUndo.map { value in
                    var rebased = value
                    rebased.revision = acknowledgedRevision
                    return rebased
                }
                redoStack.removeAll()
            } else {
                undoStack.removeAll(); redoStack.removeAll()
            }
            if response.ok {
                pendingRenderRetrySections.removeAll()
                saveState = .previewPending
                await refreshDeviceRender()
                startPreviewRefresh(generation: response.generation)
            } else {
                pendingRenderRetrySections = acknowledged
                saveState = .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely.")
            }
        } catch APIError.conflict { saveState = .conflict }
        catch { saveState = .failed(error.localizedDescription) }
    }

    /// Refresh a conflicted baseline without discarding the creator's local
    /// work. The latest renderer-owned document becomes clean, then only the
    /// locally dirty sections are replayed on top as one undoable change.
    func rebaseAfterConflict() async {
        guard saveState == .conflict, !isSaving,
              let api, let jobID, let variantKey, let itemID else { return }
        isSaving = true
        defer { isSaving = false }
        let localDocument = document
        let localSections = changedSections
        let localExplicitSections = explicitlyDirtySections
        let selected = selection
        do {
            let variant = try await api.editorVariant(jobID: jobID, variantID: variantKey)
            let generation = variant["render_generation_id"]?.stringValue
                ?? variant["render_finished_at"]?.stringValue
                ?? document.revision.baseGeneration
            let snapshot = DraftSnapshot(
                draftID: "conflict-\(projectID.uuidString)",
                itemID: itemID,
                variantKey: variantKey,
                draftRevision: document.revision.number ?? 0,
                snapshotHash: "",
                etag: etag,
                baseJobID: jobID.uuidString,
                baseGenerationID: generation,
                snapshot: [:],
                canUndo: false,
                createdAt: .now
            )
            let latestDraft = snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant)
            var latestClean = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(latestDraft))
            latestClean.revision.baseGeneration = generation
            var rebased = latestClean
            for section in localSections {
                copy(section, from: localDocument, into: &rebased)
            }
            document = rebased
            cleanDocument = latestClean
            changedSections = localSections
            explicitlyDirtySections = localExplicitSections.intersection(localSections)
            redoStack.removeAll()
            configureCapabilities(from: variant)
            refreshDirtyState()
            undoStack = hasUnsavedChanges ? [latestClean] : []
            if let selected, selectionExists(selected) { selection = selected } else { selection = nil }
            durationSourcesInvalidated = hasUnsavedChanges && (changedSections.contains(.timeline) || changedSections.contains(.carouselMoment))
            setAuthoritativeDuration(Self.number(variant["duration_s"]))
            refreshDuration()
            saveState = .idle
        } catch {
            saveState = .failed("Your edits are still here, but the latest version couldn’t be loaded. \(error.localizedDescription)")
        }
    }

    private func transact(section: EditorSection, _ body: (inout EditorDraft) -> Void) {
        var next = draft; body(&next); guard next != draft else { return }
        invalidateDurationSources(for: Set([section]))
        if transactionBaseline == nil { appendUndo(document); redoStack.removeAll() }
        replace(with: next); changedSections.insert(section); refreshDirtyState(); refreshDuration()
    }

    private func transactDocument(section: EditorSection, _ body: (inout EditorDocument) -> Void) {
        transactDocument(sections: [section], body)
    }

    private func transactDocument(sections: Set<EditorSection>, _ body: (inout EditorDocument) -> Void) {
        var next = document; body(&next); guard next != document else { return }
        invalidateDurationSources(for: sections)
        if transactionBaseline == nil { appendUndo(document); redoStack.removeAll() }
        document = next; changedSections.formUnion(sections); refreshDirtyState(); refreshDuration()
    }

    private func canEditOperation(_ keys: [String], section: EditorSection) -> Bool {
        for key in keys {
            if let capability = document.capabilities[key] { return capability.editable }
        }
        return canEditSection(section)
    }

    func operationCapability(_ key: String) -> EditorCapability? { document.capabilities[key] }
    func operationCapabilityReason(_ key: String) -> String? { document.capabilities[key]?.reason }
    func readOnlyReason(forOperation key: String) -> String? { operationCapabilityReason(key) }

    private func mutateTimedEffect(kind: EditorSelectionKind, id: String, section: EditorSection, operationKeys: [String], _ body: (inout EditorTimedEffect) -> Void) {
        guard canEditOperation(operationKeys, section: section) else { return }
        transactDocument(section: section) { doc in
            switch kind {
            case .soundEffect:
                guard let index = doc.soundEffects.firstIndex(where: { $0.id == id }) else { return }; body(&doc.soundEffects[index])
            case .mediaOverlay:
                guard let index = doc.mediaOverlays.firstIndex(where: { $0.id == id }) else { return }; body(&doc.mediaOverlays[index])
            default: break
            }
        }
    }

    private func mutateVisualBlock(id: String, operationKeys: [String], _ body: (inout EditorVisualBlock) -> Void) {
        guard canEditOperation(operationKeys, section: .visualBlocks), let index = document.visualBlocks.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .visualBlocks) { doc in body(&doc.visualBlocks[index]) }
    }

    private func mutateCameraEffect(id: String, keys: [String], _ body: (inout EditorCameraEffect) -> Void) {
        guard canEditOperation(keys, section: .cameraEffects), let index = document.cameraEffects.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .cameraEffects) { doc in body(&doc.cameraEffects[index]) }
    }

    private func canEditMotionScene(id: String, keys: [String]) -> Bool {
        canEditOperation(keys, section: .motionScenes) && !isMotionSceneReadOnly(id: id)
    }

    private func runtimeMismatch() -> Bool {
        guard let capability = document.capabilities["motion_scenes"] else { return false }
        return !capability.editable && capability.reason == "motion_runtime_mismatch"
    }

    private func orderedLayers() -> [(selection: EditorSelection, z: Double)] {
        var layers: [(EditorSelection, Double, Int)] = []
        for (index, item) in document.textElements.enumerated() { layers.append((EditorSelection(kind: .text, id: item.id), Self.number(item.raw["z"] ?? item.raw["z_index"]) ?? 300 + Double(index), 0)) }
        for (index, item) in document.visualBlocks.enumerated() { layers.append((EditorSelection(kind: .visualBlock, id: item.id), Self.number(item.raw["z"] ?? item.raw["z_index"]) ?? 150 + Double(index), 1)) }
        for (index, item) in document.mediaOverlays.enumerated() { layers.append((EditorSelection(kind: .mediaOverlay, id: item.id), Self.number(item.raw["z"] ?? item.raw["z_index"]) ?? 200 + Double(index), 2)) }
        return layers.sorted { lhs, rhs in lhs.1 == rhs.1 ? lhs.2 < rhs.2 : lhs.1 < rhs.1 }.map { ($0.0, $0.1) }
    }

    private func section(for kind: EditorSelectionKind) -> EditorSection? {
        switch kind {
        case .text: return .text
        case .soundEffect: return .soundEffects
        case .visualBlock: return .visualBlocks
        case .mediaOverlay: return .mediaOverlays
        case .motionScene: return .motionScenes
        case .cameraEffect: return .cameraEffects
        default: return nil
        }
    }

    private func setLayerZ(_ selection: EditorSelection, z: Double, in document: inout EditorDocument) {
        switch selection.kind {
        case .text:
            guard let index = document.textElements.firstIndex(where: { $0.id == selection.id }) else { return }; document.textElements[index].raw["z"] = .number(z)
        case .visualBlock:
            guard let index = document.visualBlocks.firstIndex(where: { $0.id == selection.id }) else { return }; document.visualBlocks[index].raw["z"] = .number(z)
        case .mediaOverlay:
            guard let index = document.mediaOverlays.firstIndex(where: { $0.id == selection.id }) else { return }; document.mediaOverlays[index].raw["z"] = .number(z)
        default: break
        }
    }

    private func beginTimedEdit(selection: EditorSelection, kind: TimedEditKind, edge: NativeTrimEdge?) {
        let operation = kind == .move ? "timing" : "trim"
        guard activeTimedEdit == nil,
              let section = section(for: selection.kind),
              canEditOperation(["\(section.rawValue).\(operation)", section.rawValue], section: section),
              timedObjectExists(selection),
              supportsTimedGesture(selection) else { return }
        activeTimedEdit = ActiveTimedEdit(selection: selection, edge: edge, kind: kind, baseline: document, redoBaseline: redoStack)
    }

    private func updateTimedEdit(by translation: TimeInterval) {
        guard translation.isFinite, var active = activeTimedEdit else { return }
        var next = active.baseline
        guard let section = section(for: active.selection.kind) else { return }
        guard let bounds = timedBounds(active.selection, in: next) else { return }
        let minimum = minimumClipDuration
        let totalDuration = max(duration, 0)
        var start = bounds.start; var end = bounds.end
        switch active.kind {
        case .move:
            let length = max(minimum, end - start)
            let maxStart = totalDuration > 0 ? max(0, totalDuration - length) : .greatestFiniteMagnitude
            start = min(max(0, start + translation), maxStart); end = start + length
        case .trim:
            guard let edge = active.edge else { return }
            if edge == .leading { start = min(max(0, start + translation), end - minimum) }
            else { end = max(start + minimum, min(totalDuration > 0 ? totalDuration : .greatestFiniteMagnitude, end + translation)) }
        }
        if active.kind == .trim, active.selection.kind == .soundEffect {
            setSoundEffectTrimBounds(active.selection.id, edge: active.edge ?? .trailing, translation: translation, in: &next)
        } else {
            setTimedBounds(active.selection, start: start, end: end, in: &next)
        }
        guard next != active.baseline else {
            if active.recordedUndo {
                undoStack.removeLast()
                redoStack = active.redoBaseline
                active.recordedUndo = false
            }
            document = active.baseline
            refreshDirtyState()
            refreshDuration()
            activeTimedEdit = active
            return
        }
        if !active.recordedUndo { appendUndo(active.baseline); redoStack.removeAll(); active.recordedUndo = true }
        document = next; changedSections.insert(section); refreshDirtyState(); refreshDuration(); activeTimedEdit = active
    }

    private func endTimedEdit() { activeTimedEdit = nil }

    private func timedObjectExists(_ selection: EditorSelection) -> Bool { timedBounds(selection, in: document) != nil }

    private func timedBounds(_ selection: EditorSelection, in document: EditorDocument) -> (start: Double, end: Double)? {
        switch selection.kind {
        case .soundEffect: guard let item = document.soundEffects.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .mediaOverlay: guard let item = document.mediaOverlays.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .visualBlock: guard let item = document.visualBlocks.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .motionScene: guard let item = document.motionScenes.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .cameraEffect: guard let item = document.cameraEffects.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        default: return nil
        }
    }

    private func setTimedBounds(_ selection: EditorSelection, start: Double, end: Double, in document: inout EditorDocument) {
        switch selection.kind {
        case .soundEffect:
            guard let index = document.soundEffects.firstIndex(where: { $0.id == selection.id }) else { return }; document.soundEffects[index].startS = start; document.soundEffects[index].endS = end; if document.soundEffects[index].pointS != nil { document.soundEffects[index].pointS = start }
        case .mediaOverlay: guard let index = document.mediaOverlays.firstIndex(where: { $0.id == selection.id }) else { return }; document.mediaOverlays[index].startS = start; document.mediaOverlays[index].endS = end
        case .visualBlock:
            guard let index = document.visualBlocks.firstIndex(where: { $0.id == selection.id }), document.visualBlocks[index].kind != "montage" else { return }
            var value = document.visualBlocks[index]
            value.startS = start
            value.endS = end
            if value.kind == "text_card" { value.endS = min(value.endS, value.startS + 10) }
            if value.kind == "media", value.raw["media_kind"]?.stringValue == "video" {
                let trimStart = Self.number(value.raw["trim_start_s"]) ?? 0
                let trimEnd = Self.number(value.raw["trim_end_s"]) ?? Self.number(value.raw["source_duration_s"])
                if let trimEnd { value.endS = min(value.endS, value.startS + max(minimumClipDuration, trimEnd - trimStart)) }
            }
            document.visualBlocks[index] = value
        case .motionScene:
            guard let index = document.motionScenes.firstIndex(where: { $0.id == selection.id }) else { return }
            document.motionScenes[index].startS = Self.roundToMotionFrame(start)
            document.motionScenes[index].endS = Self.roundToMotionFrame(min(end, start + 8))
        case .cameraEffect:
            guard let index = document.cameraEffects.firstIndex(where: { $0.id == selection.id }) else { return }
            document.cameraEffects[index].startS = start
            document.cameraEffects[index].endS = min(start + 2, max(start + 0.4, end))
        default: break
        }
    }

    private func supportsTimedGesture(_ selection: EditorSelection) -> Bool {
        if selection.kind == .visualBlock {
            return document.visualBlocks.first(where: { $0.id == selection.id })?.kind != "montage"
        }
        return true
    }

    private func setSoundEffectTrimBounds(_ id: String, edge: NativeTrimEdge, translation: Double, in document: inout EditorDocument) {
        guard let index = document.soundEffects.firstIndex(where: { $0.id == id }) else { return }
        var effect = document.soundEffects[index]
        let sourceDuration = max(minimumClipDuration, Self.number(effect.raw["duration_s"]) ?? max(minimumClipDuration, effect.endS - effect.startS))
        let trimStart = max(0, Self.number(effect.raw["trim_start_s"]) ?? 0)
        let trimEnd = min(sourceDuration, Self.number(effect.raw["trim_end_s"]) ?? sourceDuration)
        switch edge {
        case .leading:
            effect.raw["trim_start_s"] = .number(min(max(0, trimStart + translation), trimEnd - minimumClipDuration))
        case .trailing:
            effect.raw["trim_end_s"] = .number(max(trimStart + minimumClipDuration, min(sourceDuration, trimEnd + translation)))
        }
        document.soundEffects[index] = effect
    }

    private func sectionCapabilityKey(_ section: EditorSection) -> String {
        switch section {
        case .timeline: return "timeline"
        case .text: return "text_elements"
        case .captionMeta, .captions: return "captions"
        case .mix, .music, .backgroundMusic: return "mix"
        default: return section.rawValue
        }
    }
    private func sectionCapabilityKeys(_ section: EditorSection) -> [String] {
        switch section {
        case .soundEffects: return ["sound_effects", "sfx", "lanes.sfx"]
        case .mediaOverlays: return ["media_overlays", "overlays", "lanes.overlays"]
        case .visualBlocks: return ["visual_blocks", "lanes.visual_blocks"]
        case .motionScenes: return ["motion_scenes", "lanes.motion_scenes"]
        case .cameraEffects: return ["camera_effects", "lanes.camera_effects"]
        case .carouselMoment: return ["carousel_moment", "carousel"]
        default: return [section.rawValue, sectionCapabilityKey(section)]
        }
    }
    private func canEditSection(_ section: EditorSection) -> Bool {
        if let capability = capability(for: section) { return capability.editable }
        switch section {
        case .timeline: return canEditTimeline
        case .text: return canEditText
        case .captions, .captionMeta: return canEditCaptions
        case .mix, .music, .backgroundMusic: return canEditMix
        case .soundEffects, .mediaOverlays, .visualBlocks, .motionScenes, .cameraEffects,
             .carouselMoment, .lyrics, .orientation, .title: return false
        }
    }

    private func refreshDirtyState() {
        changedSections.formUnion(explicitlyDirtySections)
        for section in EditorSection.allCases {
            // Compare typed lanes rather than their encoded envelopes. The
            // latter may intentionally contain compatibility mirrors (for
            // example `ios_editor` and canonical `text_elements`) which can
            // differ in shape without representing a user edit.
            let differs: Bool
            switch section {
            case .timeline: differs = document.clips != cleanDocument.clips || document.tombstones != cleanDocument.tombstones
            case .text: differs = document.textElements != cleanDocument.textElements
            case .captions: differs = document.captionCues != cleanDocument.captionCues
            case .captionMeta: differs = document.captionMeta != cleanDocument.captionMeta
            case .mix: differs = document.mix != cleanDocument.mix
            case .music: differs = document.music != cleanDocument.music
            case .backgroundMusic: differs = document.backgroundMusic != cleanDocument.backgroundMusic
            case .lyrics: differs = document.lyrics != cleanDocument.lyrics
            case .orientation: differs = document.orientation != cleanDocument.orientation
            case .soundEffects: differs = document.soundEffects != cleanDocument.soundEffects
            case .mediaOverlays: differs = document.mediaOverlays != cleanDocument.mediaOverlays
            case .visualBlocks: differs = document.visualBlocks != cleanDocument.visualBlocks
            case .motionScenes: differs = document.motionScenes != cleanDocument.motionScenes || document.motionRuntimeHash != cleanDocument.motionRuntimeHash
            case .cameraEffects: differs = document.cameraEffects != cleanDocument.cameraEffects
            case .carouselMoment: differs = document.carouselMoment != cleanDocument.carouselMoment
            case .title: differs = document.title != cleanDocument.title
            }
            if differs { changedSections.insert(section) }
            else if !explicitlyDirtySections.contains(section) { changedSections.remove(section) }
        }
        hasUnsavedChanges = !changedSections.isEmpty
        durationSourcesInvalidated = changedSections.contains(.timeline) || changedSections.contains(.carouselMoment)
    }

    private func legacyDraft(from value: EditorDocument) -> EditorDraft {
        projectedDraft(from: value)
    }

    private func timelineZ(_ raw: [String: JSONValue], fallback: Int) -> Int {
        Int((Self.number(raw["z"] ?? raw["z_index"]) ?? Double(fallback)).rounded())
    }

    private static func snapshotPreservingClipMetadata(_ draft: EditorDraft) -> [String: JSONValue] {
        var root = draft.persistedSnapshot()
        let sourcePayload = object(draft.serverSnapshot["editor_payload"])
        let sourceSections = object(sourcePayload?["sections"]) ?? [:]
        var payload = object(root["editor_payload"]) ?? [:]
        var sections = object(payload["sections"]) ?? [:]
        if draft.text.isEmpty, sourceSections["text_elements"] != nil { sections["text_elements"] = sourceSections["text_elements"] }
        if draft.clips.isEmpty, sourceSections["timeline_slots"] != nil { sections["timeline_slots"] = sourceSections["timeline_slots"] }
        if draft.music == nil {
            if let value = sourceSections["music_track_id"] { sections["music_track_id"] = value }
            if let value = sourceSections["music_window"] { sections["music_window"] = value }
        }
        let rows = Self.array(sections["timeline_slots"])
        if !rows.isEmpty {
            sections["timeline_slots"] = .array(rows.enumerated().map { index, row in
                guard index < draft.clips.count, var value = Self.object(row) else { return row }
                let clip = draft.clips[index]
                value["asset_id"] = .string(clip.assetID.uuidString); value["muted"] = .bool(clip.muted)
                if let sourceIndex = clip.sourceClipIndex { value["clip_index"] = .number(Double(sourceIndex)) }
                if let sourceDuration = clip.sourceDuration { value["source_duration_s"] = .number(sourceDuration) }
                return .object(value)
            })
        }
        payload["sections"] = .object(sections); root["editor_payload"] = .object(payload)
        return root
    }

    private func projectedDraft(from value: EditorDocument) -> EditorDraft {
        var projected = DraftSnapshot(
            draftID: "native-\(projectID.uuidString)", itemID: itemID ?? "", variantKey: variantKey ?? "",
            draftRevision: value.revision.number ?? 0, snapshotHash: value.revision.snapshotHash ?? "", etag: etag,
            baseJobID: jobID?.uuidString, baseGenerationID: value.revision.baseGeneration.nilIfEmpty,
            snapshot: value.encodeSnapshot(), canUndo: canUndo, createdAt: .now
        ).editorDraft(projectID: projectID)
        for (index, slot) in value.clips.enumerated() where projected.clips.indices.contains(index) {
            guard let slotID = slot.id else { continue }
            let stableID = clipID(for: slotID)
            let clip = projected.clips[index]
            projected.clips[index] = EditorClip(id: stableID, assetID: clip.assetID, sourceClipIndex: clip.sourceClipIndex, start: clip.start, end: clip.end, trimIn: clip.trimIn, trimOut: clip.trimOut, sourceDuration: clip.sourceDuration, muted: clip.muted, slotID: slotID)
        }
        projected.captions.enabled = Self.bool(value.captionMeta["enabled"]) ?? projected.captions.enabled
        projected.captions.style = value.captionMeta["style"]?.stringValue ?? projected.captions.style
        if var music = projected.music, let level = Self.number(value.mix["music_level"]) { music.volume = level; projected.music = music }
        return projected
    }

    /// Keeps legacy UUID-backed views attached to canonical string slot IDs.
    /// Existing draft metadata wins; otherwise UUID slot IDs are reused and
    /// opaque server IDs receive a session-stable adapter ID.
    private func clipID(for slotID: String?) -> UUID {
        guard let slotID else { return UUID() }
        if let id = clipIDsBySlot[slotID] { return id }
        let id = UUID(uuidString: slotID) ?? UUID()
        clipIDsBySlot[slotID] = id
        return id
    }

    private func replace(with value: EditorDraft) {
        etag = value.etag
        var next = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(value))
        next.revision.number = value.revision
        rememberClipIDs(from: value, in: next)
        rememberCompatibilityMetadata(from: value)
        document = next
    }

    private func rememberClipIDs(from value: EditorDraft, in document: EditorDocument) {
        for (index, clip) in value.clips.enumerated() where document.clips.indices.contains(index) {
            if let slotID = document.clips[index].id { clipIDsBySlot[slotID] = clip.id }
        }
    }

    private func rememberCompatibilityMetadata(from value: EditorDraft) {
        for clip in value.clips {
            compatibilityClipMetadata[clip.id] = (clip.sourceClipIndex, clip.slotID)
        }
    }

    private func commitRequest(sections: [String: JSONValue]?, baseGeneration: String) -> EditorCommitRequest {
        let value = sections ?? [:]
        func array(_ key: String, _ section: EditorSection) -> [JSONValue]? {
            changedSections.contains(section) ? Self.array(value[key]) : nil
        }
        func object(_ key: String, _ section: EditorSection) -> [String: JSONValue]? {
            changedSections.contains(section) ? Self.object(value[key]) ?? [:] : nil
        }
        let musicObject = Self.object(value["music_window"])
        let backgroundObject = Self.object(value["background_music"])
        let lyricsObject = Self.object(value["lyrics"])
        let carouselObject = Self.object(value["carousel_moment"])
        return EditorCommitRequest(
            timelineSlots: array("timeline_slots", .timeline),
            textElements: array("text_elements", .text),
            captionCues: array("caption_cues", .captions),
            captionMeta: object("caption_meta", .captionMeta),
            mix: changedSections.contains(.mix) ? (Self.object(value["mix"]) ?? Self.object(value["audio_mix"]) ?? [:]) : nil,
            musicTrackID: changedSections.contains(.music) ? value["music_track_id"]?.stringValue : nil,
            removeMusic: changedSections.contains(.music) && document.music == nil,
            musicWindow: changedSections.contains(.music) && document.music != nil
                ? EditorCommitMusicWindow(startS: Self.number(musicObject?["start_s"]) ?? document.music?.startS ?? 0, alignment: EditorMusicAlignment(rawValue: musicObject?["alignment"]?.stringValue ?? document.music?.alignment ?? "preserve_cuts") ?? .preserveCuts) : nil,
            backgroundMusic: changedSections.contains(.backgroundMusic) ? (backgroundObject.map { EditorCommitBackgroundMusic(trackID: $0["track_id"]?.stringValue, enabled: Self.bool($0["enabled"]) ?? true, startS: Self.number($0["start_s"]), endS: Self.number($0["end_s"]), gainDB: Self.number($0["gain_db"]), muted: Self.bool($0["muted"]) ?? false) } ?? EditorCommitBackgroundMusic(enabled: false)) : nil,
            lyrics: changedSections.contains(.lyrics) ? (lyricsObject.map { EditorCommitLyrics(enabled: Self.bool($0["enabled"]), lineOverrides: Self.object($0["line_overrides"])) } ?? EditorCommitLyrics(enabled: false)) : nil,
            orientation: changedSections.contains(.orientation) ? value["orientation"]?.stringValue : nil,
            soundEffects: array("sound_effects", .soundEffects),
            mediaOverlays: array("media_overlays", .mediaOverlays),
            visualBlocks: array("visual_blocks", .visualBlocks),
            motionScenes: array("motion_scenes", .motionScenes),
            motionRuntimeHash: changedSections.contains(.motionScenes) ? document.motionRuntimeHash : nil,
            cameraEffects: array("camera_effects", .cameraEffects),
            carouselMoment: changedSections.contains(.carouselMoment) ? (carouselObject.map(EditorCarouselMomentPatch.replace) ?? .remove) : .omitted,
            title: changedSections.contains(.title) ? value["title"]?.stringValue : nil,
            guidedRevisionNumber: guidedRevisionNumber,
            baseGeneration: baseGeneration
        )
    }

    private func acknowledgedSections(
        _ sections: EditorCommitSections,
        submittedSections: Set<EditorSection>
    ) -> Set<EditorSection> {
        var result = Set<EditorSection>()
        if sections.timeline { result.insert(.timeline) }; if sections.textElements { result.insert(.text) }
        if sections.captionCues { result.insert(.captions) }; if sections.captionMeta { result.insert(.captionMeta) }
        if sections.mix { result.insert(.mix) }; if sections.music { result.insert(.music) }
        if sections.backgroundMusic { result.insert(.backgroundMusic) }; if sections.lyrics { result.insert(.lyrics) }
        if sections.orientation { result.insert(.orientation) }; if sections.soundEffects { result.insert(.soundEffects) }
        if sections.mediaOverlays { result.insert(.mediaOverlays) }; if sections.visualBlocks { result.insert(.visualBlocks) }
        if sections.motionScenes { result.insert(.motionScenes) }; if sections.cameraEffects { result.insert(.cameraEffects) }
        if sections.carouselMoment { result.insert(.carouselMoment) }; if sections.title { result.insert(.title) }
        return result.intersection(submittedSections)
    }

    private func acknowledge(_ sections: Set<EditorSection>, generation: String, submittedDocument: EditorDocument) {
        guard !sections.isEmpty else { return }
        document.revision.baseGeneration = generation
        let acknowledgedRevision = document.revision
        for section in sections {
            copy(section, from: submittedDocument, into: &cleanDocument)
        }
        cleanDocument.revision = acknowledgedRevision
        changedSections.subtract(sections)
        explicitlyDirtySections.subtract(sections)
        refreshDirtyState()
    }

    private func copy(_ section: EditorSection, from submitted: EditorDocument, into baseline: inout EditorDocument) {
        switch section {
        case .timeline:
            baseline.clips = submitted.clips
            baseline.tombstones = submitted.tombstones
        case .text: baseline.textElements = submitted.textElements
        case .captions: baseline.captionCues = submitted.captionCues
        case .captionMeta: baseline.captionMeta = submitted.captionMeta
        case .mix: baseline.mix = submitted.mix
        case .music: baseline.music = submitted.music
        case .backgroundMusic: baseline.backgroundMusic = submitted.backgroundMusic
        case .lyrics: baseline.lyrics = submitted.lyrics
        case .orientation: baseline.orientation = submitted.orientation
        case .soundEffects: baseline.soundEffects = submitted.soundEffects
        case .mediaOverlays: baseline.mediaOverlays = submitted.mediaOverlays
        case .visualBlocks: baseline.visualBlocks = submitted.visualBlocks
        case .motionScenes:
            baseline.motionScenes = submitted.motionScenes
            baseline.motionRuntimeHash = submitted.motionRuntimeHash
        case .cameraEffects: baseline.cameraEffects = submitted.cameraEffects
        case .carouselMoment: baseline.carouselMoment = submitted.carouselMoment
        case .title: baseline.title = submitted.title
        }
    }
    private func reflow(_ clips: inout [EditorClip], from index: Int) {
        guard !clips.isEmpty else { return }
        let start = min(max(0, index), clips.count - 1)
        for i in start..<clips.count { let length = max(minimumClipDuration, clips[i].end - clips[i].start); let previousEnd = i == 0 ? 0 : clips[i - 1].end; clips[i].start = previousEnd; clips[i].end = previousEnd + length }
    }
    private func reflowSlots(_ clips: inout [EditorTimelineSlot], from index: Int) {
        _ = clips; _ = index
    }
    private func installPlayer(url: URL, preferredDuration: TimeInterval? = nil) {
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        durationLoadTask?.cancel()
        mediaDuration = nil
        durationSourcesInvalidated = false

        let item = AVPlayerItem(url: url)
        let next = AVPlayer(playerItem: item)
        player = next
        observingPlayer = next
        if let preferredDuration, preferredDuration.isFinite, preferredDuration > 0 {
            authoritativeDuration = preferredDuration
        }
        refreshDuration()

        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self, weak next] _ in
            guard let next else { return }
            Task { @MainActor [weak self, weak next] in
                guard let self, let next, self.player === next else { return }
                self.finishPlayback(for: next)
            }
        }
        timeObserver = next.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.05, preferredTimescale: 600), queue: .main) { [weak self] time in
            MainActor.assumeIsolated {
                guard let self, self.player === next else { return }
                let seconds = time.seconds
                if seconds.isFinite {
                    self.currentTime = min(max(0, seconds), max(0, self.duration))
                }
                self.reconcilePlaybackState(next.timeControlStatus == .playing)
            }
        }

        // AVAsset.duration is commonly indefinite immediately after creating
        // the item. Await the asset load before reconciling the timeline.
        durationLoadTask = Task { @MainActor [weak self, weak next] in
            guard let next else { return }
            guard let loaded = try? await next.currentItem?.asset.load(.duration) else { return }
            guard let self, self.player === next else { return }
            let seconds = loaded.seconds
            guard seconds.isFinite, seconds > 0 else { return }
            self.mediaDuration = seconds
            self.refreshDuration()
        }
    }
    func reconcilePlaybackState(_ nextIsPlaying: Bool) {
        guard isPlaying != nextIsPlaying else { return }
        isPlaying = nextIsPlaying
    }
    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }
    private static func number(_ value: JSONValue?) -> Double? { if case let .number(value) = value { value } else { nil } }
    private static func bool(_ value: JSONValue?) -> Bool? { if case let .bool(value) = value { value } else { nil } }
    private static func roundToMotionFrame(_ value: TimeInterval) -> TimeInterval {
        (value * 30).rounded() / 30
    }

    private func startTime(for selection: EditorSelection) -> TimeInterval? {
        if selection.kind == .clip, let id = UUID(uuidString: selection.id) { return timelineClips.first(where: { $0.id == id })?.start }
        return timelineItems.first(where: { $0.selection == selection })?.start
    }

    /// Public geometry helpers keep the view layer from implementing subtly
    /// different clamping or hit-target rules.
    func timelineX(for time: TimeInterval, width: CGFloat) -> CGFloat { NativeEditorInteraction.x(forTime: time, duration: duration, width: width) }
    func timelineTime(for x: CGFloat, width: CGFloat) -> TimeInterval { NativeEditorInteraction.time(forX: x, duration: duration, width: width) }

    private func refreshDuration() {
        let timelineDuration = timelineProjection.totalDuration
        duration = durationSourcesInvalidated ? timelineDuration : (authoritativeDuration ?? mediaDuration ?? timelineDuration)
        if !duration.isFinite || duration < 0 { duration = max(0, timelineDuration) }
        timelineItemsCache = nil
        if currentTime > duration { seek(to: duration) }
    }

    private func setAuthoritativeDuration(_ value: TimeInterval?) {
        guard let value, value.isFinite, value > 0 else {
            authoritativeDuration = nil
            return
        }
        authoritativeDuration = value
    }

    private func invalidateDurationSources(for sections: Set<EditorSection>) {
        guard sections.contains(.timeline) || sections.contains(.carouselMoment) else { return }
        durationSourcesInvalidated = true
    }

    private func finishPlayback(for endedPlayer: AVPlayer) {
        guard player === endedPlayer else { return }
        endedPlayer.pause()
        currentTime = max(0, duration)
        isPlaying = false
    }

    private func configureCapabilities(from variant: [String: JSONValue]?) {
        rendersOnDevice = variant?["render_destination"]?.stringValue == "device"
        guidedRevisionNumber = Self.number(variant?["editor_revision_number"]).flatMap { $0 >= 1 && $0 <= Double(Int32.max) ? Int($0) : nil }
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
        pendingPreviewGeneration = generation
        guard let api, let jobID, let variantKey else {
            saveState = .previewFailed("Your edit is saved, but this device cannot check the new preview yet.")
            return
        }
        previewRefreshTask = Task { [weak self] in
            var hadNetworkFailure = false
            for _ in 0..<300 {
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled else { return }
                let variant: [String: JSONValue]
                do {
                    variant = try await api.editorVariant(jobID: jobID, variantID: variantKey)
                } catch {
                    hadNetworkFailure = true
                    continue
                }
                let currentGeneration = variant["render_generation_id"]?.stringValue ?? variant["render_finished_at"]?.stringValue
                guard currentGeneration == generation else { continue }
                let status = variant["render_status"]?.stringValue
                if status == "ready", let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
                    guard let self else { return }
                    self.rebaseCleanDraft(from: variant)
                    self.installPlayer(url: url)
                    self.pendingPreviewGeneration = nil
                    self.saveState = .saved
                    return
                }
                if status == "failed" {
                    self?.saveState = .previewFailed("Your edit is saved, but its new preview could not be rendered.")
                    return
                }
            }
            guard !Task.isCancelled, let self, self.pendingPreviewGeneration == generation else { return }
            self.saveState = .previewFailed(
                hadNetworkFailure
                    ? "Your edit is saved, but Kria could not finish checking the preview. Check your connection and try again."
                    : "Your edit is saved, but the preview is taking longer than expected. Try checking again."
            )
        }
    }

    func retryPreviewRefresh() {
        guard let generation = pendingPreviewGeneration else { return }
        saveState = .previewPending
        startPreviewRefresh(generation: generation)
    }

    func retryRender() async {
        guard !isSaving, !pendingRenderRetrySections.isEmpty else { return }
        explicitlyDirtySections.formUnion(pendingRenderRetrySections)
        refreshDirtyState()
        await save()
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
            draftID: "rendered-\(projectID.uuidString)",
            itemID: itemID,
            variantKey: variantKey,
            draftRevision: document.revision.number ?? 0,
            snapshotHash: "",
            etag: etag,
            baseJobID: jobID?.uuidString,
            baseGenerationID: generation,
            snapshot: document.encodeSnapshot(),
            canUndo: false,
            createdAt: .now
        )
        let selected = selection
        replace(with: snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant))
        cleanDocument = document
        undoStack.removeAll()
        redoStack.removeAll()
        changedSections.removeAll()
        explicitlyDirtySections.removeAll()
        pendingRenderRetrySections.removeAll()
        hasUnsavedChanges = false
        if let selected, selectionExists(selected) { selection = selected } else { selection = nil }
        configureCapabilities(from: variant)
        durationSourcesInvalidated = false
        setAuthoritativeDuration(Self.number(variant["duration_s"]))
        refreshDuration()
        return true
    }

    private func selectionExists(_ value: EditorSelection) -> Bool {
        if value.kind == .clip, let id = UUID(uuidString: value.id) { return draft.clips.contains { $0.id == id } }
        return timelineItems.contains { $0.selection == value }
    }
}

private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
