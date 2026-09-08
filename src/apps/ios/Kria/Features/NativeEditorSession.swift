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
    /// Canonical mutable editor state. The legacy `draft` accessor below is a
    /// compatibility projection for first-pass views and test fixtures.
    @Published private(set) var document: EditorDocument
    /// Cross-kind selection is the source of truth. `selectedClipID` below is
    /// a source-compatible adapter for the first-pass clip views.
    @Published private(set) var selection: EditorSelection?
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

    private let projectID: UUID
    private var etag: String = ""

    var draft: EditorDraft {
        get {
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
    private var undoStack: [EditorDocument] = []
    private var redoStack: [EditorDocument] = []
    private var cleanDocument: EditorDocument
    private var api: (any KriaAPIClient)?
    private var threadID: UUID?
    private var itemID: String?
    private var variantKey: String?
    private var jobID: UUID?
    private var previewRefreshTask: Task<Void, Never>?
    private var changedSections: Set<EditorSection> = []
    private var explicitlyDirtySections: Set<EditorSection> = []
    private var clipIDsBySlot: [String: UUID] = [:]
    private var compatibilityClipMetadata: [UUID: (sourceClipIndex: Int?, slotID: String?)] = [:]
    private var compatibilitySnapshot: [String: JSONValue] = [:]
    private var activeTrim: ActiveTrim?
    private var transactionBaseline: EditorDocument?
    nonisolated(unsafe) private var timeObserver: Any?
    nonisolated(unsafe) private var observingPlayer: AVPlayer?

    private struct ActiveTrim {
        let clipID: UUID
        let edge: NativeTrimEdge
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
    func capability(for section: EditorSection) -> EditorCapability? { document.capabilities[section.rawValue] ?? document.capabilities[sectionCapabilityKey(section)] }
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
            undoStack.append(baseline); redoStack.removeAll()
        }
        transactionBaseline = nil
    }
    func beginEditTransaction() { beginTransaction() }
    func endEditTransaction() { endTransaction() }

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
        var items: [NativeEditorTimelineItem] = draft.clips.enumerated().map { index, clip in
            NativeEditorTimelineItem(selection: EditorSelection(kind: .clip, id: clip.id.uuidString), start: clip.start, end: clip.end, sourceIndex: index)
        }
        items += document.textElements.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: item.id), start: item.startS, end: item.endS, zIndex: 300, sourceIndex: index) }
        items += document.captionCues.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .captionCue, id: item.id), start: item.startS, end: item.endS, zIndex: 400, sourceIndex: index) }
        items += document.soundEffects.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .soundEffect, id: item.id), start: item.startS, end: item.endS, zIndex: 100, sourceIndex: index) }
        items += document.mediaOverlays.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .mediaOverlay, id: item.id), start: item.startS, end: item.endS, zIndex: 200, sourceIndex: index) }
        items += document.visualBlocks.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .visualBlock, id: item.id), start: item.startS, end: item.endS, zIndex: 150, sourceIndex: index) }
        items += document.motionScenes.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .motionScene, id: item.id), start: item.startS, end: item.endS, zIndex: 250, sourceIndex: index) }
        items += document.cameraEffects.enumerated().map { index, item in NativeEditorTimelineItem(selection: EditorSelection(kind: .cameraEffect, id: item.id), start: item.startS, end: item.endS, zIndex: 260, sourceIndex: index) }
        if let music = document.music { items.append(NativeEditorTimelineItem(selection: EditorSelection(kind: .music, id: music.trackID), start: music.startS, end: max(duration, music.startS + duration), zIndex: 50, sourceIndex: 0)) }
        return items
    }

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
            cleanDocument = document; undoStack.removeAll(); redoStack.removeAll(); changedSections.removeAll(); explicitlyDirtySections.removeAll(); hasUnsavedChanges = false; saveState = .idle
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
            draft = snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant)
            configureCapabilities(from: variant)
            cleanDocument = document
            undoStack.removeAll()
            redoStack.removeAll()
            changedSections.removeAll()
            explicitlyDirtySections.removeAll()
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
            undoStack.append(active.baseline)
            redoStack.removeAll()
            active.recordedUndo = true
        }
        document = next
        activeTrim = active
        refreshDuration()
        refreshDirtyState()
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
        mutateClip(clipID: clipID) { $0.lookPreset = preset }
    }
    func setClipLookPreset(clipID: UUID, preset: String?) { setClipLookPreset(clipID: clipID.uuidString, preset: preset) }
    func updateClipLook(clipID: String, preset: String?, adjustments: [String: JSONValue]? = nil) {
        mutateClip(clipID: clipID) { slot in slot.lookPreset = preset; if let adjustments { slot.lookAdjustments = adjustments } }
    }
    func updateClipLook(clipID: UUID, preset: String?, adjustments: [String: JSONValue]? = nil) { updateClipLook(clipID: clipID.uuidString, preset: preset, adjustments: adjustments) }
    func setClipLookAdjustments(clipID: String, adjustments: [String: JSONValue]?) {
        mutateClip(clipID: clipID) { $0.lookAdjustments = adjustments }
    }
    func setClipLookAdjustments(clipID: UUID, adjustments: [String: JSONValue]?) { setClipLookAdjustments(clipID: clipID.uuidString, adjustments: adjustments) }
    func setClipTransition(clipID: String, transition: String, durationS: Double? = nil) {
        mutateClip(clipID: clipID) { $0.transitionAfter = transition; $0.transitionDurationS = durationS }
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
    func setTextAnimation(id: String, animation: String?) { setTextRaw(id: id, key: "effect", value: animation.map(JSONValue.string) ?? .null) }
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
    func setCaptionFont(_ font: String?) { setCaptionMeta(key: "font", value: font.map(JSONValue.string)) }
    func setCaptionSize(_ sizePX: Double?) { setCaptionMeta(key: "size_px", value: sizePX.map(JSONValue.number)) }
    func setCaptionColor(_ color: String?) { setCaptionMeta(key: "color", value: color.map(JSONValue.string)) }
    func setCaptionHighlightColor(_ color: String?) { setCaptionMeta(key: "highlight_color", value: color.map(JSONValue.string)) }
    func setCaptionStrokeWidth(_ width: Double?) { setCaptionMeta(key: "stroke_width", value: width.map(JSONValue.number)) }
    func setCaptionShadowEnabled(_ enabled: Bool) { setCaptionMeta(key: "shadow_enabled", value: .bool(enabled)) }
    func setCaptionPositionY(_ y: Double?) { setCaptionMeta(key: "y_frac", value: y.map { .number(min(max(0, $0), 1)) }) }

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
    func setOriginalVolume(_ level: Double) { setOriginalMixLevel(level) }
    func setMusicWindow(startS: Double? = nil, alignment: String? = nil) {
        guard canEditSection(.music), document.music != nil else { return }
        transactDocument(section: .music) { music in
            guard var value = music.music else { return }
            if let startS { value.startS = max(0, startS) }; if let alignment { value.alignment = alignment }; music.music = value
        }
    }
    func setMusic(trackID: String, startS: Double = 0, alignment: String? = nil) {
        guard canEditSection(.music) else { return }
        transactDocument(section: .music) { $0.music = EditorMusic(trackID: trackID, startS: max(0, startS), alignment: alignment) }
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
        redoStack.append(document); document = previous; refreshDuration(); refreshDirtyState()
    }

    func redo() {
        guard let next = redoStack.popLast() else { return }
        undoStack.append(document); document = next; refreshDuration(); refreshDirtyState()
    }

    func save() async {
        guard !isSaving, hasUnsavedChanges else { return }
        guard let api, let itemID, let variantKey else { saveState = .failed("Load the project before saving edits."); return }
        isSaving = true; saveState = .saving
        defer { isSaving = false }
        let snapshot = document.encodeSnapshot()
        let payload = Self.object(snapshot["editor_payload"]); let sections = Self.object(payload?["sections"])
        let request = commitRequest(sections: sections, baseGeneration: payload?["base_generation"]?.stringValue ?? document.revision.baseGeneration)
        do {
            let response = try await api.editorCommit(itemID: itemID, variantID: variantKey, request: request)
            let acknowledged = acknowledgedSections(response.sections)
            document.revision.number = response.revisionNumber ?? document.revision.number
            document.revision.hash = response.revisionHash ?? document.revision.hash
            acknowledge(acknowledged, generation: response.generation)
            undoStack.removeAll(); redoStack.removeAll()
            saveState = response.ok ? .previewPending : .failed("The edit was saved, but its new preview could not be rendered.")
            if response.ok, !acknowledged.isEmpty { startPreviewRefresh(generation: response.generation) }
        } catch APIError.conflict { saveState = .conflict }
        catch { saveState = .failed(error.localizedDescription) }
    }

    private func transact(section: EditorSection, _ body: (inout EditorDraft) -> Void) {
        var next = draft; body(&next); guard next != draft else { return }
        if transactionBaseline == nil { undoStack.append(document); redoStack.removeAll() }
        replace(with: next); changedSections.insert(section); refreshDuration(); refreshDirtyState()
    }

    private func transactDocument(section: EditorSection, _ body: (inout EditorDocument) -> Void) {
        var next = document; body(&next); guard next != document else { return }
        if transactionBaseline == nil { undoStack.append(document); redoStack.removeAll() }
        document = next; changedSections.insert(section); refreshDuration(); refreshDirtyState()
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
    private func canEditSection(_ section: EditorSection) -> Bool {
        if let capability = capability(for: section) { return capability.editable }
        switch section {
        case .timeline: return canEditTimeline
        case .text: return canEditText
        case .captions, .captionMeta: return canEditCaptions
        case .mix, .music, .backgroundMusic: return canEditMix
        default: return true
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
    }

    private func legacyDraft(from value: EditorDocument) -> EditorDraft {
        projectedDraft(from: value)
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
            let stableID = clipIDsBySlot[slotID] ?? UUID(uuidString: slotID) ?? UUID()
            clipIDsBySlot[slotID] = stableID
            let clip = projected.clips[index]
            projected.clips[index] = EditorClip(id: stableID, assetID: clip.assetID, sourceClipIndex: clip.sourceClipIndex, start: clip.start, end: clip.end, trimIn: clip.trimIn, trimOut: clip.trimOut, sourceDuration: clip.sourceDuration, muted: clip.muted, slotID: slotID)
        }
        projected.captions.enabled = Self.bool(value.captionMeta["enabled"]) ?? projected.captions.enabled
        projected.captions.style = value.captionMeta["style"]?.stringValue ?? projected.captions.style
        if var music = projected.music, let level = Self.number(value.mix["music_level"]) { music.volume = level; projected.music = music }
        return projected
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
                ? EditorCommitMusicWindow(startS: Self.number(musicObject?["start_s"]) ?? document.music?.startS ?? 0, alignment: musicObject?["alignment"]?.stringValue ?? document.music?.alignment ?? "preserve_cuts") : nil,
            backgroundMusic: changedSections.contains(.backgroundMusic) ? (backgroundObject.map { EditorCommitBackgroundMusic(trackID: $0["track_id"]?.stringValue, enabled: Self.bool($0["enabled"]) ?? true, startS: Self.number($0["start_s"]), endS: Self.number($0["end_s"]), gainDB: Self.number($0["gain_db"]), muted: Self.bool($0["muted"]) ?? false) } ?? EditorCommitBackgroundMusic(enabled: false)) : nil,
            lyrics: changedSections.contains(.lyrics) ? (lyricsObject.map { EditorCommitLyrics(enabled: Self.bool($0["enabled"]), lineOverrides: Self.object($0["line_overrides"])) } ?? EditorCommitLyrics(enabled: false)) : nil,
            orientation: changedSections.contains(.orientation) ? value["orientation"]?.stringValue : nil,
            soundEffects: array("sound_effects", .soundEffects),
            mediaOverlays: array("media_overlays", .mediaOverlays),
            visualBlocks: array("visual_blocks", .visualBlocks),
            motionScenes: array("motion_scenes", .motionScenes),
            motionRuntimeHash: changedSections.contains(.motionScenes) ? document.motionScenes.first?.runtimeHash : nil,
            cameraEffects: array("camera_effects", .cameraEffects),
            carouselMoment: changedSections.contains(.carouselMoment) ? (carouselObject.map(EditorCarouselMomentPatch.replace) ?? .remove) : .omitted,
            title: changedSections.contains(.title) ? value["title"]?.stringValue : nil,
            baseGeneration: baseGeneration
        )
    }

    private func acknowledgedSections(_ sections: EditorCommitSections) -> Set<EditorSection> {
        var result = Set<EditorSection>()
        if sections.timeline { result.insert(.timeline) }; if sections.textElements { result.insert(.text) }
        if sections.captionCues { result.insert(.captions) }; if sections.captionMeta { result.insert(.captionMeta) }
        if sections.mix { result.insert(.mix) }; if sections.music { result.insert(.music) }
        if sections.backgroundMusic { result.insert(.backgroundMusic) }; if sections.lyrics { result.insert(.lyrics) }
        if sections.orientation { result.insert(.orientation) }; if sections.soundEffects { result.insert(.soundEffects) }
        if sections.mediaOverlays { result.insert(.mediaOverlays) }; if sections.visualBlocks { result.insert(.visualBlocks) }
        if sections.motionScenes { result.insert(.motionScenes) }; if sections.cameraEffects { result.insert(.cameraEffects) }
        if sections.carouselMoment { result.insert(.carouselMoment) }; if sections.title { result.insert(.title) }
        return result.intersection(changedSections)
    }

    private func acknowledge(_ sections: Set<EditorSection>, generation: String) {
        guard !sections.isEmpty else { return }
        document.revision.baseGeneration = generation
        let acknowledgedRevision = document.revision
        let current = document.encodeSnapshot()
        var baseline = cleanDocument.encodeSnapshot()
        let currentPayload = Self.object(current["editor_payload"]) ?? [:]
        let currentSections = Self.object(currentPayload["sections"]) ?? [:]
        var baselinePayload = Self.object(baseline["editor_payload"]) ?? [:]
        var baselineSections = Self.object(baselinePayload["sections"]) ?? [:]
        for section in sections {
            for key in wireKeys(for: section) { if let value = currentSections[key] { baselineSections[key] = value } else { baselineSections.removeValue(forKey: key) } }
        }
        baselinePayload["base_generation"] = .string(generation); baselinePayload["sections"] = .object(baselineSections); baseline["editor_payload"] = .object(baselinePayload)
        cleanDocument = EditorDocument(snapshot: baseline)
        cleanDocument.revision = acknowledgedRevision
        changedSections.subtract(sections)
        explicitlyDirtySections.subtract(sections)
        hasUnsavedChanges = !changedSections.isEmpty
    }

    private func wireKeys(for section: EditorSection) -> [String] {
        if section == .music { return ["music_track_id", "music_window", "music"] }
        switch section {
        case .timeline: return ["timeline_slots"]; case .text: return ["text_elements"]; case .captions: return ["caption_cues"]
        case .captionMeta: return ["caption_meta"]; case .mix: return ["mix", "audio_mix"]; case .backgroundMusic: return ["background_music"]
        case .lyrics: return ["lyrics"]; case .orientation: return ["orientation"]; case .soundEffects: return ["sound_effects"]
        case .mediaOverlays: return ["media_overlays"]; case .visualBlocks: return ["visual_blocks"]; case .motionScenes: return ["motion_scenes"]
        case .cameraEffects: return ["camera_effects"]; case .carouselMoment: return ["carousel_moment"]; case .title: return ["title"]
        case .music: return ["music_track_id", "music_window"]
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
    private static func number(_ value: JSONValue?) -> Double? { if case let .number(value) = value { value } else { nil } }
    private static func bool(_ value: JSONValue?) -> Bool? { if case let .bool(value) = value { value } else { nil } }

    private func startTime(for selection: EditorSelection) -> TimeInterval? {
        if selection.kind == .clip, let id = UUID(uuidString: selection.id) { return draft.clips.first(where: { $0.id == id })?.start }
        return timelineItems.first(where: { $0.selection == selection })?.start
    }

    /// Public geometry helpers keep the view layer from implementing subtly
    /// different clamping or hit-target rules.
    func timelineX(for time: TimeInterval, width: CGFloat) -> CGFloat { NativeEditorInteraction.x(forTime: time, duration: duration, width: width) }
    func timelineTime(for x: CGFloat, width: CGFloat) -> TimeInterval { NativeEditorInteraction.time(forX: x, duration: duration, width: width) }

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
        hasUnsavedChanges = false
        if let selected, selectionExists(selected) { selection = selected } else { selection = nil }
        configureCapabilities(from: variant)
        refreshDuration()
        return true
    }

    private func selectionExists(_ value: EditorSelection) -> Bool {
        if value.kind == .clip, let id = UUID(uuidString: value.id) { return draft.clips.contains { $0.id == id } }
        return timelineItems.contains { $0.selection == value }
    }
}

private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
