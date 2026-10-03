import AVFoundation
import KriaMediaEngine
import os
import SwiftUI
import ImageIO
import UIKit

enum NativeSourcePreviewState: Equatable {
    case idle, preparing, ready, failed(String)
    /// KRI-211: the project's original clips are not on this iPhone (made on
    /// another device, or the file changed). Retrying cannot fix it — only
    /// finding the files can — so it is its own state with its own copy.
    case originalsUnavailable

    /// Both settle on the finished render (when there is one); only the way
    /// out differs (Retry versus finding the originals).
    var isFailure: Bool {
        switch self {
        case .failed, .originalsUnavailable: true
        case .idle, .preparing, .ready: false
        }
    }
}

/// A player item that built fine but failed while loading or playing (KRI-200). Before this, a failed
/// item left `play()` a silent no-op: the button flipped straight back and nothing explained why.
enum NativeEditorPlaybackFailure: Error, Equatable {
    /// The editable source composition failed; the finished render takes over.
    case liveItem
    /// The finished render itself could not be loaded, even after a fresh link.
    case finishedItem
}

enum NativeTrimEdge: Sendable { case leading, trailing }

/// Lets a background-task expiration handler end the very assertion it belongs to. The identifier only
/// exists after `begin` returns, but the handler is created before that.
@MainActor final class BackgroundAssertionHandle {
    private var identifier: UIBackgroundTaskIdentifier = .invalid
    func set(_ identifier: UIBackgroundTaskIdentifier) { self.identifier = identifier }
    /// Ends the assertion once; later calls are no-ops, so the handler and the normal exit can't double-end.
    func end(using activity: any BackgroundActivityAssertion) {
        guard identifier != .invalid else { return }
        let ending = identifier
        identifier = .invalid
        activity.end(ending)
    }
}

enum NativeEditorSaveState: Equatable, Sendable {
    case idle
    case saving
    case saved
    case previewPending
    case renderRetryNeeded(String)
    case deviceRenderRetryNeeded(String)
    case conflict
    case loadFailed(String)
    case refreshFailed(String)
    case previewFailed(String)
    case failed(String)
}

enum NativeEditorLoadState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case failed(String)
}

enum NativeEditorVideoDownloadRoute: Equatable {
    case sourcePreview
    case localFile(URL)
    case server(NativeEditorVideoDownloadTarget)
}

struct NativeEditorTemporaryVideo {
    let fileURL: URL
    let cleanupURL: URL
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
            scheduleSourcePreviewUpdate()
        }
    }
    /// Cross-kind selection is the source of truth. `selectedClipID` below is
    /// a source-compatible adapter for the first-pass clip views.
    @Published private(set) var selection: EditorSelection? { didSet { prepareInteractionLayers() } }
    @Published private(set) var selectionRequest = 0
    /// A new text item is staged separately until Done. Cancel must not roll
    /// back unrelated edits or leave a placeholder in the persisted document.
    @Published private(set) var pendingText: EditorTextElement? {
        didSet { scheduleSourcePreviewUpdate() }
    }
    let playbackClock = NativeEditorPlaybackClock()
    var currentTime: TimeInterval {
        get { playbackClock.currentTime }
        set { playbackClock.currentTime = newValue }
    }
    /// Editable content only; branding never expands authoring ranges.
    @Published var duration: TimeInterval = 0
    /// The visible source composition includes the outro after the editable
    /// content. Transport and scrubbing must reach that tail as well.
    var playbackDuration: TimeInterval {
        if isPlayingBrandedSourcePreview, let sourcePreview {
            return sourcePreview.preview.description.duration
        }
        return duration
    }
    /// The scrub/seek boundary while a preview rebuild is pending or in
    /// flight. `playbackDuration` is the *composition's* duration, which is
    /// stale until the rebuild lands — sometimes for seconds after a timing
    /// gesture ends on a heavy document. Auto-scroll (and any seek) must be
    /// allowed to reach the timeline's true projected end during that window,
    /// not just the last-compiled composition's end. Gated on preview
    /// staleness rather than gesture activity so the bound stays widened for
    /// as long as the async rebuild is actually pending, not just while a
    /// finger is on screen.
    var timelineScrubDuration: TimeInterval {
        sourcePreviewSettled ? playbackDuration : max(playbackDuration, timelineProjection.totalDuration)
    }
    /// The source preview reflects the latest document edit. False while a
    /// rebuild is pending, which for audio edits is a full one (KRI-241).
    private var sourcePreviewSettled: Bool {
        !sourcePreviewUpdateDeferred && sourcePreviewSequence == sourcePreviewSettledSequence
    }
    /// KRI-166: true while the player shows the branded source preview
    /// (always built with `branding: .standard`, see `scheduleSourcePreviewUpdate`)
    /// rather than a rendered MP4. The outro placeholder trusts a much smaller
    /// gap after the last clip in this mode, since the outro is guaranteed to
    /// be there; a rendered asset's `duration` may already bake the tail in.
    var isPlayingBrandedSourcePreview: Bool {
        guard let sourcePreview else { return false }
        return player?.currentItem === sourcePreview.preview.playerItem
    }
    @Published var isPlaying = false
    @Published var isSaving = false
    @Published var hasUnsavedChanges = false
    @Published var saveState: NativeEditorSaveState = .idle {
        didSet { saveStateBeforePromptFailure = nil }
    }
    private var saveStateBeforePromptFailure: NativeEditorSaveState?
    private var conversationRuntimeVersion = 2
    private var legacyPromptSnapshot: [String: JSONValue]?
    @Published private(set) var loadState: NativeEditorLoadState = .loaded
    @Published var player: AVPlayer?
    struct TextInteractionFrame {
        let element: EditorTextElement
        let time: Double
        let below: UIImage
        let above: UIImage
        let text: UIImage
        let rect: CGRect
    }
    @Published private(set) var textInteractionFrame: TextInteractionFrame?
    struct MediaInteractionFrame {
        let selection: EditorSelection
        let time: Double
        let below: UIImage
        let above: UIImage
        let media: UIImage
        let rect: CGRect
    }
    @Published private(set) var mediaInteractionFrame: MediaInteractionFrame?
    private var textInteractionTask: Task<Void, Never>?
    private var textInteractionSequence = 0

    private func prepareInteractionLayers() {
        prepareTextInteraction()
        prepareMediaInteraction()
    }

    private func prepareTextInteraction() {
        guard !isDirectManipulating, !isPlaying, let selection, selection.kind == .text,
              let element = document.textElements.first(where: { $0.id == selection.id }),
              let preview = sourcePreview, let item = player?.currentItem,
              sourcePreviewState == .ready else { return }
        textInteractionTask?.cancel()
        textInteractionSequence += 1
        let sequence = textInteractionSequence
        let time = min(currentTime, max(0, duration - 1.0 / 600))
        textInteractionTask = Task { @MainActor [weak self] in
            do {
                // Prepare after scrubbing settles, never for every scrub frame.
                try await Task.sleep(for: .milliseconds(100))
                try Task.checkCancellation()
                let layers = try preview.textInteractionLayers(id: element.id, time: time)
                @MainActor func image(_ composition: AVVideoComposition) async throws -> UIImage {
                    let generator = AVAssetImageGenerator(asset: item.asset)
                    generator.videoComposition = composition
                    generator.maximumSize = CGSize(width: 1080, height: 1920)
                    generator.requestedTimeToleranceBefore = .zero
                    generator.requestedTimeToleranceAfter = .zero
                    let cg: CGImage = try await withCheckedThrowingContinuation { continuation in
                        generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: CMTime(seconds: time, preferredTimescale: 600))]) { _, image, _, result, error in
                            if result == .succeeded, let image { continuation.resume(returning: image) }
                            else { continuation.resume(throwing: error ?? MediaEngineError.exportFailed) }
                        }
                    }
                    return UIImage(cgImage: cg)
                }
                let below = try await image(layers.below)
                let above = try await image(layers.above)
                guard let self, !Task.isCancelled, self.textInteractionSequence == sequence,
                      self.selection == selection, self.document.textElements.first(where: { $0.id == element.id }) == element else { return }
                self.textInteractionFrame = TextInteractionFrame(element: element, time: time,
                    below: below, above: above, text: UIImage(cgImage: layers.text), rect: layers.rect)
            } catch {
                guard !Task.isCancelled else { return }
                #if DEBUG
                NativePreviewDiagnostics.failure("text-interaction-prepare", error: error)
                #endif
            }
        }
    }

    private func prepareMediaInteraction() {
        guard !isDirectManipulating, !isPlaying, let selection,
              selection.kind == .mediaOverlay || selection.kind == .visualBlock,
              let preview = sourcePreview, let item = player?.currentItem,
              sourcePreviewState == .ready else { return }
        let mediaID: String
        switch selection.kind {
        case .mediaOverlay: mediaID = "overlay:" + selection.id
        case .visualBlock: mediaID = "visual-\(selection.id)-\(selection.id)"
        default: return
        }
        textInteractionTask?.cancel()
        textInteractionSequence += 1
        let sequence = textInteractionSequence
        let time = min(currentTime, max(0, duration - 1.0 / 600))
        textInteractionTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(for: .milliseconds(100))
                try Task.checkCancellation()
                let layers = try preview.mediaInteractionLayers(id: mediaID, time: time)
                @MainActor func image(_ composition: AVVideoComposition) async throws -> UIImage {
                    let generator = AVAssetImageGenerator(asset: item.asset)
                    generator.videoComposition = composition
                    generator.maximumSize = CGSize(width: 1080, height: 1920)
                    generator.requestedTimeToleranceBefore = .zero
                    generator.requestedTimeToleranceAfter = .zero
                    let cg: CGImage = try await withCheckedThrowingContinuation { continuation in
                        generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: CMTime(seconds: time, preferredTimescale: 600))]) { _, image, _, result, error in
                            if result == .succeeded, let image { continuation.resume(returning: image) }
                            else { continuation.resume(throwing: error ?? MediaEngineError.exportFailed) }
                        }
                    }
                    return UIImage(cgImage: cg)
                }
                async let below = image(layers.below)
                async let above = image(layers.above)
                async let media = image(layers.media)
                let rendered = try await (below, above, media)
                guard let self, !Task.isCancelled, self.textInteractionSequence == sequence,
                      self.selection == selection else { return }
                self.mediaInteractionFrame = MediaInteractionFrame(selection: selection, time: time,
                    below: rendered.0, above: rendered.1, media: rendered.2, rect: layers.rect)
            } catch {
                guard !Task.isCancelled else { return }
                #if DEBUG
                NativePreviewDiagnostics.failure("media-interaction-prepare", error: error)
                #endif
            }
        }
    }

    @Published private(set) var scrubPreviewFrame: UIImage?
    private(set) var scrubPreviewTime: TimeInterval?
    private var scrubFrameGenerator: AVAssetImageGenerator?
    private weak var scrubFramePlayerItem: AVPlayerItem?
    private var scrubFrameComposition: AVVideoComposition?
    private var scrubFrameTask: Task<Void, Never>?
    private var pendingScrubFrameTime: TimeInterval?
    private var scrubFrameGeneration = 0
    /// KRI-95: measures the real editor's scrub-to-visible-frame latency.
    /// `MetricsCollector.record` is a lock + array append — cheap at the rate
    /// scrub gestures actually happen — so, matching how `MediaDiagnosticView`
    /// (DEBUG-only) already records unconditionally, this isn't gated behind a
    /// toggle. Previously only that debug screen collected this metric; this
    /// is the path KRI-97's physical-device seek p95 measurement actually
    /// needs numbers from.
    let previewInstrumentation = MetricsCollector()
    private var seekLatencyMeter = SeekLatencyMeter()
    @Published private(set) var sourcePreviewState: NativeSourcePreviewState = .idle
    private var finishedRenderURL: URL?
    private var finishedRenderDuration: TimeInterval?
    private var finishedRenderPlayer: AVPlayer?
    /// Whether `finishedRenderPlayer`'s render is known to reflect the
    /// document currently loaded (server `render_status == "ready"` at
    /// install time), not a render that predates a save still catching up.
    /// See `installPlayer(url:preferredDuration:isCurrent:)`.
    private var finishedRenderIsCurrent = true
    private var sourcePreview: LivePreviewComposition?
    private var sourceCompiler: NativeEditorRenderCompiler?
    private var sourceResolver: NativeEditorSourceResolver?
    private var resolvedAudio: [String: ResolvedEditorSource] = [:]
    /// A device recipe may intentionally have no narration. Remember that
    /// successful empty resolution for this generation so ordinary editor
    /// rebuilds do not repeatedly poll the device-render endpoint.
    private var deviceNarrationResolutionGeneration: String?
    private var previewVariant: [String: JSONValue] = [:]
    var musicPlaybackMode: NativeMusicPlaybackMode { .init(variant: previewVariant) }
    var songReference: NativeSongReference? {
        guard musicPlaybackMode == .referenceOnly else { return nil }
        return NativeSongReference(variant: previewVariant)
    }
    var editorSongReferencePresentation: NativeEditorSongReferencePresentation? {
        guard let songReference else { return nil }
        return .make(reference: songReference, baselineDuration: authoritativeDuration,
            currentDuration: duration, durationChanged: durationSourcesInvalidated)
    }
    private var sourceAudioPreserved: Bool { previewVariant["source_audio_preserved"]?.boolValue ?? true }
    private var sourcePool: NativeEditorSourcePool?
    /// The source pool the last preview attempt asked for. `sourcePool` is only set once every source
    /// resolves, so a preview that fails on a missing original has no pool there; this one stays, and
    /// is what "Find original files" derives its targets from (KRI-211).
    private var originalsRecoveryPool: NativeEditorSourcePool?
    private var resolvedMedia: [String: ResolvedEditorSource] = [:]
    private var resolvedSources: [Int: ResolvedEditorSource]?
    private var sourcePreviewTask: Task<Void, Never>?
    private var sourcePreviewSequence = 0
    /// The last `sourcePreviewSequence` a `rebuildSourcePreview` attempt
    /// actually settled (success or failure) — never a superseded/cancelled
    /// attempt. `sourcePreviewTask` itself is never reset to nil on
    /// completion, so comparing sequence numbers (not `sourcePreviewTask !=
    /// nil`) is the only reliable "is a rebuild still pending" signal.
    private var sourcePreviewSettledSequence = 0
    private var sourcePreviewGeneration: String?
    /// True for ~3 s after a pending save's render lands (KRI-227), so the
    /// creator sees the edit was applied. Never set on editor open.
    @Published private(set) var showsEditApplied = false
    private var editAppliedTask: Task<Void, Never>?
    private var sourcePreviewUpdateDeferred = false
    var hasSourcePreview: Bool { sourcePreviewState == .ready }
    /// A source-composited player is editable. A failed source preparation can
    /// instead show only the server-rendered video, with canvas interaction
    /// deliberately disabled until a retry produces a source preview.
    var isShowingRenderedFallback: Bool {
        guard sourcePreviewState.isFailure else { return false }
        return player != nil && player === finishedRenderPlayer
    }

    var canDisplayCurrentPlayer: Bool {
        guard player != nil else { return false }
        switch sourcePreviewState {
        case .idle:
            // Before the first load resolves, the only player available is
            // whatever `initialPlaybackURL` seeded — trustworthy when a
            // caller just fetched it for this exact screen (e.g. ResultsView
            // refreshing its own playback link), not when it merely fell
            // back to a project summary's possibly long-stale `outputURL`
            // (the shared chat-editor session's construction path). An
            // untrusted seed shows the loading surface instead of a video
            // that might already be wrong.
            return seedIsTrusted
        case .ready:
            return true
        case .preparing:
            // While the local, editable source preview is still building
            // from the current document, only show the finished render if
            // it's known to already reflect that document (render_status
            // was "ready" at load time). A render still catching up to the
            // last save would otherwise flash pre-edit title/text styling
            // for the few seconds this preparation takes — the loading
            // surface is the honest state until the source preview, built
            // straight from the current document, is ready.
            return player === finishedRenderPlayer && finishedRenderIsCurrent
        case .failed, .originalsUnavailable:
            // Once source-preview construction has genuinely failed (not
            // merely still preparing), a stale finished render is still
            // strictly better than nothing — the user can at least see and
            // download *a* video while a retry is pending.
            return isShowingRenderedFallback
        }
    }
    @Published private(set) var isDirectManipulating = false
    @Published private(set) var canEditTimeline = true
    @Published private(set) var canEditText = true
    @Published private(set) var canEditCaptions = false
    @Published private(set) var canEditMix = false
    let operations: any EditorOperations
    @Published private(set) var rendersOnDevice = false
    /// True when the last source-preview failure cannot be fixed by retrying.
    @Published private(set) var sourcePreviewFailureIsPermanent = false
    private var deviceRenders: DeviceRenderSessions?
    private var pendingDeviceRenderIdentity: DeviceRenderIdentity?
    private var guidedRevisionNumber: Int?
    var deviceRenderKey: DeviceRenderKey? {
        guard rendersOnDevice, let jobID, let variantKey else { return nil }
        return DeviceRenderKey(projectID: threadID ?? projectID, jobID: jobID, variantID: variantKey)
    }

    func useDeviceRendering(_ sessions: DeviceRenderSessions) { deviceRenders = sessions }
    /// Upload records outlive this screen. The session only observes their
    /// results and turns a ready source into one local document transaction.
    func useMediaUploads(_ uploads: BackgroundUploadCoordinator) { mediaUploads = uploads }
    func suspendEditorImports() { editorImportsActive = false }
    func resumeEditorImports() async {
        editorImportsActive = true
        await resumePendingEditorPlacements()
    }

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
    private var undoStack: [EditorDocument] = [] { didSet { undoHistoryVersion &+= 1 } }
    /// KRI-240: changes on every undo-history change (new step, undo, trim at the
    /// limit, clear), so the caption editor's Undo notice can tell its removal is
    /// still the newest step. A count can't: it saturates at `historyLimit`.
    private(set) var undoHistoryVersion = 0
    private var redoStack: [EditorDocument] = []
    private var cleanDocument: EditorDocument
    private var api: (any KriaAPIClient)?
    private weak var mediaUploads: BackgroundUploadCoordinator?
    private var pendingPlacementIDs: Set<UUID> = []
    private var editorImportsActive = true
    private var threadID: UUID?
    private var itemID: String?
    var visualItemID: String? { itemID }
    @Published private(set) var visualLibrary: [CreationVisual] = []
    @Published private(set) var visualLibraryLimit = 20
    @Published private(set) var visualLibraryLoading = false
    /// `GET /sound-effects` catalog for the Effects tab's browse list.
    /// Shared with `preparePreviewAudio`'s sfx fallback below, which needs
    /// the same lookup for a just-placed effect that isn't in the timeline's
    /// precomputed source pool yet.
    @Published private(set) var soundEffectCatalog: [NativeEditorSoundEffect] = []
    @Published private(set) var soundEffectCatalogLoading = false
    /// Automatic reanalysis budget for transient analysis failures. It lives
    /// on the session, not the panel, so reopening Visuals doesn't reset it.
    @Published private(set) var visualAutoRetry = VisualAutoRetryScheduler()
    private var visualPollRunning = false
    /// Clock seam so a test can step the automatic-retry schedule.
    var visualAutoRetryClock: () -> Date = { Date() }
    @Published private(set) var isAddingVisual = false
    @Published var visualError: String?
    @Published private(set) var isAddingClip = false
    /// Seam over `UIApplication.beginBackgroundTask` so a test can drive expiration.
    var backgroundActivity: any BackgroundActivityAssertion = UIKitBackgroundActivityAssertion()
    @Published var addClipError: String?
    /// Default window given to a freshly added timeline clip/photo. The server
    /// has never probed this source, so it can't bound the window itself — see
    /// `resolve_timeline_slots_for_edit`'s "New clips the AI never probed have
    /// no known duration" skip. Beat-gridded (song) variants snap this to the
    /// nearest beat; no-grid variants snap it to the nearest half second — the
    /// same server-side resolution every other slot edit already goes through.
    private static let addedClipDurationS: Double = 3.0
    /// Most clips one edit can hold. Matches the server's add-clip pool cap
    /// (`_MAX_POOL_CLIPS`) and the creation cap (`_MAX_CLIPS_PER_ITEM`), both 50 —
    /// a lower number here made edits created with 21+ clips unable to add more.
    static let maxTimelineClips = 50
    private var authoredVisualSources: [String: ResolvedEditorSource] = [:]
    private var variantKey: String?
    private var jobID: UUID?
    private var previewRefreshTask: Task<Void, Never>?
    private var pendingPreviewGeneration: String?
    private var changedSections: Set<EditorSection> = []
    private var explicitlyDirtySections: Set<EditorSection> = []
    /// Highest chat-draft revision already staged (or deliberately left to the
    /// conflict path) so an undo/discard is not re-applied by the next sync.
    private var appliedChatDraftRevision: Int?
    /// The document exactly as staged from a chat draft, and its lanes. While
    /// the live document still matches it lane-for-lane the only unsaved edits
    /// are the chat's, which the server already holds in its draft head, so
    /// they need no editor-commit before the next chat message.
    private var chatStagedDocument: EditorDocument?
    private var chatStagedSections: Set<EditorSection> = []

    /// Editor documents submitted with a chat turn, keyed by the `client_state_id`
    /// the server echoes on the draft it builds from them. The apply path uses the
    /// submitted document as the three-way merge base. In-memory only; bounded.
    private struct SubmittedEditorState { let document: EditorDocument; let sections: Set<EditorSection> }
    private var submittedEditorStates: [String: SubmittedEditorState] = [:]
    private var submittedEditorStateOrder: [String] = []
    private static let submittedEditorStateLimit = 8

    /// The editor's current UNSAVED state for a chat turn, or nil when it cannot be
    /// sent (not loaded, no baseline generation, or over `maxBytes`) and the caller
    /// must fall back to the legacy save-then-send flow. A clean editor exports
    /// request metadata without changed lanes, meaning "no unsaved edits".
    func exportEditorState(maxBytes: Int? = nil) -> EditorStateRequest? {
        guard loadState == .loaded, !isSaving else { return nil }
        // A text field still being typed is part of the creator's current state.
        _ = finishTextCreation()
        refreshDirtyState()
        let payload = Self.object(document.encodeSnapshot()["editor_payload"])
        let sections = Self.object(payload?["sections"])
        let generation = payload?["base_generation"]?.stringValue.flatMap { $0.isEmpty ? nil : $0 }
            ?? document.revision.baseGeneration.nilIfEmpty ?? cleanDocument.revision.baseGeneration.nilIfEmpty
        guard let generation else { return nil }
        var lanes = commitRequest(sections: sections, baseGeneration: generation)
        // Save-only: the server rejects these on a turn's editor state.
        lanes.guidedRevisionNumber = nil; lanes.guidedRevision = nil
        lanes.acceptedSuggestionIDs = nil; lanes.copilotReceiptIDs = []; lanes.retryGuidedRevision = false
        let request = EditorStateRequest(baseGeneration: generation, clientStateID: UUID().uuidString, lanes: lanes)
        if let maxBytes, let encoded = try? JSONEncoder().encode(request), encoded.count > maxBytes {
            Self.stagingLog.debug("editor_state over cap bytes=\(encoded.count, privacy: .public) max=\(maxBytes, privacy: .public); legacy flush")
            return nil
        }
        submittedEditorStates[request.clientStateID] = SubmittedEditorState(document: document, sections: changedSections)
        submittedEditorStateOrder.append(request.clientStateID)
        while submittedEditorStateOrder.count > Self.submittedEditorStateLimit {
            submittedEditorStates[submittedEditorStateOrder.removeFirst()] = nil
        }
        return request
    }

    /// Lane equality without comparing whole documents (revision metadata differs).
    private func lanesEqual(_ section: EditorSection, _ a: EditorDocument, _ b: EditorDocument) -> Bool {
        var probe = a
        copy(section, from: b, into: &probe)
        return probe == a
    }

    /// Three-way per-lane merge of a chat draft built on `submitted`. Returns nil
    /// when some lane changed on BOTH sides (caller raises `.conflict`).
    private func mergeChatDraft(_ staged: (document: EditorDocument, sections: Set<EditorSection>), over submitted: EditorDocument) -> (document: EditorDocument, taken: Set<EditorSection>)? {
        var merged = document
        var taken: Set<EditorSection> = []
        for section in staged.sections {
            let localUntouched = lanesEqual(section, document, submitted)
            let serverUntouched = lanesEqual(section, staged.document, submitted)
            if localUntouched {
                if !serverUntouched { copy(section, from: staged.document, into: &merged); taken.insert(section) }
            } else if !serverUntouched, !lanesEqual(section, document, staged.document) {
                return nil
            }
        }
        merged.revision.number = staged.document.revision.number ?? merged.revision.number
        return (merged, taken)
    }

    var hasOnlyChatStagedChanges: Bool {
        guard hasUnsavedChanges, let staged = chatStagedDocument, !changedSections.isEmpty,
              changedSections.isSubset(of: chatStagedSections), pendingText == nil else { return false }
        for section in changedSections {
            var probe = document
            copy(section, from: staged, into: &probe)
            if probe != document { return false }
        }
        return true
    }
    private var pendingRenderRetrySections: Set<EditorSection> = []
    private var clipIDsBySlot: [String: UUID] = [:]
    private var compatibilityClipMetadata: [UUID: (sourceClipIndex: Int?, slotID: String?)] = [:]
    private var compatibilitySnapshot: [String: JSONValue] = [:]
    /// Whether the player `installPlayer(url:)` seeded from `initialPlaybackURL`
    /// at construction can be trusted before `load()` confirms it. See
    /// `canDisplayCurrentPlayer`'s `.idle` case.
    private let seedIsTrusted: Bool
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
    private var playbackStateObserver: NSKeyValueObservation?
    private var itemStatusObserver: NSKeyValueObservation?
    nonisolated(unsafe) private var itemFailureObserver: NSObjectProtocol?
    /// The item whose failure was already reported, so status + failed-to-end (which can both fire for one
    /// failure) produce one diagnostic and one recovery.
    private weak var failureHandledItem: AVPlayerItem?
    /// A play tap that arrived while the editable preview was still building. Played as soon as a
    /// displayable player exists (the ready preview, or the finished-render fallback).
    private var pendingPlayRequest = false
    private var finishedRenderRefreshAttempted = false
    private var finishedRenderRecoveryTask: Task<Void, Never>?
    private var durationLoadTask: Task<Void, Never>?
    private var promptRefreshSequence: UInt64 = 0
    private let playbackEndTolerance: TimeInterval = 0.05

    var videoDownloadTarget: NativeEditorVideoDownloadTarget? {
        guard let jobID, let variantKey else { return nil }
        let generation = document.revision.baseGeneration.isEmpty ? nil : document.revision.baseGeneration
        return NativeEditorVideoDownloadTarget(
            jobID: jobID,
            variantID: variantKey,
            expectedGenerationID: generation,
            expectedOutputPath: finishedRenderURL?.path
        )
    }

    var canDownloadCurrentVideo: Bool {
        guard document.editorState != "empty" else { return false }
        let displayedVideoIsCurrent: Bool
        switch sourcePreviewState {
        case .ready:
            // An undo back to the saved document clears `hasUnsavedChanges`
            // before the rebuild lands; never export the composition it replaces.
            displayedVideoIsCurrent = sourcePreviewSettled && (sourcePreview.map { player?.currentItem === $0.preview.playerItem } ?? false)
        case .failed, .originalsUnavailable:
            displayedVideoIsCurrent = player != nil && player === finishedRenderPlayer
        case .idle, .preparing:
            displayedVideoIsCurrent = false
        }
        return videoDownloadTarget != nil && displayedVideoIsCurrent
            && !hasUnsavedChanges && !isSaving && pendingPreviewGeneration == nil
    }

    /// Why export is unavailable, in the user's own words — the same
    /// conditions ``canDownloadCurrentVideo`` checks, explained. `nil` exactly
    /// when export is available.
    var exportBlockReason: String? {
        guard !canDownloadCurrentVideo else { return nil }
        if document.editorState == "empty" { return "Add a clip before exporting." }
        if isSaving { return "Saving your changes…" }
        if hasUnsavedChanges { return "Save your changes to export the current video." }
        if pendingPreviewGeneration != nil || (sourcePreviewState == .ready && !sourcePreviewSettled) {
            return "Kria is updating the preview. Try again in a moment."
        }
        switch sourcePreviewState {
        case .idle, .preparing:
            return "Preparing the preview…"
        case .failed, .originalsUnavailable, .ready:
            // The source preview is ready or has fallen back to the rendered
            // video, but the player hasn't caught up yet (a brief window
            // during a rebuild) or the session has no job to export from.
            return "This video isn’t ready to export yet."
        }
    }

    func videoDownloadRoute(deviceLocalFile: URL?) throws -> NativeEditorVideoDownloadRoute {
        switch sourcePreviewState {
        case .ready:
            guard sourcePreviewSettled, let sourcePreview,
                  player?.currentItem === sourcePreview.preview.playerItem else {
                throw NativeEditorVideoDownloadError.unavailable
            }
            return .sourcePreview
        case .failed, .originalsUnavailable:
            break
        case .idle, .preparing:
            throw NativeEditorVideoDownloadError.unavailable
        }
        if let deviceLocalFile,
           let asset = player?.currentItem?.asset as? AVURLAsset,
           asset.url.standardizedFileURL == deviceLocalFile.standardizedFileURL {
            return .localFile(deviceLocalFile)
        }
        if player === finishedRenderPlayer, let target = videoDownloadTarget {
            return .server(target)
        }
        throw NativeEditorVideoDownloadError.unavailable
    }

    /// Exports the exact composition on screen. On device this competes with
    /// the live player for a hardware decode session (iOS caps concurrent
    /// VideoToolbox sessions per app; the Simulator does not, which is why
    /// this path passes there and fails on a phone). Releasing the player's
    /// session first is the single highest-value fix; a lone retry after that
    /// catches the remaining transient contention without masking a real
    /// failure — unsupported source, storage, cancellation — which still
    /// throws through to the caller rather than falling back to the last
    /// cloud render. The user must always get what the preview shows.
    func exportDisplayedSourcePreview() async throws -> NativeEditorTemporaryVideo {
        guard sourcePreviewState == .ready, sourcePreviewSettled,
              let sourcePreview,
              player?.currentItem === sourcePreview.preview.playerItem else {
            throw NativeEditorVideoDownloadError.unavailable
        }
        let snapshot = sourcePreview.exportSnapshot()
        let wasPlaying = isPlaying
        pausePlayback()
        defer { if wasPlaying { togglePlayback() } }
        do {
            return try await performLocalExport(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs)
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-export-first-attempt", error: error)
            #endif
            return try await performLocalExport(recipe: snapshot.recipe, assetURLs: snapshot.assetURLs)
        }
    }

    private func performLocalExport(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL]) async throws -> NativeEditorTemporaryVideo {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "kria-editor-download-\(UUID().uuidString)", directoryHint: .isDirectory)
        let output = directory.appending(path: "current-preview.mp4")
        do {
            let checkpoint = try await AVFoundationLocalExporter(
                stateStore: FileExportStateStore(directory: directory.appending(path: "state", directoryHint: .isDirectory))
            ).export(recipe: recipe, assetURLs: assetURLs, outputURL: output)
            guard checkpoint.status == .completed, checkpoint.outputURL == output else {
                throw NativeEditorVideoDownloadError.unavailable
            }
            return NativeEditorTemporaryVideo(fileURL: output, cleanupURL: directory)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            throw error
        }
    }

    private struct ActiveTrim {
        let clipID: UUID
        let edge: NativeTrimEdge
        var baseline: EditorDocument
        var redoBaseline: [EditorDocument]
        var translationOrigin: TimeInterval = 0
        var lastTranslation: TimeInterval = 0
        var recordedUndo = false
    }

    private enum TimedEditKind { case move, trim }
    private struct ActiveTimedEdit {
        let selection: EditorSelection
        let edge: NativeTrimEdge?
        let kind: TimedEditKind
        var baseline: EditorDocument
        var redoBaseline: [EditorDocument]
        var projection: NativeEditorTimelineProjection
        var translationOrigin: TimeInterval = 0
        var lastTranslation: TimeInterval = 0
        var recordedUndo = false
    }

    init(
        draft: EditorDraft = EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0),
        operations: any EditorOperations = LocalEditorOperations(),
        initialPlaybackURL: URL? = nil,
        trustsInitialPlaybackURL: Bool = true
    ) {
        var initialDocument = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(draft))
        initialDocument.revision.number = draft.revision
        self.projectID = draft.projectID
        self.etag = draft.etag
        self.compatibilitySnapshot = draft.serverSnapshot
        self.document = initialDocument
        self.cleanDocument = initialDocument
        self.operations = operations
        self.seedIsTrusted = trustsInitialPlaybackURL
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
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-legacy-visuals") {
            itemID = "fixture-visual-item"
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-delayed-source") { loadState = .loading }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-song-reference") {
            previewVariant = NativeEditorUITestFixtures.songReferenceVariant
        }
        // KRI-167: draft-based fixtures never go through `configureCapabilities(from:)`
        // (that only runs off a network-fetched `variant`), so `rendersOnDevice`
        // is otherwise unreachable as `true` in a UI test -- and the Visuals
        // tab's device-only Media restriction is exactly the thing this
        // ticket needs regression coverage against on the real (device)
        // rendering path, not just the untested cloud one.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-device") {
            rendersOnDevice = true
        }
        // KRI-211: a project whose originals live on another device. The recovery sheet derives its
        // targets from the source pool the preview tried to resolve, so the fixture supplies one.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-missing-originals") {
            let indices = Set(timelineClips.compactMap(\.sourceClipIndex)).union([0]).sorted()
            originalsRecoveryPool = NativeEditorSourcePool(clips: indices.map { index in
                .init(clipIndex: index, nativeSource: .init(
                    mediaID: "fixture-source-\(index)", sourceURL: nil,
                    original: OriginalMediaDescriptor(sha256: String(repeating: "ab", count: 32), byteCount: 4096,
                        durationS: 3, width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true),
                    localRequired: true))
            }, baseGeneration: "fixture", nativeAssets: [])
        }
        // KRI-167: no existing shape fixture closes `clips.transitions`, and
        // UI tests can't construct an EditorDocument directly -- they only
        // get a process launch arg.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-transitions-closed") {
            document.capabilities["clips.transitions"] = EditorCapability(editable: false, reason: "transitions_disabled")
        }
        #endif
    }

    convenience init(
        project: ProjectSummary,
        operations: any EditorOperations = LocalEditorOperations(),
        initialPlaybackURL: URL? = nil
    ) {
        self.init(
            draft: EditorDraft(projectID: project.id, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0),
            operations: operations,
            initialPlaybackURL: initialPlaybackURL ?? project.outputURL,
            // An explicit URL just came from a caller that fetched it for
            // this exact screen (e.g. ResultsView's own playback refresh).
            // Falling back to project.outputURL instead means trusting
            // whatever a cached ProjectSummary happened to hold — the shared
            // chat-editor session's path, and the source of KRI-91's stale
            // "old video, then the correct one ~10s later" report.
            trustsInitialPlaybackURL: initialPlaybackURL != nil
        )
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
        sourcePreviewTask?.cancel()
        durationLoadTask?.cancel()
        seekRecoveryTask?.cancel()
        scrubFrameTask?.cancel()
        observingPlayer?.pause()
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        if let itemFailureObserver { NotificationCenter.default.removeObserver(itemFailureObserver) }
        finishedRenderRecoveryTask?.cancel()
    }

    var canUndo: Bool { !undoStack.isEmpty }
    var undoHistoryCount: Int { undoStack.count }
    var isTimingGestureActive: Bool { activeTimedEdit != nil || activeTrim != nil }
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
    var previewAspectRatio: CGFloat {
        switch document.orientation {
        case "landscape": 16.0 / 9.0
        case "square": 1
        default: 9.0 / 16.0
        }
    }

    func beginEditTransaction() { beginTransaction() }
    func endEditTransaction() { endTransaction() }
    func beginDirectManipulation() {
        if isPlaying {
            player?.pause()
            isPlaying = false
            if let time = player?.currentTime().seconds, time.isFinite { currentTime = time }
            prepareInteractionLayers()
        }
        isDirectManipulating = true
        beginTransaction()
    }
    func endDirectManipulation() {
        endTransaction()
        isDirectManipulating = false
        flushDeferredSourcePreviewUpdate()
    }

    /// Selects any timeline/preview object. Selection seeks without changing
    /// playback state; callers can opt out for compatibility with old clip
    /// buttons that only changed the inspector.
    func select(_ value: EditorSelection?, seekToStart: Bool = true) {
        selection = value
        selectionRequest &+= 1
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
        // A mirrored caption is its cue's sentence again (`isCaptionCueMirror`).
        // Listing both stacked every caption into a second CAPTIONS row and
        // left an unrendered tap target over the burned caption.
        items += document.textElements.enumerated().filter { !document.isCaptionCueMirror($0.element) }.map { index, item in projected(EditorSelection(kind: .text, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 300 + index), sourceIndex: index) }
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
            // The output window may be extended to carry narration through its
            // tail. Source trimming remains based on the authored moving clip,
            // otherwise that hold would ask the decoder for frames beyond the
            // source asset.
            let authoredWindow = timelineProjection.baseClipWindows.first { $0.sourceIndex == window.sourceIndex }
            let movingDuration = max(minimumClipDuration, slot.durationS ?? authoredWindow.map { $0.end - $0.start } ?? window.end - window.start)
            let sourceStart = max(0, slot.inS)
            let sourceDuration = Self.number(slot.raw["source_duration_s"] ?? slot.raw["source_duration"])
            let available = sourceDuration.map { max(0, $0 - sourceStart) }
            let sourceSpan = min(
                Self.number(slot.raw["native_source_span_s"]) ?? movingDuration,
                available ?? .greatestFiniteMagnitude
            )
            let assetID = slot.raw["asset_id"]?.stringValue.flatMap(UUID.init(uuidString:)) ?? id
            return EditorClip(
                id: id,
                assetID: assetID,
                sourceClipIndex: slot.clipIndex,
                start: window.start,
                end: window.end,
                trimIn: sourceStart,
                trimOut: sourceStart + sourceSpan,
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
        conversationRuntimeVersion = 2
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
            let loadedDraft = snapshot.editorDraft(projectID: threadID, authoritativeVariant: authoritativeVariant)
            draft = loadedDraft
            configureCapabilities(from: authoritativeVariant)
            cleanDocument = document; undoStack.removeAll(); redoStack.removeAll(); changedSections.removeAll(); explicitlyDirtySections.removeAll(); pendingRenderRetrySections.removeAll(); hasUnsavedChanges = false; saveState = .idle
            appliedChatDraftRevision = nil; chatStagedDocument = nil; chatStagedSections = []
            itemID = snapshot.itemID; variantKey = requestedVariantKey
            durationSourcesInvalidated = false
            setAuthoritativeDuration(Self.number(authoritativeVariant?["duration_s"]))
            if conversationRuntimeVersion == 2,
               let staged = stagedChatDocument(snapshot: snapshot, draft: loadedDraft, variant: authoritativeVariant) {
                applyStagedChatDocument(staged, revision: snapshot.draftRevision)
            }
            refreshDuration()
            if let output = authoritativeVariant?["output_url"]?.stringValue, let url = URL(string: output) {
                // See installPlayer's isCurrent doc comment: render_status
                // other than "ready" means this output_url predates the
                // document just loaded above.
                installPlayer(url: url, preferredDuration: authoritativeDuration,
                    isCurrent: authoritativeVariant?["render_status"]?.stringValue == "ready")
            } else if allowPlaybackFallback, let jobID, let url = try? await api.playbackURL(jobID: jobID) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            }
            loadState = .loaded
            await prepareSourcePreview()
            await resumePendingEditorPlacements()
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-load-failed", error: error)
            NativePreviewDiagnostics.record("editor-load-context", fields: ["job": jobID?.uuidString ?? "none"])
            #endif
            saveState = .loadFailed(error.localizedDescription)
            loadState = .failed(error.localizedDescription)
        }
    }

    /// True when the rendered variant is the generation this editor's clean
    /// baseline was built from. Documents cannot be compared directly: each
    /// fetch mints fresh placeholder asset ids for slots that lack one.
    private func variantUnchanged(_ variant: [String: JSONValue]?) -> Bool {
        guard let generation = variant?["render_generation_id"]?.stringValue, !generation.isEmpty else { return false }
        return generation == cleanDocument.revision.baseGeneration
    }

    /// The chat draft's staged lanes as a document, or nil when the draft is
    /// stale/absent/already applied (see `DraftSnapshot.stagedChatEdit`).
    private func stagedChatDocument(snapshot: DraftSnapshot, draft: EditorDraft, variant: [String: JSONValue]?) -> (document: EditorDocument, sections: Set<EditorSection>)? {
        let edit = snapshot.stagedChatEdit(over: Self.snapshotPreservingClipMetadata(draft), variant: variant, appliedRevision: appliedChatDraftRevision)
        Self.stagingLog.debug("draft received rev=\(snapshot.draftRevision, privacy: .public) applied=\(self.appliedChatDraftRevision ?? -1, privacy: .public) gen=\(variant?["render_generation_id"]?.stringValue ?? "-", privacy: .public) staged=\(edit.map { $0.sections.map(\.rawValue).sorted().joined(separator: ",") } ?? "skipped", privacy: .public)")
        guard let edit else { return nil }
        var value = EditorDocument(snapshot: edit.snapshot)
        value.revision.number = draft.revision
        return (value, edit.sections)
    }

    /// Show a staged chat edit as unsaved changes on top of the clean document.
    /// The clean baseline stays on the undo stack, so one Undo discards it.
    private func applyStagedChatDocument(_ staged: (document: EditorDocument, sections: Set<EditorSection>), revision: Int) {
        appendUndo(document); redoStack.removeAll()
        document = staged.document
        changedSections.formUnion(staged.sections)
        appliedChatDraftRevision = revision
        refreshDirtyState()
        chatStagedDocument = staged.document
        chatStagedSections = staged.sections
        Self.stagingLog.debug("staged applied rev=\(revision, privacy: .public) dirty=\(self.changedSections.map(\.rawValue).sorted().joined(separator: ","), privacy: .public); preview rebuild scheduled")
        // Same path a local edit takes: make sure the live preview recompiles now.
        scheduleSourcePreviewUpdate()
    }

    /// Reconcile the same project after an agent turn. A response that arrives
    /// after a manual edit cannot replace it; conflict resolution remains explicit.
    private static let stagingLog = Logger(subsystem: "com.kria.app", category: "chat-staging")
    private var syncNeedsRetry = false

    func synchronizePromptRevision() async {
        // A concurrent save/hydration can change the clean baseline while this
        // suspends; a dropped sync would otherwise leave a chat edit invisible
        // until the editor is reloaded (nothing else re-polls without new events).
        for attempt in 0..<3 {
            syncNeedsRetry = false
            await runPromptRevisionSync()
            guard syncNeedsRetry, !Task.isCancelled else { return }
            Self.stagingLog.debug("sync retry attempt=\(attempt + 1, privacy: .public)")
        }
    }

    private func runPromptRevisionSync() async {
        guard loadState == .loaded, !isSaving, let api, let threadID else { return }
        promptRefreshSequence &+= 1
        let sequence = promptRefreshSequence
        let baseline = cleanDocument
        do {
            let snapshot: DraftSnapshot
            let variant: [String: JSONValue]?
            if conversationRuntimeVersion == 1 {
                // Legacy conversations have no runtime-v2 draft endpoint.
                // Read the same rendered authority used when opening this editor.
                let latest = try? await api.project(threadID: threadID)
                let targetJob: UUID
                let targetVariant: String
                let targetItem: String
                if let latest {
                    // Playback summaries may show another ready cut while the
                    // selected edit renders. Mutation authority stays selected.
                    guard let activeJob = latest.activeJobID.flatMap(UUID.init(uuidString:)),
                          let activeItem = latest.activePlanItemID else { throw APIError.invalidResponse }
                    let variants = latest.job?.variants ?? []
                    let selected = latest.state?["selected_variant_id"]?.stringValue
                        ?? (variants.count == 1 ? variants.first?.variantID : nil)
                    guard let selected, variants.contains(where: { $0.variantID == selected }) else {
                        throw APIError.invalidResponse
                    }
                    targetJob = activeJob; targetVariant = selected; targetItem = activeItem
                } else {
                    // An unavailable projection can only reuse the complete
                    // currently loaded target; never mix old and new IDs.
                    guard let jobID, let variantKey, let itemID else { return }
                    targetJob = jobID; targetVariant = variantKey; targetItem = itemID
                }
                let current = try await api.editorVariant(jobID: targetJob, variantID: targetVariant)
                variant = current
                snapshot = DraftSnapshot(
                    draftID: "job-\(targetJob.uuidString)", itemID: targetItem, variantKey: targetVariant,
                    draftRevision: 0, snapshotHash: "", etag: "", baseJobID: targetJob.uuidString,
                    baseGenerationID: current["render_generation_id"]?.stringValue
                        ?? current["render_finished_at"]?.stringValue ?? "",
                    snapshot: [:], canUndo: false, createdAt: .now)
            } else {
                snapshot = try await api.draft(threadID: threadID)
                if let targetJob = snapshot.baseJobID.flatMap(UUID.init) {
                    variant = try await api.editorVariant(jobID: targetJob, variantID: snapshot.variantKey)
                } else { variant = nil }
            }
            let nextJobID = snapshot.baseJobID.flatMap(UUID.init)
            let nextVariantKey = snapshot.variantKey
            let nextDraft = snapshot.editorDraft(projectID: threadID, authoritativeVariant: variant)
            let sameTarget = nextJobID == jobID && nextVariantKey == variantKey && snapshot.itemID == itemID
            // A different job needs its video reloaded: adoptLatestJob owns that.
            if conversationRuntimeVersion == 2, nextJobID != jobID {
                await adoptActiveJobFromThread(api: api, threadID: threadID)
                return
            }
            if conversationRuntimeVersion == 1, sameTarget, nextDraft.serverSnapshot == legacyPromptSnapshot,
               sequence == promptRefreshSequence, cleanDocument == baseline, !Task.isCancelled {
                if let previous = saveStateBeforePromptFailure { saveState = previous }
                return
            }
            let nextDocument = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(nextDraft))
            // A runtime-v2 chat turn parks its editor edit in the draft head
            // (never rendered); show it as unsaved edits instead of ignoring it.
            let staged = conversationRuntimeVersion == 2 && sameTarget
                ? stagedChatDocument(snapshot: snapshot, draft: nextDraft, variant: variant) : nil
            guard sequence == promptRefreshSequence, !Task.isCancelled else { return }
            // A save/load completed while this request was suspended. Its newer
            // authority wins, even when the local document is now clean.
            guard cleanDocument == baseline else { syncNeedsRetry = true; return }
            if let previous = saveStateBeforePromptFailure { saveState = previous }
            guard !sameTarget || nextDocument != cleanDocument || staged != nil else { return }
            // Only the previous chat draft is unsaved: the newer cumulative
            // head replaces it wholesale instead of conflicting with it.
            // Editor-state turn: the draft says which submitted state it was built on,
            // so merge three-way against THAT instead of guessing from the head.
            if let staged, sameTarget, variantUnchanged(variant), pendingText == nil, !isSaving,
               let stateID = snapshot.snapshot["client_state_id"]?.stringValue,
               let submitted = submittedEditorStates[stateID] {
                guard let result = mergeChatDraft(staged, over: submitted.document) else {
                    Self.stagingLog.debug("sync conflict: same lane edited locally and by chat rev=\(snapshot.draftRevision, privacy: .public)")
                    saveState = .conflict
                    return
                }
                // One Undo restores the creator's own pre-chat UNSAVED state. The clean
                // baseline (the rendered variant) is deliberately left untouched.
                appendUndo(document); redoStack.removeAll()
                document = result.document
                changedSections.formUnion(staged.sections)
                appliedChatDraftRevision = snapshot.draftRevision
                chatStagedDocument = nil; chatStagedSections = []
                refreshDirtyState(); refreshDuration()
                scheduleSourcePreviewUpdate()
                return
            }
            let onlyChatStaged = hasOnlyChatStagedChanges
            if onlyChatStaged, staged == nil, sameTarget, variantUnchanged(variant) { return }
            guard !hasUnsavedChanges || onlyChatStaged, pendingText == nil, !isSaving else {
                // Unsaved local edits. When the rendered variant is unchanged
                // and the chat touched only lanes the creator has not, both
                // sides can be kept lane-by-lane; otherwise stay explicit.
                if let staged, variantUnchanged(variant), pendingText == nil, !isSaving,
                   staged.sections.isDisjoint(with: changedSections.union(explicitlyDirtySections)) {
                    appendUndo(document); redoStack.removeAll()
                    var merged = document
                    for section in staged.sections { copy(section, from: staged.document, into: &merged) }
                    document = merged
                    changedSections.formUnion(staged.sections)
                    appliedChatDraftRevision = snapshot.draftRevision
                    chatStagedDocument = nil
                    refreshDirtyState(); refreshDuration()
                    return
                }
                Self.stagingLog.debug("sync conflict: local edits overlap chat draft rev=\(snapshot.draftRevision, privacy: .public)")
                saveState = .conflict
                return
            }
            let selected = selection
            let time = currentTime
            draft = nextDraft
            if !sameTarget { appliedChatDraftRevision = nil }
            if conversationRuntimeVersion == 1 { legacyPromptSnapshot = nextDraft.serverSnapshot }
            cleanDocument = document
            jobID = nextJobID; variantKey = nextVariantKey; itemID = snapshot.itemID
            configureCapabilities(from: variant)
            undoStack.removeAll(); redoStack.removeAll()
            changedSections.removeAll(); explicitlyDirtySections.removeAll()
            durationSourcesInvalidated = false
            setAuthoritativeDuration(Self.number(variant?["duration_s"]))
            if let staged { applyStagedChatDocument(staged, revision: snapshot.draftRevision) }
            refreshDuration()
            if let selected, selectionExists(selected) { select(selected, seekToStart: false) }
            else { select(nil) }
            currentTime = min(time, duration)
            await prepareSourcePreview()
        } catch {
            guard sequence == promptRefreshSequence, cleanDocument == baseline, !Task.isCancelled else { return }
            // The loaded job was superseded by a re-plan render (its editor
            // routes now answer "content plan unavailable"/409): follow the
            // thread to its current job instead of reporting a refresh failure.
            if conversationRuntimeVersion == 2,
               let apiError = error as? APIError, apiError == .contentPlanUnavailable || apiError == .conflict {
                let before = jobID
                await adoptActiveJobFromThread(api: api, threadID: threadID)
                if jobID != before || newerJobPrompt != nil { return }
            }
            // A refresh error temporarily owns the banner. Any intervening
            // save/render status assignment relinquishes that ownership.
            let previous = saveStateBeforePromptFailure ?? saveState
            saveState = .refreshFailed("Your conversation changed, but the editor couldn’t refresh. Your local edit is still here. \(error.localizedDescription)")
            saveStateBeforePromptFailure = previous
        }
    }

    /// A shared session (`ChatWorkspaceView`'s editor tab) can be reused
    /// across multiple editor opens within one chat visit. `loadState ==
    /// .loaded` alone doesn't mean the displayed video is still current — the
    /// workspace's own polling can observe a newer server revision (e.g. a
    /// chat-driven edit finished rendering) while the editor was closed.
    /// Without this, that second open would show the stale video
    /// indefinitely rather than swapping once, ~10s later, like a first open.
    private var loadedServerRevision: Int?

    func needsReload(for project: ProjectSummary) -> Bool {
        guard loadState == .loaded else { return true }
        // A re-plan can render a NEW job for the same thread; the loaded
        // session then still shows the old job's video.
        if let target = project.activeJobID, let jobID, target != jobID { return true }
        guard let loadedServerRevision else { return false }
        return loadedServerRevision != project.serverRevision
    }

    func load(project: ProjectSummary, api: any KriaAPIClient) async {
        loadState = .loading
        var resolvedProject = project
        if project.activeJobID != nil,
           (project.activePlanItemID == nil || project.outputVariantID == nil),
           let refreshed = try? await api.project(threadID: project.id) {
            resolvedProject = refreshed.summary
        }
        loadedServerRevision = resolvedProject.serverRevision
        conversationRuntimeVersion = resolvedProject.runtimeVersion
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

    /// A newer render job than the one loaded is waiting on a manual-edit decision.
    @Published private(set) var newerJobPrompt: ProjectSummary?

    func isBehindActiveJob(_ project: ProjectSummary) -> Bool {
        guard loadState == .loaded, let target = project.activeJobID, let jobID else { return false }
        return target != jobID
    }

    /// The thread's active job changed (a re-plan rendered a new video). Edits
    /// staged from chat belong to the old job and are obsolete, so they are
    /// dropped and the new job loads automatically. The creator's own unsaved
    /// edits are never discarded silently: they raise `newerJobPrompt` instead.
    func adoptLatestJob(_ project: ProjectSummary, api: any KriaAPIClient) async {
        guard isBehindActiveJob(project), !isSaving else { return }
        if hasUnsavedChanges, !hasOnlyChatStagedChanges {
            newerJobPrompt = project
            return
        }
        await switchToLatestJob(project, api: api)
    }

    /// Switch now; discards any unsaved edits (caller has decided).
    func switchToLatestJob(_ project: ProjectSummary, api: any KriaAPIClient) async {
        newerJobPrompt = nil
        await load(project: project, api: api)
        await refreshDeviceRender()
    }

    /// Ask the server which job the thread points at now, then run the adopt decision.
    func adoptActiveJobFromThread(api: any KriaAPIClient, threadID: UUID) async {
        guard let thread = try? await api.project(threadID: threadID) else { return }
        await adoptLatestJob(thread.summary, api: api)
    }

    func keepEditingCurrentJob() { newerJobPrompt = nil }

    /// The thread now points at a different render job: the old job's player,
    /// source composition and device-render identity must not keep showing.
    private func discardPlaybackForNewJob() {
        sourcePreviewTask?.cancel()
        previewRefreshTask?.cancel(); pendingPreviewGeneration = nil
        promptRefreshSequence &+= 1
        sourcePreviewSequence += 1
        player?.pause()
        player = nil
        finishedRenderURL = nil; finishedRenderPlayer = nil; finishedRenderIsCurrent = true
        sourcePreview = nil; sourceCompiler = nil; sourcePool = nil; resolvedSources = nil
        sourcePreviewState = .idle; sourcePreviewGeneration = nil
        previewVariant = [:]
        pendingDeviceRenderIdentity = nil
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
        if let previous = jobID, previous != editorJobID { discardPlaybackForNewJob() }
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
            self.threadID = threadID ?? receipt?.creationThreadID
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
                resolved = (variantID, try await api.editorVariant(jobID: editorJobID, variantID: variantID))
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
            let loadedDraft = snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant)
            draft = loadedDraft
            legacyPromptSnapshot = loadedDraft.serverSnapshot
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
                // render_status other than "ready" means this output_url is
                // a render that predates the document just loaded above (a
                // save queued a re-render still in flight) — see
                // installPlayer's isCurrent doc comment.
                installPlayer(url: url, preferredDuration: authoritativeDuration,
                    isCurrent: variant["render_status"]?.stringValue == "ready")
            } else if let url = try? await api.playbackURL(jobID: editorJobID) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            }
            // A chat turn may already have drafted edits for this render:
            // stage them as unsaved. Never let this block opening the editor.
            appliedChatDraftRevision = nil; chatStagedDocument = nil; chatStagedSections = []
            if conversationRuntimeVersion == 2, let threadID, let head = try? await api.draft(threadID: threadID),
               head.itemID == resolvedPlanItemID, head.variantKey == resolved.variantID,
               head.baseJobID.flatMap(UUID.init) == editorJobID,
               let staged = stagedChatDocument(snapshot: head, draft: loadedDraft, variant: variant) {
                applyStagedChatDocument(staged, revision: head.draftRevision)
                refreshDuration()
            }
            loadState = .loaded
            await prepareSourcePreview()
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-load-failed", error: error)
            NativePreviewDiagnostics.record("editor-load-context", fields: ["job": jobID?.uuidString ?? "none"])
            #endif
            saveState = .loadFailed(error.localizedDescription)
            loadState = .failed(error.localizedDescription)
        }
    }

    #if DEBUG
    /// Account-free verification still uses the production compiler and compositor.
    func prepareFixtureSourcePreview(url: URL, delayedLoad: Bool = false, mediaSources: [String: ResolvedEditorSource] = [:], forceFailure: Bool = false,
                                      failure: Error = NativeEditorRenderError.missingVideoTrack) async {
        sourcePreviewSequence += 1
        let sequence = sourcePreviewSequence
        sourcePreviewState = .preparing
        do {
            if forceFailure { throw failure }
            if delayedLoad {
                loadState = .loaded
                try await Task.sleep(for: .milliseconds(600))
            }
            guard let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) else {
                throw NativeEditorRenderError.missingFont("bundled fonts")
            }
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
            sourceCompiler = try NativeEditorRenderCompiler(fontDirectory: fonts)
            authoredVisualSources.merge(mediaSources) { _, source in source }
            resolvedSources = try Dictionary(uniqueKeysWithValues: Set(timelineClips.compactMap(\.sourceClipIndex)).map { index in
                var sourceURL = url
                var sourceFingerprint = fingerprint
                if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-color-cuts") {
                    sourceURL = FileManager.default.temporaryDirectory.appendingPathComponent("scrub-color-\(index).png")
                    let image = UIGraphicsImageRenderer(size: CGSize(width: 96, height: 160)).image { context in
                        (index == 0 ? UIColor.red : UIColor.blue).setFill()
                        context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
                    }
                    try image.pngData()!.write(to: sourceURL)
                    sourceFingerprint = try SHA256Fingerprinter().fingerprint(file: sourceURL)
                }
                return (index, ResolvedEditorSource(clipIndex: index, mediaID: "fixture-source-\(index)",
                    asset: MediaAsset(id: "fixture-\(index)", relativePath: sourceURL.lastPathComponent, fingerprint: sourceFingerprint), url: sourceURL))
            })
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-caption-visuals") {
                let source = ResolvedEditorSource(clipIndex: -1, mediaID: "fixture", asset: MediaAsset(id: "fixture", relativePath: url.lastPathComponent, fingerprint: fingerprint, duration: 4), url: url)
                authoredVisualSources["visual:paper-media:paper-media"] = source
            }
            await rebuildSourcePreview(sequence: sequence)
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("preview-failure", error: error)
            #endif
            failSourcePreview(error)
        }
    }
    #endif

    /// An original this edit needs that isn't on this iPhone (or no longer matches the approved file).
    struct OriginalRelinkTarget: Identifiable, Equatable, Sendable {
        let mediaID: String
        let descriptor: OriginalMediaDescriptor
        let title: String
        var id: String { mediaID }
    }

    /// The local-required sources the preview needs whose bound file is absent or doesn't match the
    /// approved descriptor — exactly what `SourceAssetStore.resolve` fails on (KRI-211). Empty when the
    /// preview never got as far as asking for a pool.
    func originalsNeedingRelink() async -> [OriginalRelinkTarget] {
        guard let pool = originalsRecoveryPool else { return [] }
        let required = Set(timelineClips.compactMap(\.sourceClipIndex))
        var seen = Set<String>()
        var wanted: [(mediaID: String, descriptor: OriginalMediaDescriptor)] = []
        for clip in pool.clips where required.isEmpty || required.contains(clip.clipIndex) {
            guard let source = clip.nativeSource, source.localRequired, let original = source.original,
                  seen.insert(source.mediaID).inserted else { continue }
            wanted.append((source.mediaID, original))
        }
        let store = SourceAssetStore(project: BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID))
        return await Task.detached {
            var missing: [OriginalRelinkTarget] = []
            for (mediaID, descriptor) in wanted {
                let binding = try? store.bindings().first { $0.mediaID == mediaID }
                let matches = binding?.original.fingerprint?.hex == descriptor.sha256
                    && binding?.original.fingerprint?.byteCount == descriptor.byteCount
                if !matches || (try? store.resolve(mediaIDs: [mediaID])) == nil {
                    missing.append(OriginalRelinkTarget(mediaID: mediaID, descriptor: descriptor, title: "Original \(missing.count + 1)"))
                }
            }
            return missing
        }.value
    }

    /// Verifies a creator-chosen file is the exact approved original, then binds it to this project.
    /// Throws when it isn't; the caller says so and leaves the target listed.
    func relinkOriginal(_ target: OriginalRelinkTarget, from file: URL) async throws {
        let project = BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID)
        let original = try await AssetImportCoordinator(project: project).importAsset(from: file)
        let imported = project.root.appendingPathComponent(original.relativePath)
        let reference = RenderAssetReference(
            id: target.mediaID,
            fingerprint: RenderFingerprint(sha256: target.descriptor.sha256, byteCount: target.descriptor.byteCount),
            source: .original(mediaID: target.mediaID))
        do {
            try await Task.detached { try SourceAssetStore(project: project).relink(reference, original: original) }.value
        } catch {
            try? FileManager.default.removeItem(at: imported)
            throw error
        }
    }

    /// KRI-211: plain words for "the originals live somewhere else", shared by the preview and
    /// the device-render panel so both explain the same thing the same way.
    static let originalsUnavailableMessage = "The original clips for this edit are on another device. Find the files to edit here."

    static func isMissingOriginals(_ error: Error) -> Bool {
        switch error {
        case SourceAssetError.missingOriginal, SourceAssetError.changedOriginal: true
        default: false
        }
    }

    /// Rebuilding the same document hits the same error, so "Try again" is hidden.
    static func isPermanentSourcePreviewFailure(_ error: Error) -> Bool {
        switch error {
        case NativeEditorRenderError.missingVideoTrack, NativeEditorRenderError.unsupportedLane,
             NativeEditorRenderError.missingFont:
            return true
        default:
            return false
        }
    }

    static func sourcePreviewMessage(for error: Error) -> String {
        switch error {
        case APIError.conflict:
            return "A newer version of this video exists. Close and reopen the editor to see it."
        case APIError.sessionExpired:
            return "Sign in again to load the source videos."
        case NativeEditorRenderError.unsupportedLane:
            return "This edit contains an effect that iPhone preview does not support yet."
        case NativeEditorRenderError.missingFont:
            return "A required font is missing from this app build. Update the app and try again."
        case NativeEditorRenderError.missingVideoTrack:
            return "This edit’s video can’t be previewed on iPhone yet."
        case NativeEditorPlaybackFailure.liveItem:
            return "The editable preview couldn’t play on this iPhone, so the finished video is shown. Retry to rebuild it."
        case NativeEditorPlaybackFailure.finishedItem:
            return "The video couldn’t be loaded. Check your connection and retry."
        case SourceAssetError.missingOriginal, SourceAssetError.changedOriginal:
            return Self.originalsUnavailableMessage
        default:
            return RequestFailureCause(error) == .connection
                ? "A source video or edit asset could not be loaded. Check your connection and retry."
                : "A source video or edit asset could not be loaded. \(error.localizedDescription)"
        }
    }

    func prepareSourcePreview() async {
        guard let api, let jobID, let variantKey else { return }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        let sequence = sourcePreviewSequence
        let generation = document.revision.baseGeneration
        sourcePreviewGeneration = generation
        sourcePreviewState = .preparing
        player?.pause()
        isPlaying = false
        resolvedSources = nil
        sourcePool = nil
        resolvedAudio = [:]
        deviceNarrationResolutionGeneration = nil
        resolvedMedia = [:]
        do {
            #if DEBUG
            NativePreviewDiagnostics.record("source-pool-request", fields: ["job": jobID.uuidString, "variant": variantKey])
            #endif
            var pool = try await api.editorSourcePool(jobID: jobID, variantID: variantKey)
            #if DEBUG
            NativePreviewDiagnostics.record("source-pool-loaded", fields: ["clips": String(pool.clips.count), "resolved": String(pool.clips.filter { $0.nativeSource != nil }.count), "required": timelineClips.compactMap(\.sourceClipIndex).map(String.init).joined(separator: ",")])
            #endif
            if pool.baseGeneration == nil {
                // Older timeline responses omit the baseline. Re-read the owned
                // variant before accepting its immutable source URLs.
                let current = try await api.editorVariant(jobID: jobID, variantID: variantKey)
                let currentGeneration = current["render_generation_id"]?.stringValue
                    ?? current["render_finished_at"]?.stringValue ?? ""
                guard !generation.isEmpty, currentGeneration == generation else { throw APIError.conflict }
                pool = NativeEditorSourcePool(clips: pool.clips, baseGeneration: generation, nativeAssets: pool.nativeAssets)
            }
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { return }
            let resolver = sourceResolver ?? NativeEditorSourceResolver(project: BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID), jobID: jobID)
            sourceResolver = resolver
            guard !generation.isEmpty, pool.baseGeneration == generation else { throw APIError.conflict }
            originalsRecoveryPool = pool
            let sources: [Int: ResolvedEditorSource]
            var phoneTalkingIndex: Int?
            var phoneNarratedSeeds: [NativePhoneNarratedSource.Seed] = []
            if let base = NativeEditorBaseSource(variant: previewVariant, document: document) {
                #if DEBUG
                NativePreviewDiagnostics.record("prepare-composite-base")
                #endif
                let resolved = try await resolver.resolveMedia(id: base.mediaID, url: base.url, generation: generation)
                guard sequence == sourcePreviewSequence, !Task.isCancelled,
                      document.revision.baseGeneration == generation else { return }
                guard let duration = resolved.asset.duration else { throw APIError.invalidResponse }
                // Hydration is server state, not an edit. Preserve text history
                // created while media downloaded and keep Undo's base consistent.
                document = try base.hydrate(document, duration: duration)
                cleanDocument = try base.hydrate(cleanDocument, duration: duration)
                chatStagedDocument = try chatStagedDocument.map { try base.hydrate($0, duration: duration) }
                undoStack = try undoStack.map { try base.hydrate($0, duration: duration) }
                redoStack = try redoStack.map { try base.hydrate($0, duration: duration) }
                let source = NativeTimelineSource(mediaID: base.mediaID, sourceURL: base.url, original: nil, localRequired: false)
                pool = NativeEditorSourcePool(clips: [.init(clipIndex: 0, nativeSource: source)], baseGeneration: generation, nativeAssets: pool.nativeAssets)
                sources = [0: ResolvedEditorSource(clipIndex: 0, mediaID: base.mediaID, asset: resolved.asset, url: resolved.url)]
                setAuthoritativeDuration(duration)
                refreshDuration()
            } else {
                phoneTalkingIndex = NativePhoneTalkingSource.sourceIndex(variant: previewVariant, document: document,
                    pool: pool, lanesEditable: canEdit(.mediaOverlays) || canEdit(.soundEffects))
                if NativePhoneNarratedSource.applies(variant: previewVariant, document: document) {
                    phoneNarratedSeeds = await loadPhoneNarratedSeeds(pool: pool, generation: generation, sequence: sequence)
                }
                sources = try await resolver.resolve(pool, generation: generation,
                    requiredIndices: Set(timelineClips.compactMap(\.sourceClipIndex) + (phoneTalkingIndex.map { [$0] } ?? [])
                        + document.clipAudio.map(\.sourceClipIndex) + phoneNarratedSeeds.map(\.clipIndex)))
            }
            guard sequence == sourcePreviewSequence, !Task.isCancelled,
                  document.revision.baseGeneration == generation else { return }
            if let phoneTalkingIndex {
                guard let duration = sources[phoneTalkingIndex]?.asset.duration else { throw APIError.invalidResponse }
                // Captions/lanes are on the speech-cleanup cut timeline (KRI-232).
                let removed = NativePhoneTalkingSource.removedSpans(variant: previewVariant)
                #if DEBUG
                NativePreviewDiagnostics.record("phone-talking-source", fields: ["index": String(phoneTalkingIndex), "duration": String(duration), "removed": String(removed.count)])
                #endif
                document = try NativePhoneTalkingSource.hydrate(document, clipIndex: phoneTalkingIndex, duration: duration, removed: removed)
                cleanDocument = try NativePhoneTalkingSource.hydrate(cleanDocument, clipIndex: phoneTalkingIndex, duration: duration, removed: removed)
                chatStagedDocument = try chatStagedDocument.map { try NativePhoneTalkingSource.hydrate($0, clipIndex: phoneTalkingIndex, duration: duration, removed: removed) }
                undoStack = try undoStack.map { try NativePhoneTalkingSource.hydrate($0, clipIndex: phoneTalkingIndex, duration: duration, removed: removed) }
                redoStack = try redoStack.map { try NativePhoneTalkingSource.hydrate($0, clipIndex: phoneTalkingIndex, duration: duration, removed: removed) }
                refreshDuration()
            }
            if !phoneNarratedSeeds.isEmpty {
                // Server state, not an edit: keep history and the clean baseline consistent.
                func seed(_ doc: EditorDocument) -> EditorDocument {
                    NativePhoneNarratedSource.hydrate(doc, seeds: phoneNarratedSeeds, sources: sources)
                }
                document = seed(document)
                cleanDocument = seed(cleanDocument)
                chatStagedDocument = chatStagedDocument.map(seed)
                undoStack = undoStack.map(seed)
                redoStack = redoStack.map(seed)
                refreshDuration()
            }
            if previewVariant["resolved_archetype"] == .string("narrated") {
                document = try NativeNarratedSourceTiming.hydrate(document, sources: sources)
                cleanDocument = try NativeNarratedSourceTiming.hydrate(cleanDocument, sources: sources)
                chatStagedDocument = try chatStagedDocument.map { try NativeNarratedSourceTiming.hydrate($0, sources: sources) }
                undoStack = try undoStack.map { try NativeNarratedSourceTiming.hydrate($0, sources: sources) }
                redoStack = try redoStack.map { try NativeNarratedSourceTiming.hydrate($0, sources: sources) }
                refreshDuration()
            }
            guard let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) else {
                throw NativeEditorRenderError.missingFont("bundled fonts")
            }
            sourceCompiler = try NativeEditorRenderCompiler(fontDirectory: fonts)
            sourcePool = pool
            resolvedSources = sources
            await rebuildSourcePreview(sequence: sequence)
        } catch {
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { return }
            #if DEBUG
            NativePreviewDiagnostics.failure("preview-failure", error: error)
            #endif
            failSourcePreview(error)
        }
    }

    /// Locked source clips for a phone Narrated/Voiceover edit whose timeline came back
    /// empty, read from the pinned device recipe. Fail-soft: any problem leaves the
    /// document untouched and the normal preview error path decides.
    private func loadPhoneNarratedSeeds(pool: NativeEditorSourcePool, generation: String, sequence: Int) async -> [NativePhoneNarratedSource.Seed] {
        guard let api, let jobID, let variantKey,
              let status = try? await api.deviceRender(jobID: jobID, variantID: variantKey),
              sequence == sourcePreviewSequence, !Task.isCancelled,
              status.request.identity.jobID == jobID, status.request.identity.variantID == variantKey,
              status.phase == "published", status.publishedGeneration == generation else { return [] }
        return NativePhoneNarratedSource.seeds(recipe: status.request.recipe, pool: pool)
    }

    /// A save acknowledgement advances the document before its render is ready.
    /// Only an authoritative rebase replaces generation-owned preview inputs.
    private func refreshRebasedSourcePreview() {
        let generation = document.revision.baseGeneration
        guard sourcePreviewGeneration != generation else { return }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        resolvedSources = nil
        sourcePool = nil
        resolvedAudio.removeAll()
        deviceNarrationResolutionGeneration = nil
        resolvedMedia.removeAll()
        sourcePreview = nil
        textInteractionTask?.cancel()
        textInteractionSequence += 1
        textInteractionFrame = nil
        cancelScrubFrames()
        player?.pause()
        isPlaying = false
        sourcePreviewState = .preparing
        // prepareSourcePreview owns cancellation of the rebuild task; do not
        // install this task there or it would cancel itself on entry.
        Task { @MainActor [weak self] in
            guard let self, self.document.revision.baseGeneration == generation else { return }
            await self.prepareSourcePreview()
        }
    }

    static func previewMusicURL(trackID: String, variant: [String: JSONValue]) -> URL? {
        // UUID encoders use uppercase while the API serializes lowercase.
        // Match identity across that round trip, including archived tracks that
        // remain attached to an edit but no longer appear in the public picker.
        if variant["music_track_id"]?.stringValue?.caseInsensitiveCompare(trackID) == .orderedSame {
            return variant["music_preview_url"]?.stringValue.flatMap(URL.init(string:))
        }
        let background = Self.object(variant["background_music"])
        if background?["track_id"]?.stringValue?.caseInsensitiveCompare(trackID) == .orderedSame {
            return background?["preview_url"]?.stringValue.flatMap(URL.init(string:))
        }
        return nil
    }

    static func usesRenderedNarration(_ variant: [String: JSONValue]) -> Bool {
        let archetype = variant["resolved_archetype"]?.stringValue ?? ""
        let id = variant["variant_id"]?.stringValue ?? ""
        return ["narrated", "voiceover"].contains(archetype)
            || ["voiceover_only", "voiceover_music", "narrated"].contains(id)
            || (archetype == "guided_story" && variant["render_receipt"]?.objectValue?["narration_applied"] == .bool(true))
    }

    /// Device-rendered variants have no cloud receipt or base-video URL. Their
    /// current recipe is the authority for narration, and its published
    /// generation must still be the document we are reconstructing.
    static func currentDeviceNarrationRequest(
        _ status: DeviceRenderStatusResponse,
        jobID: UUID,
        variantID: String,
        generation: String
    ) throws -> DeviceRenderRequest? {
        guard status.request.identity.jobID == jobID,
              status.request.identity.variantID == variantID,
              status.phase == "published",
              status.publishedGeneration == generation else {
            throw APIError.conflict
        }
        return status.request.recipe.audio.narrationAssetID == nil ? nil : status.request
    }

    /// Narration is a generation-owned preview input: it belongs to the
    /// generation the preview was prepared for, not to a save acknowledgement
    /// whose render is still pending (the phone publishes under its own id).
    private func narrationOwnerGeneration(_ document: EditorDocument) -> String {
        Self.narrationOwnerGeneration(previewGeneration: sourcePreviewGeneration, documentGeneration: document.revision.baseGeneration)
    }

    static func narrationOwnerGeneration(previewGeneration: String?, documentGeneration: String) -> String {
        previewGeneration ?? documentGeneration
    }

    private func resolveDeviceNarration(document: EditorDocument, sequence: Int) async throws -> ResolvedEditorSource? {
        guard let api, let jobID, let variantKey else { throw APIError.invalidResponse }
        let status = try await api.deviceRender(jobID: jobID, variantID: variantKey)
        guard sequence == sourcePreviewSequence, !Task.isCancelled,
              document.revision.baseGeneration == self.document.revision.baseGeneration else {
            throw CancellationError()
        }
        guard let request = try Self.currentDeviceNarrationRequest(
            status, jobID: jobID, variantID: variantKey, generation: narrationOwnerGeneration(document)
        ) else { return nil }
        guard let narrationID = request.recipe.audio.narrationAssetID,
              var asset = request.recipe.assets.first(where: { $0.id == narrationID }) else {
            throw MediaEngineError.missingAsset("narration")
        }
        let project = BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID)
        let authorized = AuthorizedDeviceSourceResolver(
            api: api,
            request: request,
            originals: SourceAssetStore(project: project),
            library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
        )
        let url = try await authorized.resolveNarration()
        let duration = try await AVURLAsset(url: url).load(.duration).seconds
        guard duration.isFinite, duration > 0 else { throw APIError.invalidResponse }
        asset.duration = duration
        guard sequence == sourcePreviewSequence, !Task.isCancelled,
              document.revision.baseGeneration == self.document.revision.baseGeneration else {
            throw CancellationError()
        }
        return ResolvedEditorSource(clipIndex: -1, mediaID: narrationID, asset: asset, url: url)
    }

    private func preparePreviewAudio(document: EditorDocument, sequence: Int) async throws -> [String: ResolvedEditorSource] {
        guard let resolver = sourceResolver else { return [:] }
        if rendersOnDevice {
            // Reopen from the server-authoritative recipe, rather than the
            // app-owned DeviceRenderSessions cache, which is empty on a cold
            // launch. A missing or stale required asset fails the preview
            // visibly instead of silently exporting an AAC silence track.
            if resolvedAudio["narration"] == nil,
               deviceNarrationResolutionGeneration != narrationOwnerGeneration(document) {
                let narration = try await resolveDeviceNarration(document: document, sequence: sequence)
                guard sequence == sourcePreviewSequence, !Task.isCancelled,
                      document.revision.baseGeneration == self.document.revision.baseGeneration else {
                    throw CancellationError()
                }
                if let narration { resolvedAudio["narration"] = narration }
                deviceNarrationResolutionGeneration = narrationOwnerGeneration(document)
            }
        } else if Self.usesRenderedNarration(previewVariant), resolvedAudio["narration"] == nil {
            // Legacy narrated renders persist the exact cleaned voice + bed in
            // their caption-free base. Use its audio with original visual cuts;
            // never replay the finished video's burned captions.
            guard previewVariant["base_video_path"]?.stringValue?.isEmpty == false,
                  let value = previewVariant["base_video_url"]?.stringValue,
                  let url = URL(string: value),
                  url.path != previewVariant["output_url"]?.stringValue.flatMap({ URL(string: $0)?.path }) else {
                throw MediaEngineError.missingAsset("narration")
            }
            let resolved = try await resolver.resolveAudio(id: "narration", url: url, generation: document.revision.baseGeneration)
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
            resolvedAudio["narration"] = resolved
        }
        let referenceOnlyMusic = musicPlaybackMode == .referenceOnly
        let ids = Set([referenceOnlyMusic || resolvedAudio["narration"] != nil ? nil : document.music?.trackID,
                       !referenceOnlyMusic && document.backgroundMusic?.enabled == true && document.backgroundMusic?.muted != true
                        ? document.backgroundMusic?.trackID : nil].compactMap { $0 })
        for id in ids where resolvedAudio[id] == nil {
            sourcePreviewState = .preparing
            var url = Self.previewMusicURL(trackID: id, variant: previewVariant)
            if url == nil {
                let tracks = try await api?.editorMusicTracks() ?? []
                let track = tracks.first(where: { $0.id.caseInsensitiveCompare(id) == .orderedSame })
                url = track?.previewAudioURL
                #if DEBUG
                NativePreviewDiagnostics.record("music-source-lookup", fields: ["tracks": String(tracks.count), "matched": String(track != nil), "url": String(url != nil)])
                #endif
            }
            guard let url else { throw MediaEngineError.missingAsset(id) }
            let resolved = try await resolver.resolveAudio(id: id, url: url, generation: document.revision.baseGeneration)
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
            resolvedAudio[id] = resolved
        }
        for effect in document.soundEffects {
            let key = "sfx:" + effect.id
            guard resolvedAudio[key] == nil else { continue }
            sourcePreviewState = .preparing
            var previewID = effect.id
            var previewURL: URL?
            if let asset = sourcePool?.nativeAssets.first(where: { $0.kind == "sound_effect" && $0.id == effect.id }) {
                previewID = asset.mediaID
                previewURL = asset.sourceURL
            } else if let catalogID = effect.raw["sound_effect_id"]?.stringValue {
                // A newly-placed effect (from the Effects tab's catalog) isn't
                // in the timeline's precomputed source pool yet -- fall back
                // to the same catalog the tab browsed, exactly like music's
                // fallback above.
                let catalog = await loadSoundEffectCatalog()
                let match = catalog.first(where: { $0.id == catalogID })
                previewID = match?.id ?? catalogID
                previewURL = match?.previewAudioURL
            }
            guard let previewURL else { throw MediaEngineError.missingAsset(key) }
            let resolved = try await resolver.resolveAudio(id: previewID, url: previewURL, generation: document.revision.baseGeneration)
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
            resolvedAudio[key] = resolved
        }
        return resolvedAudio
    }

    private func preparePreviewMedia(document: EditorDocument, sequence: Int) async throws -> [String: ResolvedEditorSource] {
        guard let resolver = sourceResolver else { return resolvedMedia.merging(authoredVisualSources) { _, authored in authored } }
        for overlay in document.mediaOverlays {
            let key = "overlay:" + overlay.id
            guard resolvedMedia[key] == nil else { continue }
            guard let asset = sourcePool?.nativeAssets.first(where: { $0.kind == "media_overlay" && $0.id == overlay.id }) else {
                throw MediaEngineError.missingAsset(key)
            }
            sourcePreviewState = .preparing
            var resolved = try await resolver.resolveMedia(id: asset.mediaID, url: asset.sourceURL, generation: document.revision.baseGeneration)
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
            resolved.preserveAlpha = asset.preserveAlpha == true
            resolvedMedia[key] = resolved
        }
        if !document.motionScenes.isEmpty {
            for asset in sourcePool?.nativeAssets.filter({ $0.kind == "motion_scene" }) ?? [] {
                let key = "motion:" + asset.id
                if resolvedMedia[key] != nil { continue }
                sourcePreviewState = .preparing
                let resolved = try await resolver.resolveMedia(id: asset.mediaID, url: asset.sourceURL, generation: document.revision.baseGeneration)
                guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
                resolvedMedia[key] = resolved
            }
        }
        if !document.visualBlocks.isEmpty {
            for asset in sourcePool?.nativeAssets.filter({ $0.kind == "visual_block" }) ?? [] {
                let key = "visual:" + asset.id
                if resolvedMedia[key] != nil { continue }
                sourcePreviewState = .preparing
                let resolved = try await resolver.resolveMedia(id: asset.mediaID, url: asset.sourceURL, generation: document.revision.baseGeneration)
                guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
                resolvedMedia[key] = resolved
            }
        }
        return resolvedMedia.merging(authoredVisualSources) { _, authored in authored }
    }

    private func scheduleSourcePreviewUpdate() {
        guard resolvedSources != nil, sourceCompiler != nil else { return }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        guard !isTimingGestureActive, !isDirectManipulating else {
            sourcePreviewUpdateDeferred = true
            return
        }
        sourcePreviewUpdateDeferred = false
        let sequence = sourcePreviewSequence
        // Inside a gesture or typing transaction (slider drag, caption
        // keystrokes) every sample would otherwise recompile and repaint the
        // whole composition — on a guided story that is ~170 caption layouts
        // per sample, seconds of main-thread work per drag. Coalesce samples
        // until the finger pauses; a standalone edit still rebuilds at once.
        let coalesce = transactionBaseline != nil
        sourcePreviewTask = Task { [weak self] in
            if coalesce {
                try? await Task.sleep(for: .milliseconds(Self.previewCoalesceMilliseconds))
            } else {
                await Task.yield()
            }
            guard !Task.isCancelled else { return }
            await self?.rebuildSourcePreview(sequence: sequence)
        }
    }

    static let previewCoalesceMilliseconds = 80

    private func rebuildSourcePreview(sequence: Int) async {
        guard let compiler = sourceCompiler, var sources = resolvedSources else { return }
        let baseline = document
        let pending = pendingText
        var snapshot = baseline
        var items = timelineItems
        let clips = timelineClips
        if let pending, !pending.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            snapshot.textElements.append(pending)
            let projection = timelineProjection
            items.append(NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: pending.id),
                start: projection.projectBaseTime(pending.startS), end: projection.projectBaseTime(pending.endS), zIndex: 999, sourceIndex: items.count))
        }
        do {
            let needed = Set(clips.compactMap(\.sourceClipIndex) + baseline.clipAudio.map(\.sourceClipIndex))
            if !needed.isSubset(of: Set(sources.keys)), let pool = sourcePool, let resolver = sourceResolver {
                sourcePreviewState = .preparing
                sources = try await resolver.resolve(pool, generation: baseline.revision.baseGeneration, requiredIndices: needed)
                guard sequence == sourcePreviewSequence, !Task.isCancelled, document == baseline else { return }
                resolvedSources = sources
            }
            #if DEBUG
            NativePreviewDiagnostics.record("prepare-audio")
            #endif
            let audio = try await preparePreviewAudio(document: snapshot, sequence: sequence)
            #if DEBUG
            NativePreviewDiagnostics.record("prepare-media")
            #endif
            let media = try await preparePreviewMedia(document: snapshot, sequence: sequence)
            guard sequence == sourcePreviewSequence, !Task.isCancelled, document == baseline, pendingText == pending else { return }
            #if DEBUG
            sourcePreviewCompileCount += 1
            NativePreviewDiagnostics.record("compile", fields: [
                "textCount": String(snapshot.textElements.count),
                "uniqueTextIDs": String(Set(snapshot.textElements.map(\.id)).count),
                "rawActiveText": String(snapshot.textElements.filter { $0.startS <= 0 && $0.endS > 0 }.count),
                "captionCount": String(snapshot.captionCues.count),
                "rawActiveCaptions": String(snapshot.captionCues.filter { $0.startS <= 0 && $0.endS > 0 }.count),
                "projectedActiveText": String(items.filter { ($0.kind == .text || $0.kind == .captionCue) && $0.start <= 0 && $0.end > 0 }.count),
                "textWindows": snapshot.textElements.prefix(8).map { "\($0.startS):\($0.endS)" }.joined(separator: ",")])
            #endif
            let program = try compiler.compile(document: snapshot, clips: clips, items: items,
                                               sources: sources, audioSources: audio, mediaSources: media,
                                               referenceOnlyMusic: musicPlaybackMode == .referenceOnly,
                                               sourceAudioPreserved: sourceAudioPreserved,
                                               deviceCaptions: rendersOnDevice)
            // The in-place text update only applies while the canvas is still
            // on this composition. After a transient compile failure handed
            // the canvas to the finished-render fallback, its player item was
            // detached and must not be re-attached to a new player; fall
            // through and build a fresh composition so the canvas comes back
            // to the live edit — otherwise every later edit lands off-screen
            // while the user keeps watching the stale cloud render (KRI-110).
            if let preview = sourcePreview, player?.currentItem === preview.preview.playerItem,
               updatePreviewInPlace(preview, program: program) {
                sourcePreviewState = .ready
                sourcePreviewSettledSequence = sequence
                if !isPlaying { seek(to: currentTime) }
                prepareInteractionLayers()
                startPendingPlaybackIfPossible()
                return
            }
            #if DEBUG
            NativePreviewDiagnostics.record("composition-start")
            #endif
            let preview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs, branding: .standard)
            guard sequence == sourcePreviewSequence, !Task.isCancelled, document == baseline, pendingText == pending else { return }
            let latestTime = currentTime
            let resumePlayback = isPlaying
            sourcePreview = preview
            installPlayer(item: preview.preview.playerItem, preferredDuration: TimelineMath.totalDuration(of: program.recipe))
            sourcePreviewState = .ready
            sourcePreviewSettledSequence = sequence
            if resumePlayback {
                player?.seek(to: CMTime(seconds: min(latestTime, max(0, playbackDuration - 1.0 / 600)), preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero, completionHandler: { _ in })
                activatePreviewAudio()
                player?.play()
            } else {
                seek(to: latestTime)
            }
            prepareInteractionLayers()
            startPendingPlaybackIfPossible()
            #if DEBUG
            NativePreviewDiagnostics.record("preview-ready")
            #endif
        } catch {
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { return }
            player?.pause()
            isPlaying = false
            #if DEBUG
            NativePreviewDiagnostics.failure("preview-failure", error: error)
            #endif
            failSourcePreview(error)
            sourcePreviewSettledSequence = sequence
            // failSourcePreview -> restoreFinishedRenderFallback -> installPlayer
            // does not reseek. Without this, a clock pushed past the real
            // (finished-render) length by an in-progress auto-scroll or trim
            // has nothing to re-clamp it once the widened scrub bound above
            // narrows back — the transport would sit past the actual end.
            if currentTime > playbackDuration { seek(to: playbackDuration) }
        }
    }

    /// Any refusal (an audio edit since KRI-241, timing, sources) takes the full
    /// rebuild. DEBUG builds log the refusal, so an edit that should have stayed
    /// in place, such as a caption keystroke, shows up in the diagnostics file.
    private func updatePreviewInPlace(_ preview: LivePreviewComposition, program: NativeEditorRenderProgram) -> Bool {
        do {
            try preview.updateText(recipe: program.recipe, assetURLs: program.assetURLs)
            return true
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("live-update-refused", error: error)
            #endif
            return false
        }
    }

    func previewSelectionBounds(for selection: EditorSelection, at time: Double) -> TextSelectionBounds? {
        guard sourcePreviewState == .ready else { return nil }
        if selection.kind == .mediaOverlay { return sourcePreview?.mediaSelectionBounds(id: "overlay:" + selection.id, time: time) }
        let id = selection.kind == .captionCue ? "caption-" + selection.id : selection.id
        return sourcePreview?.textSelectionBounds(id: id, time: time)
    }

    func togglePlayback() {
        guard document.editorState != "empty" else { return }
        guard canDisplayCurrentPlayer, let player else {
            // While the editable preview is still building there is nothing to play yet. Remember the tap
            // instead of dropping it, so play starts the moment the preview (or its fallback) is ready.
            pendingPlayRequest = sourcePreviewState == .preparing && !isPlaying
            player?.pause()
            isPlaying = false
            return
        }
        pendingPlayRequest = false
        if isPlaying {
            pausePlayback()
            return
        }
        // Scrubbing renders stills independently of AVPlayer. Only that handoff
        // (or replay) needs a seek; ordinary resume keeps the decoded surface.
        if playbackDuration > 0, currentTime >= playbackDuration - playbackEndTolerance {
            currentTime = 0
            playbackSeekTarget = 0
        }
        if seekInFlight || pendingSeekTime != nil { playbackSeekTarget = currentTime }
        seekRecoveryTask?.cancel()
        seekRecoveryTask = nil
        seekSequence += 1
        seekInFlight = false
        pendingSeekTime = nil
        isPlaying = true
        if let target = playbackSeekTarget {
            cancelScrubFrames(clearImage: false)
            let generation = scrubFrameGeneration
            playbackHandoffInFlight = true
            player.seek(to: CMTime(seconds: target, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero) { [weak self, weak player] finished in
                Task { @MainActor [weak self, weak player] in
                    guard let self, self.player === player, self.scrubFrameGeneration == generation else { return }
                    self.playbackHandoffInFlight = false
                    guard finished else { return }
                    self.playbackSeekTarget = nil
                    self.scrubPreviewFrame = nil
                    self.scrubPreviewTime = nil
                }
            }
        }
        player.play()
        // AVAudioSession.setCategory/setActive block the calling thread for tens
        // of ms while they round-trip to mediaserverd. Deferring just that call
        // one run-loop turn lets SwiftUI flush the isPlaying=true icon swap
        // immediately instead of stalling behind audio-session setup on every
        // play tap; player.play() itself is cheap and stays synchronous so a
        // pause landing before this fires still wins.
        Task { @MainActor [weak self, weak player] in
            guard let self, self.player === player, self.isPlaying else { return }
            self.activatePreviewAudio()
        }
    }

    func pausePlayback() {
        pendingPlayRequest = false
        player?.pause()
        isPlaying = false
        // Preserve a scrub target while its seek is still pending. Otherwise
        // freeze at the player's actual clock, not the last 50ms UI sample.
        if playbackSeekTarget == nil, let player, player.currentItem?.status == .readyToPlay {
            let seconds = player.currentTime().seconds
            if seconds.isFinite { currentTime = min(max(0, seconds), max(0, playbackDuration)) }
        }
    }

    /// Editor playback is media audio, including when the silent switch is on.
    /// Reassert on play because narration recording changes the shared session.
    private func activatePreviewAudio() {
        do {
            let audio = AVAudioSession.sharedInstance()
            if audio.category != .playback || audio.mode != .moviePlayback {
                try audio.setCategory(.playback, mode: .moviePlayback)
            }
            try audio.setActive(true)
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("preview-audio-session", error: error)
            #endif
        }
    }

    private var playbackSeekTarget: TimeInterval?
    private var playbackHandoffInFlight = false
    private var pendingSeekTime: TimeInterval?
    private var seekInFlight = false
    private var seekSequence = 0
    private var seekRecoveryTask: Task<Void, Never>?

    func seek(to time: TimeInterval) {
        // A timeline gesture owns the clock. Continuing playback can advance
        // the displayed frame between finger samples and race the next seek.
        if isPlaying || (player?.rate ?? 0) != 0 {
            player?.pause()
            isPlaying = false
        }
        // The clock/scrub range uses timelineScrubDuration (widened while a
        // preview rebuild is pending) so auto-scroll and any seek can reach
        // the timeline's true end. requestScrubFrame below still clamps the
        // actual *frame request* to playbackDuration — the composition can
        // only render what it has compiled until the rebuild lands.
        let clamped = min(max(0, time), max(0, timelineScrubDuration))
        currentTime = clamped
        if requestScrubFrame(at: clamped) {
            seekRecoveryTask?.cancel()
            seekSequence += 1
            seekInFlight = false
            pendingSeekTime = nil
            return
        }
        pendingSeekTime = clamped
        drainPendingSeek()
    }

    /// Paused composition seeks can retain a previous cut's frame. Generate
    /// the still from the same immutable composition instead; AVPlayer remains
    /// responsible for continuous playback. Keep one render in flight and
    /// coalesce finger events into the latest requested position.
    private func requestScrubFrame(at time: TimeInterval) -> Bool {
        guard let item = player?.currentItem, let composition = item.videoComposition else { return false }
        if scrubFramePlayerItem !== item || scrubFrameComposition !== composition {
            cancelScrubFrames(clearImage: false)
            let generator = AVAssetImageGenerator(asset: item.asset)
            generator.videoComposition = composition
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
            generator.maximumSize = composition.renderSize.width > composition.renderSize.height
                ? CGSize(width: 1280, height: 720) : CGSize(width: 720, height: 1280)
            scrubFrameGenerator = generator
            scrubFramePlayerItem = item
            scrubFrameComposition = composition
        }
        playbackSeekTarget = min(time, max(0, playbackDuration - 1.0 / 600))
        pendingScrubFrameTime = playbackSeekTarget
        // Coalescing finger events (above) means only the latest requested
        // position ever becomes visible, so re-arming on every call and
        // measuring from the last one read is the correct "time to the frame
        // the user actually sees," not an average across dropped intermediates.
        seekLatencyMeter.request()
        guard scrubFrameTask == nil, let generator = scrubFrameGenerator else { return true }
        let generation = scrubFrameGeneration
        scrubFrameTask = Task { @MainActor [weak self, generator] in
            while !Task.isCancelled, let target = self?.pendingScrubFrameTime {
                self?.pendingScrubFrameTime = nil
                do {
                    let frame: (image: CGImage, actualTime: CMTime) = try await withCheckedThrowingContinuation { continuation in
                        generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: CMTime(seconds: target, preferredTimescale: 600))]) { _, image, actualTime, result, error in
                            if result == .succeeded, let image {
                                continuation.resume(returning: (image, actualTime))
                            } else {
                                continuation.resume(throwing: error ?? MediaEngineError.exportFailed)
                            }
                        }
                    }
                    guard !Task.isCancelled, let self, self.scrubFrameGeneration == generation else { return }
                    self.scrubPreviewTime = frame.actualTime.seconds
                    self.scrubPreviewFrame = UIImage(cgImage: frame.image)
                    self.prepareInteractionLayers()
                    if let elapsed = self.seekLatencyMeter.visible() {
                        self.previewInstrumentation.record(MetricEvent(name: .seekLatency, value: elapsed))
                    }
                } catch {
                    guard !Task.isCancelled, let self, self.scrubFrameGeneration == generation else { return }
                    #if DEBUG
                    NativePreviewDiagnostics.failure("scrub-frame-failure", error: error)
                    #endif
                    self.scrubPreviewFrame = nil
                    self.scrubPreviewTime = nil
                    break
                }
            }
            guard let self, self.scrubFrameGeneration == generation else { return }
            self.scrubFrameTask = nil
        }
        return true
    }

    private func cancelScrubFrames(clearImage: Bool = true) {
        playbackHandoffInFlight = false
        scrubFrameGeneration += 1
        scrubFrameTask?.cancel()
        scrubFrameTask = nil
        scrubFrameGenerator?.cancelAllCGImageGeneration()
        scrubFrameGenerator = nil
        scrubFramePlayerItem = nil
        scrubFrameComposition = nil
        pendingScrubFrameTime = nil
        if clearImage {
            scrubPreviewFrame = nil
            scrubPreviewTime = nil
        }
    }

    /// AVPlayer cancels an outstanding seek when another is submitted. Keep
    /// one in flight and replace queued positions with the newest finger sample.
    private func drainPendingSeek() {
        guard !seekInFlight, let target = pendingSeekTime else { return }
        guard let player else { pendingSeekTime = nil; return }
        pendingSeekTime = nil
        seekInFlight = true
        seekSequence += 1
        let sequence = seekSequence
        // The timeline's end is exclusive in the compositor. Keep the ruler
        // at the requested end while displaying the final valid video frame.
        let playableTarget = min(target, max(0, playbackDuration - 1.0 / 600))
        seekRecoveryTask?.cancel()
        seekRecoveryTask = Task { @MainActor [weak self, weak player] in
            do { try await Task.sleep(for: .milliseconds(200)) } catch { return }
            guard let self, let player, self.player === player,
                  self.seekSequence == sequence, self.seekInFlight else { return }
            // AVPlayer can omit a paused composition seek's completion at a
            // cut. Submitting the newest seek cancels that stalled request;
            // the sequence check prevents its late callback from reopening it.
            self.seekInFlight = false
            guard player.status != .failed, player.currentItem?.status != .failed else {
                self.pendingSeekTime = nil
                return
            }
            self.pendingSeekTime = self.pendingSeekTime ?? target
            self.drainPendingSeek()
        }
        player.seek(to: CMTime(seconds: playableTarget, preferredTimescale: 600),
                    toleranceBefore: .zero, toleranceAfter: .zero) { [weak self, weak player] _ in
            Task { @MainActor [weak self, weak player] in
                guard let self, self.seekSequence == sequence else { return }
                self.seekRecoveryTask?.cancel()
                self.seekRecoveryTask = nil
                self.seekInFlight = false
                guard self.player === player else { self.pendingSeekTime = nil; return }
                self.drainPendingSeek()
            }
        }
    }

    #if DEBUG
    /// How many times the source composition has been recompiled from the
    /// document. Tests use it to pin that a gesture's samples coalesce.
    private(set) var sourcePreviewCompileCount = 0

    /// The recipe behind the composition the canvas is showing right now.
    /// `nil` whenever the player is on anything else — the finished cloud
    /// render, a stale composition — so a test can assert an edit reached
    /// what the user actually sees, not merely some off-screen object.
    var displayedSourcePreviewRecipe: KriaMediaEngine.EditRecipe? {
        guard sourcePreviewState == .ready, let sourcePreview,
              player?.currentItem === sourcePreview.preview.playerItem else { return nil }
        return sourcePreview.exportSnapshot().recipe
    }

    func auditPreviewWindow() async -> [[String: String]] {
        var rows: [[String: String]] = []
        let cache = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        for clip in timelineClips where clip.start < 20.5 && clip.end > 17.5 {
            var row = ["stage": "window-source", "start": String(clip.start), "end": String(clip.end),
                       "sourceIndex": String(clip.sourceClipIndex ?? -1), "sourceIn": String(clip.trimIn)]
            if let index = clip.sourceClipIndex, let source = resolvedSources?[index] {
                row["file"] = source.url.lastPathComponent
                if let image = CGImageSourceCreateWithURL(source.url as CFURL, nil) {
                    let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [CFString: Any] ?? [:]
                    row["orientation"] = String((properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1)
                    row["width"] = String((properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0)
                    row["height"] = String((properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue ?? 0)
                    if let decoded = CGImageSourceCreateImageAtIndex(image, 0, nil) {
                        row["decoded"] = "\(decoded.width)x\(decoded.height)"
                    }
                } else if let track = try? await AVURLAsset(url: source.url).loadTracks(withMediaType: .video).first {
                    if let size = try? await track.load(.naturalSize) { row["decoded"] = "\(size.width)x\(size.height)" }
                    if let transform = try? await track.load(.preferredTransform) { row["transform"] = "\(transform)" }
                }
            }
            rows.append(row)
        }
        if let item = player?.currentItem {
            let generator = AVAssetImageGenerator(asset: item.asset)
            generator.videoComposition = item.videoComposition
            generator.maximumSize = CGSize(width: 360, height: 640)
            for second in [17.9, 18.5, 19.5, 20.1] where second < duration {
                do {
                    let frame = try await generator.image(at: CMTime(seconds: second, preferredTimescale: 600))
                    let name = "native-window-\(second).jpg"
                    try UIImage(cgImage: frame.image).jpegData(compressionQuality: 0.85)?.write(to: cache.appendingPathComponent(name))
                    rows.append(["stage": "window-frame", "time": String(second), "file": name])
                } catch { rows.append(["stage": "window-frame-error", "time": String(second), "code": String((error as NSError).code)]) }
            }
            let targets = [19.0, 3.0, 28.0, 18.5, 1.0, 20.0, 5.0, 19.5]
            for target in targets {
                let started = Date()
                seek(to: min(target, duration))
                for _ in 0..<100 where seekInFlight || pendingSeekTime != nil { try? await Task.sleep(for: .milliseconds(50)) }
                rows.append(["stage": "window-seek", "target": String(target), "actual": String(player?.currentTime().seconds ?? -1),
                             "elapsed": String(Date().timeIntervalSince(started)), "pending": String(seekInFlight),
                             "status": String(item.status.rawValue), "error": String((item.error as NSError?)?.code ?? 0)])
            }
        }
        return rows
    }
    #endif

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
        active.lastTranslation = translation
        let translation = translation - active.translationOrigin
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
        // Later clips ripple by construction (windows are a walk of the slot durations); the
        // per-clip labels must ripple with them, derived from the gesture baseline every update.
        rebaseGuidedLabels(&next, from: active.baseline)

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
        flushDeferredSourcePreviewUpdate()
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
        guard NativeEditorWireContract.lookPresets.contains(value), canSetClipLook(preset: value, adjustments: nil) else { return }
        mutateClip(clipID: clipID) { $0.lookPreset = value }
    }
    func setClipLookPreset(clipID: UUID, preset: String?) { setClipLookPreset(clipID: clipID.uuidString, preset: preset) }
    func updateClipLook(clipID: String, preset: String?, adjustments: [String: JSONValue]? = nil) {
        let value = preset ?? "none"
        guard NativeEditorWireContract.lookPresets.contains(value), canSetClipLook(preset: value, adjustments: adjustments) else { return }
        mutateClip(clipID: clipID) { slot in slot.lookPreset = value; if let adjustments { slot.lookAdjustments = adjustments } }
    }
    func updateClipLook(clipID: UUID, preset: String?, adjustments: [String: JSONValue]? = nil) { updateClipLook(clipID: clipID.uuidString, preset: preset, adjustments: adjustments) }
    func setClipLookAdjustments(clipID: String, adjustments: [String: JSONValue]?) {
        guard canSetClipLook(preset: "none", adjustments: adjustments) else { return }
        mutateClip(clipID: clipID) { $0.lookAdjustments = adjustments }
    }
    func setClipLookAdjustments(clipID: UUID, adjustments: [String: JSONValue]?) { setClipLookAdjustments(clipID: clipID.uuidString, adjustments: adjustments) }
    /// Device variants can't save a look yet (the server closes `clips.looks`).
    /// Clearing one saved before that stays open, or the edit could never save.
    private func canSetClipLook(preset: String, adjustments: [String: JSONValue]?) -> Bool {
        if preset == "none", adjustments?.isEmpty ?? true { return true }
        return !rendersOnDevice && canEditOperation(["clips.looks"], section: .timeline)
    }
    /// How a clip slot clears a stored crop or speed. The guided revision
    /// writer keeps a key a Save omits (a new split slot inherits its
    /// parent's), so clearing needs an explicit null unless the saved copy of
    /// this slot never had the key; dropping it then keeps the snapshot shape.
    func clipSlotClearValue(slotID: String, key: String) -> JSONValue? {
        guard let saved = cleanDocument.clips.first(where: { $0.id == slotID }) else { return .null }
        return saved.raw[key] == nil ? nil : .null
    }
    func setClipTransition(clipID: String, transition: String, durationS: Double? = nil) {
        guard NativeEditorWireContract.transitions.contains(transition) else { return }
        // 0.3s is the server's hard ceiling (guided_edit_revision.py's
        // `le=0.3`, and routes/generative_jobs.py silently shrinks anything
        // larger) -- clamp here, at the mutation, so no caller can produce a
        // value that only gets shrunk later at Save.
        let duration = transition == "cut" ? nil : durationS.map { min(max(0.1, $0), 0.3) }
        mutateClip(clipID: clipID) { $0.transitionAfter = transition; $0.transitionDurationS = duration }
    }
    func setClipTransition(clipID: UUID, transition: String, durationS: Double? = nil) { setClipTransition(clipID: clipID.uuidString, transition: transition, durationS: durationS) }
    /// Sets the same transition on every boundary in one transaction (one
    /// undo step, one preview recompile) instead of looping
    /// `setClipTransition` per boundary. A one-time bulk default, not a
    /// live "setting" -- clips added or split afterward start at "cut" like
    /// any other new boundary.
    func setTransitionAcrossVideo(transition: String, durationS: Double? = nil) {
        guard NativeEditorWireContract.transitions.contains(transition), canEditSection(.timeline) else { return }
        let duration = transition == "cut" ? nil : durationS.map { min(max(0.1, $0), 0.3) }
        transactDocument(section: .timeline) { doc in
            guard doc.clips.count > 1 else { return }
            for index in doc.clips.indices.dropLast() {
                doc.clips[index].transitionAfter = transition
                doc.clips[index].transitionDurationS = duration
            }
        }
    }
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
        _ = deleteSelection(EditorSelection(kind: .clip, id: clipID))
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
        guard let selection, selection.kind == .clip else { return }
        _ = deleteSelection(selection)
    }

    /// Upload a freshly picked clip or photo and append it to the end of the
    /// timeline. Two network calls happen before anything is staged locally:
    /// reserve + upload the bytes, then mint the new `clip_index` in the job's
    /// shared footage pool (`KriaAPIClient.addClip`) — the pool is otherwise
    /// fixed at job creation, so a slot referencing an unminted index would be
    /// rejected on Save. The new slot itself then round-trips through the
    /// normal Save path like any other timeline edit.
    func addClip(fileURL: URL) async { await addClip(source: { fileURL }) }

    /// `source` produces the file. For a Photos pick that is the export out of the library, which can
    /// take seconds when the asset lives in iCloud. Running it here, rather than before the call,
    /// keeps `isAddingClip` true throughout, so the editor can show progress after the picker sheet
    /// has already dismissed instead of holding the user on it.
    func addClip(source: @MainActor () async throws -> URL, uploadSource: UploadSource = .files) async {
        // 20 mirrors the server's `_MAX_CLIPS` pool cap — an early, friendly
        // no-op instead of a round trip that would 422 anyway.
        guard !isAddingClip else { return }
        // The sheet is already gone by now, so a silent return would drop the user's pick with no signal.
        guard !rendersOnDevice else {
            isAddingClip = true
            defer { isAddingClip = false }
            do {
                let url = try await source()
                await addDeviceTimelineMedia(fileURL: url, source: uploadSource, alreadyPreparing: true)
            } catch {
                addClipError = "This file couldn’t be read. Try Files or choose it again."
            }
            return
        }
        guard (canEditTimeline || document.editorState == "empty"), let api, let jobID else {
            addClipError = "The timeline can’t be edited right now. Try adding it again in a moment."
            return
        }
        guard draft.clips.count < Self.maxTimelineClips else {
            addClipError = "This edit already has the maximum number of clips."
            return
        }
        isAddingClip = true
        addClipError = nil
        defer { isAddingClip = false }
        // With the sheet gone the user will background the app while this uploads, and `uploadFile`
        // is a foreground request, so ask for time to finish. If that time runs out the handler MUST end
        // the assertion: iOS terminates an app whose expiration handler doesn't, taking the editor's
        // unsaved timeline with it. The suspended request then fails and is reported below.
        let activity = backgroundActivity
        let handle = BackgroundAssertionHandle()
        handle.set(activity.begin(name: "com.kria.app.editor-add-clip") {
            MainActor.assumeIsolated { handle.end(using: activity) }
        })
        defer { handle.end(using: activity) }
        do {
            let fileURL = try await source()
            let values = try fileURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            let reservation = try await api.reserveUpload(filename: fileURL.lastPathComponent, contentType: contentType, size: Int64(size), purpose: nil)
            try await api.uploadFile(to: reservation, fileURL: fileURL)
            let result = try await api.addClip(jobID: jobID, gcsPath: reservation.gcsPath)
            guard canEditTimeline || document.editorState == "empty" else {
                // The clip uploaded and was minted into the job's pool, but the timeline is no longer
                // editable (a save or render began). Say so rather than silently discarding it.
                addClipError = "The clip uploaded, but the timeline changed while it was uploading. Add it again."
                return
            }
            transactDocument(section: .timeline) { doc in
                // `media_kind` lets a chat turn's editor state name an UNSAVED added clip whose
                // clip_index the server may not resolve from persisted sources yet.
                let kind: [String: JSONValue] = ["video", "image"].contains(result.kind) ? ["media_kind": .string(result.kind)] : [:]
                let clipIndex = variantKey.flatMap { result.variantClipIndices?[$0] } ?? result.clipIndex
                doc.clips.append(EditorTimelineSlot(clipIndex: clipIndex, inS: 0, durationS: Self.addedClipDurationS, raw: kind))
            }
            addClipError = nil
        } catch is AddClipSourceUnreadable {
            addClipError = "This file couldn’t be read. Try Files or choose it again."
        } catch let error as URLError where [.networkConnectionLost, .cancelled, .notConnectedToInternet, .timedOut].contains(error.code) {
            addClipError = "The upload was interrupted. Your edit is unchanged. Keep Kria open while it uploads and try again."
        } catch { addClipError = "This file couldn’t be added. Your edit is unchanged. " + error.localizedDescription }
    }

    func addText(content: String? = nil) {
        guard canEditSection(.text) else { return }
        let value = content?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty ?? "Your story"
        transact(section: .text) { $0.text.append(TextLayer(id: UUID(), content: value, position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")) }
    }

    func beginTextCreation() {
        guard canEditSection(.text), pendingText == nil, duration > 0 else { return }
        player?.pause()
        isPlaying = false
        let start = min(max(0, currentTime), max(0, duration - 0.1))
        pendingText = EditorTextElement(
            id: UUID().uuidString, text: "", startS: timelineProjection.unprojectOutputTime(start),
            endS: timelineProjection.unprojectOutputTime(min(duration, start + 2)),
            raw: ["font_family": .string("Inter Regular"), "size_px": .number(72),
                  "x_frac": .number(0.5), "y_frac": .number(0.5), "position": .string("custom"),
                  "color": .string("#FFFFFF"), "effect": .string("none"), "wrap_lines": .bool(false)]
        )
    }

    func updatePendingText(_ content: String) { pendingText?.text = content }

    func cancelTextCreation() { pendingText = nil }

    @discardableResult func finishTextCreation() -> EditorSelection? {
        guard let item = pendingText else { return nil }
        pendingText = nil
        guard !item.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, canEditSection(.text) else { return nil }
        transactDocument(section: .text) { $0.textElements.append(item) }
        let selected = EditorSelection(kind: .text, id: item.id)
        select(selected, seekToStart: false)
        return selected
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
        mutateText(id: id) { $0.text = content; $0.raw["wrap_lines"] = .bool(false) }
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
        mutateText(id: id) {
            $0.raw["position"] = .string("custom")
            $0.raw["x_frac"] = .number(min(max(0, x), 1)); $0.raw["y_frac"] = .number(min(max(0, y), 1))
        }
    }
    /// Apply every sample against the gesture's immutable starting element.
    /// Font size and wrapping width scale together, preserving the text layout.
    func transformText(from baseline: EditorTextElement, scale: Double, rotationDelta: Double) {
        guard canEditSection(.text), scale.isFinite, scale > 0, rotationDelta.isFinite else { return }
        let size = Self.textSize(for: baseline)
        let width = baseline.raw["max_width_frac"]?.numberValue ?? 0.84
        let rotation = baseline.raw["rotation_deg"]?.numberValue ?? 0
        let ratio = max(scale, max(8 / size, 0.2 / width))
        guard (size * ratio).isFinite, (width * ratio).isFinite else { return }
        mutateText(id: baseline.id) {
            $0.raw["size_px"] = .number(size * ratio)
            $0.raw["max_width_frac"] = .number(width * ratio)
            $0.raw["rotation_deg"] = .number((rotation + rotationDelta).truncatingRemainder(dividingBy: 360))
        }
    }

    func applyTextPreset(id: String, preset: String) {
        guard canEditSection(.text), ["Simple", "Bold", "Highlight"].contains(preset) else { return }
        mutateText(id: id) {
            $0.raw["editor_preset"] = .string(preset)
            $0.raw["font_family"] = .string(preset == "Simple" ? "Inter Regular" : "Inter")
            $0.raw["color"] = .string(preset == "Highlight" ? "#30352C" : "#FFFFFF")
            $0.raw["background_color"] = preset == "Highlight" ? .string("#FFF0A6") : .null
        }
    }
    func setTextPhase(id: String, phase: String, effect: String) {
        guard canEditSection(.text), ["entrance", "exit", "loop"].contains(phase),
              (phase == "loop" ? ["none", "pulse", "bounce", "float"] : ["none", "fade", "pop", "slide", "typewriter"]).contains(effect) else { return }
        mutateText(id: id) {
            var phases = Self.textPhases(for: $0)
            phases[phase] = .string(effect)
            $0.raw["animation_phases"] = .object(phases)
        }
    }
    func setTextAnimationSpeed(id: String, speed: Double) {
        guard canEditSection(.text), speed.isFinite else { return }
        mutateText(id: id) {
            var phases = Self.textPhases(for: $0)
            phases["speed"] = .number(min(3, max(0.25, speed)))
            $0.raw["animation_phases"] = .object(phases)
        }
    }
    static func textPhases(for item: EditorTextElement) -> [String: JSONValue] {
        if let phases = object(item.raw["animation_phases"]) { return phases }
        let entrance: String
        switch item.raw["effect"]?.stringValue {
        case "fade-in": entrance = "fade"
        case "pop-in": entrance = "pop"
        case "slide-in": entrance = "slide"
        case "typewriter": entrance = "typewriter"
        default: entrance = "none"
        }
        return ["entrance": .string(entrance), "exit": .string("none"), "loop": .string("none"), "speed": .number(1)]
    }
    static func textSize(for element: EditorTextElement) -> Double {
        if let size = element.raw["size_px"]?.numberValue { return size }
        let sizes: [String: Double] = ["small": 36, "medium": 72, "large": 120,
                                      "xlarge": 150, "xxlarge": 250, "jumbo": 199]
        return sizes[element.raw["size_class"]?.stringValue ?? "jumbo"] ?? 199
    }

    func setTextSize(id: String, sizePX: Double?) {
        guard let sizePX else { setTextRaw(id: id, key: "size_px", value: .null); return }
        guard sizePX.isFinite, sizePX >= 8,
              let baseline = document.textElements.first(where: { $0.id == id }) else { return }
        transformText(from: baseline, scale: sizePX / Self.textSize(for: baseline), rotationDelta: 0)
    }
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

    /// Returns whether this call stored a fresh snapshot of the current item's library.
    @discardableResult
    func refreshVisualLibrary() async -> Bool {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-analyzing-gallery") {
            visualLibrary = (0..<24).map { index in
                CreationVisual(id: "analyzing-\(index)", kind: "video", status: index == 0 ? "analyzing" : "ready",
                    sourceFilename: "Gallery video \(index)", displayURL: nil, previewURL: nil, retryable: nil,
                    sourceURL: Bundle.main.url(forResource: "montage", withExtension: "mp4"))
            }
            return true
        }
        #endif
        guard let api, let itemID, !visualLibraryLoading else { return false }
        visualLibraryLoading = true
        defer { visualLibraryLoading = false }
        do {
            let library = try await api.visuals(itemID: itemID)
            guard self.itemID == itemID, !Task.isCancelled else { return false }
            #if DEBUG
            NativePreviewDiagnostics.record("gallery-refresh", fields: [
                "count": String(library.assets.count),
                "pending": String(library.assets.filter { $0.status != "ready" }.count)
            ])
            #endif
            visualLibrary = library.assets
            visualLibraryLimit = library.maxAssets
            visualError = nil
            return true
        } catch {
            if !Task.isCancelled { visualError = "Your visuals couldn’t load. Try again. " + error.localizedDescription }
            return false
        }
    }

    /// Statuses the server moves past on its own. A fresh upload and a
    /// reanalyze both restart at `queued`, which the API reports as `uploaded`
    /// until `pool_asset_queued_status_enabled` is on.
    private static let visualInProgressStatuses: Set<String> = ["uploaded", "queued", "pending", "analyzing", "processing"]

    /// Whether the Visuals panel should keep polling: an asset is still being
    /// analyzed, or a transient failure still has an automatic retry to come.
    var visualLibraryNeedsPolling: Bool {
        visualLibrary.contains { Self.visualInProgressStatuses.contains($0.status) || visualAutoRetry.needsObservation(of: $0) }
    }

    /// One refresh of the library followed by any automatic reanalyze that is
    /// due. Transient analysis failures (`analysis_temporarily_unavailable`)
    /// retry on their own: at most three times per asset per editor session,
    /// 10/20/40 s apart, so reopening the Visuals panel never resets the
    /// budget. Serialized so an overlapping poll never acts on a snapshot
    /// older than a reanalyze already in flight.
    func pollVisualLibrary() async {
        guard !visualPollRunning else { return }
        visualPollRunning = true
        defer { visualPollRunning = false }
        guard await refreshVisualLibrary(), let api, let itemID else { return }
        var scheduler = visualAutoRetry
        let due = scheduler.observe(visualLibrary, now: visualAutoRetryClock())
        updateVisualAutoRetry(scheduler)
        guard !due.isEmpty else { return }
        for assetID in due {
            let result = try? await api.retryVisual(itemID: itemID, assetID: assetID)
            recordVisualRetry(assetID, result: result)
        }
        await refreshVisualLibrary()
    }

    /// Publishes only real changes: the panel polls every few seconds and
    /// every publish re-renders the editor.
    private func updateVisualAutoRetry(_ scheduler: VisualAutoRetryScheduler) {
        if scheduler != visualAutoRetry { visualAutoRetry = scheduler }
    }

    private func recordVisualRetry(_ id: String, result: CreationVisual?) {
        var scheduler = visualAutoRetry
        scheduler.recordAttempt(assetID: id, result: result, now: visualAutoRetryClock())
        updateVisualAutoRetry(scheduler)
    }

    /// A manual Retry supersedes a pending automatic one, and is refused while
    /// a reanalyze for the same asset is still awaiting its response.
    func retryLibraryVisual(_ id: String) async {
        guard let api, let itemID else { return }
        var scheduler = visualAutoRetry
        guard scheduler.beginManualRetry(id) else { return }
        updateVisualAutoRetry(scheduler)
        do {
            let result = try await api.retryVisual(itemID: itemID, assetID: id)
            recordVisualRetry(id, result: result)
            await refreshVisualLibrary()
        } catch {
            recordVisualRetry(id, result: nil)
            visualError = error.localizedDescription
        }
    }

    // Basic media and cards use the existing lane contract. Only edits that
    // introduce editor_style need the newer styling capability.
    var canAuthorVisuals: Bool { canEditSection(.visualBlocks) }
    private var phoneEditorMedia: [String: JSONValue] {
        Self.object(Self.object(previewVariant["editor_capabilities"])?["phone_editor_media"] ?? .null) ?? [:]
    }
    private var canRegisterPhoneSources: Bool {
        rendersOnDevice && phoneEditorMedia["enabled"] == .bool(true)
            && phoneEditorMedia["source_registration"] == .bool(true)
            && !document.revision.baseGeneration.isEmpty && guidedRevisionNumber != nil
    }
    private func phoneAllowsVisualKind(_ kind: String) -> Bool {
        guard let allowed = phoneEditorMedia["visual_kinds"]?.arrayValue?.compactMap(\.stringValue) else { return false }
        return allowed.contains(kind)
    }
    // Visual blocks and motion scenes have no on-device lane yet, so an edit
    // rendered on this iPhone can't take new visuals after it was planned.
    var canImportVisuals: Bool {
        guard itemID != nil else { return false }
        if rendersOnDevice { return canRegisterPhoneSources && canAuthorVisuals }
        return canAuthorVisuals || canEditSection(.motionScenes)
    }
    var visualImportUnavailableMessage: String? {
        if itemID == nil { return "Open a saved edit to add photos or videos." }
        if rendersOnDevice && !canImportVisuals { return "Adding media isn’t available for this edit on this iPhone." }
        if !canImportVisuals { return "Adding visuals isn’t available for this edit." }
        return nil
    }

    /// KRI-132 journey fix: the timeline's Add-clip control (`NativeEditorTimelineView
    /// .canAddClip`) disables silently for an edit rendered on this iPhone -- adding a
    /// clip would need a full original upload to the cloud for an edit the phone can
    /// never save. Same reasoning as `visualImportUnavailableMessage`'s `rendersOnDevice`
    /// case, surfaced separately since the timeline and Visuals import gate independently.
    var addClipUnavailableMessage: String? {
        rendersOnDevice && !canRegisterPhoneSources ? "Adding media isn’t available for this edit on this iPhone." : nil
    }

    var canAddTimelineMedia: Bool {
        (canEditTimeline || document.editorState == "empty") && draft.clips.count < Self.maxTimelineClips && (!rendersOnDevice || canRegisterPhoneSources)
    }
    /// KRI-166: why `canAddTimelineMedia` is false, for the quick-add menu's
    /// Video row (which otherwise just greys out). Mirrors the three
    /// conditions above; nil when adding is allowed.
    var addClipUnavailableReason: String? {
        if rendersOnDevice && !canRegisterPhoneSources { return "Adding media isn’t available for this edit on this iPhone." }
        if !canEditTimeline && document.editorState != "empty" { return "This edit’s timeline can’t be changed." }
        if draft.clips.count >= Self.maxTimelineClips { return "An edit can have up to \(Self.maxTimelineClips) clips." }
        return nil
    }

    private func editorSourceTarget(kind: EditorSourceRegistrationTarget.SourceKind) -> EditorSourceRegistrationTarget? {
        guard let itemID, let variantKey, let guidedRevisionNumber else { return nil }
        return .init(itemID: itemID, variantID: variantKey, clientImportID: UUID(),
                     baseGeneration: document.revision.baseGeneration, guidedRevisionNumber: guidedRevisionNumber, sourceKind: kind)
    }

    private func sourceDuration(_ response: EditorSourceRegistrationResponse) -> Double? {
        response.source?["duration_s"]?.numberValue
    }

    static func deviceTimelineDuration(proxyDuration: Double?, localDuration: Double?, minimum: Double) -> Double? {
        guard let shortest = [proxyDuration, localDuration].compactMap({ $0 }).min(), shortest.isFinite else { return nil }
        let usable = shortest - 0.05
        return usable >= minimum ? usable : nil
    }

    private func waitForEditorSource(_ target: EditorSourceRegistrationTarget, recordID: UUID, uploads: BackgroundUploadCoordinator) async throws -> EditorSourceRegistrationResponse {
        // Source probing can run for the server lease window. Keep the durable
        // placement intent on any timeout/cancellation; reopen resumes it.
        for _ in 0..<420 {
            guard editorImportsActive, uploads.containsEditorPlacement(recordID), !Task.isCancelled else {
                throw CancellationError()
            }
            if let result = uploads.editorSourceResults[recordID] {
                uploads.recordEditorSourceResult(placementID: recordID, response: result)
                if result.isTerminal { return result }
            }
            if let api, let result = try? await api.editorSource(itemID: target.itemID, variantID: target.variantID, importID: target.clientImportID) {
                uploads.recordEditorSourceResult(placementID: recordID, response: result)
                if result.isTerminal { return result }
            }
            try await Task.sleep(for: .seconds(1))
        }
        uploads.failEditorPlacement(recordID, error: "Preparation is taking longer than expected. Try again.")
        throw APIError.offline
    }

    func placeEditorSource(_ placement: PendingEditorSourcePlacement, response: EditorSourceRegistrationResponse) async throws {
        guard editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true,
              response.status == "ready", document.revision.baseGeneration == placement.target.baseGeneration,
              !Task.isCancelled else { return }
        let placementID = placement.id.uuidString
        switch placement.lane {
        case .timeline:
            guard canAddTimelineMedia else { throw APIError.unsupported }
            guard let index = response.sourceIndex else { throw APIError.invalidResponse }
            guard !document.clips.contains(where: { $0.raw["editor_source_placement_id"] == .string(placementID) }) else {
                mediaUploads?.acknowledgeEditorPlacement(placement.id); return
            }
            let duration: Double
            if placement.target.sourceKind == .visual {
                // Timeline photos have no temporal probe. Preserve the
                // standard still-image window rather than applying a video
                // EOF margin to an absent duration.
                duration = Self.addedClipDurationS
            } else {
                guard let usable = Self.deviceTimelineDuration(proxyDuration: sourceDuration(response), localDuration: placement.localDurationS,
                                                               minimum: minimumClipDuration) else { throw APIError.invalidResponse }
                duration = usable
            }
            transactDocument(section: .timeline) {
                $0.clips.append(EditorTimelineSlot(clipIndex: index, inS: 0, durationS: min(Self.addedClipDurationS, duration), raw: [
                    "source_duration_s": .number(sourceDuration(response) ?? placement.localDurationS ?? duration + 0.05),
                    "editor_source_placement_id": .string(placementID),
                ]))
            }
        case .visual:
            guard canImportVisuals, document.visualBlocks.count < 20,
                  let storedAsset = placement.visual, phoneAllowsVisualKind(storedAsset.kind),
                  let sourceResolver, let api else { throw APIError.invalidResponse }
            guard !document.visualBlocks.contains(where: { $0.raw["editor_source_placement_id"] == .string(placementID) }) else {
                mediaUploads?.acknowledgeEditorPlacement(placement.id); return
            }
            // An interrupted import may resume after its signed URL expires.
            // Refresh the same pool identity before resolving its original.
            let library = try await api.visuals(itemID: placement.target.itemID)
            guard let asset = library.assets.first(where: { $0.id == storedAsset.id && $0.status == "ready" }),
                  asset.kind == storedAsset.kind, let originalURL = asset.originalMediaURL,
                  editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true,
                  document.revision.baseGeneration == placement.target.baseGeneration else { throw APIError.invalidResponse }
            let admittedPath = response.source?["gcs_path"]?.stringValue ?? asset.gcsPath
            guard let admittedPath, !admittedPath.isEmpty else { throw APIError.invalidResponse }
            let admitted = CreationVisual(id: asset.id, kind: response.source?["kind"]?.stringValue ?? asset.kind,
                status: "ready", sourceFilename: asset.sourceFilename, displayURL: asset.displayURL, previewURL: asset.previewURL,
                retryable: nil, gcsPath: admittedPath, sourceURL: asset.sourceURL,
                durationS: sourceDuration(response) ?? asset.durationS, mediaStatus: asset.mediaStatus)
            let maxDuration = admitted.kind == "video" ? admitted.durationS ?? 0 : Self.addedClipDurationS
            guard maxDuration >= minimumClipDuration,
                  let window = NativeVisualAuthoring.window(at: currentTime, duration: duration, projection: timelineProjection,
                    preferred: min(Self.addedClipDurationS, maxDuration)),
                  var block = NativeVisualAuthoring.media(asset: admitted, start: window.start,
                    end: min(window.end, window.start + maxDuration),
                    z: Int(document.visualBlocks.compactMap { $0.raw["z"]?.numberValue }.max() ?? 0) + 1) else { throw APIError.invalidResponse }
            let source = try await sourceResolver.resolveMedia(id: asset.id, url: originalURL, generation: placement.target.baseGeneration)
            guard editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true,
                  !Task.isCancelled, document.revision.baseGeneration == placement.target.baseGeneration else { return }
            guard canImportVisuals, document.visualBlocks.count < 20 else { throw APIError.unsupported }
            guard !document.visualBlocks.contains(where: { $0.raw["editor_source_placement_id"] == .string(placementID) }) else { return }
            block.raw["editor_source_placement_id"] = .string(placementID)
            authoredVisualSources["visual:" + block.id + ":" + block.id] = source
            transactDocument(section: .visualBlocks) { $0.visualBlocks.append(block) }
            select(.init(kind: .visualBlock, id: block.id))
        }
        mediaUploads?.acknowledgeEditorPlacement(placement.id)
        await prepareSourcePreview()
    }

    func resumePendingEditorPlacements() async {
        guard editorImportsActive, let uploads = mediaUploads, let itemID, let variantKey, api != nil else { return }
        let allPlacements = uploads.editorPlacements(itemID: itemID, variantID: variantKey, baseGeneration: nil)
        let placements = allPlacements.filter { $0.target.baseGeneration == document.revision.baseGeneration }
        for stale in allPlacements where stale.target.baseGeneration != document.revision.baseGeneration {
            await uploads.discardEditorPlacement(stale.id)
            addClipError = "A pending import belongs to an older version of this edit and was removed."
        }
        for placement in placements where placement.status != "failed" && pendingPlacementIDs.insert(placement.id).inserted {
            Task { @MainActor [weak self] in
                defer { self?.pendingPlacementIDs.remove(placement.id) }
                guard let self else { return }
                do {
                    let response = try await self.waitForEditorSource(placement.target, recordID: placement.id, uploads: uploads)
                    try await self.placeEditorSource(placement, response: response)
                } catch {
                    if self.editorImportsActive && !(error is CancellationError) {
                        uploads.failEditorPlacement(placement.id, error: "This import couldn’t be added. Try again.")
                    }
                }
            }
        }
    }

    var pendingEditorImports: [PendingEditorSourcePlacement] {
        guard let uploads = mediaUploads, let itemID, let variantKey else { return [] }
        return uploads.editorPlacements(itemID: itemID, variantID: variantKey, baseGeneration: document.revision.baseGeneration)
    }

    func retryPendingEditorImport(_ placement: PendingEditorSourcePlacement) async {
        guard let uploads = mediaUploads else { return }
        await uploads.retryEditorPlacement(placement)
        // Keep the existing waiter token; it will consume the freshly posted
        // response without concurrent placement work.
        if !pendingPlacementIDs.contains(placement.id) { await resumePendingEditorPlacements() }
    }

    func dismissPendingEditorImport(_ placement: PendingEditorSourcePlacement) async {
        await mediaUploads?.discardEditorPlacement(placement.id)
        if placement.lane == .timeline { addClipError = nil } else { visualError = nil }
    }

    /// Device renders retain a local footage original and send only its
    /// analysis proxy through the existing project reservation.  Completion is
    /// admitted to this variant, never attached to its creation thread.
    func addDeviceTimelineMedia(fileURL: URL, source: UploadSource, alreadyPreparing: Bool = false) async {
        guard canAddTimelineMedia, let uploads = mediaUploads else {
            addClipError = "Adding media isn’t available for this edit right now."
            return
        }
        guard !isAddingClip || alreadyPreparing else { return }
        if !alreadyPreparing { isAddingClip = true }
        addClipError = nil
        defer { if !alreadyPreparing { isAddingClip = false } }
        var importID: UUID?
        do {
            let values = try fileURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            let isImage = contentType.hasPrefix("image/") || NativeDownloadedMedia.isStillImage(fileURL)
            let kind: EditorSourceRegistrationTarget.SourceKind = isImage ? .visual : .footage
            let localDuration: Double?
            if kind == .footage {
                let value = try await AVURLAsset(url: fileURL).load(.duration).seconds
                localDuration = value.isFinite && value > 0 ? value : nil
            } else { localDuration = nil }
            guard kind == .footage || phoneAllowsVisualKind("image") else { throw APIError.unsupported }
            let target = editorSourceTarget(kind: kind)!
            let recordID = UUID()
            let placement = PendingEditorSourcePlacement(id: recordID, target: target, lane: .timeline, visual: nil,
                                                         localDurationS: localDuration)
            uploads.beginEditorPlacement(placement)
            importID = placement.id
            pendingPlacementIDs.insert(placement.id)
            defer { pendingPlacementIDs.remove(placement.id) }
            let accepted = await uploads.enqueue(fileURL: fileURL, projectID: threadID ?? projectID, source: source,
                consentGiven: true, purpose: kind == .footage ? .analysisProxy : .cloudRenderSource,
                role: kind == .footage ? .clip : .visual, itemID: itemID, editorSourceTarget: target, recordID: recordID)
            guard accepted else { throw APIError.invalidResponse }
            let response = try await waitForEditorSource(target, recordID: recordID, uploads: uploads)
            guard response.status == "ready", response.sourceIndex != nil,
                  document.revision.baseGeneration == target.baseGeneration else {
                throw APIError.invalidResponse
            }
            // The server normally returns the exact probe duration. Keep the
            // local original's duration as a conservative fallback so a short
            // video never receives a 3s held tail before the refreshed pool
            // arrives.
            // The service validates against the retained original with a
            // 50-ms EOF margin. A proxy may be fractionally longer, so use
            // the shorter measured duration and reserve that same margin
            // before proposing the timeline slot.
            try await placeEditorSource(placement, response: response)
        } catch {
            guard editorImportsActive, !(error is CancellationError) else { return }
            if let importID { uploads.failEditorPlacement(importID, error: "This media couldn’t be added. Try again.") }
            addClipError = "This media couldn’t be added. Your edit is unchanged. " + error.localizedDescription
        }
    }

    func addLibraryVisual(_ asset: CreationVisual) async {
        if rendersOnDevice {
            await admitDeviceVisual(asset)
            return
        }
        guard canAuthorVisuals, !isAddingVisual, document.visualBlocks.count < 20,
              let window = NativeVisualAuthoring.window(at: currentTime, duration: duration, projection: timelineProjection,
                preferred: min(3, asset.kind == "video" ? asset.durationS ?? 0 : 3)),
              let block = NativeVisualAuthoring.media(asset: asset, start: window.start, end: window.end,
                z: Int(document.visualBlocks.compactMap { $0.raw["z"]?.numberValue }.max() ?? 0) + 1) else {
            visualError = "This visual isn’t ready, or there isn’t enough room at the playhead."
            return
        }
        isAddingVisual = true
        let generation = document.revision.baseGeneration
        defer { isAddingVisual = false }
        do {
            guard let sourceResolver, let url = asset.originalMediaURL else { throw MediaEngineError.missingAsset(asset.id) }
            let source = try await sourceResolver.resolveMedia(id: asset.id, url: url, generation: generation)
            guard !Task.isCancelled, generation == document.revision.baseGeneration, canAuthorVisuals else { return }
            authoredVisualSources["visual:" + block.id + ":" + block.id] = source
            transactDocument(section: .visualBlocks) { $0.visualBlocks.append(block) }
            visualError = nil
            select(.init(kind: .visualBlock, id: block.id))
        } catch { visualError = "This visual couldn’t be opened. Your edit is unchanged. " + error.localizedDescription }
    }

    /// Visual-pool bytes are already registered by the shared background
    /// uploader.  Device editing still needs an explicit variant admission
    /// before a media block can reference them; posting it here keeps a pool
    /// upload from changing an approved edit until the creator places it.
    private func admitDeviceVisual(_ asset: CreationVisual) async {
        guard canImportVisuals, !isAddingVisual, document.visualBlocks.count < 20, asset.status == "ready",
              phoneAllowsVisualKind(asset.kind), let target = editorSourceTarget(kind: .visual),
              let api else {
            visualError = "This visual isn’t available for this edit on this iPhone."
            return
        }
        isAddingVisual = true
        defer { isAddingVisual = false }
        var importID: UUID?
        do {
            let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .visual, visual: asset, localDurationS: nil)
            importID = placement.id
            mediaUploads?.beginEditorPlacement(placement)
            pendingPlacementIDs.insert(placement.id)
            defer { pendingPlacementIDs.remove(placement.id) }
            var response = try await api.registerEditorSource(target, sourceID: asset.id)
            mediaUploads?.recordEditorSourceResult(placementID: placement.id, response: response)
            for _ in 0..<420 where response.status == "preparing" {
                guard editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true, !Task.isCancelled else { throw CancellationError() }
                try await Task.sleep(for: .seconds(1))
                response = try await api.editorSource(itemID: target.itemID, variantID: target.variantID, importID: target.clientImportID)
                mediaUploads?.recordEditorSourceResult(placementID: placement.id, response: response)
            }
            guard response.status == "ready", document.revision.baseGeneration == target.baseGeneration else {
                throw APIError.invalidResponse
            }
            guard !Task.isCancelled, document.revision.baseGeneration == target.baseGeneration else { return }
            try await placeEditorSource(placement, response: response)
            visualError = nil
        } catch {
            guard editorImportsActive, !(error is CancellationError) else { return }
            if let importID { mediaUploads?.failEditorPlacement(importID, error: "This visual couldn’t be added. Try again.") }
            visualError = "This visual couldn’t be added. Your edit is unchanged. " + error.localizedDescription
        }
    }

    @discardableResult func addTextCard(text: String, bold: Bool) -> EditorSelection? {
        guard canAuthorVisuals, canEditSection(.text), document.visualBlocks.count < 20,
              let window = NativeVisualAuthoring.window(at: currentTime, duration: duration, projection: timelineProjection),
              let (block, element) = NativeVisualAuthoring.card(text: text, bold: bold, start: window.start, end: window.end) else {
            visualError = "Enter some text and move the playhead inside the edit."
            return nil
        }
        transactDocument(sections: [.visualBlocks, .text]) {
            $0.visualBlocks.append(block); $0.textElements.append(element)
        }
        let selected = EditorSelection(kind: .visualBlock, id: block.id)
        select(selected)
        return selected
    }

    func addMotionComposition(preset: String, assets: [CreationVisual]) async {
        guard canEditSection(.motionScenes), !runtimeMismatch(), !isAddingVisual, document.motionScenes.count < 12,
              let hash = Self.object(previewVariant["editor_capabilities"])?["motion_runtime_hash"]?.stringValue ?? document.motionRuntimeHash,
              let window = NativeVisualAuthoring.window(at: currentTime, duration: duration, projection: timelineProjection),
              let scene = NativeVisualAuthoring.motion(preset: preset, assets: assets, start: window.start, end: window.end) else {
            visualError = "Choose the required ready photos and move the playhead inside the edit."
            return
        }
        let generation = document.revision.baseGeneration
        isAddingVisual = true
        defer { isAddingVisual = false }
        do {
            guard let sourceResolver else { throw APIError.unsupported }
            var sources: [String: ResolvedEditorSource] = [:]
            for asset in assets {
                guard let url = asset.originalMediaURL else { throw MediaEngineError.missingAsset(asset.id) }
                sources["motion:" + asset.id] = try await sourceResolver.resolveMedia(id: asset.id, url: url, generation: generation)
            }
            guard !Task.isCancelled, generation == document.revision.baseGeneration else { return }
            authoredVisualSources.merge(sources) { _, new in new }
            transactDocument(section: .motionScenes) { $0.motionScenes.append(scene); $0.motionRuntimeHash = hash }
            visualError = nil
            select(.init(kind: .motionScene, id: scene.id))
        } catch { visualError = "The composition couldn’t be opened. " + error.localizedDescription }
    }

    func addCameraPulse(easing: String = CameraEmphasis.pulseEasing, intensity: Double? = nil) {
        let resolved = CameraEmphasis.resolve(easing)
        let bounds = CameraEmphasis.bounds(resolved)
        let strength = intensity ?? bounds.defaultIntensity
        guard strength.isFinite, canEditSection(.cameraEffects),
              let window = NativeVisualAuthoring.window(at: currentTime, duration: duration, projection: timelineProjection, preferred: bounds.defaultDuration, minimum: bounds.minDuration) else {
            visualError = "\(CameraEmphasis.label(resolved)) isn’t available at this position in the edit."
            return
        }
        let effect = EditorCameraEffect(id: UUID().uuidString, startS: window.start, endS: window.end, effect: CameraEmphasis.token,
            raw: ["token": .string(CameraEmphasis.token), "intensity": .number(min(CameraEmphasis.maxIntensity, max(0.01, strength))), "easing": .string(resolved), "source": .string("user")])
        transactDocument(section: .cameraEffects) { $0.cameraEffects.append(effect) }
        select(.init(kind: .cameraEffect, id: effect.id))
    }

    /// Places a catalog sound effect at the playhead as a point `sfx` lane
    /// item. The `raw` shape matches the web editor's `addSfxFromGlossary`
    /// exactly (`id`/`sound_effect_id`/`src_gcs_path`/`source`/`at_s`/`gain`/
    /// `duration_s`/`label`) so `resolve_editor_sound_effect_placements`
    /// resolves the catalog id server-side on Save.
    func addSoundEffect(_ effect: NativeEditorSoundEffect) {
        guard canEditOperation(["lanes.sfx.add", "sfx.add", "sound_effects.add", "lanes.sfx"], section: .soundEffects) else { return }
        // KRI-169: the editable end is the LAST EDITABLE CLIP's end, not
        // `duration` -- a device-rendered composition's `duration` can
        // already include the baked Kria outro tail (the same boundary the
        // "+" lane-end appends use, see NativeEditorMediaViews.lastClipEnd).
        // Fall back to `duration` when no clips are known yet.
        let editableEnd = timelineClips.map(\.end).max() ?? duration
        let at = min(max(0, currentTime), max(0, editableEnd - 0.1))
        let durationS = effect.durationS
        let item = EditorTimedEffect(id: UUID().uuidString, startS: at, endS: at + max(0.1, durationS ?? 0.1), pointS: at, kind: "sfx",
            raw: ["sound_effect_id": .string(effect.id), "src_gcs_path": .string(""), "source": .string("user"),
                  "gain": .number(1), "duration_s": durationS.map(JSONValue.number) ?? .null, "label": .string(effect.name)])
        transactDocument(section: .soundEffects) { $0.soundEffects.append(item) }
        select(.init(kind: .soundEffect, id: item.id))
    }

    /// Loads (and caches for the session) the `GET /sound-effects` catalog
    /// the Effects tab browses. A cached, non-empty result returns
    /// immediately; call sites that need a hard refresh are not expected
    /// today (the catalog rarely changes mid-session).
    @discardableResult
    func loadSoundEffectCatalog() async -> [NativeEditorSoundEffect] {
        guard let api else { return soundEffectCatalog }
        guard soundEffectCatalog.isEmpty, !soundEffectCatalogLoading else { return soundEffectCatalog }
        soundEffectCatalogLoading = true
        defer { soundEffectCatalogLoading = false }
        do {
            soundEffectCatalog = try await api.editorSoundEffects()
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("sound-effect-catalog-failed", error: error)
            #endif
        }
        return soundEffectCatalog
    }

    /// Easing of one effect, for callers that clamp timing against its bounds.
    func cameraEffectEasing(id: String) -> String {
        CameraEmphasis.resolve(document.cameraEffects.first { $0.id == id }?.raw["easing"]?.stringValue)
    }

    func visualRaw(_ selection: EditorSelection) -> [String: JSONValue]? {
        switch selection.kind {
        case .mediaOverlay: return document.mediaOverlays.first { $0.id == selection.id }?.raw
        case .visualBlock: return document.visualBlocks.first { $0.id == selection.id }?.raw
        case .motionScene: return document.motionScenes.first { $0.id == selection.id }?.raw
        case .cameraEffect: return document.cameraEffects.first { $0.id == selection.id }?.raw
        default: return nil
        }
    }

    func setVisualEditorStyle(_ selected: EditorSelection, key: String, value: JSONValue, animationPhase: String? = nil) {
        guard canEdit("visual_editor_style"), let raw = visualRaw(selected) else { return }
        var style = NativeVisualAuthoring.style(for: raw)
        style[key] = value
        guard (try? NativeEditorRenderCompiler.visualEditorStyle(.object(style))) != nil else { return }
        switch selected.kind {
        case .mediaOverlay:
            mutateTimedEffect(kind: .mediaOverlay, id: selected.id, section: .mediaOverlays, operationKeys: ["media_overlays", "overlays"]) {
                $0.raw["editor_style"] = .object(style)
                if key == "animation" {
                    if animationPhase == nil || animationPhase == "entrance" { $0.raw["entrance_token"] = .string("none") }
                    if animationPhase == nil || animationPhase == "exit" { $0.raw["exit_token"] = .string("none") }
                }
            }
        case .visualBlock:
            mutateVisualBlock(id: selected.id, operationKeys: ["visual_blocks"]) {
                $0.raw["editor_style"] = .object(style)
                if key == "animation" {
                    if animationPhase == nil || animationPhase == "entrance" { $0.raw["transition_in"] = .string("cut") }
                    if animationPhase == nil || animationPhase == "exit" { $0.raw["transition_out"] = .string("cut") }
                }
                if key == "fit_mode" || key == "zoom" {
                    var transform = $0.raw["transform"]?.objectValue ?? [:]
                    transform[key] = value
                    $0.raw["transform"] = .object(transform)
                }
            }
        default: break
        }
    }

    /// Renderer passes are fixed; z orders peer media within the same pass.
    func visualLayerSelections(for selected: EditorSelection) -> [EditorSelection] {
        if selected.kind == .mediaOverlay {
            return document.mediaOverlays.sorted { ($0.raw["z"]?.numberValue ?? 0) < ($1.raw["z"]?.numberValue ?? 0) }
                .map { .init(kind: .mediaOverlay, id: $0.id) }
        }
        if selected.kind == .visualBlock {
            return document.visualBlocks.filter { $0.kind == "media" }.sorted {
                let a = $0.raw["z"]?.numberValue ?? 0, b = $1.raw["z"]?.numberValue ?? 0
                return a == b ? ($0.startS == $1.startS ? $0.id < $1.id : $0.startS < $1.startS) : a < b
            }.map { .init(kind: .visualBlock, id: $0.id) }
        }
        return []
    }

    func moveVisualLayer(_ selected: EditorSelection, by offset: Int) {
        guard let section = section(for: selected.kind), canEditSection(section) else { return }
        var peers = visualLayerSelections(for: selected)
        guard let current = peers.firstIndex(of: selected) else { return }
        let target = min(max(0, current + offset), peers.count - 1)
        guard current != target else { return }
        peers.remove(at: current); peers.insert(selected, at: target)
        transactDocument(section: section) { document in
            for (z, peer) in peers.enumerated() { setLayerZ(peer, z: Double(z), in: &document) }
        }
    }

    func canAdjustMotionSpeed(id: String) -> Bool {
        guard canEditMotionScene(id: id, keys: ["motion_scenes"]),
              let scene = document.motionScenes.first(where: { $0.id == id }) else { return false }
        return scene.raw["preset_version"] == .number(2) && scene.raw["motion"]?.objectValue?["version"] == .number(2)
    }

    func setMotionSpeed(id: String, speed: Double) {
        guard speed.isFinite, canAdjustMotionSpeed(id: id),
              let index = document.motionScenes.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .motionScenes) {
            var motion = $0.motionScenes[index].raw["motion"]?.objectValue ?? [:]
            motion["speed"] = .number((min(4, max(0.5, speed)) * 20).rounded() / 20)
            $0.motionScenes[index].raw["motion"] = .object(motion)
        }
    }

    func setVisualTiming(_ selected: EditorSelection, outputTime: Double, isStart: Bool) {
        guard outputTime.isFinite, let bounds = timedBounds(selected, in: document) else { return }
        let total = timelineProjection.unprojectOutputTime(duration)
        let minimum = selected.kind == .cameraEffect
            ? CameraEmphasis.bounds(cameraEffectEasing(id: selected.id)).minDuration
            : selected.kind == .motionScene ? 1.0 / 30 : minimumClipDuration
        let value = timelineProjection.unprojectOutputTime(min(duration, max(0, outputTime)))
        let start = isStart ? min(max(0, value), max(0, min(total, bounds.end) - minimum)) : bounds.start
        let end = isStart ? min(total, bounds.end) : min(total, max(start + minimum, value))
        guard end > start else { return }
        switch selected.kind {
        case .mediaOverlay: setMediaOverlayTiming(id: selected.id, startS: start, endS: end)
        case .visualBlock: setVisualBlockTiming(id: selected.id, startS: start, endS: end)
        case .motionScene: setMotionSceneTiming(id: selected.id, startS: start, endS: end)
        case .cameraEffect: setCameraEffectTiming(id: selected.id, startS: start, endS: end)
        default: break
        }
    }

    /// Whether a standalone text bar may be deleted from the editor, and why not.
    /// Caption cues belong to the Captions panel; bars linked to a card go with the
    /// card; lyric lines are generated. Everything else (title, clip labels, closing,
    /// the creator's own text) drops out of `text_elements`, which the server treats
    /// as a user deletion (full-replacement; guided tombstone `user_removed`).
    enum TextDeletion: Equatable {
        case allowed
        case blocked(String)
        var isAllowed: Bool { self == .allowed }
    }

    func textDeletion(id: String) -> TextDeletion {
        guard document.textElements.contains(where: { $0.id == id }) else { return .blocked("This text no longer exists.") }
        return .allowed
    }

    /// One undo step; the live preview recompiles through the document change.
    @discardableResult
    func deleteText(id: String) -> Bool {
        deleteSelection(EditorSelection(kind: .text, id: id))
    }

    func removeVisualSelection(_ selected: EditorSelection) {
        _ = deleteSelection(selected)
    }

    /// The only destructive editor operation.  It owns the mapping from a
    /// visual selection to the server's stable deletion identity so all
    /// context strips, inspectors and lane panels agree on one undo step.
    @discardableResult
    func deleteSelection(_ selected: EditorSelection) -> Bool {
        func append(_ deletion: EditorDeletion, to document: inout EditorDocument) {
            guard !document.deletions.contains(deletion) else { return }
            document.deletions.append(deletion)
        }
        func appendIfPersisted(_ deletion: EditorDeletion, selection: EditorSelection, to document: inout EditorDocument) {
            // A locally-added record has no server-side predecessor to suppress.
            // Keep the removal in this in-memory transaction, but don't tell the
            // server to delete an object that it never knew about.
            guard isBaselineSelection(selection) else { return }
            append(deletion, to: &document)
        }
        switch selected.kind {
        case .clip:
            guard let index = clipSlotIndex(selected.id) else { return false }
            let slotID = document.clips[index].id ?? selected.id
            transactDocument(section: .timeline) { document in
                appendIfPersisted(EditorDeletion(kind: "clip", id: slotID), selection: EditorSelection(kind: .clip, id: slotID), to: &document)
                if document.clips.count == 1 {
                    // An empty timeline is intentional and must survive
                    // re-open/hydration without recreating a composite clip.
                    document.clips.removeAll()
                    document.tombstones.removeAll()
                    document.editorState = "empty"
                } else {
                    var removed = document.clips.remove(at: index)
                    removed.removed = true
                    document.tombstones.append(removed)
                }
            }
        case .text:
            guard let element = document.textElements.first(where: { $0.id == selected.id }) else { return false }
            let lyricID = lyricDeletionID(for: element)
            let deletion = EditorDeletion(kind: element.isCaption ? "caption_cue" : (lyricID == nil ? "text" : "lyric_line"), id: lyricID ?? element.id)
            let sections: Set<EditorSection> = element.isCaption ? [.text, .captions] : [.text]
            transactDocument(sections: sections) { document in
                document.textElements.removeAll { $0.id == selected.id }
                if element.isCaption { document.captionCues.removeAll { $0.id == selected.id } }
                if let blockID = element.raw["visual_block_id"]?.stringValue {
                    detachTextReference(selected.id, fromVisualBlock: blockID, in: &document)
                }
                appendIfPersisted(deletion, selection: selected, to: &document)
            }
        case .captionCue:
            transactDocument(sections: [.captions, .text]) { document in
                let existed = document.captionCues.contains { $0.id == selected.id }
                    || document.textElements.contains { $0.id == selected.id && $0.isCaption }
                guard existed else { return }
                document.captionCues.removeAll { $0.id == selected.id }
                document.textElements.removeAll { $0.id == selected.id && $0.isCaption }
                appendIfPersisted(EditorDeletion(kind: "caption_cue", id: selected.id), selection: selected, to: &document)
            }
        case .music:
            guard document.music != nil else { return false }
            transactDocument(section: .music) { document in
                appendIfPersisted(EditorDeletion(kind: "music", id: document.music?.trackID ?? selected.id), selection: selected, to: &document)
                document.music = nil
            }
        case .soundEffect:
            guard document.soundEffects.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(section: .soundEffects) { document in
                document.soundEffects.removeAll { $0.id == selected.id }
                appendIfPersisted(EditorDeletion(kind: "sound_effect", id: selected.id), selection: selected, to: &document)
            }
        case .mediaOverlay:
            guard document.mediaOverlays.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(section: .mediaOverlays) { document in
                document.mediaOverlays.removeAll { $0.id == selected.id }
                appendIfPersisted(EditorDeletion(kind: "media_overlay", id: selected.id), selection: selected, to: &document)
            }
        case .visualBlock:
            guard document.visualBlocks.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(sections: [.visualBlocks, .text]) { document in
                document.visualBlocks.removeAll { $0.id == selected.id }
                let linked = document.textElements.filter { $0.raw["visual_block_id"] == .string(selected.id) }
                document.textElements.removeAll { $0.raw["visual_block_id"] == .string(selected.id) }
                appendIfPersisted(EditorDeletion(kind: "visual_block", id: selected.id), selection: selected, to: &document)
                for text in linked {
                    let textSelection = EditorSelection(kind: .text, id: text.id)
                    appendIfPersisted(EditorDeletion(kind: "text", id: text.id), selection: textSelection, to: &document)
                }
            }
        case .motionScene:
            guard document.motionScenes.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(section: .motionScenes) { document in
                document.motionScenes.removeAll { $0.id == selected.id }
                appendIfPersisted(EditorDeletion(kind: "motion_scene", id: selected.id), selection: selected, to: &document)
            }
        case .cameraEffect:
            guard document.cameraEffects.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(section: .cameraEffects) { document in
                document.cameraEffects.removeAll { $0.id == selected.id }
                appendIfPersisted(EditorDeletion(kind: "camera_effect", id: selected.id), selection: selected, to: &document)
            }
        case .carousel:
            guard document.carouselMoment != nil else { return false }
            transactDocument(section: .carouselMoment) { document in
                document.carouselMoment = nil
                appendIfPersisted(EditorDeletion(kind: "carousel", id: selected.id), selection: selected, to: &document)
            }
        }
        select(nil)
        return true
    }

    private func isBaselineSelection(_ selection: EditorSelection) -> Bool {
        switch selection.kind {
        case .clip: return cleanDocument.clips.contains { $0.id == selection.id }
        case .text: return cleanDocument.textElements.contains { $0.id == selection.id }
        case .captionCue:
            return cleanDocument.captionCues.contains { $0.id == selection.id }
                || cleanDocument.textElements.contains { $0.id == selection.id && $0.isCaption }
        case .music: return cleanDocument.music?.trackID == selection.id
        case .soundEffect: return cleanDocument.soundEffects.contains { $0.id == selection.id }
        case .mediaOverlay: return cleanDocument.mediaOverlays.contains { $0.id == selection.id }
        case .visualBlock: return cleanDocument.visualBlocks.contains { $0.id == selection.id }
        case .motionScene: return cleanDocument.motionScenes.contains { $0.id == selection.id }
        case .cameraEffect: return cleanDocument.cameraEffects.contains { $0.id == selection.id }
        case .carousel: return cleanDocument.carouselMoment?["id"] == .string(selection.id)
        }
    }

    private func lyricDeletionID(for element: EditorTextElement) -> String? {
        guard element.role == "lyric_line" else { return nil }
        let metadata = element.raw["source_params"]?.objectValue ?? element.raw
        func canonicalID(_ value: String) -> String? {
            let unprefixed = value.hasPrefix("lyric_") ? String(value.dropFirst("lyric_".count)) : value
            if unprefixed.hasPrefix("L"), unprefixed.count > 1 { return unprefixed }
            if let index = Int(unprefixed) { return "L\(index)" }
            return nil
        }
        // The backend snapshot contract is source_params.key = "L7" with a
        // display element id such as "lyric_L7". Prefer the source key over
        // the UI id so a delete persists as the server's lyric suppression.
        if let key = metadata["key"]?.stringValue, let lyricID = canonicalID(key) { return lyricID }
        if let lyricID = canonicalID(element.id) { return lyricID }
        for key in ["absolute_index", "absolute_line_index", "line_index"] {
            if let value = metadata[key]?.numberValue, value.isFinite, value.rounded() == value {
                return "L\(Int(value))"
            }
        }
        return nil
    }

    private func detachTextReference(_ textID: String, fromVisualBlock blockID: String, in document: inout EditorDocument) {
        guard let index = document.visualBlocks.firstIndex(where: { $0.id == blockID }) else { return }
        for key in ["text_element_id", "linked_text_id"] where document.visualBlocks[index].raw[key] == .string(textID) {
            document.visualBlocks[index].raw.removeValue(forKey: key)
        }
        if case let .array(ids)? = document.visualBlocks[index].raw["text_element_ids"] {
            document.visualBlocks[index].raw["text_element_ids"] = .array(ids.filter { $0 != .string(textID) })
        }
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
    var canEditCaptionAppearance: Bool { canEditCaptions && canEdit("caption_editor_style") }
    /// Line-level caption edits (text/timing of individual cues) are gated by
    /// the `caption_cues` capability specifically (KRI-216) — a phone
    /// subtitled variant can have cues editable while style/settings
    /// (`caption_meta`) stay locked, or vice versa. Falls back to the
    /// coarse `canEditCaptions` when the server hasn't sent a `caption_cues`
    /// key (older servers, or the guided-story text-lane path).
    var canEditCaptionLines: Bool { canEditSection(.captions) }
    /// Caption on/off, style, font, color and size writes land in
    /// `caption_meta`, so their controls are gated by the `caption_meta`
    /// capability (KRI-216) — the same guard `setCaptionEnabled`,
    /// `toggleCaptions`, `setCaptionStyle`, `setCaptionFont` and
    /// `setCaptionMeta` apply. Unlike `canEditCaptionAppearance`, this does not
    /// also require `caption_editor_style`, which only the `appearance` keys
    /// need. Falls back to `canEditCaptions` when the server hasn't sent the
    /// key (older servers, deterministic UI fixtures).
    var canEditCaptionMeta: Bool { canEditCaptions && canEditSection(.captionMeta) }

    func setCaptionAppearance(key: String, value: JSONValue) {
        guard canEditCaptionAppearance else { return }
        switch (key, value) {
        case ("alignment", .string(let value)) where ["left", "center", "right"].contains(value): break
        case ("stroke_color", .string(let value)), ("shadow_color", .string(let value)):
            guard value.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) != nil else { return }
        case ("shadow_opacity", .number(let value)) where value.isFinite && (0...1).contains(value): break
        case ("highlight_spoken_word", .bool): break
        default: return
        }
        transactDocument(section: .captionMeta) {
            var appearance = $0.captionMeta["appearance"]?.objectValue ?? [:]
            appearance[key] = value
            $0.captionMeta["appearance"] = .object(appearance)
        }
    }

    /// Opening a new display mode explicitly decouples display and highlighting.
    func setCaptionDisplay(_ style: String) {
        guard canEditCaptionAppearance, ["sentence", "word"].contains(style) else { return }
        transactDocument(section: .captionMeta) {
            var appearance = $0.captionMeta["appearance"]?.objectValue ?? [:]
            if appearance["highlight_spoken_word"] == nil { appearance["highlight_spoken_word"] = .bool(false) }
            $0.captionMeta["appearance"] = .object(appearance)
            $0.captionMeta["style"] = .string(style)
        }
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
        if let index = document.textElements.firstIndex(where: { $0.id == id && $0.isCaption }) {
            // Guided-story captions (KRI-110) are pinned to the approved
            // narration: timing and identity are server-owned (see
            // guided_story.py's narration merge), so only display text is
            // editor-owned here — mirrors the web's timing lock on narration
            // captions (KRI-18). They persist through `text_elements`, not
            // `caption_cues` — the guided commit path hard-rejects the latter.
            guard let text else { return }
            transactDocument(section: .text) { $0.textElements[index].text = text }
            return
        }
        guard let index = document.captionCues.firstIndex(where: { $0.id == id }) else { return }
        // KRI-240: keep `raw["words"]` spelling the text (see CaptionWordRewrite).
        // While the line is open in the caption editor the rewrite starts from the
        // words captured on entry; one-shot callers start from the current words.
        let current = document.captionCues[index]
        let entry = captionLineEntry?.id == id ? captionLineEntry : (id, current.text, current.raw["words"]?.arrayValue)
        transactDocument(section: .captions) { doc in
            var cue = doc.captionCues[index]
            if let text { cue.text = text }; if let startS { cue.startS = startS }; if let endS { cue.endS = max(cue.startS, endS) }
            if text != nil, let entry,
               let words = CaptionWordRewrite.words(entryText: entry.text, entryWords: entry.words, text: cue.text,
                                                    startS: cue.startS, endS: cue.endS) {
                cue.raw["words"] = .array(words)
            }
            doc.captionCues[index] = cue
        }
    }
    func updateCaptionCue(id: UUID, text: String? = nil, startS: Double? = nil, endS: Double? = nil) { updateCaptionCue(id: id.uuidString, text: text, startS: startS, endS: endS) }

    // MARK: - KRI-240 caption line editing

    /// The caption line open in the caption editor: its text and word list on entry.
    private var captionLineEntry: (id: String, text: String, words: [JSONValue]?)?

    /// Opens one undo transaction for a caption line and remembers its entry state,
    /// so returning the text to what it was restores the original word timings and
    /// leaves no undo step or unsaved dot behind.
    func beginCaptionLineEdit(id: String) {
        beginTransaction()
        if let cue = document.captionCues.first(where: { $0.id == id }) {
            captionLineEntry = (cue.id, cue.text, cue.raw["words"]?.arrayValue)
        } else {
            captionLineEntry = nil
        }
    }

    func endCaptionLineEdit() {
        captionLineEntry = nil
        endTransaction()
    }

    /// Where the preview parks for a caption line: 0.15s after it starts (word
    /// styles: after its last word starts), clamped inside the line. Captions
    /// pop in over 0.12s, so the exact start is a blank frame (plan 026 R5).
    func captionParkTime(id: String) -> TimeInterval? {
        guard let unit = document.captionUnits.first(where: { $0.id == id }) else { return nil }
        var anchor = unit.startS
        if document.captionMeta["style"]?.stringValue == "word",
           let last = unit.raw["words"]?.arrayValue?.last?.objectValue,
           case .number(let start)? = last["start_s"] {
            anchor = max(unit.startS, start)
        }
        let latest = max(unit.startS, unit.endS - 1.0 / 30)
        return timelineProjection.projectBaseTime(min(max(unit.startS, anchor + 0.15), latest))
    }

    /// The line's window on the timeline, for loop-play.
    func captionTimelineRange(id: String) -> ClosedRange<TimeInterval>? {
        guard let unit = document.captionUnits.first(where: { $0.id == id }) else { return nil }
        let start = timelineProjection.projectBaseTime(unit.startS)
        let end = timelineProjection.projectBaseTime(unit.endS)
        return end > start ? start...end : nil
    }

    /// Every caption line's timeline range from one read of the document, for callers
    /// that scan all lines (the caption panel's playing-line lookup runs per clock tick).
    func captionTimelineRanges() -> [(id: String, range: ClosedRange<TimeInterval>)] {
        document.captionUnits.compactMap { unit in
            let start = timelineProjection.projectBaseTime(unit.startS)
            let end = timelineProjection.projectBaseTime(unit.endS)
            return end > start ? (unit.id, start...end) : nil
        }
    }

    /// True when the line's text differs from the last saved version (unsaved dot).
    func isCaptionUnitEdited(id: String) -> Bool {
        document.captionUnits.first { $0.id == id }?.text != cleanDocument.captionUnits.first { $0.id == id }?.text
    }

    /// The language the captions were transcribed in (`caption_language` on the
    /// loaded variant), e.g. "tr". `nil` for variants without it.
    var captionLanguage: String? {
        previewVariant["caption_language"]?.stringValue?.nilIfEmpty
    }

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
        _ = deleteSelection(EditorSelection(kind: .soundEffect, id: id))
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
        _ = deleteSelection(EditorSelection(kind: .mediaOverlay, id: id))
    }

    func setVisualBlockTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditOperation(["lanes.visual_blocks.timing", "visual_blocks.timing", "lanes.visual_blocks"], section: .visualBlocks),
              let index = document.visualBlocks.firstIndex(where: { $0.id == id }),
              document.visualBlocks[index].kind != "montage" else { return }
        transactDocument(sections: document.visualBlocks[index].kind == "text_card" ? [.visualBlocks, .text] : [.visualBlocks]) { document in
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
            Self.syncCardTextTiming(value, in: &document)
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
        _ = deleteSelection(EditorSelection(kind: .visualBlock, id: id))
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
            let clamped = CameraEmphasis.clampWindow(
                start: value.startS, end: value.endS, easing: value.raw["easing"]?.stringValue
            )
            value.startS = clamped.0
            value.endS = clamped.1
            document.cameraEffects[index] = value
        }
    }
    func setCameraEffectIntensity(id: String, intensity: Double) {
        mutateCameraEffect(id: id, keys: ["camera_effects.intensity", "lanes.camera_effects.intensity", "camera_effects"]) { $0.raw["intensity"] = .number(min(max(0, intensity), CameraEmphasis.maxIntensity)) }
    }
    /// Switching shape re-clamps the window: a hold may run longer than a pulse,
    /// so a long hold has to shrink when it becomes one.
    func setCameraEffectEasing(id: String, easing: String) {
        let resolved = CameraEmphasis.resolve(easing)
        mutateCameraEffect(id: id, keys: ["camera_effects.easing", "lanes.camera_effects.easing", "camera_effects"]) { effect in
            effect.raw["easing"] = .string(resolved)
            let clamped = CameraEmphasis.clampWindow(start: effect.startS, end: effect.endS, easing: resolved)
            effect.startS = clamped.0
            effect.endS = clamped.1
        }
    }

    func setCarouselMomentPosition(_ position: String) {
        guard canEditOperation(["carousel.position", "carousel", "carousel_moment"], section: .carouselMoment) else { return }
        transactDocument(section: .carouselMoment) { doc in var value = doc.carouselMoment ?? [:]; value["position"] = .string(position); doc.carouselMoment = value }
    }
    func setCarouselPosition(_ position: String) { setCarouselMomentPosition(position) }
    func removeCarouselMoment() {
        let id = document.carouselMoment?["id"]?.stringValue ?? "carousel"
        _ = deleteSelection(EditorSelection(kind: .carousel, id: id))
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
        guard let trackID = document.music?.trackID else { return }
        _ = deleteSelection(EditorSelection(kind: .music, id: trackID))
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
        let saveItemID = itemID
        let saveJobID = jobID
        let saveVariantKey = variantKey
        let saveThreadID = threadID
        let saveDocumentGeneration = document.revision.baseGeneration
        let snapshot = document.encodeSnapshot()
        let payload = Self.object(snapshot["editor_payload"]); let sections = Self.object(payload?["sections"])
        let request = commitRequest(sections: sections, baseGeneration: payload?["base_generation"]?.stringValue ?? document.revision.baseGeneration)
        do {
            let response = try await api.editorCommit(itemID: itemID, variantID: variantKey, request: request)
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: saveDocumentGeneration) else { return }
            let acknowledged = acknowledgedSections(response.sections, submittedSections: submittedSections)
            let postSubmitUndo = Array(undoStack.dropFirst(submittedUndoCount))
            let hasPostSubmitEdits = document != submittedDocument
            // A durable commit fences the previous render even when this
            // response saves an empty draft and does not start a new poll.
            previewRefreshTask?.cancel()
            pendingPreviewGeneration = nil
            pendingDeviceRenderIdentity = nil
            chatStagedDocument = nil; chatStagedSections = []
            // An empty authored-phone document has its first editable revision
            // immediately, even before the next variant-status refresh exposes
            // `editor_revision_number`. Keep source registration available for
            // the first clip added after Save.
            guidedRevisionNumber = response.revisionNumber ?? guidedRevisionNumber
                ?? (response.editorState == "empty" && rendersOnDevice ? 1 : nil)
            document.revision.number = response.revisionNumber ?? document.revision.number
            document.revision.hash = response.revisionHash ?? document.revision.hash
            if response.editorState == "empty" {
                var acknowledgedEmpty = response.draft.map { EditorDocument(snapshot: $0.snapshot) } ?? submittedDocument
                acknowledgedEmpty.editorState = "empty"
                acknowledgedEmpty.revision.number = response.revisionNumber ?? acknowledgedEmpty.revision.number
                acknowledgedEmpty.revision.hash = response.revisionHash ?? acknowledgedEmpty.revision.hash
                acknowledgedEmpty.revision.baseGeneration = response.generation
                acknowledgedEmpty.deletions.removeAll()
                if !hasPostSubmitEdits {
                    document = acknowledgedEmpty
                    cleanDocument = acknowledgedEmpty
                    changedSections.removeAll(); explicitlyDirtySections.removeAll()
                    undoStack.removeAll(); redoStack.removeAll()
                } else {
                    // The empty commit is durable, but a later local edit was
                    // staged while it was in flight. Move both bases to the
                    // new generation and retain that edit and its undo entry.
                    document.revision.baseGeneration = response.generation
                    cleanDocument = acknowledgedEmpty
                    let acknowledgedRevision = document.revision
                    undoStack = postSubmitUndo.map { value in
                        var rebased = value
                        rebased.revision = acknowledgedRevision
                        return rebased
                    }
                    redoStack.removeAll()
                    if !document.clips.isEmpty { document.editorState = "renderable" }
                    consumeSubmittedDeletions(from: submittedDocument)
                    explicitlyDirtySections.subtract(acknowledged)
                }
                pendingRenderRetrySections.removeAll()
                rebaseActiveGestureHistoryAfterDurableSave()
                refreshDirtyState()
                saveState = .saved
                return
            }
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
            // A successful response has durably applied the exact deletion
            // intent sent with this commit. Remove only those IDs: a later
            // edit may have appended a different intent while awaiting it.
            consumeSubmittedDeletions(from: submittedDocument)
            // A durable save invalidates the pre-save gesture history too.
            // Rebase active gestures independently of read-after-write text
            // reconciliation, so a later zero-delta sample cannot pop an
            // undo entry that the save just cleared.
            rebaseActiveGestureHistoryAfterDurableSave()
            // The commit response acknowledges the submitted lanes, but the
            // renderer may project text differently (for example after a
            // timeline-only save). Reconcile the committed variant as soon as
            // it is durable; polling remains the fallback when this read is
            // unavailable or still stale.
            if let saveJobID,
               response.generation == document.revision.baseGeneration {
                do {
                    let committedVariant = try await api.editorVariant(jobID: saveJobID, variantID: saveVariantKey)
                    _ = reconcileAuthoritativeText(
                        from: committedVariant,
                        generation: response.generation,
                        expectedItemID: saveItemID,
                        expectedJobID: saveJobID,
                        expectedVariantKey: saveVariantKey,
                        expectedThreadID: saveThreadID,
                        expectedDocumentGeneration: response.generation
                    )
                } catch {
                    // A durable save must remain successful even when the
                    // read-after-write reconciliation is temporarily down.
                }
            }
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: response.generation) else { return }
            if response.ok {
                // The commit is durable even while its render is pending. Keep
                // the acknowledged sections retryable until a matching ready
                // generation is observed.
                pendingRenderRetrySections = acknowledged
                saveState = .previewPending
                await refreshDeviceRender()
                guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: response.generation) else { return }
                pendingDeviceRenderIdentity = deviceRenderKey.flatMap { deviceRenders?.request(for: $0)?.identity }
                startPreviewRefresh(generation: response.generation)
            } else {
                pendingRenderRetrySections = acknowledged
                saveState = .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely.")
            }
        } catch APIError.conflict {
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: saveDocumentGeneration) else { return }
            saveState = .conflict
        } catch {
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: saveDocumentGeneration) else { return }
            saveState = .failed(error.localizedDescription)
        }
    }

    private func rebaseActiveGestureHistoryAfterDurableSave() {
        if transactionBaseline != nil { transactionBaseline = document }
        if var activeTrim {
            activeTrim.baseline = document
            activeTrim.translationOrigin = activeTrim.lastTranslation
            activeTrim.redoBaseline = redoStack
            activeTrim.recordedUndo = false
            self.activeTrim = activeTrim
        }
        if var activeTimedEdit {
            activeTimedEdit.baseline = document
            activeTimedEdit.translationOrigin = activeTimedEdit.lastTranslation
            activeTimedEdit.projection = timelineProjection
            activeTimedEdit.redoBaseline = redoStack
            activeTimedEdit.recordedUndo = false
            self.activeTimedEdit = activeTimedEdit
        }
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
            if conversationRuntimeVersion == 1 { legacyPromptSnapshot = latestDraft.serverSnapshot }
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
            refreshRebasedSourcePreview()
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
        let before = document
        if transactionBaseline == nil { appendUndo(document); redoStack.removeAll() }
        replace(with: next)
        if section == .timeline { var rebased = document; rebaseGuidedLabels(&rebased, from: before); if rebased != document { document = rebased } }
        changedSections.insert(section); refreshDirtyState(); refreshDuration()
    }

    func transactDocument(section: EditorSection, _ body: (inout EditorDocument) -> Void) {
        transactDocument(sections: [section], body)
    }

    private func transactDocument(sections: Set<EditorSection>, _ body: (inout EditorDocument) -> Void) {
        var next = document; body(&next)
        if !next.clips.isEmpty { next.editorState = "renderable" }
        guard next != document else { return }
        rebaseGuidedLabels(&next, from: document)
        invalidateDurationSources(for: sections)
        if transactionBaseline == nil { appendUndo(document); redoStack.removeAll() }
        document = next; changedSections.formUnion(sections); refreshDirtyState(); refreshDuration()
    }

    func canEditOperation(_ keys: [String], section: EditorSection) -> Bool {
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
        case .captionCue: return .captions
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
        activeTimedEdit = ActiveTimedEdit(selection: selection, edge: edge, kind: kind, baseline: document, redoBaseline: redoStack, projection: timelineProjection)
    }

    private func updateTimedEdit(by translation: TimeInterval) {
        guard translation.isFinite, var active = activeTimedEdit else { return }
        active.lastTranslation = translation
        let translation = translation - active.translationOrigin
        var next = active.baseline
        guard let section = section(for: active.selection.kind) else { return }
        guard let bounds = timedBounds(active.selection, in: next) else { return }
        let minimum = minimumClipDuration
        let projection = active.projection
        let totalDuration = max(projection.unprojectOutputTime(projection.totalDuration), 0)
        var start = bounds.start; var end = bounds.end
        let anchor = active.edge == .trailing ? end : start
        let baseTranslation = projection.downstreamShift > 0
            ? ((projection.unprojectOutputTime(projection.projectBaseTime(anchor) + translation) - anchor) * 1_000).rounded() / 1_000
            : translation
        switch active.kind {
        case .move:
            let length = max(minimum, end - start)
            let maxStart = totalDuration > 0 ? max(0, totalDuration - length) : .greatestFiniteMagnitude
            start = min(max(0, start + baseTranslation), maxStart); end = start + length
        case .trim:
            guard let edge = active.edge else { return }
            if edge == .leading { start = min(max(0, start + baseTranslation), end - minimum) }
            else { end = max(start + minimum, min(totalDuration > 0 ? totalDuration : .greatestFiniteMagnitude, end + baseTranslation)) }
        }
        if active.kind == .trim, active.selection.kind == .soundEffect {
            setSoundEffectTrimBounds(active.selection.id, edge: active.edge ?? .trailing, translation: baseTranslation, in: &next)
        } else {
            setTimedBounds(active.selection, start: start, end: end, in: &next)
        }
        guard next != active.baseline else {
            if active.recordedUndo {
                undoStack.removeLast()
                redoStack = active.redoBaseline
                active.recordedUndo = false
            }
            document = next
            refreshDirtyState()
            refreshDuration()
            activeTimedEdit = active
            return
        }
        if !active.recordedUndo { appendUndo(active.baseline); redoStack.removeAll(); active.recordedUndo = true }
        document = next; changedSections.insert(section)
        if next.textElements != active.baseline.textElements { changedSections.insert(.text) }
        refreshDirtyState(); refreshDuration(); activeTimedEdit = active
    }

    private func endTimedEdit() {
        activeTimedEdit = nil
        flushDeferredSourcePreviewUpdate()
    }

    private func flushDeferredSourcePreviewUpdate() {
        if sourcePreviewUpdateDeferred { scheduleSourcePreviewUpdate() }
    }

    private func timedObjectExists(_ selection: EditorSelection) -> Bool { timedBounds(selection, in: document) != nil }

    private func timedBounds(_ selection: EditorSelection, in document: EditorDocument) -> (start: Double, end: Double)? {
        switch selection.kind {
        case .text: guard let item = document.textElements.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .captionCue: guard let item = document.captionCues.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .soundEffect: guard let item = document.soundEffects.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .mediaOverlay: guard let item = document.mediaOverlays.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .visualBlock: guard let item = document.visualBlocks.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .motionScene: guard let item = document.motionScenes.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        case .cameraEffect: guard let item = document.cameraEffects.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
        default: return nil
        }
    }

    private static func syncCardTextTiming(_ block: EditorVisualBlock, in document: inout EditorDocument) {
        guard block.kind == "text_card" else { return }
        for index in document.textElements.indices where document.textElements[index].raw["visual_block_id"] == .string(block.id) {
            document.textElements[index].startS = block.startS
            document.textElements[index].endS = block.endS
        }
    }

    private func setTimedBounds(_ selection: EditorSelection, start: Double, end: Double, in document: inout EditorDocument) {
        switch selection.kind {
        case .text:
            guard let index = document.textElements.firstIndex(where: { $0.id == selection.id }) else { return }
            document.textElements[index].startS = start; document.textElements[index].endS = end
        case .captionCue:
            guard let index = document.captionCues.firstIndex(where: { $0.id == selection.id }) else { return }
            document.captionCues[index].startS = start; document.captionCues[index].endS = end
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
            Self.syncCardTextTiming(value, in: &document)
        case .motionScene:
            guard let index = document.motionScenes.firstIndex(where: { $0.id == selection.id }) else { return }
            document.motionScenes[index].startS = Self.roundToMotionFrame(start)
            document.motionScenes[index].endS = Self.roundToMotionFrame(min(end, start + 8))
        case .cameraEffect:
            guard let index = document.cameraEffects.firstIndex(where: { $0.id == selection.id }) else { return }
            let clamped = CameraEmphasis.clampWindow(
                start: start, end: end, easing: document.cameraEffects[index].raw["easing"]?.stringValue
            )
            document.cameraEffects[index].startS = clamped.0
            document.cameraEffects[index].endS = clamped.1
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
        // KRI-216: line edits (caption_cues) and style/settings (caption_meta)
        // are separate server capabilities — `.captions` must prefer
        // `caption_cues` over the coarser legacy `captions` key so a phone
        // variant with cues editable but meta locked (or vice versa) gates
        // correctly. `.captionMeta`'s rawValue already equals `caption_meta`.
        case .captions: return ["caption_cues", "captions"]
        case .captionMeta: return ["caption_meta", "captions"]
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
        let isIntentionalEmpty = sourcePayload?["editor_state"]?.stringValue == "empty"
        if draft.text.isEmpty, sourceSections["text_elements"] != nil { sections["text_elements"] = sourceSections["text_elements"] }
        if draft.clips.isEmpty, !isIntentionalEmpty, sourceSections["timeline_slots"] != nil { sections["timeline_slots"] = sourceSections["timeline_slots"] }
        if draft.music == nil {
            if let value = sourceSections["music_track_id"] { sections["music_track_id"] = value }
            if let value = sourceSections["music_window"] { sections["music_window"] = value }
        }
        let rows = Self.array(sections["timeline_slots"])
        if !rows.isEmpty {
            sections["timeline_slots"] = .array(rows.enumerated().map { index, row in
                guard index < draft.clips.count, var value = Self.object(row) else { return row }
                let clip = draft.clips[index]
                value["asset_id"] = .string(clip.assetID.uuidString)
                // Do not manufacture an explicit unmute while hydrating a
                // legacy slot. The absence of this field carries the backend's
                // initial source-audio policy; explicit true or false is an
                // authoring choice and must survive the projection.
                if value["muted"] != nil || clip.muted { value["muted"] = .bool(clip.muted) }
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

    private var carriesGuidedLabelsWithTimeline: Bool {
        changedSections.contains(.timeline) && canEditSection(.text) && GuidedLabelRebase.hasLabels(document.textElements)
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
            // A guided timeline commit always carries the rebased label lane, like the server's
            // copilot path: the server then treats text as authored and skips its own
            // (right-biased, label-unaware) projection.
            textElements: changedSections.contains(.text) || carriesGuidedLabelsWithTimeline
                ? Self.array(value["text_elements"]) : nil,
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
            editorStateVersion: 1,
            deletions: document.deletions.isEmpty ? nil : document.deletions,
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

    private func consumeSubmittedDeletions(from submittedDocument: EditorDocument) {
        guard !submittedDocument.deletions.isEmpty else { return }
        document.deletions.removeAll { submittedDocument.deletions.contains($0) }
        cleanDocument.deletions.removeAll { submittedDocument.deletions.contains($0) }
        undoStack = undoStack.map { snapshot in
            var rebased = snapshot
            rebased.deletions.removeAll { submittedDocument.deletions.contains($0) }
            return rebased
        }
    }

    private func copy(_ section: EditorSection, from submitted: EditorDocument, into baseline: inout EditorDocument) {
        switch section {
        case .timeline:
            baseline.clips = submitted.clips
            baseline.tombstones = submitted.tombstones
            baseline.editorState = submitted.editorState
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
    /// Retimes the per-clip label bars onto their clips after any change to clip
    /// timing (trim, extend, reorder, delete, transition overlap, add). Runs inside the
    /// same transaction as the timeline edit, so one Undo reverts both, and the text lane
    /// then differs from the clean document and is saved alongside `timeline_slots`.
    /// No-op unless a window actually moved and the text lane is editable.
    private func rebaseGuidedLabels(_ next: inout EditorDocument, from previous: EditorDocument) {
        guard GuidedLabelRebase.hasLabels(next.textElements), canEditSection(.text),
              previous.clips != next.clips,
              GuidedLabelRebase.windows(of: previous.clips) != GuidedLabelRebase.windows(of: next.clips) else { return }
        next.textElements = GuidedLabelRebase.rebase(next.textElements, oldSlots: previous.clips, newSlots: next.clips)
    }
    private func installPlayer(url: URL, preferredDuration: TimeInterval? = nil, isCurrent: Bool = true) {
        // A completed cloud render remains a useful, non-editable fallback
        // when composing a source preview fails. Always retain the latest
        // authoritative URL, even if an editable source player currently owns
        // the canvas; failures can then restore the correct finished render.
        finishedRenderURL = url
        finishedRenderDuration = preferredDuration
        // `isCurrent` is false when the caller already knows this render
        // predates the document it just loaded (server render_status not
        // "ready" — a save queued a re-render that hasn't finished yet).
        // canDisplayCurrentPlayer's `.preparing` case must not show that
        // stale render during the ordinary editor-open window just because
        // it's the only player installed so far — the freshly-edited title/
        // text would flash the old style for the few seconds it takes the
        // local source preview (built straight from the current document)
        // to take over. Untrusted here only gates *display* during that
        // window; restoreFinishedRenderFallback() still uses it if source
        // preview construction genuinely fails, same as before — stale is
        // still better than nothing once every other option is exhausted.
        finishedRenderIsCurrent = isCurrent
        guard sourcePreviewState == .idle || sourcePreviewState.isFailure || player == nil else { return }
        installPlayer(item: AVPlayerItem(url: url), preferredDuration: preferredDuration)
        finishedRenderPlayer = player
    }

    /// A newly completed server render is authoritative playback even while
    /// its editable source composition is rebuilding. Replacing the stale
    /// source player here keeps video visible through that preparation window.
    func installFinishedRenderPlayer(url: URL, preferredDuration: TimeInterval? = nil) {
        finishedRenderURL = url
        finishedRenderDuration = preferredDuration
        finishedRenderIsCurrent = true
        installPlayer(item: AVPlayerItem(url: url), preferredDuration: preferredDuration)
        finishedRenderPlayer = player
    }

    private func failSourcePreview(_ error: Error) {
        sourcePreviewFailureIsPermanent = Self.isPermanentSourcePreviewFailure(error)
        restoreFinishedRenderFallback()
        sourcePreviewState = Self.isMissingOriginals(error)
            ? .originalsUnavailable
            : .failed(Self.sourcePreviewMessage(for: error))
        // A queued play tap follows the fallback; with nothing to show it is dropped, not left armed.
        if canDisplayCurrentPlayer { startPendingPlaybackIfPossible() } else { pendingPlayRequest = false }
    }

    private func startPendingPlaybackIfPossible() {
        guard pendingPlayRequest, canDisplayCurrentPlayer, !isPlaying else { return }
        togglePlayback()
    }

    private var sourcePreviewStateLabel: String {
        switch sourcePreviewState {
        case .idle: "idle"
        case .preparing: "preparing"
        case .ready: "ready"
        case .failed: "failed"
        case .originalsUnavailable: "originals_unavailable"
        }
    }

    /// One place for "the item AVPlayer is holding cannot play". Reports the error identity from every
    /// build, then recovers: a failed editable preview hands over to the finished render, and a failed
    /// finished render gets one fresh link before the failure is surfaced.
    func handlePlayerItemFailure(_ item: AVPlayerItem, error: Error?) {
        guard let failedPlayer = player, failedPlayer.currentItem === item, failureHandledItem !== item else { return }
        failureHandledItem = item
        let kind: PlaybackFailureReport.PlayerKind = failedPlayer === finishedRenderPlayer ? .finished : .live
        let report = NativePreviewDiagnostics.playerItemFailure(kind: kind, error: error, sourceState: sourcePreviewStateLabel)
        if let api, let jobID {
            Task { try? await api.reportPlaybackFailure(jobID: jobID, report: report) }
        }
        let wantsPlayback = isPlaying || pendingPlayRequest
        failedPlayer.pause()
        isPlaying = false
        switch kind {
        case .live:
            pendingPlayRequest = wantsPlayback
            failSourcePreview(NativeEditorPlaybackFailure.liveItem)
        case .finished:
            pendingPlayRequest = wantsPlayback
            recoverFinishedRender(failedPlayer: failedPlayer)
        }
    }

    private func recoverFinishedRender(failedPlayer: AVPlayer) {
        guard !finishedRenderRefreshAttempted, let api, let jobID else {
            surfaceUnplayableFinishedRender()
            return
        }
        finishedRenderRefreshAttempted = true
        finishedRenderRecoveryTask?.cancel()
        finishedRenderRecoveryTask = Task { @MainActor [weak self, weak failedPlayer] in
            let fresh = try? await api.playbackURL(jobID: jobID)
            guard let self, !Task.isCancelled, let failedPlayer, self.player === failedPlayer else { return }
            guard let fresh else { self.surfaceUnplayableFinishedRender(); return }
            self.installFinishedRenderPlayer(url: fresh, preferredDuration: self.finishedRenderDuration)
            self.startPendingPlaybackIfPossible()
        }
    }

    private func surfaceUnplayableFinishedRender() {
        // An editable preview still building will take over by itself; only a settled state needs a message.
        guard sourcePreviewState != .preparing else { return }
        sourcePreviewState = .failed(Self.sourcePreviewMessage(for: NativeEditorPlaybackFailure.finishedItem))
        pendingPlayRequest = false
    }

    private func restoreFinishedRenderFallback() {
        guard let finishedRenderURL, player !== finishedRenderPlayer else { return }
        installFinishedRenderPlayer(url: finishedRenderURL, preferredDuration: finishedRenderDuration)
    }

    private func installPlayer(item: AVPlayerItem, preferredDuration: TimeInterval? = nil) {
        playbackSeekTarget = nil
        cancelScrubFrames()
        seekRecoveryTask?.cancel()
        seekRecoveryTask = nil
        seekSequence += 1
        seekInFlight = false
        pendingSeekTime = nil
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        durationLoadTask?.cancel()
        mediaDuration = nil
        durationSourcesInvalidated = false

        playbackStateObserver?.invalidate()
        itemStatusObserver?.invalidate()
        if let itemFailureObserver { NotificationCenter.default.removeObserver(itemFailureObserver) }
        // SwiftUI may retain the outgoing VideoPlayer after this replacement.
        // Stop and detach it first so its audio cannot outlive the visible player.
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        let next = AVPlayer(playerItem: item)
        player = next
        observingPlayer = next
        playbackStateObserver = next.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self, weak next] _, _ in
            Task { @MainActor [weak self, weak next] in
                guard let self, let next, self.player === next else { return }
                self.reconcilePlaybackState(next.timeControlStatus)
            }
        }
        // timeControlStatus alone cannot tell "buffering" from "this item will never play": a failed item
        // just sits paused and the play button snaps back (KRI-200).
        itemStatusObserver = item.observe(\.status, options: [.initial, .new]) { [weak self, weak item] observed, _ in
            let status = observed.status
            let error = observed.error
            Task { @MainActor [weak self, weak item] in
                guard let self, let item else { return }
                if status == .failed { self.handlePlayerItemFailure(item, error: error) }
                else if status == .readyToPlay, self.player?.currentItem === item, self.player === self.finishedRenderPlayer {
                    self.finishedRenderRefreshAttempted = false
                }
            }
        }
        itemFailureObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemFailedToPlayToEndTime, object: item, queue: .main
        ) { [weak self, weak item] note in
            let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
            Task { @MainActor [weak self, weak item] in
                guard let self, let item else { return }
                self.handlePlayerItemFailure(item, error: error)
            }
        }
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
                if seconds.isFinite && self.isPlaying && !self.seekInFlight && self.pendingSeekTime == nil && self.playbackSeekTarget == nil {
                    self.currentTime = min(max(0, seconds), max(0, self.playbackDuration))
                }
                self.reconcilePlaybackState(next.timeControlStatus)
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
    func reconcilePlaybackState(_ status: AVPlayer.TimeControlStatus) {
        // Waiting for a composed frame is not a pause. Keep the play request
        // alive so the seek handoff cannot strand a scrub still over the player.
        if status == .waitingToPlayAtSpecifiedRate { return }
        if status == .paused && playbackHandoffInFlight { return }
        let nextIsPlaying = status == .playing
        if nextIsPlaying && !playbackHandoffInFlight { playbackSeekTarget = nil }
        if nextIsPlaying, scrubPreviewFrame != nil {
            scrubPreviewFrame = nil
            scrubPreviewTime = nil
        }
        guard isPlaying != nextIsPlaying else { return }
        isPlaying = nextIsPlaying
    }
    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }
    private static func number(_ value: JSONValue?) -> Double? { if case let .number(value) = value { value } else { nil } }
    private static func bool(_ value: JSONValue?) -> Bool? { if case let .bool(value) = value { value } else { nil } }
    /// Reads an `editor_capabilities` entry that may arrive as a bare bool
    /// (legacy) or as `{"editable": Bool, "reason": String?}` (KRI-216).
    /// Returns nil when the key itself is absent, so callers can distinguish
    /// "server didn't send this capability" from "server sent it as false".
    private static func capabilityEditable(_ value: JSONValue?) -> Bool? {
        guard let value else { return nil }
        if let flag = bool(value) { return flag }
        if let object = object(value) { return bool(object["editable"]) ?? false }
        return nil
    }
    private static func roundToMotionFrame(_ value: TimeInterval) -> TimeInterval {
        (value * 30).rounded() / 30
    }

    private func startTime(for selection: EditorSelection) -> TimeInterval? {
        if selection.kind == .clip, let id = UUID(uuidString: selection.id) { return timelineClips.first(where: { $0.id == id })?.start }
        return timelineItems.first(where: { $0.selection == selection })?.start
    }

    /// Public geometry helpers keep the view layer from implementing subtly
    /// different clamping or hit-target rules.
    func timelineX(for time: TimeInterval, width: CGFloat) -> CGFloat { NativeEditorInteraction.x(forTime: time, duration: playbackDuration, width: width) }
    func timelineTime(for x: CGFloat, width: CGFloat) -> TimeInterval { NativeEditorInteraction.time(forX: x, duration: playbackDuration, width: width) }

    private func refreshDuration() {
        let timelineDuration = timelineProjection.totalDuration
        duration = durationSourcesInvalidated ? timelineDuration : (authoritativeDuration ?? mediaDuration ?? timelineDuration)
        if !duration.isFinite || duration < 0 { duration = max(0, timelineDuration) }
        timelineItemsCache = nil
        // Uses timelineScrubDuration, not playbackDuration: this runs on
        // every timing-gesture sample (updateTrim/updateTimedEdit), and
        // clamping to the stale composition length here would fight
        // auto-scroll's own widened bound mid-drag.
        if currentTime > timelineScrubDuration { seek(to: timelineScrubDuration) }
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
        currentTime = max(0, playbackDuration)
        isPlaying = false
        // A composition has no active layers at its half-open end time.
        // Retain a generated frame from just inside the endpoint instead of
        // relying on VideoPlayer to keep its last surface after natural EOF.
        _ = requestScrubFrame(at: playbackDuration)
    }

    private func configureCapabilities(from variant: [String: JSONValue]?) {
        previewVariant = variant ?? [:]
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
        let cueNativeCaptions = ["subtitled", "narrated"].contains(archetype)
            && variant["base_video_path"]?.stringValue != nil
        // Guided-story captions are caption_cue-tagged TextElements (KRI-110)
        // rather than caption_cues rows. The archetype allowlist above can
        // never see them, so fall back to `text_elements` — the backend has
        // no dedicated "captions" capability key; a caption-tagged element
        // is only ever mutable through the same permission as ordinary text.
        // Always OR'd in below, independent of the explicit-key branch, since
        // no server ever emits a dedicated capability for this lane.
        let textLaneCaptions = canEditText && document.textElements.contains(where: \.isCaption)
        // KRI-216: phone (`render_destination == "device"`) renders never have
        // `base_video_path`, so the legacy allowlist above always reads them
        // as not caption-editable even when the server's `caption_cues`/
        // `caption_meta` keys say otherwise. Prefer those explicit per-lane
        // capabilities when the server sends them; only fall back to the
        // base_video_path heuristic for older servers that omit both keys.
        let explicitCaptionCues = Self.capabilityEditable(capabilities?["caption_cues"])
        let explicitCaptionMeta = Self.capabilityEditable(capabilities?["caption_meta"])
        if explicitCaptionCues != nil || explicitCaptionMeta != nil {
            canEditCaptions = (explicitCaptionCues ?? false) || (explicitCaptionMeta ?? false) || textLaneCaptions
        } else {
            canEditCaptions = cueNativeCaptions || textLaneCaptions
        }
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
            var lastNonTransportError: Error?
            for _ in 0..<300 {
                guard !Task.isCancelled else { return }
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled else { return }
                let variant: [String: JSONValue]
                do {
                    variant = try await api.editorVariant(jobID: jobID, variantID: variantKey)
                } catch is CancellationError {
                    return
                } catch {
                    if RequestFailureCause(error) == .connection {
                        hadNetworkFailure = true
                    } else {
                        lastNonTransportError = error
                    }
                    continue
                }
                guard !Task.isCancelled else { return }
                guard let self, self.pendingPreviewGeneration == generation else { return }
                // Device failures are reported on the authoritative variant;
                // only reconcile the local renderer for that terminal status.
                // Save-time reconciliation already covers the normal path,
                // while this branch lets a later server failure reach the
                // device retry affordance without doing a full reconcile on
                // every poll tick.
                let variantGeneration = variant["render_generation_id"]?.stringValue ?? variant["render_finished_at"]?.stringValue
                let refreshDevice = self.deviceRenderKey != nil && (
                    variant["render_status"]?.stringValue == "needs_attention"
                    || (variant["render_status"]?.stringValue == "ready" && variantGeneration != generation)
                )
                if refreshDevice {
                    await self.refreshDeviceRender()
                    guard !Task.isCancelled, self.pendingPreviewGeneration == generation else { return }
                }
                if self.applyPreviewVariant(variant, generation: generation) { return }
            }
            guard !Task.isCancelled, let self, self.pendingPreviewGeneration == generation else { return }
            self.saveState = .previewFailed(
                hadNetworkFailure
                    ? "Your edit is saved, but Kria could not finish checking the preview. Check your connection and try again."
                    : lastNonTransportError.map {
                        "Your edit is saved, but Kria could not finish checking the preview. \($0.localizedDescription)"
                    } ?? "Your edit is saved, but the preview is taking longer than expected. Try checking again."
            )
        }
    }

    private func announceEditApplied() {
        showsEditApplied = true
        editAppliedTask?.cancel()
        editAppliedTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            self?.showsEditApplied = false
        }
    }

    func retryPreviewRefresh() {
        guard let generation = pendingPreviewGeneration else { return }
        saveState = .previewPending
        startPreviewRefresh(generation: generation)
    }

    /// Applies one status response from the render-generation poll. Kept
    /// internal so tests can cover terminal states without waiting five
    /// minutes for the production poll loop.
    @discardableResult
    func applyPreviewVariant(_ variant: [String: JSONValue], generation: String) -> Bool {
        guard pendingPreviewGeneration == generation else { return false }
        let currentGeneration = variant["render_generation_id"]?.stringValue ?? variant["render_finished_at"]?.stringValue
        guard let currentGeneration else { return false }
        if currentGeneration != generation {
            guard let key = deviceRenderKey,
                  variant["render_status"]?.stringValue == "ready",
                  let presentation = deviceRenders?.presentations[key],
                  presentation.publishedGeneration == currentGeneration,
                  let expectedIdentity = pendingDeviceRenderIdentity,
                  deviceRenders?.request(for: key)?.identity == expectedIdentity else { return false }
        }
        _ = reconcileAuthoritativeText(
            from: variant,
            generation: currentGeneration,
            expectedItemID: itemID,
            expectedJobID: jobID,
            expectedVariantKey: variantKey,
            expectedThreadID: threadID
        )
        if let key = deviceRenderKey,
           let presentation = deviceRenders?.presentations[key],
           presentation.requiresServerRetry {
            saveState = .deviceRenderRetryNeeded(
                presentation.message ?? "Your edit is saved, but this iPhone needs to retry rendering it."
            )
            return true
        }
        let status = variant["render_status"]?.stringValue
        if status == "ready", let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
            if !rebaseCleanDraft(from: variant) {
                rebaseGenerationPreservingLocalEdits(currentGeneration)
            }
            installFinishedRenderPlayer(url: url, preferredDuration: authoritativeDuration)
            pendingRenderRetrySections.removeAll()
            pendingPreviewGeneration = nil
            saveState = .saved
            announceEditApplied()
            return true
        }
        if status == "failed" {
            saveState = .renderRetryNeeded("Your edit is saved, but its preview render failed. You can retry it safely.")
            return true
        }
        return false
    }

    /// Adopt only renderer-owned text returned for the exact current
    /// generation. The other lanes stay untouched, and text changed after the
    /// save continues to win over the server projection.
    @discardableResult
    private func reconcileAuthoritativeText(
        from variant: [String: JSONValue],
        generation: String,
        expectedItemID: String?,
        expectedJobID: UUID?,
        expectedVariantKey: String?,
        expectedThreadID: UUID?,
        expectedDocumentGeneration: String? = nil
    ) -> Bool {
        guard let returnedGeneration = variant["render_generation_id"]?.stringValue
                ?? variant["render_finished_at"]?.stringValue,
              returnedGeneration == generation,
              expectedDocumentGeneration == nil || document.revision.baseGeneration == expectedDocumentGeneration,
              itemID == expectedItemID,
              jobID == expectedJobID,
              variantKey == expectedVariantKey,
              threadID == expectedThreadID,
              let textValue = variant["text_elements"],
              case .array = textValue else { return false }
        if let returnedVariantKey = variant["variant_id"]?.stringValue,
           returnedVariantKey != expectedVariantKey { return false }

        let oldCleanText = cleanDocument.textElements
        let authoritativeText = authoritativeTextElements(from: variant, generation: generation)
        let currentWasClean = document.textElements == oldCleanText

        cleanDocument.textElements = authoritativeText
        cleanDocument.revision.baseGeneration = generation
        if currentWasClean { document.textElements = authoritativeText }

        func rebase(_ value: EditorDocument) -> EditorDocument {
            guard value.textElements == oldCleanText else { return value }
            var rebased = value
            rebased.textElements = authoritativeText
            return rebased
        }
        undoStack = undoStack.map(rebase)
        redoStack = redoStack.map(rebase)
        if let transactionBaseline { self.transactionBaseline = rebase(transactionBaseline) }
        if var activeTrim {
            activeTrim.baseline = rebase(activeTrim.baseline)
            activeTrim.redoBaseline = activeTrim.redoBaseline.map(rebase)
            self.activeTrim = activeTrim
        }
        if var activeTimedEdit {
            activeTimedEdit.baseline = rebase(activeTimedEdit.baseline)
            activeTimedEdit.redoBaseline = activeTimedEdit.redoBaseline.map(rebase)
            self.activeTimedEdit = activeTimedEdit
        }
        refreshDirtyState()
        if let selected = selection, !selectionExists(selected) { selection = nil }
        return true
    }

    private func authoritativeTextElements(from variant: [String: JSONValue], generation: String) -> [EditorTextElement] {
        let snapshot = DraftSnapshot(
            draftID: "reconciled-\(projectID.uuidString)",
            itemID: itemID ?? "",
            variantKey: variantKey ?? "",
            draftRevision: document.revision.number ?? 0,
            snapshotHash: "",
            etag: etag,
            baseJobID: jobID?.uuidString,
            baseGenerationID: generation,
            snapshot: document.encodeSnapshot(),
            canUndo: false,
            createdAt: .now
        )
        let draft = snapshot.editorDraft(projectID: projectID, authoritativeVariant: variant)
        return EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(draft)).textElements
    }

    private func saveIdentityMatches(itemID: String?, jobID: UUID?, variantKey: String?, threadID: UUID?, documentGeneration: String) -> Bool {
        self.itemID == itemID && self.jobID == jobID && self.variantKey == variantKey && self.threadID == threadID && document.revision.baseGeneration == documentGeneration
    }

    func retryDeviceRender() async {
        guard !isSaving, let key = deviceRenderKey, let deviceRenders,
              let generation = pendingPreviewGeneration else { return }
        let retried = await deviceRenders.retryNeedsAttention(key)
        guard retried, !Task.isCancelled, pendingPreviewGeneration == generation, deviceRenderKey == key else { return }
        await refreshDeviceRender(retry: true)
        guard !Task.isCancelled, pendingPreviewGeneration == generation, deviceRenderKey == key else { return }
        pendingDeviceRenderIdentity = deviceRenders.request(for: key)?.identity
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
        if conversationRuntimeVersion == 1 {
            // Compare raw authority, before client metadata/hydration. Reusing
            // encodeSnapshot here would cache random compatibility clip IDs.
            let authority = DraftSnapshot(
                draftID: snapshot.draftID, itemID: itemID, variantKey: variantKey,
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: snapshot.baseJobID,
                baseGenerationID: generation, snapshot: [:], canUndo: false, createdAt: .now
            )
            legacyPromptSnapshot = authority.editorDraft(projectID: projectID, authoritativeVariant: variant).serverSnapshot
        }
        cleanDocument = document
        undoStack.removeAll()
        redoStack.removeAll()
        changedSections.removeAll()
        explicitlyDirtySections.removeAll()
        pendingRenderRetrySections.removeAll()
        hasUnsavedChanges = false
        if let selected, selectionExists(selected) { selection = selected } else { selection = nil }
        configureCapabilities(from: variant)
        refreshRebasedSourcePreview()
        durationSourcesInvalidated = false
        setAuthoritativeDuration(Self.number(variant["duration_s"]))
        refreshDuration()
        return true
    }

    /// Advances renderer ownership after a device publication while retaining
    /// local follow-up edits. The clean baseline and history snapshots must
    /// move with the document or the next commit will use the old generation.
    private func rebaseGenerationPreservingLocalEdits(_ generation: String) {
        document.revision.baseGeneration = generation
        cleanDocument.revision.baseGeneration = generation
        undoStack = undoStack.map { snapshot in
            var value = snapshot
            value.revision.baseGeneration = generation
            return value
        }
        redoStack = redoStack.map { snapshot in
            var value = snapshot
            value.revision.baseGeneration = generation
            return value
        }
        // The follow-up edit remains in `document`, but all resolved media is
        // owned by the preceding generation. Rebuild it so device narration
        // is fetched from the newly published recipe and fenced to this base.
        refreshRebasedSourcePreview()
    }

    private func selectionExists(_ value: EditorSelection) -> Bool {
        if value.kind == .clip, let id = UUID(uuidString: value.id) { return draft.clips.contains { $0.id == id } }
        return timelineItems.contains { $0.selection == value }
    }
}

private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
