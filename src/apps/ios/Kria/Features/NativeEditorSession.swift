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
    case loadFailed(String)
    case previewFailed(String)
    case failed(String)
}

/// How the editor's background refresh (chat drafts, newer renders) is doing. Kept apart from
/// `NativeEditorSaveState` on purpose: a failed background read must never replace a save or render
/// banner and its button, and a temporary failure is not something the creator has to act on.
enum NativeEditorRefreshSync: Equatable, Sendable {
    case idle
    /// Reads keep failing for a temporary reason (offline, a server hiccup). Retrying on its own.
    case reconnecting
    /// The project moved to a new version that is still rendering. The current video stays editable.
    case newVersionRendering
    /// The project no longer points at this edit (started over or removed elsewhere).
    case targetLost(String)
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

struct NativeEditorTemporaryVideo: Sendable {
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
            // KRI-432: a song volume change on a settled preview is applied to the player, not rebuilt.
            if liveSongVolumeActive, sourcePreviewSettled, let built = previewBuiltDocument,
               Self.differsOnlyBySongVolume(document, built) {
                applyLiveSongVolume()
                return
            }
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
        if player != nil, player === finishedRenderPlayer {
            return finishedRenderDuration ?? mediaDuration ?? duration
        }
        return duration
    }
    /// Editor geometry/scrubbing always reaches the projected document end;
    /// preview failures do not hide newly appended clips. Playback and frame
    /// requests remain bounded by the actual composition's playbackDuration.
    var timelineScrubDuration: TimeInterval {
        max(playbackDuration, timelineProjection.totalDuration)
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
    @Published var saveState: NativeEditorSaveState = .idle
    @Published private(set) var refreshSync: NativeEditorRefreshSync = .idle
    /// Waits between background refresh retries; the last one repeats. Internal so tests can shorten it.
    var refreshRetryDelays: [Duration] = [.seconds(1), .seconds(3), .seconds(9), .seconds(27), .seconds(60)]
    private var refreshRetryTask: Task<Void, Never>?
    private var refreshFailureStreak = 0
    /// The project lost this edit target, or the chat runtime is off: further draft reads cannot succeed.
    private var promptSyncStopped = false
    /// Polls for a new job that the thread points at but that is not ready to edit yet.
    private var newVersionWatchTask: Task<Void, Never>?
    private var newVersionWatchTarget: UUID?
    /// Shown on the opening screen while a first open waits for its video or for a connection.
    @Published private(set) var loadWaitingMessage: String?
    private var loadRetryTask: Task<Void, Never>?
    private var loadRetryAttempt = 0
    var loadRetryDelays: [Duration] = [.seconds(1), .seconds(3), .seconds(9), .seconds(20)]
    var loadRenderingDelays: [Duration] = [.seconds(3), .seconds(5), .seconds(10), .seconds(15)]
    /// About 30 s of quiet retries at the default delays before the opening screen offers Retry.
    static let loadTransientRetryLimit = 4
    var newVersionPollDelays: [Duration] = [.seconds(3), .seconds(5), .seconds(10), .seconds(15)]
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
    private let previewFrameRenderer: NativePreviewFrameRenderer
    private var textInteractionTask: Task<Void, Never>?
    private var textInteractionSequence = 0

    private func prepareInteractionLayers() {
        textInteractionTask?.cancel()
        textInteractionSequence += 1
        prepareTextInteraction()
        prepareMediaInteraction()
    }

    private func prepareTextInteraction() {
        guard !isDirectManipulating, !isPlaying, let selection, selection.kind == .text,
              let element = document.textElements.first(where: { $0.id == selection.id }),
              let preview = sourcePreview, let item = player?.currentItem,
              sourcePreviewState == .ready else { return }
        let sequence = textInteractionSequence
        let time = min(currentTime, max(0, duration - 1.0 / 600))
        let renderFrame = previewFrameRenderer
        textInteractionTask = Task { @MainActor [weak self] in
            do {
                // Prepare after scrubbing settles, never for every scrub frame.
                try await Task.sleep(for: .milliseconds(100))
                try Task.checkCancellation()
                let layers = try preview.textInteractionLayers(id: element.id, time: time)
                @MainActor func image(_ composition: AVVideoComposition) async throws -> UIImage {
                    try Task.checkCancellation()
                    let generator = AVAssetImageGenerator(asset: item.asset)
                    generator.videoComposition = composition
                    generator.maximumSize = CGSize(width: 1080, height: 1920)
                    generator.requestedTimeToleranceBefore = .zero
                    generator.requestedTimeToleranceAfter = .zero
                    let frame = try await renderFrame(generator, CMTime(seconds: time, preferredTimescale: 600))
                    return UIImage(cgImage: frame.image)
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
        let sequence = textInteractionSequence
        let time = min(currentTime, max(0, duration - 1.0 / 600))
        let renderFrame = previewFrameRenderer
        textInteractionTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(for: .milliseconds(100))
                try Task.checkCancellation()
                let layers = try preview.mediaInteractionLayers(id: mediaID, time: time)
                @MainActor func image(_ composition: AVVideoComposition) async throws -> UIImage {
                    try Task.checkCancellation()
                    let generator = AVAssetImageGenerator(asset: item.asset)
                    generator.videoComposition = composition
                    generator.maximumSize = CGSize(width: 1080, height: 1920)
                    generator.requestedTimeToleranceBefore = .zero
                    generator.requestedTimeToleranceAfter = .zero
                    let frame = try await renderFrame(generator, CMTime(seconds: time, preferredTimescale: 600))
                    return UIImage(cgImage: frame.image)
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
    private var textMeasurer: NativeEditorRenderCompiler?
    private var sourceResolver: NativeEditorSourceResolver?
    private var resolvedAudio: [String: ResolvedEditorSource] = [:]
    /// A device recipe may intentionally have no narration. Remember that
    /// successful empty resolution for this generation so ordinary editor
    /// rebuilds do not repeatedly poll the device-render endpoint.
    private var deviceNarrationResolutionGeneration: String?
    /// How the pinned device recipe plays the creator's song (KRI-374); nil when
    /// the recipe has none. Resolved with the narration, from the same recipe.
    @Published private(set) var deviceSongBed: NativeEditorSongBed?
    /// The creator's song file once the preview has resolved it (KRI-428): the start-point bar draws
    /// its waveform from it. Nil until then; the bar degrades to a plain slider.
    @Published private(set) var userSongAudioURL: URL?
    /// Song edits the server has acknowledged since the current variant loaded (KRI-428). They are
    /// folded out of `document.userSong` on Save so a retry or a rebase never sends them again.
    private var acknowledgedUserSong: EditorUserSongState?
    /// KRI-432: the built composition plays the song at unity gain and the PLAYER carries the level, so a
    /// volume change is `AVPlayer.volume` (instant, no rebuild, no black frame). Re-assigning the item's
    /// audio mix is not an option: on iPhone any live `audioMix` swap silences the preview (KRI-241).
    /// Only when the song is the sole audible track; with SFX, music or narration the level stays baked in.
    private(set) var liveSongVolumeActive = false
    /// The document the current source composition was compiled from.
    private var previewBuiltDocument: EditorDocument?
    /// A song start drag is open: preview rebuilds wait for the release (one rebuild, not one per tick).
    private var isSongStartDragActive = false
    private var isRestoringVideoAfterAudition = false
    /// Plays the song from the chosen start while the creator slides the start bar.
    private(set) lazy var songAudition = NativeSongAuditionController(
        engine: AVAudioSongAuditionEngine(),
        host: .init(
            pauseVideo: { [weak self] in
                guard let self else { return false }
                let wasPlaying = self.isPlaying
                if wasPlaying { self.pausePlayback() }
                return wasPlaying
            },
            resumeVideo: { [weak self] in
                guard let self, !self.isPlaying else { return }
                self.isRestoringVideoAfterAudition = true
                self.togglePlayback()
                self.isRestoringVideoAfterAudition = false
            },
            activateAudioSession: { [weak self] in self?.activatePreviewAudio() }))
    #if DEBUG
    /// UI-test fixtures have no source pool; this stands in for the takes' offsets by clip index.
    private var fixtureLipsyncDeltas: [Int: Double] = [:]
    #endif
    /// Re-maps unsaved song edits (in the document and its undo history) onto the song as just saved.
    private var userSongRebase: (EditorUserSongState?) -> EditorUserSongState? = { $0 }
    /// The acknowledged volume / start a render retry sends again (a removal is never resent).
    private var userSongRetry: EditorUserSongState?
    private var songRemovalAlreadyApplied = false
    private var previewVariant: [String: JSONValue] = [:] {
        didSet { timelineClipsCache = nil }
    }
    /// Narrated footage slows to fill its voiceover step (`timelineClips`).
    private var usesNarratedSourceFit: Bool { previewVariant["resolved_archetype"] == .string("narrated") }
    var musicPlaybackMode: NativeMusicPlaybackMode { .init(variant: previewVariant) }
    /// The creator's own song, for the Sounds tab. The server's `user_song` wins; an
    /// older server falls back to the recipe's `song` clip.
    var yourSong: NativeEditorYourSong? {
        guard !userSongRemoved else { return nil }
        return NativeEditorYourSong.make(userSong: effectiveUserSong, bed: effectiveSongBed)
    }
    /// The server's song (`user_song`) exactly as the loaded variant lists it.
    private var baseUserSong: NativeUserSong? { NativeUserSong(variant: previewVariant) }
    /// The song as last SAVED: the variant plus every edit the server has acknowledged since that
    /// variant loaded (a refresh of the variant resets the acknowledged edits, see `configureCapabilities`).
    /// The unsaved edits in `document.userSong` are relative to this. Nil once a removal was saved.
    private var savedUserSong: NativeUserSong? { baseUserSong?.applying(acknowledgedUserSong) }
    /// The latest start: it must leave `NativeUserSong.minPlayableS` of song. The video no longer bounds it;
    /// a song that runs out before the video does simply stops.
    private func maxUserSongStart(for song: NativeUserSong) -> Double? { NativeUserSong.maxStart(for: song) }
    /// The video length the song follows right now.
    private func currentVideoLength(for song: NativeUserSong) -> Double { duration > 0 ? duration : song.windowLengthS }
    /// Acknowledged plus unsaved edits, with a pending start re-clamped to the CURRENT video length
    /// (the video may have grown since the start was set).
    private var effectiveUserSongState: EditorUserSongState? {
        guard let state = EditorUserSongState.merged(acknowledgedUserSong, document.userSong) else { return nil }
        return reclamped(state)
    }
    /// A pending start re-clamped to the song as it is NOW: it must leave a second of song, and (KRI-561) a
    /// second before the creator's end, so the server never refuses it.
    private func reclamped(_ state: EditorUserSongState) -> EditorUserSongState {
        guard let song = baseUserSong else { return state }
        return NativeUserSong.reclamped(state, base: song, acknowledged: acknowledgedUserSong)
    }
    /// The song as the editor currently shows it: saved values plus unsaved volume / start / end edits.
    /// A lip-sync window follows the singer: where the cuts now put the song start (`lipsyncSongStartShift`).
    var effectiveUserSong: NativeUserSong? {
        guard let base = baseUserSong else { return nil }
        return NativeUserSong.effective(base: base, acknowledged: acknowledgedUserSong, draft: document.userSong,
                                        lipsyncShift: lipsyncSongStartShift, videoLength: currentVideoLength(for: base))
    }

    // MARK: KRI-576 lyric captions follow the song window in the live preview

    /// The song start (in true song seconds) the document's `song-caption-N` rows were last timed against: the
    /// loaded variant's `user_song` window, or after a Save the window the server re-projected them for.
    /// Preview-only bookkeeping, never part of the document.
    private var songCaptionWindowStartS: Double?
    /// Lip-sync only: `base` stays the loaded variant's window until the next variant load, but a Save has
    /// already folded the cuts' shift into the clean clips (`lipsyncSongStartShift` reads 0 again). This is the
    /// shift Saved since that variant loaded, so `effective start + carry` stays the TRUE start.
    private var songCaptionLipsyncCarryS = 0.0
    /// How the preview re-times the lyric rows right now; nil when they already sit where the song window is.
    var songCaptionShift: NativeSongCaptionShift? {
        if userSongRemoved { return NativeSongCaptionShift(shiftS: 0, windowLengthS: 0, songRemoved: true) }
        guard let anchor = songCaptionWindowStartS, let song = effectiveUserSong else { return nil }
        let shift = song.windowStartS + songCaptionLipsyncCarryS - anchor
        guard abs(shift) > NativeSongCaptionShift.tolerance else { return nil }
        return NativeSongCaptionShift(shiftS: shift, windowLengthS: song.windowLengthS, songRemoved: false)
    }
    /// The song start as the editor shows it right now, in true song seconds.
    private var trueSongWindowStartS: Double? { effectiveUserSong.map { $0.windowStartS + songCaptionLipsyncCarryS } }
    /// KRI-561: the server offers (and the edit allows) trimming the song.
    private var songTrimOffered: Bool { document.capabilities["user_song.trim"] != nil }
    private var songTrimEditable: Bool { document.capabilities["user_song.trim"]?.editable == true }
    /// The creator removed the song, saved or not.
    var userSongRemoved: Bool {
        (acknowledgedUserSong?.removed == true || document.userSong?.removed == true)
            && (baseUserSong != nil || deviceSongBed != nil)
    }
    /// The song level the preview plays at right now, saved or not.
    private var currentSongVolume: Double { effectiveSongBed?.volume ?? effectiveUserSongState?.volume ?? 1 }

    /// Everything but the song volume is the same: the one difference a settled preview absorbs live.
    static func differsOnlyBySongVolume(_ a: EditorDocument, _ b: EditorDocument) -> Bool {
        func stripped(_ value: EditorDocument) -> EditorDocument {
            var copy = value
            if var state = copy.userSong { state.volume = nil; copy.userSong = state.isEmpty ? nil : state }
            return copy
        }
        return a != b && stripped(a) == stripped(b)
    }

    /// Sets the player's level for the song on the composition now on screen.
    private func applyLiveSongVolume() {
        guard let player, let preview = sourcePreview, player.currentItem === preview.preview.playerItem else { return }
        player.volume = liveSongVolumeActive ? Float(min(1, max(0, currentSongVolume))) : 1
        previewBuiltDocument = document
    }

    /// The recipe on screen with the live song level written back into the song clip, so what is
    /// exported or inspected carries the volume the creator hears.
    private func recipeWithLiveSongVolume(_ recipe: KriaMediaEngine.EditRecipe) -> KriaMediaEngine.EditRecipe {
        guard liveSongVolumeActive else { return recipe }
        var result = recipe
        let level = currentSongVolume
        result.tracks = result.tracks.map { track in
            guard track.id == NativeEditorSongBed.trackID, track.kind == .audio else { return track }
            var copy = track
            copy.clips = track.clips.map { var clip = $0; clip.volume = level; return clip }
            return copy
        }
        return result
    }

    /// True when the song is the only thing audible in `recipe`: every other clip is silent.
    private static func songIsOnlyAudibleTrack(_ recipe: KriaMediaEngine.EditRecipe) -> Bool {
        let song = recipe.tracks.filter { $0.id == NativeEditorSongBed.trackID && $0.kind == .audio }
        guard song.count == 1 else { return false }
        return recipe.tracks.filter { $0.id != NativeEditorSongBed.trackID }
            .allSatisfy { $0.clips.allSatisfy { $0.volume == 0 } }
    }

    /// What the preview plays: the pinned recipe's bed with the user's unsaved volume / start applied,
    /// the same values the server writes into the recipe on Save. Nil after Remove.
    private var effectiveSongBed: NativeEditorSongBed? {
        guard let bed = deviceSongBed, !userSongRemoved else { return nil }
        let shift = lipsyncSongStartShift
        let state = effectiveUserSongState
        // The creator's end (background only) is absolute song time, so it needs no start arithmetic.
        let end = baseUserSong.flatMap { $0.mode == .background ? $0.creatorEnd(applying: state) : nil }
        guard let edit = state else {
            guard shift != 0 || end != nil else { return bed }
            return NativeEditorSongBed(assetID: bed.assetID, sourceStart: max(0, bed.sourceStart + shift),
                                       sourceDuration: bed.sourceDuration, volume: bed.volume,
                                       fadeIn: bed.fadeIn, fadeOut: bed.fadeOut, endS: end)
        }
        return NativeEditorSongBed(assetID: bed.assetID, sourceStart: max(0, (edit.windowStartS ?? bed.sourceStart) + shift),
                                   sourceDuration: bed.sourceDuration, volume: edit.volume ?? bed.volume,
                                   fadeIn: bed.fadeIn, fadeOut: bed.fadeOut, endS: end)
    }

    /// Lip-sync only: how far the cuts moved the song start since the last Save. Every pinned take votes for
    /// `delta + source_start - output_start` (what the server derives on Save), so a head trim, a dropped
    /// cut or a song trim all keep the singer on the song. Without the takes' offsets (an older server, or
    /// the source pool not loaded yet) the first cut's head is the fallback (`NativeLipsyncSongAnchor`).
    private var lipsyncSongStartShift: Double { lipsyncStartShift(of: document, against: cleanDocument) }
    private func lipsyncStartShift(of document: EditorDocument, against cleanDocument: EditorDocument) -> Double {
        guard baseUserSong?.mode == .lipsync else { return 0 }
        let deltas = lipsyncDeltaByClipIndex
        if !deltas.isEmpty,
           let current = NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: document.clips), deltaByClipIndex: deltas),
           let saved = NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: cleanDocument.clips), deltaByClipIndex: deltas) {
            let shift = current - saved
            return abs(shift) > NativeLipsyncSongAnchor.frameTolerance ? shift : 0
        }
        func head(_ document: EditorDocument) -> EditorTimelineSlot? { document.clips.first { !$0.removed } }
        let saved = head(cleanDocument), current = head(document)
        return NativeLipsyncSongAnchor.startShift(savedClipIndex: saved?.clipIndex, savedInS: saved?.inS,
                                                  currentClipIndex: current?.clipIndex, currentInS: current?.inS)
    }
    /// Each pinned take's song offset, by the clip index the timeline uses for it.
    private var lipsyncDeltaByClipIndex: [Int: Double] {
        #if DEBUG
        if !fixtureLipsyncDeltas.isEmpty { return fixtureLipsyncDeltas }
        #endif
        guard let takes = baseUserSong?.takes, !takes.isEmpty, let pool = sourcePool ?? originalsRecoveryPool else { return [:] }
        var result: [Int: Double] = [:]
        for clip in pool.clips {
            if let mediaID = clip.nativeSource?.mediaID, let delta = takes[mediaID] { result[clip.clipIndex] = delta }
        }
        return result
    }
    /// The Sounds-tab controls for the song, nil for a server that sends no `user_song`.
    var yourSongControls: NativeEditorYourSongControls? {
        guard !userSongRemoved, let song = effectiveUserSong else { return nil }
        let videoLength = currentVideoLength(for: song)
        // Background: the whole song. Lip-sync: the window as last saved; the handles only move inward
        // (the footage outside the current window is gone, Undo brings it back).
        let saved = savedUserSong
        let bar: (Double, Double) = song.mode == .background
            ? (0, song.durationS ?? 0)
            : (min(saved?.windowStartS ?? song.windowStartS, song.windowStartS), max(saved?.windowEndS ?? song.windowEndS, song.windowEndS))
        let latestStart = baseUserSong.flatMap(maxUserSongStart(for:))
        return NativeEditorYourSongControls(
            mode: song.mode, volume: song.volume, startS: song.windowStartS, windowLengthS: song.windowLengthS,
            songDurationS: song.durationS,
            maxStartS: song.mode == .background
                ? song.creatorEndS.map { end in min(latestStart ?? .infinity, max(0, end - NativeUserSong.minPlayableS)) } ?? latestStart
                : latestStart,
            songEndsBeforeVideo: song.windowLengthS < videoLength - 0.001,
            canEditVolume: canEditOperation(["user_song.volume"], section: .userSong),
            canEditStart: song.mode == .background && canEditOperation(["user_song.window"], section: .userSong),
            canRemove: canEditOperation(["user_song.remove"], section: .userSong),
            trimOffered: songTrimOffered,
            canTrim: songTrimOffered && songTrimEditable && cloudEditorUnavailableMessage == nil
                && (song.mode == .lipsync ? canEditSection(.timeline) : true),
            endS: song.windowEndS, hasCreatorEnd: song.creatorEndS != nil, videoLengthS: videoLength,
            barStartS: bar.0, barEndS: bar.1)
    }
    private func updateUserSong(_ body: (inout EditorUserSongState, NativeUserSong) -> Void) {
        guard let saved = savedUserSong, document.userSong?.removed != true else { return }
        transactDocument(section: .userSong) { doc in
            var state = doc.userSong ?? EditorUserSongState()
            body(&state, saved)
            doc.userSong = state.isEmpty ? nil : state
        }
    }
    /// Song level 0...1. An edit equal to the saved level clears itself, so dragging back is not a change.
    func setUserSongVolume(_ value: Double) {
        guard value.isFinite, canEditOperation(["user_song.volume"], section: .userSong) else { return }
        let level = (min(1, max(0, value)) * 100).rounded() / 100
        updateUserSong { state, saved in state.volume = abs(level - saved.volume) < 0.0005 ? nil : level }
    }
    /// Where the song starts, in seconds into the file. Background only: a lip-sync take's offset depends
    /// on the song start. Clamped so the song still covers the whole video.
    func setUserSongStart(_ value: Double) {
        guard value.isFinite, savedUserSong?.mode == .background,
              canEditOperation(["user_song.window"], section: .userSong) else { return }
        // Round first, clamp after: rounding up past the limit would be rejected by the server.
        var start = (max(0, value) * 100).rounded() / 100
        if let song = baseUserSong, let limit = maxUserSongStart(for: song) { start = min(start, limit) }
        // KRI-561: moving the start later keeps the creator's end, so it must leave a second before it.
        if let end = effectiveUserSong?.creatorEndS { start = min(start, max(0, end - NativeUserSong.minPlayableS)) }
        updateUserSong { state, saved in state.windowStartS = abs(start - saved.windowStartS) < 0.0005 ? nil : start }
    }
    /// Where a BACKGROUND song stops (KRI-561), in seconds into the file. At least a second after the start,
    /// at most where the video or the song would stop anyway: dragging the handle back to that natural end
    /// clears the creator's end (the song's own length on the wire). An edit equal to the saved end clears
    /// itself.
    func setUserSongEnd(_ value: Double) {
        guard value.isFinite, savedUserSong?.mode == .background, songTrimEditable, cloudEditorUnavailableMessage == nil,
              let song = effectiveUserSong, let duration = song.durationS else { return }
        let natural = min(song.windowStartS + currentVideoLength(for: song), duration)
        let lowest = min(song.windowStartS + NativeUserSong.minPlayableS, natural)
        let end = min(max((value * 100).rounded() / 100, lowest), natural)
        let clears = end >= natural - 0.005
        updateUserSong { state, saved in
            let savedEnd = saved.creatorEndS
            let same = clears ? savedEnd == nil : savedEnd.map { abs($0 - end) < 0.0005 } ?? false
            state.windowEndS = same ? nil : (clears ? duration : end)
        }
    }
    /// A start-bar drag began: one undo step, no preview rebuilds until it ends, and the song plays from
    /// the chosen start so the creator hears what they pick (KRI-432).
    func beginSongStartDrag() {
        beginTransaction()
        isSongStartDragActive = true
        if userSongAudioURL != nil { songAudition.begin() }
    }

    /// The start moved to `value`: updates the document and (debounced) restarts the audition from it.
    func moveSongStart(_ value: Double) {
        setUserSongStart(value)
        guard songAudition.isActive, let song = effectiveUserSong, let url = userSongAudioURL else { return }
        // The window is what plays: the video length, or what is left of the song when that is shorter.
        songAudition.update(SongAuditionRequest(url: url, start: song.windowStartS, length: song.windowLengthS, volume: Float(song.volume)))
    }

    /// The drag ended: close the undo step, rebuild the preview ONCE at the new start, and let the audition
    /// play the window out before handing the video back.
    func endSongStartDrag() {
        endTransaction()
        isSongStartDragActive = false
        flushDeferredSourcePreviewUpdate()
        songAudition.end()
    }

    /// The end handle moved (same drag transaction as the start): plays the last stretch before the end so
    /// the creator hears where the music stops.
    func beginSongEndDrag() { beginSongStartDrag() }
    func moveSongEnd(_ value: Double) {
        setUserSongEnd(value)
        guard songAudition.isActive, let song = effectiveUserSong, let url = userSongAudioURL else { return }
        let length = min(Self.songEndAuditionS, song.windowLengthS)
        songAudition.update(SongAuditionRequest(url: url, start: max(song.windowStartS, song.windowEndS - length), length: length,
                                                volume: Float(song.volume)))
    }
    func endSongEndDrag() { endSongStartDrag() }
    private static let songEndAuditionS = 3.0

    /// Lip-sync: plays the song from the dragged range while a handle is held (nothing changes until release).
    func previewLipsyncSongTrim(start: Double, end: Double) {
        guard songAudition.isActive, let song = effectiveUserSong, song.mode == .lipsync, let url = userSongAudioURL,
              start.isFinite, end.isFinite else { return }
        let from = min(max(start, song.windowStartS), song.windowEndS)
        songAudition.update(SongAuditionRequest(url: url, start: from, length: max(0.1, min(end, song.windowEndS) - from),
                                                volume: Float(song.volume)))
    }

    /// Lip-sync trim (KRI-561): keeps the stretch of the video that plays the song range `[start, end]` (song
    /// seconds, inside the current window). Cuts outside it are dropped like a clip removal, the cuts on its
    /// edges lose their head / tail like a clip trim, and the rest ripples left; one undo step. The song start
    /// then follows the cuts (`lipsyncSongStartShift`), here and on the server. Returns whether anything changed.
    @discardableResult
    func applyLipsyncSongTrim(start: Double, end: Double) -> Bool {
        guard start.isFinite, end.isFinite, songTrimEditable, canEditSection(.timeline),
              let song = effectiveUserSong, song.mode == .lipsync else { return false }
        let from = min(max(start, song.windowStartS), song.windowEndS)
        let to = min(max(end, from), song.windowEndS)
        guard let plan = NativeLipsyncSongTrim.plan(slots: document.clips, t0: from - song.windowStartS, t1: to - song.windowStartS,
                                                    minimumClipS: minimumClipDuration) else { return false }
        let before = document
        transactDocument(section: .timeline) { doc in
            for kept in plan.kept where kept.trimmed {
                doc.clips[kept.index].inS = kept.inS
                doc.clips[kept.index].durationS = kept.durationS
                doc.clips[kept.index].durationBeats = nil
                if doc.clips[kept.index].raw["source_end_s"] != nil {
                    let rate = Self.explicitPlaybackRate(doc.clips[kept.index])
                    doc.clips[kept.index].raw["source_end_s"] = .number(kept.inS + kept.durationS * rate)
                }
            }
            let removedSlots = plan.dropped.sorted().map { doc.clips[$0] }
            for slot in removedSlots {
                // A locally-added cut has no server-side predecessor to suppress (same rule as a clip removal).
                if let id = slot.id, cleanDocument.clips.contains(where: { $0.id == id }) {
                    let deletion = EditorDeletion(kind: "clip", id: id)
                    if !doc.deletions.contains(deletion) { doc.deletions.append(deletion) }
                }
            }
            for index in plan.dropped.sorted(by: >) { doc.clips.remove(at: index) }
            doc.tombstones.append(contentsOf: removedSlots.map { var slot = $0; slot.removed = true; return slot })
        }
        return document != before
    }

    /// Drops the song for this edit; camera audio plays again. Undoable until Save.
    func removeUserSong() {
        guard savedUserSong != nil, document.userSong?.removed != true, canEditOperation(["user_song.remove"], section: .userSong) else { return }
        updateUserSong { state, _ in state = EditorUserSongState(removed: true) }
    }
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
    /// `overlay:<id>` cards whose photo or video could not be loaded for the live preview. They stay in the
    /// document; the preview just doesn't draw them. Cleared whenever the sources are re-resolved from scratch.
    private var skippedPreviewMedia: Set<String> = []
    /// How many of the edit's cards the live preview is not drawing (a non-blocking note, never an error).
    @Published private(set) var previewSkippedMediaCount = 0
    /// Cards loaded for the preview at once. Each file keeps its own request/resource timeouts.
    static let previewMediaConcurrency = 4
    private var resolvedSources: [Int: ResolvedEditorSource]?
    /// KRI-719: a multi-clip Talking edit's footage cutaways, drawn over the speakers in the live preview
    /// only (never part of the document). Set with `resolvedSources`, cleared with it.
    private var previewTalkingCutaways: [NativePhoneTalkingSource.Cutaway] = []
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
    /// The fallback card offers Retry: the live preview failed for a reason a rebuild can fix. A permanent
    /// failure (no video track, an unsupported effect, a missing font) would fail the same way again.
    var fallbackOffersRetry: Bool {
        guard isShowingRenderedFallback, case .failed = sourcePreviewState else { return false }
        return !sourcePreviewFailureIsPermanent
    }

    /// A source-composited player is editable. A failed source preparation can
    /// instead show only the server-rendered video, with canvas interaction
    /// deliberately disabled until a retry produces a source preview.
    var isShowingRenderedFallback: Bool {
        // KRI-587: a finished render that could not be loaded is a black box,
        // not a fallback. Claiming "Showing your last finished video" over it
        // was the confusing screen; the plain failure view shows instead.
        guard sourcePreviewState.isFailure, !finishedRenderFailed, finishedRenderIsCurrent,
              !hasUnsavedChanges, chatStagedDocument == nil else { return false }
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
            return player === finishedRenderPlayer && finishedRenderIsCurrent && !hasUnsavedChanges && chatStagedDocument == nil
        case .failed, .originalsUnavailable:
            // A failed preview may use only a finished render that still
            // represents this document. Dirty or staged edits need the retry
            // surface so the canvas cannot silently revert their changes.
            return isShowingRenderedFallback
        }
    }
    @Published private(set) var isDirectManipulating = false
    @Published private(set) var canEditTimeline = true
    @Published private(set) var canEditText = true
    @Published private(set) var canEditCaptions = false
    @Published private(set) var canEditMix = false
    /// A legacy cloud render remains playable after an account moves to device-only creation,
    /// but accepting another editor mutation would require the unavailable cloud path.
    @Published private(set) var cloudEditorUnavailableMessage: String?
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

    func useDeviceRendering(_ sessions: DeviceRenderSessions) {
        deviceRenders = sessions
        observeDeviceRenderPresentations(sessions)
    }

    /// `deviceRenderRetryNeeded` follows the live render instead of freezing the
    /// moment it was set: the chat poll, a Resume in the render sheet or the
    /// coordinator itself can heal the render while the editor is open, and the
    /// banner (and its retry button) must clear when they do.
    private func observeDeviceRenderPresentations(_ sessions: DeviceRenderSessions) {
        withObservationTracking {
            _ = sessions.presentations
        } onChange: { [weak self, weak sessions] in
            Task { @MainActor [weak self, weak sessions] in
                guard let self, let sessions, self.deviceRenders === sessions else { return }
                self.deviceRenderPresentationChanged()
                self.observeDeviceRenderPresentations(sessions)
            }
        }
    }

    /// Re-derives the device-render retry banner from the current presentation.
    func deviceRenderPresentationChanged() {
        guard case .deviceRenderRetryNeeded = saveState, let key = deviceRenderKey,
              let presentation = deviceRenders?.presentations[key] else { return }
        if Self.deviceRenderNeedsRetry(presentation) {
            let updated = NativeEditorSaveState.deviceRenderRetryNeeded(Self.deviceRenderRetryMessage(presentation))
            if saveState != updated { saveState = updated }
            return
        }
        // Healed: go back to waiting for this save's video, which the preview poll installs.
        if let generation = pendingPreviewGeneration {
            saveState = .previewPending
            startPreviewRefresh(generation: generation)
        } else {
            saveState = .idle
        }
    }

    /// The render cannot finish without the creator (a retry, a Resume, a change).
    static func deviceRenderNeedsRetry(_ presentation: DeviceRenderPresentation) -> Bool {
        presentation.requiresServerRetry || presentation.phase == .needsAttention
            || presentation.phase == .cancelled
            || (presentation.phase == .localReady && presentation.message != nil)
    }

    static func deviceRenderRetryMessage(_ presentation: DeviceRenderPresentation) -> String {
        switch DeviceRenderAttentionCopy.nextStep(presentation) {
        case .resume:
            return "Your edit is saved. Rendering is paused on this iPhone; resume it when you’re ready."
        case .reviewInChat:
            return "Your edit is saved. This iPhone can’t render this version; review it in chat and Kria will make one it can."
        case .updateApp:
            return "Your edit is saved. This edit needs a newer version of Kria. Update the app to finish it."
        case .renderNow:
            return "Your edit is saved. Kria can render it on this iPhone now."
        case .retry:
            break
        }
        return presentation.message ?? "Your edit is saved, but this iPhone needs to retry rendering it."
    }

    /// The device render behind the retry banner is the creator's own pause.
    var deviceRenderIsPaused: Bool { deviceRenderNextStep == .resume }

    /// What the retry banner offers: a retry, a Resume, the conversation for an edit
    /// this iPhone declined (a retry would be declined the same way), or an app update.
    var deviceRenderNextStep: DeviceRenderAttentionCopy.NextStep {
        guard let key = deviceRenderKey, let presentation = deviceRenders?.presentations[key] else { return .retry }
        return DeviceRenderAttentionCopy.nextStep(presentation)
    }
    /// Upload records outlive this screen. The session only observes their
    /// results and turns a ready source into one local document transaction.
    func useMediaUploads(_ uploads: BackgroundUploadCoordinator) { mediaUploads = uploads }
    func suspendEditorImports() { editorImportsActive = false }
    func resumeEditorImports() async {
        editorImportsActive = true
        await resumePendingEditorPlacements()
    }

    /// The last phone-rendering answer the server actually gave. A failed read says nothing new.
    private var lastPhoneRenderingCapabilities: PhoneRenderingCapabilities?
    /// A reconcile was skipped because capabilities were never readable; the preview poll retries it.
    private var deviceReconcilePending = false

    func refreshDeviceRender(retry: Bool = false) async {
        guard let key = deviceRenderKey, let api, let deviceRenders else { return }
        let capabilities: PhoneRenderingCapabilities
        do {
            let response = try await api.creationCapabilities()
            capabilities = response.phoneRendering ?? .disabled
            lastPhoneRenderingCapabilities = capabilities
        } catch {
            // Substituting `.disabled` for a failed read routed the edit to "Rendering on iPhone is
            // not available for this edit yet" and stopped it, offline or on a 5xx. Keep the last
            // real answer; without one, wait and try again rather than guess.
            guard let last = lastPhoneRenderingCapabilities else {
                deviceReconcilePending = true
                return
            }
            capabilities = last
        }
        deviceReconcilePending = false
        await deviceRenders.reconcile(key, capabilities: capabilities, retry: retry)
    }

    func showDeviceOutput(_ url: URL) {
        guard rendersOnDevice else { return }
        // KRI-587: a render made on this phone starts a fresh recovery, and its own copy is what a failed
        // finished render retries first.
        deviceOutputURL = url
        startPlaybackRecoveryEpisode()
        // KRI-535: a device render made before a staged chat edit lacks that edit.
        let isCurrent = chatStagedDocument == nil
        if finishedRenderIsDead {
            // Replaced even while the editable preview builds: nothing else will swap out a dead player.
            finishedRenderIsCurrent = isCurrent
            reinstallFinishedRender(url: url, preferredDuration: authoritativeDuration)
            return
        }
        installPlayer(url: url, preferredDuration: authoritativeDuration, isCurrent: isCurrent)
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
    /// The creation thread this edit belongs to (from open-in-editor for a gallery video).
    var creationThreadID: UUID? { threadID }
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
    /// How long each library Visual has been preparing (KRI-294), so a slow
    /// wait says so. Session-owned like the retry budget.
    @Published private(set) var visualPreparation = VisualPreparationClock()
    private var visualPollRunning = false
    /// Clock seam so a test can step the automatic-retry and preparation schedules.
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

    /// `exportEditorState` after staging any chat draft the editor has not shown yet (KRI-529).
    /// The server treats this state as authoritative and ignores its own head draft, so a state
    /// exported before the previous chat edit was staged silently drops that edit. The sync is
    /// best-effort (it retries internally and never throws), so it can never block a send.
    func exportEditorStateSyncingDraft(maxBytes: Int? = nil) async -> EditorStateRequest? {
        await synchronizePromptRevision()
        return exportEditorState(maxBytes: maxBytes)
    }

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

    /// Three-way per-lane merge of a chat draft built on `submitted`. A lane only
    /// one side changed keeps that side. KRI-935: a lane changed on BOTH sides
    /// takes the chat's version, because it is the newest edit and the creator
    /// asked for it; the caller's single Undo step restores their own version.
    private func mergeChatDraft(_ staged: (document: EditorDocument, sections: Set<EditorSection>), over submitted: EditorDocument) -> (document: EditorDocument, taken: Set<EditorSection>) {
        var merged = document
        var taken: Set<EditorSection> = []
        for section in staged.sections where !lanesEqual(section, staged.document, submitted) {
            copy(section, from: staged.document, into: &merged); taken.insert(section)
        }
        merged.revision.number = staged.document.revision.number ?? merged.revision.number
        // Deletes made after the chat saw this state stay deleted.
        let unseen = Set(document.deletions).subtracting(submitted.deletions)
        return (Self.honoringDeletions(unseen, in: merged), taken)
    }

    /// A chat lane copied over the creator's own edit never brings back what they
    /// deleted and the chat never saw deleted (KRI-935).
    private static func honoringDeletions(_ deletions: Set<EditorDeletion>, in value: EditorDocument) -> EditorDocument {
        guard !deletions.isEmpty else { return value }
        func ids(_ kind: String) -> Set<String> { Set(deletions.filter { $0.kind == kind }.map(\.id)) }
        let text = ids("text"), cues = ids("caption_cue"), sounds = ids("sound_effect")
        let overlays = ids("media_overlay"), blocks = ids("visual_block")
        var kept = value
        kept.textElements.removeAll { text.contains($0.id) || ($0.isCaption && cues.contains($0.id)) }
        kept.captionCues.removeAll { cues.contains($0.id) }
        kept.soundEffects.removeAll { sounds.contains($0.id) }
        kept.mediaOverlays.removeAll { overlays.contains($0.id) }
        kept.visualBlocks.removeAll { blocks.contains($0.id) }
        return kept
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
    /// KRI-626: iOS restarted its media services, so every player, audio session and composition is dead.
    nonisolated(unsafe) private var mediaServicesResetObserver: NSObjectProtocol?
    /// The item whose failure was already reported, so status + failed-to-end (which can both fire for one
    /// failure) produce one diagnostic and one recovery.
    private weak var failureHandledItem: AVPlayerItem?
    /// A play tap that arrived while the editable preview was still building. Played as soon as a
    /// displayable player exists (the ready preview, or the finished-render fallback).
    private var pendingPlayRequest = false
    /// KRI-587: spaced automatic retries for a finished render that would not load. Right after the
    /// phone's own export, AVFoundation can refuse a perfectly good MP4 for a moment (busy decoders,
    /// media services settling). One immediate refresh was not enough and left "The video couldn't be
    /// loaded" up for good. Internal so tests can shorten it.
    var finishedRenderRetryDelays: [Duration] = [.milliseconds(500), .milliseconds(1500), .seconds(3)]
    private var finishedRenderRetryCount = 0
    private var finishedRenderRecoveryTask: Task<Void, Never>?
    private var finishedRenderRecoveryInFlight = false
    /// This phone's own copy of a phone render: retried before any network link.
    private var deviceOutputURL: URL?
    /// Every retry of the finished render failed. The canvas then shows the plain failure view, never
    /// the "Showing your last finished video" card over a dead player.
    @Published private(set) var finishedRenderFailed = false
    /// The editable preview's state before the finished-render failure replaced its message.
    private var stateBeforeFinishedRenderFailure: NativeSourcePreviewState?
    /// KRI-587: one quiet rebuild of the editable preview after a playback-level failure, before the
    /// fallback card shows. Internal so tests can shorten it.
    var sourcePreviewQuietRetryDelay: Duration = .seconds(1)
    private var sourcePreviewQuietRetryUsed = false
    private var sourcePreviewQuietRetryTask: Task<Void, Never>?
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
            displayedVideoIsCurrent = isShowingRenderedFallback
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
        if let rendered = finishedRenderRoute(deviceLocalFile: deviceLocalFile) { return rendered }
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

    /// KRI-1032: with no edit on top of the finished render, the creator's saved or shared copy IS
    /// that render: the file the phone made (or its upload), never a recompile of the editor
    /// document. The recompile only approximates the render: it lost the clip-sound mutes, the
    /// bars and the label placement the render had, and "Done: only the Recoleta clip keeps its
    /// sound" described a video the creator never got. A saved edit makes the render stale
    /// (`acknowledge`) until its re-render lands, and the editable preview is the copy then.
    private func finishedRenderRoute(deviceLocalFile: URL?) -> NativeEditorVideoDownloadRoute? {
        guard let finishedRenderURL, finishedRenderIsCurrent, !hasUnsavedChanges, chatStagedDocument == nil,
              !isSaving, pendingPreviewGeneration == nil, let target = videoDownloadTarget else { return nil }
        if let deviceLocalFile, let deviceOutputURL,
           deviceLocalFile.standardizedFileURL == deviceOutputURL.standardizedFileURL {
            return .localFile(deviceLocalFile)
        }
        // This phone's own render file stands in for the upload (`showDeviceOutput`).
        if finishedRenderURL.isFileURL {
            return FileManager.default.fileExists(atPath: finishedRenderURL.path) ? .localFile(finishedRenderURL) : nil
        }
        return .server(target)
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
    func exportDisplayedSourcePreview(progress: (@Sendable (Double) -> Void)? = nil) async throws -> NativeEditorTemporaryVideo {
        guard sourcePreviewState == .ready, sourcePreviewSettled,
              let sourcePreview,
              player?.currentItem === sourcePreview.preview.playerItem else {
            throw NativeEditorVideoDownloadError.unavailable
        }
        let snapshot = sourcePreview.exportSnapshot()
        let exportRecipe = recipeWithLiveSongVolume(snapshot.recipe)
        let wasPlaying = isPlaying
        pausePlayback()
        defer { if wasPlaying && !Task.isCancelled { togglePlayback() } }
        do {
            return try await performLocalExport(recipe: exportRecipe, assetURLs: snapshot.assetURLs, progress: progress)
        } catch {
            if error is CancellationError { throw error }
            try Task.checkCancellation()
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-export-first-attempt", error: error)
            #endif
            return try await performLocalExport(recipe: exportRecipe, assetURLs: snapshot.assetURLs, progress: progress)
        }
    }

    private func performLocalExport(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], progress: (@Sendable (Double) -> Void)?) async throws -> NativeEditorTemporaryVideo {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "kria-editor-download-\(UUID().uuidString)", directoryHint: .isDirectory)
        let output = directory.appending(path: "current-preview.mp4")
        do {
            let checkpoint = try await AVFoundationLocalExporter(
                stateStore: FileExportStateStore(directory: directory.appending(path: "state", directoryHint: .isDirectory))
            ).export(recipe: recipe, assetURLs: assetURLs, outputURL: output, progress: progress)
            guard checkpoint.status == .completed, checkpoint.outputURL == output else {
                throw NativeEditorVideoDownloadError.unavailable
            }
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-gameday-add"),
               let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
                let evidence = documents.appendingPathComponent("kri-669-current-preview.mp4")
                try? FileManager.default.removeItem(at: evidence)
                try FileManager.default.copyItem(at: output, to: evidence)
            }
            #endif
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
        trustsInitialPlaybackURL: Bool = true,
        previewFrameRenderer: @escaping NativePreviewFrameRenderer = NativePreviewImageRequest.render
    ) {
        var initialDocument = EditorDocument(snapshot: Self.snapshotPreservingClipMetadata(draft))
        initialDocument.revision.number = draft.revision
        self.projectID = draft.projectID
        self.etag = draft.etag
        self.compatibilitySnapshot = draft.serverSnapshot
        self.document = initialDocument
        self.cleanDocument = initialDocument
        self.previewFrameRenderer = previewFrameRenderer
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
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-captured-creation") {
            // A file-backed fixture needs the same identity as an opened job.
            // Keep the normal export readiness/dirty/revision checks in force.
            jobID = draft.projectID
            variantKey = "guided_story"
        }
        // KRI-288: the API stub throws, so the Effects library would always be empty in UI tests.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
            soundEffectCatalog = NativeEditorUITestFixtures.soundEffectCatalog
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-legacy-visuals") {
            itemID = "fixture-visual-item"
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") {
            if ProcessInfo.processInfo.environment["KRIA_OVERLAY_RESET"] == "1" {
                UserDefaults.standard.removeObject(forKey: "overlay-fixture-saved-visuals")
                UserDefaults.standard.removeObject(forKey: "overlay-fixture-generation-count")
                UserDefaults.standard.removeObject(forKey: "overlay-fixture-registrations")
                for key in UserDefaults.standard.dictionaryRepresentation().keys {
                    if key.hasPrefix("overlay-plan.overlay-ui-fixture.") {
                        UserDefaults.standard.removeObject(forKey: key)
                    } else if key.hasPrefix("generated-media-recovery."),
                              let data = Data(base64Encoded: String(key.dropFirst("generated-media-recovery.".count))),
                              let identity = try? JSONDecoder().decode([String].self, from: data),
                              identity.first == "overlay-ui-fixture" {
                        UserDefaults.standard.removeObject(forKey: key)
                    }
                }
            }
            if let saved = UserDefaults.standard.data(forKey: "overlay-fixture-saved-visuals"),
               let visuals = try? JSONDecoder().decode(JSONValue.self, from: saved) {
                var snapshot = document.encodeSnapshot()
                if var payload = snapshot["editor_payload"]?.objectValue {
                    var sections = payload["sections"]?.objectValue ?? [:]
                    sections["visual_blocks"] = visuals
                    payload["sections"] = .object(sections)
                    snapshot["editor_payload"] = .object(payload)
                } else { snapshot["visual_blocks"] = visuals }
                document = EditorDocument(snapshot: snapshot)
                cleanDocument = document
            }
            api = NativeEditorUITestCommitTransport.api()
            sourceResolver = NativeEditorSourceResolver(project: BackgroundUploadCoordinator.projectDirectory(draft.projectID), jobID: draft.projectID)
            itemID = "fixture-overlay-item"
            jobID = draft.projectID
            variantKey = "initial"
            rendersOnDevice = true
            guidedRevisionNumber = 3
            for key in ["visual_blocks", "visual_editor_style", "text_elements"] {
                document.capabilities[key] = EditorCapability(editable: true)
            }
            cleanDocument.capabilities = document.capabilities
            previewVariant = ["editor_capabilities": .object(["phone_editor_media": .object([
                "enabled": .bool(true), "source_registration": .bool(true), "visual_kinds": .array([.string("image")])
            ])])]
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-drawing") {
            // KRI-589: Save goes through the real request encoder to a stub transport (no account, no server).
            api = NativeEditorUITestCommitTransport.api()
            itemID = "fixture-drawing-item"
            variantKey = "initial"
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-speaker-background") {
            // KRI-1118: same stub commit transport; the pool visuals are canned (see `refreshVisualLibrary`).
            api = NativeEditorUITestCommitTransport.api()
            itemID = "fixture-speaker-background-item"
            variantKey = "initial"
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-delayed-source") { loadState = .loading }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-song-reference") {
            previewVariant = NativeEditorUITestFixtures.songReferenceVariant
        }
        let userSongLipSync = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-user-song-lipsync")
        if userSongLipSync || ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-user-song") {
            let songTrimShape = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-song-trim")
            previewVariant = userSongLipSync
                ? (songTrimShape ? NativeEditorUITestFixtures.userSongLipSyncTrimVariant : NativeEditorUITestFixtures.userSongLipSyncVariant)
                : (songTrimShape ? NativeEditorUITestFixtures.userSongBackgroundTrimVariant : NativeEditorUITestFixtures.userSongVariant)
            if userSongLipSync, songTrimShape { fixtureLipsyncDeltas = NativeEditorUITestFixtures.songTrimTakeDeltas }
            if songTrimShape {
                // The fixture's preview video is not 10 s long; the timeline (4 x 2.5 s) is the video's length.
                setAuthoritativeDuration(10)
                refreshDuration()
            }
            // The server advertises these on a real load; a fixture has no status response to read them from.
            // `-ui-testing-editor-user-song-no-trim` is a server that predates `user_song.trim`.
            let noTrim = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-user-song-no-trim")
            for (key, capability) in NativeEditorUITestFixtures.userSongCapabilities(lipSync: userSongLipSync, trim: !noTrim) {
                document.capabilities[key] = capability
            }
            // A real creator-song video carries no `original_level` until the creator sets one (the song
            // plays alone), unlike the generic fixture mix.
            document.mix.removeValue(forKey: "original_level")
            cleanDocument.capabilities = document.capabilities
            cleanDocument.mix = document.mix
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
        // KRI-306: the video-shape picker, open or closed by the server capability.
        // Values stay nil on the document so the fixture loads clean (no unsaved edit).
        // `-video-shape-fit-only`: orientation closed, bars/crop open (a voiceover montage).
        // `-video-shape-closed`: both closed (a cloud editor): no header button.
        let shapeOpen = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-video-shape")
        let shapeFitOnly = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-video-shape-fit-only")
        if shapeOpen || shapeFitOnly || ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-video-shape-closed") {
            document.capabilities["orientation"] = EditorCapability(
                editable: shapeOpen, reason: shapeOpen ? nil : "orientation_unsupported", value: "portrait")
            document.capabilities["landscape_fit"] = EditorCapability(
                editable: shapeOpen || shapeFitOnly, reason: shapeOpen || shapeFitOnly ? nil : "cloud_unsupported", value: "fit")
            // The loaded baseline carries the same capabilities, as a real load does.
            cleanDocument.capabilities = document.capabilities
        }
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
        mediaServicesResetObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in self?.handleMediaServicesReset() }
        }
    }

    deinit {
        if let mediaServicesResetObserver { NotificationCenter.default.removeObserver(mediaServicesResetObserver) }
        previewRefreshTask?.cancel()
        sourcePreviewTask?.cancel()
        durationLoadTask?.cancel()
        seekRecoveryTask?.cancel()
        scrubFrameTask?.cancel()
        textInteractionTask?.cancel()
        observingPlayer?.pause()
        if let timeObserver, let observingPlayer { observingPlayer.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        if let itemFailureObserver { NotificationCenter.default.removeObserver(itemFailureObserver) }
        finishedRenderRecoveryTask?.cancel()
        sourcePreviewQuietRetryTask?.cancel()
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
    /// KRI-306: the server advertises the video-shape controls (either capability present).
    var hasVideoShapeCapability: Bool { document.capabilities["orientation"] != nil || document.capabilities["landscape_fit"] != nil }
    /// At least one axis can be changed: the header button exists only then. A cloud editor
    /// carries a closed `landscape_fit` on every map and keeps its read-only orientation row.
    var canEditVideoShape: Bool { canEditSection(.orientation) || canEditSection(.landscapeFit) }
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
        switch EditorDocument.canonicalOrientation(document.orientation) {
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
        if captionGestureChange == nil { endTransaction() }
        else { captionGestureChoicePresented = true }
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
        let frameDuration = player?.currentItem?.videoComposition?.frameDuration.seconds ?? 1.0 / 30
        // A sub-frame start otherwise decodes the preceding frame, before this item exists.
        seek(to: (start / frameDuration).rounded(.up) * frameDuration)
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
        // left an unrendered tap target over the burned caption. Read-only text
        // (`isReadOnly`, the server's kill-switch shape for a phone Voiceover
        // title, KRI-465) gets no item: the preview still draws it, but nothing
        // can select, drag or reorder it into a text Save the server refuses.
        // An editable title has no `read_only`, so it is listed like any text.
        items += document.textElements.enumerated().filter { !document.isCaptionCueMirror($0.element) && !$0.element.isReadOnly }.map { index, item in projected(EditorSelection(kind: .text, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 300 + index), sourceIndex: index) }
        items += document.captionCues.enumerated().map { index, item in projected(EditorSelection(kind: .captionCue, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 400 + index), sourceIndex: index) }
        items += document.soundEffects.enumerated().map { index, item in projected(EditorSelection(kind: .soundEffect, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 100 + index), sourceIndex: index) }
        items += document.mediaOverlays.enumerated().map { index, item in projected(EditorSelection(kind: .mediaOverlay, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 200 + index), sourceIndex: index) }
        items += document.visualBlocks.enumerated().map { index, item in projected(EditorSelection(kind: .visualBlock, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 150 + index), sourceIndex: index) }
        items += document.motionScenes.enumerated().map { index, item in projected(EditorSelection(kind: .motionScene, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 250 + index), sourceIndex: index) }
        items += document.cameraEffects.enumerated().map { index, item in projected(EditorSelection(kind: .cameraEffect, id: item.id), start: item.startS, end: item.endS, zIndex: timelineZ(item.raw, fallback: 260 + index), sourceIndex: index) }
        items += document.drawings.enumerated().map { index, item in projected(EditorSelection(kind: .drawing, id: item.id), start: item.startS, end: item.endS, zIndex: 270 + index, sourceIndex: index) }
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
            let sourceSpan: TimeInterval
            if let available, usesNarratedSourceFit, slot.raw["native_composite_source"] != .bool(true) {
                // KRI-290: narrated clips are trimmable, so derive the slow-to-fill
                // span from the slot's current length; the span hydrated on load
                // would replay a trimmed clip sped up or slowed down.
                sourceSpan = NativeNarratedSourceTiming.span(target: movingDuration, available: available)
            } else {
                let selectedSpan = Self.number(slot.raw["source_end_s"]).flatMap { end -> Double? in
                    let span = end - sourceStart
                    return span.isFinite && span > 0 ? span : nil
                }
                sourceSpan = min(
                    selectedSpan ?? Self.number(slot.raw["native_source_span_s"]) ?? movingDuration,
                    available ?? .greatestFiniteMagnitude
                )
            }
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

    func load(api: any KriaAPIClient, threadID: UUID, variantID: String? = nil, allowPlaybackFallback: Bool = true) async {
        conversationRuntimeVersion = 2
        resetBackgroundRefresh()
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
            await applyCloudReadOnlyIfNeeded(api: api, variant: authoritativeVariant)
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
                    isCurrent: authoritativeVariant?["render_status"]?.stringValue == "ready"
                        && chatStagedDocument == nil)
            } else if allowPlaybackFallback, let jobID, let url = try? await api.playbackURL(jobID: jobID) {
                installPlayer(url: url, preferredDuration: authoritativeDuration)
            }
            loadState = .loaded
            loadSucceeded()
            await prepareSourcePreview()
            await resumePendingEditorPlacements()
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-load-failed", error: error)
            NativePreviewDiagnostics.record("editor-load-context", fields: ["job": jobID?.uuidString ?? "none"])
            #endif
            if scheduleLoadRetry(after: error, { [weak self] in
                await self?.load(api: api, threadID: threadID, variantID: variantID, allowPlaybackFallback: allowPlaybackFallback)
            }) { return }
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
    /// after a manual edit never replaces it: the newer version becomes the base
    /// and the creator's unsaved lanes are replayed on top (KRI-935).
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
        if isSaving, loadState == .loaded, !promptSyncStopped,
           refreshSync == .reconnecting || isRunningRefreshRetry {
            // A retry that lands mid-save would otherwise be dropped and leave the
            // offline hint up with nothing scheduled to clear it: try again shortly.
            scheduleRefreshRetry(retryAfter: nil)
            return
        }
        guard loadState == .loaded, !isSaving, !promptSyncStopped, let api, let threadID else { return }
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
                    // No active job yet (between jobs, or a re-plan whose variant
                    // is not seeded) is a normal moment, not an error: keep the
                    // loaded edit and wait for the next event.
                    guard let activeJob = latest.activeJobID.flatMap(UUID.init(uuidString:)),
                          let activeItem = latest.activePlanItemID else { return }
                    let variants = latest.job?.variants ?? []
                    let selected = latest.state?["selected_variant_id"]?.stringValue
                        ?? (variants.count == 1 ? variants.first?.variantID : nil)
                    guard let selected, variants.contains(where: { $0.variantID == selected }) else { return }
                    targetJob = activeJob; targetVariant = selected; targetItem = activeItem
                } else {
                    // An unavailable projection can only reuse the complete
                    // currently loaded target; never mix old and new IDs.
                    guard let jobID, let variantKey, let itemID else { return }
                    targetJob = jobID; targetVariant = variantKey; targetItem = itemID
                }
                let current = try await api.editorVariant(jobID: targetJob, variantID: targetVariant)
                noteRefreshReadSucceeded()
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
                // Only a complete read proves the path works: a draft read followed by a failing
                // variant read must not reset the streak, or a persistent conflict retries forever.
                noteRefreshReadSucceeded()
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
            if sameTarget { refreshEditingPermissions(from: variant, document: nextDocument) }
            guard !sameTarget || nextDocument != cleanDocument || staged != nil else { return }
            // Only the previous chat draft is unsaved: the newer cumulative
            // head replaces it wholesale instead of conflicting with it.
            // Editor-state turn: the draft says which submitted state it was built on,
            // so merge three-way against THAT instead of guessing from the head.
            if let staged, sameTarget, variantUnchanged(variant), pendingText == nil, !isSaving,
               let stateID = snapshot.snapshot["client_state_id"]?.stringValue,
               let submitted = submittedEditorStates[stateID] {
                let result = mergeChatDraft(staged, over: submitted.document)
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
                // Text still being typed or a save in flight owns the document; the
                // next sync (every chat send runs one first) stages this draft then.
                guard pendingText == nil, !isSaving else {
                    Self.stagingLog.debug("sync deferred: text entry or save in flight rev=\(snapshot.draftRevision, privacy: .public)")
                    return
                }
                // Unsaved local edits on the same rendered variant: keep both sides
                // lane-by-lane. A lane the chat also changed takes the chat's version
                // (the newest edit, which the creator asked for); one Undo restores
                // the creator's own version (KRI-935).
                if let staged, variantUnchanged(variant) {
                    appendUndo(document); redoStack.removeAll()
                    var merged = document
                    for section in staged.sections { copy(section, from: staged.document, into: &merged) }
                    document = Self.honoringDeletions(Set(merged.deletions), in: merged)
                    changedSections.formUnion(staged.sections)
                    appliedChatDraftRevision = snapshot.draftRevision
                    chatStagedDocument = nil
                    refreshDirtyState(); refreshDuration()
                    return
                }
                // Same rendered generation and no new chat draft: nothing newer exists,
                // so the creator's edits and their Undo history stay exactly as they are.
                if staged == nil, sameTarget, variantUnchanged(variant) { return }
                // A newer version was rendered (or the chat moved the edit to another
                // variant) while the creator has unsaved edits. Load it without asking:
                // it becomes the clean base, the creator's lanes are replayed on top
                // (one Undo drops them), and a staged chat edit lands last.
                Self.stagingLog.debug("sync rebase: newer version under local edits rev=\(snapshot.draftRevision, privacy: .public)")
                if !sameTarget { appliedChatDraftRevision = nil }
                if conversationRuntimeVersion == 1 { legacyPromptSnapshot = nextDraft.serverSnapshot }
                jobID = nextJobID; variantKey = nextVariantKey; itemID = snapshot.itemID
                rebaseLocalEdits(onto: nextDocument, variant: variant, forcePreviewRebuild: !sameTarget)
                if let staged {
                    appendUndo(document); redoStack.removeAll()
                    var merged = document
                    for section in staged.sections { copy(section, from: staged.document, into: &merged) }
                    document = Self.honoringDeletions(Set(merged.deletions), in: merged)
                    changedSections.formUnion(staged.sections)
                    appliedChatDraftRevision = snapshot.draftRevision
                    refreshDirtyState(); refreshDuration()
                }
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
            let failure = Self.refreshFailure(error)
            // The loaded job was superseded by a re-plan render (its editor
            // routes now answer "content plan unavailable"/409): follow the
            // thread to its current job instead of reporting anything.
            if conversationRuntimeVersion == 2, failure == .readConflict {
                let before = jobID
                await adoptActiveJobFromThread(api: api, threadID: threadID)
                if jobID != before || newerJobPrompt != nil || refreshSync == .newVersionRendering { return }
            }
            handleRefreshFailure(failure)
        }
    }

    /// Why a background refresh read failed, sorted by what the editor should do about it.
    enum RefreshFailure: Equatable {
        /// The request was cancelled (view gone, superseded). Nothing happened.
        case cancelled
        /// Signed out or app update required. The app-wide gates already took over.
        case appGate
        /// Offline, timeout, 5xx, 429. Retry on its own; `retryAfter` is the server's hint in seconds.
        case transient(retryAfter: TimeInterval?)
        /// A read-path 409 (generation race, re-plan in flight, superseded job). Never "changed elsewhere".
        case readConflict
        /// The thread no longer has this edit (started over, deleted on another device).
        case targetLost
        /// The chat runtime is switched off; the editor keeps working without draft sync.
        case runtimeUnavailable
        /// Anything else. Logged, never shown: a background read has no action for the creator.
        case other
    }

    static let targetLostMessage = "This project was started over or removed in another place, so this edit isn’t linked to it anymore. Go back to the chat to keep going."

    static func refreshFailure(_ error: Error) -> RefreshFailure {
        if error is CancellationError { return .cancelled }
        if let urlError = error as? URLError, urlError.code == .cancelled { return .cancelled }
        if RequestFailureCause(error) == .connection { return .transient(retryAfter: nil) }
        guard let apiError = error as? APIError else { return .other }
        switch apiError {
        case .sessionExpired, .nativeUpdateRequired:
            return .appGate
        case .contentPlanUnavailable:
            return .readConflict
        case .conflict:
            return apiError.conflictCode == "draft_target_missing" ? .targetLost : .readConflict
        case let .requestFailed(status, detail):
            if status == 408 || status == 429 || (500...599).contains(status) {
                return .transient(retryAfter: detail.retryAfter)
            }
            guard status == 404 else { return .other }
            switch detail.code {
            case "thread_not_found", "thread_deleted", "draft_target_missing": return .targetLost
            case "kria_runtime_unavailable": return .runtimeUnavailable
            default: return .other
            }
        default:
            return .other
        }
    }

    /// Any successful refresh read proves the path works again: drop the quiet hint and pending retry.
    private func noteRefreshReadSucceeded() {
        refreshFailureStreak = 0
        refreshRetryTask?.cancel(); refreshRetryTask = nil
        if refreshSync == .reconnecting { refreshSync = .idle }
    }

    private func handleRefreshFailure(_ failure: RefreshFailure) {
        Self.stagingLog.debug("sync failed kind=\(String(describing: failure), privacy: .public) streak=\(self.refreshFailureStreak, privacy: .public)")
        switch failure {
        case .cancelled, .other:
            return
        case .appGate:
            refreshRetryTask?.cancel(); refreshRetryTask = nil
            refreshFailureStreak = 0
            if refreshSync == .reconnecting { refreshSync = .idle }
        case let .transient(retryAfter):
            refreshFailureStreak += 1
            if refreshFailureStreak >= Self.reconnectingHintAfterFailures, refreshSync == .idle { refreshSync = .reconnecting }
            scheduleRefreshRetry(retryAfter: retryAfter)
        case .readConflict:
            // A race between two reads settles in a moment; a persistent one has nothing to retry.
            refreshFailureStreak += 1
            if refreshFailureStreak <= Self.readConflictRetryLimit { scheduleRefreshRetry(retryAfter: nil) }
        case .targetLost:
            stopPromptSync()
            refreshSync = .targetLost(Self.targetLostMessage)
        case .runtimeUnavailable:
            stopPromptSync()
        }
    }

    static let reconnectingHintAfterFailures = 3
    static let readConflictRetryLimit = 2

    private func scheduleRefreshRetry(retryAfter: TimeInterval?) {
        refreshRetryTask?.cancel()
        guard !refreshRetryDelays.isEmpty else { refreshRetryTask = nil; return }
        var delay = refreshRetryDelays[min(max(refreshFailureStreak - 1, 0), refreshRetryDelays.count - 1)]
        // Honour the server's Retry-After, capped so a bad header cannot park the editor.
        if let retryAfter { delay = max(delay, .seconds(min(retryAfter, 120))) }
        refreshRetryTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self else { return }
            self.refreshRetryTask = nil
            await self.runRefreshRetry()
        }
    }

    /// True while a scheduled or resumed retry runs, so a save in progress defers it instead of dropping it.
    private var isRunningRefreshRetry = false

    private func runRefreshRetry() async {
        isRunningRefreshRetry = true
        defer { isRunningRefreshRetry = false }
        await synchronizePromptRevision()
    }

    private func stopPromptSync() {
        promptSyncStopped = true
        refreshRetryTask?.cancel(); refreshRetryTask = nil
        refreshFailureStreak = 0
    }

    /// The connection came back or the app returned to the foreground: run a pending retry now
    /// instead of waiting out its backoff.
    func resumeRefreshSync() async {
        guard refreshRetryTask != nil || refreshSync == .reconnecting else { return }
        refreshRetryTask?.cancel(); refreshRetryTask = nil
        await runRefreshRetry()
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

    /// KRI-529: reopen an already-loaded editor after chat turns WITHOUT a full reload.
    ///
    /// Every chat turn bumps the thread revision, so `needsReload` is true on each reopen and
    /// the full `load` ran: loading state, the OLD rendered video, then the chat edit a few
    /// seconds later, and any unsaved local edit reset. When the rendered variant is still the
    /// generation this editor was built from, nothing was rendered in the meantime: stage the
    /// newest chat draft in place instead. A different job, a different variant or a newer
    /// render returns false, and the caller keeps the full reload (which swaps the video).
    ///
    /// KRI-535: the creator's own unsaved edits are never dropped by this path. A newer render of
    /// the same job raises `newerJobPrompt` ("Switch" discards, "Keep editing" stays), and a
    /// transient failure to read the variant keeps the session as it is.
    func reconcileOnOpen(project: ProjectSummary, api: any KriaAPIClient) async -> Bool {
        guard loadState == .loaded, !isSaving, project.runtimeVersion == 2, conversationRuntimeVersion == 2,
              let jobID, let variantKey,
              project.activeJobID.map({ $0 == jobID }) ?? true,
              project.outputVariantID.map({ $0 == variantKey }) ?? true else { return false }
        let hasOwnEdits = hasUnsavedChanges && !hasOnlyChatStagedChanges
        let current: [String: JSONValue]
        do {
            current = try await api.editorVariant(jobID: jobID, variantID: variantKey)
        } catch {
            // Superseded (re-plan) keeps the full path, which follows the thread to its new job.
            // Anything else (a dropped connection) must not wipe local edits or error the editor.
            switch Self.refreshFailure(error) {
            case .readConflict:
                return false
            case .transient:
                // Keep the editor that works, clean or not, and look again quietly. A full reload
                // here would replace it with "The editor couldn't open" while offline.
                handleRefreshFailure(Self.refreshFailure(error))
                return true
            case .cancelled, .appGate:
                return true
            case .targetLost, .runtimeUnavailable, .other:
                return hasOwnEdits
            }
        }
        if variantUnchanged(current) {
            loadedServerRevision = project.serverRevision
            await synchronizePromptRevision()
            return true
        }
        guard hasOwnEdits, !isSaving else { return false }
        // A newer render exists and reloading would discard the creator's work: ask first.
        // `loadedServerRevision` stays put, so the next open asks again until they decide.
        newerJobPrompt = project
        return true
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
        guard isBehindActiveJob(project), !isSaving, let target = project.activeJobID else { return }
        // A re-plan points the thread at its new job before that job has a video. Loading it then
        // threw away the working video for "The editor couldn't open", and the prompt claimed a new
        // version was ready while it was still rendering. Keep editing the current video until then.
        switch await Self.readiness(ofJob: target, preferredVariantID: project.outputVariantID, api: api) {
        case .ready:
            break
        case .rendering:
            refreshSync = .newVersionRendering
            watchNewVersion(project, api: api)
            return
        case .failed:
            // The new render failed; the chat says so. The current video stays as it is.
            newVersionWatchTask?.cancel(); newVersionWatchTask = nil
            if refreshSync == .newVersionRendering { refreshSync = .idle }
            return
        }
        guard isBehindActiveJob(project), !isSaving else { return }
        newVersionWatchTask?.cancel(); newVersionWatchTask = nil
        if refreshSync == .newVersionRendering { refreshSync = .idle }
        if hasUnsavedChanges, !hasOnlyChatStagedChanges {
            newerJobPrompt = project
            return
        }
        await switchToLatestJob(project, api: api)
    }

    enum JobReadiness: Equatable { case ready, rendering, failed }

    /// Whether a job has a variant the editor can open. An unreadable status (offline, a server
    /// hiccup) counts as still rendering: the watch asks again, and nothing is discarded meanwhile.
    static func readiness(ofJob jobID: UUID, preferredVariantID: String?, api: any KriaAPIClient) async -> JobReadiness {
        guard let variants = try? await api.editorVariants(jobID: jobID) else { return .rendering }
        let identified = variants.filter { $0["variant_id"]?.stringValue != nil }
        let candidates = preferredVariantID.flatMap { id in
            let matching = identified.filter { $0["variant_id"]?.stringValue == id }
            return matching.isEmpty ? nil : matching
        } ?? identified
        if candidates.contains(where: { $0["render_status"]?.stringValue == "ready" }) { return .ready }
        if !candidates.isEmpty, candidates.allSatisfy({ $0["render_status"]?.stringValue == "failed" }) { return .failed }
        return .rendering
    }

    /// Ask again until the new job is ready, then run the normal adopt decision once.
    private func watchNewVersion(_ project: ProjectSummary, api: any KriaAPIClient) {
        guard let target = project.activeJobID else { return }
        if newVersionWatchTask != nil, newVersionWatchTarget == target { return }
        newVersionWatchTask?.cancel()
        newVersionWatchTarget = target
        let delays = newVersionPollDelays
        newVersionWatchTask = Task { [weak self] in
            var attempt = 0
            // About 15 minutes at the default cadence, longer than any render the server allows.
            while attempt < 64, !delays.isEmpty {
                try? await Task.sleep(for: delays[min(attempt, delays.count - 1)])
                attempt += 1
                guard !Task.isCancelled, let self, self.isBehindActiveJob(project) else { return }
                let readiness = await Self.readiness(ofJob: target, preferredVariantID: project.outputVariantID, api: api)
                guard !Task.isCancelled else { return }
                if readiness != .rendering {
                    self.newVersionWatchTask = nil
                    await self.adoptLatestJob(project, api: api)
                    return
                }
            }
            guard !Task.isCancelled, let self else { return }
            self.newVersionWatchTask = nil
            if self.refreshSync == .newVersionRendering { self.refreshSync = .idle }
        }
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

    /// A load starts from the server's current answer, so earlier refresh trouble no longer applies.
    private func resetBackgroundRefresh() {
        refreshRetryTask?.cancel(); refreshRetryTask = nil
        newVersionWatchTask?.cancel(); newVersionWatchTask = nil; newVersionWatchTarget = nil
        loadRetryTask?.cancel(); loadRetryTask = nil
        refreshFailureStreak = 0
        promptSyncStopped = false
        refreshSync = .idle
    }

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
        sourcePreview = nil; sourceCompiler = nil; sourcePool = nil; resolvedSources = nil; previewTalkingCutaways = []
        liveSongVolumeActive = false; previewBuiltDocument = nil
        sourcePreviewState = .idle; sourcePreviewGeneration = nil
        previewVariant = [:]
        pendingDeviceRenderIdentity = nil
        // The old job's recovery must not touch the new job's player.
        finishedRenderRecoveryTask?.cancel(); finishedRenderRecoveryInFlight = false
        sourcePreviewQuietRetryTask?.cancel()
        deviceOutputURL = nil
        finishedRenderFailed = false; stateBeforeFinishedRenderFailure = nil
        startPlaybackRecoveryEpisode()
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
        resetBackgroundRefresh()
        if let previous = jobID, previous != editorJobID { discardPlaybackForNewJob() }
        jobID = editorJobID
        isSaving = true
        saveState = .saving
        loadState = .loading
        Perf.begin("editor.open", id: editorJobID.uuidString)
        defer { isSaving = false; Perf.end("editor.open", id: editorJobID.uuidString); Perf.mark("editor.open.done") }
        // Neither of these needs the job's status, so they start now and overlap it. They used to follow it one after
        // the other, three round trips in a row before the editor could show anything. The draft is compared with the
        // resolved variant only once that is known.
        let capabilitiesTask = Task { try? await api.creationCapabilities() }
        let draftTask: Task<DraftSnapshot?, Never>? = if conversationRuntimeVersion == 2, let threadID {
            Task { try? await api.draft(threadID: threadID) }
        } else {
            nil
        }
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
            await applyCloudReadOnlyIfNeeded(api: api, variant: variant, capabilities: capabilitiesTask)
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
            if conversationRuntimeVersion == 2, let head = await draftTask?.value,
               head.itemID == resolvedPlanItemID, head.variantKey == resolved.variantID,
               head.baseJobID.flatMap(UUID.init) == editorJobID,
               let staged = stagedChatDocument(snapshot: head, draft: loadedDraft, variant: variant) {
                applyStagedChatDocument(staged, revision: head.draftRevision)
                refreshDuration()
                // KRI-535: the rendered video predates this staged chat edit. Keep it
                // hidden behind "Preparing preview" until the local composition (which
                // has the edit) is ready, instead of showing the old version as current.
                finishedRenderIsCurrent = false
            }
            loadState = .loaded
            loadSucceeded()
            await prepareSourcePreview()
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.failure("editor-load-failed", error: error)
            NativePreviewDiagnostics.record("editor-load-context", fields: ["job": jobID?.uuidString ?? "none"])
            #endif
            if scheduleLoadRetry(after: error, { [weak self] in
                await self?.load(editorJobID: editorJobID, planItemID: planItemID, preferredVariantID: preferredVariantID,
                                 threadID: threadID, api: api)
            }) { return }
            saveState = .loadFailed(error.localizedDescription)
            loadState = .failed(error.localizedDescription)
        }
    }

    private func loadSucceeded() {
        loadRetryAttempt = 0
        loadWaitingMessage = nil
    }

    /// A first open that failed for a reason that passes on its own (still rendering, offline, a
    /// server hiccup, the two-read generation race) waits and tries again behind the opening screen
    /// instead of showing "The editor couldn't open". Returns false when the failure should show.
    private func scheduleLoadRetry(after error: Error, _ retry: @escaping @MainActor () async -> Void) -> Bool {
        let delays: [Duration]
        let limit: Int
        let message: String
        let apiError = error as? APIError
        if apiError == .editorNotReady {
            delays = loadRenderingDelays
            limit = 80
            message = "Your video is still rendering. The editor opens as soon as it’s ready."
        } else {
            switch Self.refreshFailure(error) {
            case .transient:
                delays = loadRetryDelays; limit = Self.loadTransientRetryLimit
                message = RequestFailureCause(error) == .connection
                    ? "Waiting for a connection. The editor opens on its own when you’re back online."
                    : "Kria is taking a moment. Trying again…"
            case .readConflict where apiError != .contentPlanUnavailable && apiError?.conflictCode == nil:
                delays = loadRetryDelays; limit = 2
                message = "Loading the latest cut…"
            default:
                loadSucceeded()
                return false
            }
        }
        guard loadRetryAttempt < limit, !delays.isEmpty else {
            loadSucceeded()
            return false
        }
        let delay = delays[min(loadRetryAttempt, delays.count - 1)]
        loadRetryAttempt += 1
        loadWaitingMessage = message
        saveState = .idle
        loadState = .loading
        loadRetryTask?.cancel()
        loadRetryTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self else { return }
            // Cleared first: the load cancels any pending retry, and this task must not cancel itself.
            self.loadRetryTask = nil
            await retry()
        }
        return true
    }

    #if DEBUG
    /// KRI-669: deterministic provider/HTTP fixtures, real original resolution,
    /// placement, compiler, undo and local export. No account or network.
    @Published private(set) var gamedayFixtureSaved = false

    func prepareGamedayFixture(url: URL, overlays: Bool = false) async {
        do {
            api = NativeEditorUITestCommitTransport.api()
            if !overlays {
                itemID = "fixture-gameday-item"; variantKey = "guided_story"; jobID = projectID
                rendersOnDevice = true; guidedRevisionNumber = 3
                previewVariant = ["resolved_archetype": .string("guided_story"), "editor_capabilities": .object([
                    "phone_editor_media": .object(["enabled": .bool(true), "source_registration": .bool(true)])])]
            }
            let project = BackgroundUploadCoordinator.projectDirectory(projectID)
            let store = SourceAssetStore(project: project)
            func original(_ resource: URL, mediaID: String) async throws -> (MediaAsset, JSONValue) {
                let fingerprint = try SHA256Fingerprinter().fingerprint(file: resource)
                let asset: MediaAsset
                if let existing = try store.bindings().first(where: { $0.mediaID == mediaID }) {
                    guard existing.original.fingerprint == fingerprint else { throw SourceAssetError.changedOriginal(mediaID) }
                    _ = try store.resolve(mediaIDs: [mediaID])
                    asset = existing.original
                } else {
                    asset = try await AssetImportCoordinator(project: project).importAsset(from: resource)
                }
                let video = AVURLAsset(url: resource)
                guard let track = try await video.loadTracks(withMediaType: .video).first else { throw NativeEditorRenderError.missingVideoTrack }
                let size = try await track.load(.naturalSize)
                let transform = try await track.load(.preferredTransform)
                let orientation = (Int((atan2(transform.b, transform.a) * 180 / .pi).rounded()) % 360 + 360) % 360
                let descriptor = OriginalMediaDescriptor(sha256: fingerprint.hex, byteCount: fingerprint.byteCount,
                    durationS: try await video.load(.duration).seconds, width: Int(size.width), height: Int(size.height),
                    orientationDegrees: orientation, hasAudio: !(try await video.loadTracks(withMediaType: .audio)).isEmpty)
                return (asset, try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(descriptor)))
            }
            let (asset, originalValue) = try await original(url, mediaID: "gameday-source-0")
            guard let fifthURL = Bundle.main.url(forResource: "istanbul", withExtension: "mp4") else { throw APIError.invalidResponse }
            let (fifthAsset, fifthOriginal) = try await original(fifthURL, mediaID: "gameday-istanbul-source-4")
            var clips: [JSONValue] = []
            for index in 0...4 {
                let mediaID = index == 4 ? "gameday-istanbul-source-4" : "gameday-source-\(index)"
                try store.bind(mediaID: mediaID, original: index == 4 ? fifthAsset : asset)
                clips.append(.object(["clip_index": .number(Double(index)), "native_source": .object([
                    "media_id": .string(mediaID), "original": index == 4 ? fifthOriginal : originalValue, "local_required": .bool(true)])]))
            }
            let pool: [String: JSONValue] = ["base_generation": .string(document.revision.baseGeneration), "clips": .array(clips)]
            UserDefaults.standard.set(try JSONEncoder().encode(pool), forKey: NativeEditorUITestFixtures.gamedayPoolKey)
            let request = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: projectID, variantID: variantKey ?? "guided_story",
                recipeRevision: 3, recipeDigest: String(repeating: "a", count: 64)),
                recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
                    tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 4)])]))
            let requestValue = try JSONDecoder().decode(JSONValue.self, from: RecipeJSON.encoder().encode(request))
            let recipeStatus: [String: JSONValue] = ["phase": .string("published"), "request": requestValue,
                "published_generation": .string(document.revision.baseGeneration)]
            UserDefaults.standard.set(try JSONEncoder().encode(recipeStatus), forKey: NativeEditorUITestFixtures.gamedayRecipeKey)
            cleanDocument = document
            gamedayFixtureSaved = UserDefaults.standard.data(forKey: NativeEditorUITestFixtures.gamedaySavedKey) != nil
            mediaUploads = BackgroundUploadCoordinator(api: api!, defaultsKey: "gameday-fixture-placement", sessionConfiguration: .ephemeral)
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays"),
               let api, let itemID, let sourceResolver, !document.visualBlocks.isEmpty {
                let library = try await api.visuals(itemID: itemID)
                for block in document.visualBlocks where block.kind == "media" {
                    guard let asset = library.assets.first(where: { $0.id == block.raw["asset_id"]?.stringValue }),
                          let original = asset.originalMediaURL else { throw APIError.invalidResponse }
                    authoredVisualSources["visual:" + block.id + ":" + block.id] = try await sourceResolver.resolveMedia(
                        id: asset.id, url: original, generation: document.revision.baseGeneration)
                }
            }
            await prepareSourcePreview()
            NSLog("KRI669 fixture prepared %@", gamedayFixtureSummary)
        } catch {
            NSLog("KRI669 fixture setup failed %@", String(describing: error))
            failSourcePreview(error)
        }
    }

    func addGamedayFixtureClip() async {
        guard let api, let itemID, let variantKey, let mediaUploads else { return }
        let target = EditorSourceRegistrationTarget(itemID: itemID, variantID: variantKey, clientImportID: UUID(),
            baseGeneration: document.revision.baseGeneration, guidedRevisionNumber: 3, sourceKind: .footage)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 4)
        mediaUploads.beginEditorPlacement(placement)
        do {
            let response = try await api.registerEditorSource(target, sourceID: "gameday-istanbul-source-4")
            try await placeEditorSource(placement, response: response)
        } catch { addClipError = error.localizedDescription }
    }

    var gamedayFixtureSummary: String {
        let texts = displayedSourcePreviewRecipe?.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }.joined(separator: ";") ?? "unavailable"
        let count = displayedSourcePreviewRecipe?.tracks.filter { $0.kind == .video }.flatMap(\.clips).count ?? 0
        return "clips=\(document.clips.count);previewClips=\(count);text=\(texts);current=\(hasSourcePreview);saved=\(gamedayFixtureSaved);state=\(String(describing: sourcePreviewState));generation=\(document.revision.baseGeneration);saveState=\(String(describing: saveState));dirty=\(hasUnsavedChanges)"
    }

    func saveGamedayFixture() async {
        await save()
        NSLog("KRI669 fixture after save %@", gamedayFixtureSummary)
        guard !hasUnsavedChanges else { return }
        do { UserDefaults.standard.set(try JSONEncoder().encode(draft), forKey: NativeEditorUITestFixtures.gamedaySavedKey); gamedayFixtureSaved = true }
        catch { saveState = .failed(error.localizedDescription) }
    }

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
                if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-captured-creation"),
                   ProcessInfo.processInfo.environment["KRIA_UI_FIXTURE_LOCAL_SOURCES"] == "1" {
                    // Physical QA copies server-compiled sources into this separate app's Documents.
                    // Missing media must fail visibly, never substitute the bundled demo footage.
                    let root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                        .appendingPathComponent("researchqa")
                    let paths = try JSONDecoder().decode([String: String].self,
                        from: Data(contentsOf: root.appendingPathComponent("source-files.json")))
                    guard let name = paths[String(index)] else { throw NativeEditorRenderError.missingVideoTrack }
                    sourceURL = root.appendingPathComponent(name)
                    sourceFingerprint = try SHA256Fingerprinter().fingerprint(file: sourceURL)
                }
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
             NativeEditorRenderError.missingFont, NativeEditorRenderError.tooManyTextLayers,
             NativeEditorRenderError.missingSpeechCaptionTiming:
            return true
        default:
            return false
        }
    }

    static func sourcePreviewMessage(for error: Error) -> String {
        switch error {
        case APIError.conflict:
            return "This video changed while it was loading. Retry to load the newest version."
        case APIError.sessionExpired:
            return "Sign in again to load the source videos."
        case NativeEditorRenderError.missingSpeechCaptionTiming:
            return "Your range is kept. Add captions for the newly exposed speech, turn captions off, or restore the previous range before exporting."
        case NativeEditorRenderError.unsupportedLane:
            return "This edit contains an effect that iPhone preview does not support yet."
        case NativeEditorRenderError.missingFont:
            return "A required font is missing from this app build. Update the app and try again."
        case NativeEditorRenderError.missingVideoTrack:
            return "This edit’s video can’t be previewed on iPhone yet."
        case NativeEditorRenderError.tooManyTextLayers:
            return "This edit has more on-screen text than iPhone preview can show. Remove some text or captions to preview it."
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
        Perf.begin("editor.preview", id: jobID.uuidString)
        defer { Perf.end("editor.preview", id: jobID.uuidString) }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        let sequence = sourcePreviewSequence
        let generation = document.revision.baseGeneration
        sourcePreviewGeneration = generation
        sourcePreviewState = .preparing
        player?.pause()
        isPlaying = false
        resolvedSources = nil
        previewTalkingCutaways = []
        sourcePool = nil
        resolvedAudio = [:]
        deviceNarrationResolutionGeneration = nil
        resolvedMedia = [:]
        skippedPreviewMedia = []
        previewSkippedMediaCount = 0
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
            var phoneTalkingIndices: [Int]?
            var phoneTalkingCutaways: [NativePhoneTalkingSource.Cutaway] = []
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
                phoneTalkingIndices = NativePhoneTalkingSource.sourceIndices(variant: previewVariant, document: document,
                    pool: pool, lanesEditable: phoneTalkingLanesEditable)
                if let speakers = phoneTalkingIndices {
                    // A cutaway the preview can't place would show the speaker where the MP4 cuts away:
                    // fall back to the finished MP4 rather than preview a different video.
                    if let cutaways = NativePhoneTalkingSource.cutaways(variant: previewVariant, pool: pool, speakerIndices: speakers) {
                        phoneTalkingCutaways = cutaways
                    } else {
                        phoneTalkingIndices = nil
                    }
                }
                if NativePhoneNarratedSource.applies(variant: previewVariant, document: document) {
                    phoneNarratedSeeds = await loadPhoneNarratedSeeds(pool: pool, generation: generation, sequence: sequence)
                }
                // Built up in steps: one long `+` chain times out the type checker on CI.
                var requiredIndices = Set<Int>(timelineClips.compactMap(\.sourceClipIndex))
                requiredIndices.formUnion(phoneTalkingIndices ?? [])
                requiredIndices.formUnion(phoneTalkingCutaways.map(\.clipIndex))
                requiredIndices.formUnion(document.clipAudio.map(\.sourceClipIndex))
                requiredIndices.formUnion(phoneNarratedSeeds.map(\.clipIndex))
                sources = try await resolver.resolve(pool, generation: generation, requiredIndices: requiredIndices)
            }
            guard sequence == sourcePreviewSequence, !Task.isCancelled,
                  document.revision.baseGeneration == generation else { return }
            if let phoneTalkingIndices {
                let speakers = try phoneTalkingIndices.map { index in
                    guard let duration = sources[index]?.asset.duration else { throw APIError.invalidResponse }
                    return NativePhoneTalkingSource.Speaker(clipIndex: index, duration: duration)
                }
                // Captions/lanes are on the speech-cleanup cut timeline (KRI-232).
                let removed = NativePhoneTalkingSource.removedSpans(variant: previewVariant)
                #if DEBUG
                NativePreviewDiagnostics.record("phone-talking-source", fields: ["index": phoneTalkingIndices.map(String.init).joined(separator: ","), "duration": speakers.map { String($0.duration) }.joined(separator: ","), "removed": String(removed.count)])
                #endif
                document = try NativePhoneTalkingSource.hydrate(document, speakers: speakers, removed: removed)
                cleanDocument = try NativePhoneTalkingSource.hydrate(cleanDocument, speakers: speakers, removed: removed)
                chatStagedDocument = try chatStagedDocument.map { try NativePhoneTalkingSource.hydrate($0, speakers: speakers, removed: removed) }
                undoStack = try undoStack.map { try NativePhoneTalkingSource.hydrate($0, speakers: speakers, removed: removed) }
                redoStack = try redoStack.map { try NativePhoneTalkingSource.hydrate($0, speakers: speakers, removed: removed) }
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
            previewTalkingCutaways = phoneTalkingIndices == nil ? [] : phoneTalkingCutaways
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
    private func refreshRebasedSourcePreview(force: Bool = false) {
        let generation = document.revision.baseGeneration
        guard force || sourcePreviewGeneration != generation else { return }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        resolvedSources = nil
        previewTalkingCutaways = []
        sourcePool = nil
        resolvedAudio.removeAll()
        deviceNarrationResolutionGeneration = nil
        resolvedMedia.removeAll()
        skippedPreviewMedia = []
        previewSkippedMediaCount = 0
        sourcePreview = nil
        liveSongVolumeActive = false; previewBuiltDocument = nil
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
    /// current recipe is the authority for narration and the creator's song. Its
    /// saved or published generation must be the document we are reconstructing.
    static func currentDeviceRecipeRequest(
        _ status: DeviceRenderStatusResponse,
        jobID: UUID,
        variantID: String,
        generation: String
    ) throws -> DeviceRenderRequest {
        let matchesGeneration = status.phase == "published"
            ? status.publishedGeneration == generation
            : ["awaiting_device", "syncing", "needs_attention"].contains(status.phase)
                && status.baseGeneration == generation
        guard !generation.isEmpty, status.request.identity.jobID == jobID,
              status.request.identity.variantID == variantID, matchesGeneration else {
            throw APIError.conflict
        }
        return status.request
    }

    static func currentDeviceNarrationRequest(
        _ status: DeviceRenderStatusResponse,
        jobID: UUID,
        variantID: String,
        generation: String
    ) throws -> DeviceRenderRequest? {
        let request = try currentDeviceRecipeRequest(status, jobID: jobID, variantID: variantID, generation: generation)
        return request.recipe.audio.narrationAssetID == nil ? nil : request
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

    /// The pinned recipe's audio the preview must reconstruct on a cold launch.
    private struct DeviceRecipeAudio {
        var narration: ResolvedEditorSource?
        var song: (source: ResolvedEditorSource, bed: NativeEditorSongBed)?
    }

    private func resolveDeviceRecipeAudio(document: EditorDocument, sequence: Int) async throws -> DeviceRecipeAudio {
        guard let api, let jobID, let variantKey else { throw APIError.invalidResponse }
        let status = try await api.deviceRender(jobID: jobID, variantID: variantKey)
        guard sequence == sourcePreviewSequence, !Task.isCancelled,
              document.revision.baseGeneration == self.document.revision.baseGeneration else {
            throw CancellationError()
        }
        let request = try Self.currentDeviceRecipeRequest(
            status, jobID: jobID, variantID: variantKey, generation: narrationOwnerGeneration(document)
        )
        let narrationAssetID = request.recipe.audio.narrationAssetID
        let songBed = NativeEditorSongBed(recipe: request.recipe)
        var result = DeviceRecipeAudio()
        guard narrationAssetID != nil || songBed != nil else { return result }
        let project = BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID)
        let authorized = AuthorizedDeviceSourceResolver(
            api: api,
            request: request,
            originals: SourceAssetStore(project: project),
            library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
        )
        func measured(_ url: URL, _ asset: MediaAsset) async throws -> MediaAsset {
            let duration = try await AVURLAsset(url: url).load(.duration).seconds
            guard duration.isFinite, duration > 0 else { throw APIError.invalidResponse }
            var asset = asset
            asset.duration = duration
            return asset
        }
        if let narrationID = narrationAssetID {
            guard let asset = request.recipe.assets.first(where: { $0.id == narrationID }) else {
                throw MediaEngineError.missingAsset("narration")
            }
            let url = try await authorized.resolveNarration()
            result.narration = ResolvedEditorSource(clipIndex: -1, mediaID: narrationID, asset: try await measured(url, asset), url: url)
        }
        if let songBed {
            guard let asset = request.recipe.assets.first(where: { $0.id == songBed.assetID }) else {
                throw MediaEngineError.missingAsset(NativeEditorSongBed.trackID)
            }
            let url = try await authorized.resolveSong()
            result.song = (ResolvedEditorSource(clipIndex: -1, mediaID: songBed.assetID, asset: try await measured(url, asset), url: url), songBed)
        }
        guard sequence == sourcePreviewSequence, !Task.isCancelled,
              document.revision.baseGeneration == self.document.revision.baseGeneration else {
            throw CancellationError()
        }
        return result
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
                let recipeAudio = try await resolveDeviceRecipeAudio(document: document, sequence: sequence)
                guard sequence == sourcePreviewSequence, !Task.isCancelled,
                      document.revision.baseGeneration == self.document.revision.baseGeneration else {
                    throw CancellationError()
                }
                if let narration = recipeAudio.narration { resolvedAudio["narration"] = narration }
                // The creator's own song plays from the recipe's `song` clip whatever
                // `music_playback_mode` says; a recipe without one leaves the bed nil.
                if let song = recipeAudio.song {
                    resolvedAudio[NativeEditorRenderCompiler.songSourceKey] = song.source
                    userSongAudioURL = song.source.url
                }
                deviceSongBed = recipeAudio.song?.bed
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
        let ids = Set([referenceOnlyMusic || resolvedAudio["narration"] != nil || resolvedAudio[NativeEditorRenderCompiler.songSourceKey] != nil ? nil : document.music?.trackID,
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
        // One unloadable card must not drop the whole live preview (a 33-card edit once fell back to the
        // finished video over a single bad file): it is skipped, kept in the document, and noted. Cards load
        // a few at a time rather than one after another.
        let generation = document.revision.baseGeneration
        var wanted: [(key: String, asset: NativeEditorAsset)] = []
        for overlay in document.mediaOverlays {
            let key = "overlay:" + overlay.id
            guard resolvedMedia[key] == nil, authoredVisualSources[key] == nil, !skippedPreviewMedia.contains(key) else { continue }
            guard let asset = sourcePool?.nativeAssets.first(where: { $0.kind == "media_overlay" && $0.id == overlay.id }) else {
                skippedPreviewMedia.insert(key)
                NativePreviewDiagnostics.record("preview-media-skipped", fields: ["reason": "no-source"])
                continue
            }
            wanted.append((key, asset))
        }
        if !wanted.isEmpty {
            sourcePreviewState = .preparing
            let outcomes = try await Self.resolveConcurrently(wanted, limit: Self.previewMediaConcurrency) { asset in
                var resolved = try await resolver.resolveMedia(id: asset.mediaID, url: asset.sourceURL, generation: generation)
                resolved.preserveAlpha = asset.preserveAlpha == true
                return resolved
            }
            guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
            for (key, outcome) in outcomes {
                switch outcome {
                case .success(let resolved): resolvedMedia[key] = resolved
                case .failure:
                    skippedPreviewMedia.insert(key)
                    NativePreviewDiagnostics.record("preview-media-skipped", fields: ["reason": "load-failed"])
                }
            }
        }
        let skippedNow = document.mediaOverlays.filter { skippedPreviewMedia.contains("overlay:" + $0.id) }.count
        if previewSkippedMediaCount != skippedNow { previewSkippedMediaCount = skippedNow }
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
        if let background = document.speakerBackground {
            let key = EditorSpeakerBackground.sourceKey(forMediaID: background.mediaID)
            if resolvedMedia[key] == nil, authoredVisualSources[key] == nil {
                sourcePreviewState = .preparing
                let resolved = try await resolveSpeakerBackgroundSource(background, resolver: resolver)
                guard sequence == sourcePreviewSequence, !Task.isCancelled else { throw CancellationError() }
                resolvedMedia[key] = resolved
            }
        }
        return resolvedMedia.merging(authoredVisualSources) { _, authored in authored }
    }

    /// Runs `work` for every item with at most `limit` in flight. A failing item is returned as a failure and
    /// never stops the others; only cancellation of the surrounding task is rethrown. Results keep input order.
    static func resolveConcurrently(_ items: [(key: String, asset: NativeEditorAsset)], limit: Int,
                                    _ work: @escaping @Sendable (NativeEditorAsset) async throws -> ResolvedEditorSource)
        async throws -> [(key: String, outcome: Result<ResolvedEditorSource, Error>)] {
        var results = [Result<ResolvedEditorSource, Error>?](repeating: nil, count: items.count)
        try await withThrowingTaskGroup(of: (Int, Result<ResolvedEditorSource, Error>).self) { group in
            var next = 0
            func launch() {
                guard next < items.count else { return }
                let index = next, asset = items[next].asset
                next += 1
                group.addTask {
                    do { return (index, .success(try await work(asset))) }
                    catch is CancellationError { throw CancellationError() }
                    catch { return (index, .failure(error)) }
                }
            }
            for _ in 0..<max(1, limit) { launch() }
            while let (index, outcome) = try await group.next() {
                results[index] = outcome
                launch()
            }
        }
        try Task.checkCancellation()
        return items.indices.map { (items[$0].key, results[$0] ?? .failure(CancellationError())) }
    }

    private func scheduleSourcePreviewUpdate() {
        guard resolvedSources != nil, sourceCompiler != nil else { return }
        sourcePreviewTask?.cancel()
        sourcePreviewSequence += 1
        guard !isTimingGestureActive, !isDirectManipulating, !isSongStartDragActive else {
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
            let talkingCutaways = previewTalkingCutaways
            let needed = Set(clips.compactMap(\.sourceClipIndex) + baseline.clipAudio.map(\.sourceClipIndex)
                + talkingCutaways.map(\.clipIndex))
            if !needed.isSubset(of: Set(sources.keys)), let pool = sourcePool, let resolver = sourceResolver {
                sourcePreviewState = .preparing
                sources = try await resolver.resolve(pool, generation: baseline.revision.baseGeneration, requiredIndices: needed)
                guard sequence == sourcePreviewSequence, !Task.isCancelled, document == baseline else { return }
                resolvedSources = sources
            }
            #if DEBUG
            NativePreviewDiagnostics.record("prepare-audio")
            #endif
            var audio = try await preparePreviewAudio(document: snapshot, sequence: sequence)
            // KRI-428: the unsaved song edits play the way the server will render them. A removed song
            // leaves no song source (so the compiler stops muting the camera) and no bed; otherwise the
            // pinned recipe's bed carries the edited volume / start.
            let songRemoved = userSongRemoved
            if songRemoved {
                audio.removeValue(forKey: NativeEditorRenderCompiler.songSourceKey)
                // The server's recipe falls back to the camera's own level once the song is gone.
                // (The creator's own original-audio level was chosen for the mix WITH the song, so the
                // server drops it too.)
                snapshot.mix.removeValue(forKey: "original_level")
            }
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
            // KRI-432: the song is compiled at unity gain and the player carries its level (see
            // `liveSongVolumeActive`); only when something else is audible does the level stay baked in.
            let hasSong = audio[NativeEditorRenderCompiler.songSourceKey] != nil
            let realBed = hasSong ? effectiveSongBed : nil
            // KRI-576: the lyric rows follow the song window as it is now; `snapshot` itself is never rewritten.
            let lyricShift = songCaptionShift
            func compileProgram(songBed: NativeEditorSongBed?) throws -> NativeEditorRenderProgram {
                try compiler.compile(document: snapshot, clips: clips, items: items,
                                     sources: sources, audioSources: audio, mediaSources: media,
                                     referenceOnlyMusic: musicPlaybackMode == .referenceOnly,
                                     sourceAudioPreserved: sourceAudioPreserved || songRemoved,
                                     songBed: songBed, deviceCaptions: rendersOnDevice, guidedStory: isGuidedStory,
                                     songCaptionShift: lyricShift,
                                     skippedMediaOverlayIDs: skippedPreviewMedia,
                                     talkingCutaways: talkingCutaways)
            }
            var program = try compileProgram(songBed: realBed.map {
                NativeEditorSongBed(assetID: $0.assetID, sourceStart: $0.sourceStart, sourceDuration: $0.sourceDuration,
                                    volume: 1, fadeIn: $0.fadeIn, fadeOut: $0.fadeOut, endS: $0.endS)
            })
            let liveVolume = hasSong && Self.songIsOnlyAudibleTrack(program.recipe)
            if hasSong, !liveVolume { program = try compileProgram(songBed: realBed) }
            // The in-place text update only applies while the canvas is still
            // on this composition. After a transient compile failure handed
            // the canvas to the finished-render fallback, its player item was
            // detached and must not be re-attached to a new player; fall
            // through and build a fresh composition so the canvas comes back
            // to the live edit — otherwise every later edit lands off-screen
            // while the user keeps watching the stale cloud render (KRI-110).
            if let preview = sourcePreview, player?.currentItem === preview.preview.playerItem,
               updatePreviewInPlace(preview, program: program) {
                liveSongVolumeActive = liveVolume
                previewBuiltDocument = baseline
                applyLiveSongVolume()
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
            liveSongVolumeActive = liveVolume
            previewBuiltDocument = baseline
            applyLiveSongVolume()
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
        if selection.kind == .visualBlock,
           document.visualBlocks.first(where: { $0.id == selection.id })?.kind == "media" {
            // Same media identity as prepareMediaInteraction: use the rendered
            // footprint for both selection handles and the danger-zone guide.
            return sourcePreview?.mediaSelectionBounds(id: "visual-\(selection.id)-\(selection.id)", time: time)
        }
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
        // The creator pressed play themselves: the audition ends and does not hand the video back.
        if songAudition.isActive, !isRestoringVideoAfterAudition { songAudition.cancel(restoreVideo: false) }
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
                    guard let renderer = self?.previewFrameRenderer else { return }
                    let frame = try await renderer(generator, CMTime(seconds: target, preferredTimescale: 600))
                    guard !Task.isCancelled, let self, self.scrubFrameGeneration == generation else { return }
                    if self.pendingScrubFrameTime != nil { continue }
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
                    if self.pendingScrubFrameTime == nil {
                        self.scrubPreviewFrame = nil
                        self.scrubPreviewTime = nil
                    }
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
        return recipeWithLiveSongVolume(sourcePreview.exportSnapshot().recipe)
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
        // One geometry for every way of changing a window (KRI-770): the Range sheet and this edge drag both
        // go through `KriaSourceWindowGeometry`, which is rate-aware, never returns a length under the 0.1 s
        // minimum (computing `(in + 0.1) - in` directly gives 0.09999999999999998, which the server refused),
        // and mirrors the server's bound. A drag is in output seconds, so it moves the source by `* rate`.
        let rate = Self.explicitPlaybackRate(slot)
        let window = KriaSourceWindowGeometry.Window(start: sourceIn, length: currentDuration)
        let resized: KriaSourceWindowGeometry.Window
        switch active.edge {
        case .leading:
            // Left trim preserves the source Out point. A negative translation restores earlier source frames
            // until the file's beginning. A slot that already overruns its footage is cut back to the footage.
            var base = window
            if let sourceDuration, KriaSourceWindowGeometry.sourceEnd(window, rate: rate) > sourceDuration {
                base.length = max(minimumClipDuration, (sourceDuration - sourceIn) / rate)
            }
            resized = KriaSourceWindowGeometry.resizedLeading(base, by: translation * rate, rate: rate)
        case .trailing:
            // Right trim preserves source In. Later clips are not a ceiling: they ripple after this duration
            // changes. A clip already longer than its footage (narrated footage slowed to fill its voiceover
            // step) can shrink smoothly. With no known source length the end can shrink but never grow past
            // what is there now, because the server refuses an unmeasured source (`source_unmeasured`).
            let end = KriaSourceWindowGeometry.sourceEnd(window, rate: rate)
            resized = KriaSourceWindowGeometry.resizedTrailing(window, by: translation * rate, rate: rate, sourceDuration: sourceDuration ?? end)
        }
        // A gesture that lands back on the baseline must compare equal to it (no phantom undo step).
        if abs(resized.start - slot.inS) >= 1e-9 { slot.raw["source_start_locked"] = .bool(true) }
        slot.inS = abs(resized.start - slot.inS) < 1e-9 ? slot.inS : resized.start
        slot.durationS = abs(resized.length - currentDuration) < 1e-9 && slot.durationS != nil
            ? slot.durationS : max(minimumClipDuration, resized.length)
        if slot.raw["source_end_s"] != nil {
            slot.raw["source_end_s"] = .number(resized.start + resized.length * rate)
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

    #if DEBUG
    /// Test seam (KRI-770): stage slot fields (`playback_rate`, a talking-source id) that a bare draft cannot carry.
    func replaceDocumentForTesting(_ value: EditorDocument) { document = value }
    #endif

    /// The creator-set playback rate (`playback_rate`), 1 when none. Derived rates (slowed narration) are
    /// not edge-draggable at a different speed, so they trim as they always did.
    static func explicitPlaybackRate(_ slot: EditorTimelineSlot) -> Double {
        if let explicit = slot.raw["playback_rate"]?.numberValue, explicit.isFinite, explicit > 0 { return explicit }
        return 1
    }

    func endTrim() {
        activeTrim = nil
        flushDeferredSourcePreviewUpdate()
    }

    /// Dragging or nudging a clip to a new place. The server offers it as `clips.reorder`; a variant that lists
    /// no such capability falls back to the timeline section's editability.
    var canReorderClips: Bool { canEditTimeline && canEditOperation(["clips.reorder"], section: .timeline) }

    func moveSelected(by offset: TimeInterval) {
        guard canReorderClips, let id = selectedClipID, abs(offset) > 0.000001 else { return }
        let ordered = timelineClips
        guard let index = ordered.firstIndex(where: { $0.id == id }) else { return }
        moveClip(id: id, toIndex: min(max(0, index + (offset < 0 ? -1 : 1)), ordered.count - 1))
    }

    /// Puts a clip at `toIndex` among the clips as the timeline draws them. One undo step; the length is unchanged
    /// because each cut stays at its position (`NativeEditorInteraction.reorderedSlots`).
    func moveClip(id: UUID, toIndex: Int) {
        guard canReorderClips else { return }
        let ordered = timelineClips
        guard ordered.indices.contains(toIndex),
              let from = clipSlotIndex(id.uuidString),
              let to = clipSlotIndex(ordered[toIndex].id.uuidString), from != to else { return }
        transactDocument(section: .timeline) { $0.clips = NativeEditorInteraction.reorderedSlots($0.clips, from: from, to: to) }
    }

    func slideSourceWindow(by offset: TimeInterval) {
        guard canEditTimeline, let id = selectedClipID, let index = clipSlotIndex(id.uuidString), document.clips.indices.contains(index) else { return }
        let slot = document.clips[index]
        let length = max(minimumClipDuration, slot.durationS ?? minimumClipDuration)
        // The source span is the length times the real rate, or the slide could write a window the server refuses.
        let found = sourceWindowSlot(for: EditorSelection(kind: .clip, id: id.uuidString))
        let span = length * (found.map { effectiveSourceRate(slot: $0.slot, clip: $0.clip) } ?? 1)
        let sourceEnd = Self.number(slot.raw["source_duration_s"] ?? slot.raw["source_duration"]) ?? slot.inS + span
        let start = min(max(0, slot.inS + offset), max(0, sourceEnd - span))
        transactDocument(section: .timeline) {
            $0.clips[index].inS = start
            if start != slot.inS { $0.clips[index].raw["source_start_locked"] = .bool(true) }
            if let selectedEnd = slot.raw["source_end_s"]?.numberValue {
                $0.clips[index].raw["source_end_s"] = .number(start + max(0, selectedEnd - slot.inS))
            }
        }
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
            if let inS {
                if abs(inS - slot.inS) > 1e-9 { slot.raw["source_start_locked"] = .bool(true) }
                slot.inS = max(0, inS)
            }
            if let durationS { slot.durationS = max(minimumClipDuration, durationS) }
            if let durationBeats { slot.durationBeats = durationBeats }
            if inS != nil || durationS != nil {
                slot.durationBeats = nil
                // Generic timeline callers don't provide a source window.
                slot.raw.removeValue(forKey: "source_end_s")
            }
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
        if let cloudEditorUnavailableMessage {
            addClipError = cloudEditorUnavailableMessage
            return
        }
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

    /// True while this session opened the transaction that coalesces "Add text" keystrokes.
    private var ownsTextCreationTransaction = false

    /// Typing a new text item republishes `pendingText` per keystroke; inside a transaction the
    /// preview rebuild is coalesced (80ms) instead of recompiling the composition per character.
    private func beginTextCreationTransaction() {
        guard transactionBaseline == nil else { return }
        beginTransaction()
        ownsTextCreationTransaction = true
    }

    private func endTextCreationTransaction() {
        guard ownsTextCreationTransaction else { return }
        ownsTextCreationTransaction = false
        endTransaction()
    }

    func beginTextCreation() {
        guard canEditSection(.text), pendingText == nil, duration > 0 else { return }
        beginTextCreationTransaction()
        player?.pause()
        isPlaying = false
        pendingText = newTextElement(content: "")
    }

    /// A new centered text element at the playhead, 2 s long. Shared by the Text tool and paste.
    private func newTextElement(content: String) -> EditorTextElement {
        let start = min(max(0, currentTime), max(0, duration - 0.1))
        return EditorTextElement(
            id: UUID().uuidString, text: content, startS: timelineProjection.unprojectOutputTime(start),
            endS: timelineProjection.unprojectOutputTime(min(duration, start + 2)),
            raw: ["font_family": .string("Inter Regular"), "size_px": .number(72),
                  "x_frac": .number(0.5), "y_frac": .number(0.5), "position": .string("custom"),
                  "color": .string("#FFFFFF"), "effect": .string("none"), "wrap_lines": .bool(false)]
        )
    }

    // MARK: - Paste (KRI-647)

    /// True while a pasted image is uploading to the visual pool and becoming a layer.
    @Published private(set) var isPastingImage = false
    /// Shown as an alert by the editor: paste comes from the "+" menu, where the Visuals panel's own error line isn't on screen.
    @Published var pasteError: String?

    /// Why Paste can't act right now, or nil when it can. Text needs the Text lane, an image needs visuals.
    func pasteUnavailableMessage(for content: PastedContent) -> String? {
        switch content {
        case .text:
            return canEditSection(.text) && duration > 0 ? nil : "Adding text isn’t available for this edit."
        case .image:
            if itemID == nil { return "Open a saved edit to paste a picture." }
            if let message = visualImportUnavailableMessage { return message }
            guard canAuthorVisuals else { return "Adding pictures isn’t available for this edit." }
            // The same limits `addLibraryVisual` / `admitDeviceVisual` enforce after the upload, checked first
            // so a refused picture is never uploaded.
            if document.visualBlocks.count >= 20 { return "This edit already has 20 visuals. Remove one to paste another." }
            if rendersOnDevice, !phoneAllowsVisualKind("image") || editorSourceTarget(kind: .visual) == nil {
                return "Adding pictures isn’t available for this edit on this iPhone."
            }
            return nil
        }
    }

    /// Entry point for the system Paste control. Anything that can't be placed says why, never a silent no-op.
    func paste(providers: [NSItemProvider]) async {
        guard let content = await PasteboardIntake.load(from: providers) else {
            pasteError = "Kria can’t use what’s on your clipboard. Copy text or a picture and try again."
            return
        }
        if let message = pasteUnavailableMessage(for: content) {
            pasteError = message
            if case .image(let url) = content { try? FileManager.default.removeItem(at: url) }
            return
        }
        pasteError = nil
        visualError = nil
        switch content {
        case .text(let text): pasteText(text)
        case .image(let url): await pasteImage(fileURL: url)
        }
    }

    @discardableResult func pasteText(_ text: String) -> EditorSelection? {
        guard let value = PasteboardIntake.normalizedText(text), canEditSection(.text), duration > 0 else { return nil }
        player?.pause()
        isPlaying = false
        let item = newTextElement(content: value)
        transactDocument(section: .text) { $0.textElements.append(item) }
        let selected = EditorSelection(kind: .text, id: item.id)
        select(selected, seekToStart: false)
        return selected
    }

    /// Uploads the picture into the edit's visual pool, then places it exactly as a library tap would
    /// (cloud edit: `addLibraryVisual`; phone-rendered edit: `admitDeviceVisual`), so a pasted PNG
    /// keeps whatever transparency handling those paths give a picked one.
    func pasteImage(fileURL: URL) async {
        guard !isPastingImage else {
            try? FileManager.default.removeItem(at: fileURL)
            pasteError = "Still adding your last picture. Try again in a moment."
            return
        }
        guard let uploads = mediaUploads, let itemID else {
            try? FileManager.default.removeItem(at: fileURL)
            pasteError = "Adding a picture isn’t available right now."
            return
        }
        isPastingImage = true
        defer { isPastingImage = false }
        // The picture lands at the playhead when it finishes uploading, so hold the playhead still.
        player?.pause()
        isPlaying = false
        // `refreshVisualLibrary` skips while another refresh runs; the baseline must be a real read.
        var baselineRead = false
        for _ in 0..<5 where !baselineRead {
            baselineRead = await refreshVisualLibrary()
            if !baselineRead { try? await Task.sleep(for: .milliseconds(400)) }
        }
        guard baselineRead else {
            try? FileManager.default.removeItem(at: fileURL)
            pasteError = "Kria couldn’t reach your visuals. Check your connection and try again."
            return
        }
        let known = Set(visualLibrary.map(\.id))
        let filename = BackgroundUploadCoordinator.displayFilename(fileURL.lastPathComponent)
        let accepted = await uploads.enqueue(fileURL: fileURL, projectID: threadID ?? projectID, source: .files,
            consentGiven: true, purpose: .cloudRenderSource, role: .visual, itemID: itemID)
        // Not deleted on refusal: a busy server leaves a Retry in the visual failures that re-reads this file.
        guard accepted else {
            pasteError = "That picture couldn’t be added. Your edit is unchanged."
            return
        }
        // The background uploader registers the asset with the pool, then analysis marks it ready.
        for _ in 0..<120 {
            guard !Task.isCancelled, self.itemID == itemID else { return }
            _ = await refreshVisualLibrary()
            // Match the upload by its own filename (named from its bytes, so a repeat paste of the same picture
            // finds the pool's existing copy) so a stale refresh or another upload can't hand back a different asset.
            let fresh = visualLibrary.filter { !known.contains($0.id) }
            if let asset = visualLibrary.first(where: { $0.sourceFilename == filename })
                ?? fresh.first(where: { $0.sourceFilename == nil }) {
                if asset.status == "ready" {
                    await addLibraryVisual(asset)
                    if let failure = visualError { pasteError = failure }
                    return
                }
                if asset.status == "failed" {
                    pasteError = "That picture couldn’t be prepared. Your edit is unchanged."
                    return
                }
            }
            try? await Task.sleep(for: .seconds(1))
        }
        pasteError = "Adding the picture is taking longer than expected. If it is already in your visuals, add it from there."
    }

    func updatePendingText(_ content: String) { pendingText?.text = content }

    func cancelTextCreation() {
        pendingText = nil
        endTextCreationTransaction()
    }

    @discardableResult func finishTextCreation() -> EditorSelection? {
        // The staged item is added inside the transaction, so ending it records one undo step.
        defer { endTextCreationTransaction() }
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
        if offersCaptionStyleScope, let caption = document.textElements.first(where: { $0.id == id && $0.isCaption }) {
            let current = captionPosition(id: id)
            let position = isDirectManipulating
                ? CGPoint(x: current.x + x - caption.anchor.x, y: current.y + y - caption.anchor.y)
                : CGPoint(x: x, y: y)
            setCaptionGesturePosition(id: id, position: position)
            return
        }
        guard canEditSection(.text) else { return }
        mutateText(id: id) {
            $0.raw["position"] = .string("custom")
            $0.raw["x_frac"] = .number(min(max(0, x), 1)); $0.raw["y_frac"] = .number(min(max(0, y), 1))
        }
    }
    /// Apply every sample against the gesture's immutable starting element.
    /// Font size and wrapping width scale together, preserving the text layout.
    func transformText(from baseline: EditorTextElement, scale: Double, rotationDelta: Double) {
        if baseline.isCaption, offersCaptionStyleScope {
            setCaptionGestureSize(id: baseline.id, size: captionStyleSize(.line(baseline.id)) * scale)
            return
        }
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
              (phase == "loop" ? ["none", "pulse", "bounce", "float"] : TextAnimationPhases.Edge.allCases.map(\.rawValue)).contains(effect) else { return }
        mutateText(id: id) {
            var phases = Self.textPhases(for: $0)
            phases[phase] = .string(effect)
            if TextAnimationPhases.Edge(rawValue: effect)?.isDesigned == true {
                $0.raw["theme_transition"] = nil
                $0.raw["effect"] = .string("static")
            }
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
    /// Every renderer reads `x_frac` as the block's left edge, centre or right edge by alignment, so switching
    /// alignment alone would swing the block around its old anchor. Re-anchor from the drawn block instead, and
    /// free a preset `position` row (whose fracs are ignored) at the spot it was drawn.
    func setTextAlignment(id: String, alignment: String?) {
        guard canEditSection(.text), let element = textElement(id: id), !element.isReadOnly else { return }
        let block = textBlockRect(id: id)
        let drawnAt = element.anchor
        mutateText(id: id) {
            $0.raw["alignment"] = alignment.map(JSONValue.string) ?? .null
            guard let block else { return }
            $0.raw["x_frac"] = .number(NativeTextBlockGeometry.anchorX(keeping: block, alignment: alignment))
            if element.raw["position"]?.stringValue.flatMap({ EditorTextElement.presetY[$0] }) != nil {
                $0.raw["y_frac"] = .number(drawnAt.y)
                $0.raw["position"] = .string("custom")
            }
        }
    }

    /// Flush the drawn block against the safe area (or the frame's centre) on one axis; alignment stays. Moving the
    /// anchor changes the room a text has to wrap in, so the block is measured again at each trial position.
    func placeTextBlock(id: String, _ placement: NativeTextBlockPlacement) {
        guard canEditSection(.text), let element = textElement(id: id), !element.isReadOnly,
              var block = textBlockRect(id: id) else { return }
        var trial = element
        var anchor = element.anchor
        for _ in 0..<4 {
            let target = NativeTextBlockGeometry.anchor(placing: block, anchor: anchor, at: placement)
            if hypot(target.x - anchor.x, target.y - anchor.y) < 0.0005 { break }
            anchor = target
            trial.raw["position"] = .string("custom")
            trial.raw["x_frac"] = .number(anchor.x); trial.raw["y_frac"] = .number(anchor.y)
            guard let next = textMeasurer?.textBlockRect(for: trial, in: document) else { break }
            block = next
        }
        setTextPosition(id: id, x: anchor.x, y: anchor.y)
    }

    func textBlockPlacements(id: String) -> Set<NativeTextBlockPlacement> {
        textBlockRect(id: id).map(NativeTextBlockGeometry.placements(of:)) ?? []
    }

    private func textBlockRect(id: String) -> CGRect? {
        guard let element = textElement(id: id) else { return nil }
        if textMeasurer == nil, let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) {
            textMeasurer = try? NativeEditorRenderCompiler(fontDirectory: fonts)
        }
        return textMeasurer?.textBlockRect(for: element, in: document)
    }
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
        guard let index = document.textElements.firstIndex(where: { $0.id == id }),
              !document.textElements[index].isReadOnly else { return }
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
        // KRI-1118: the Background fixture's canned pool.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-speaker-background") {
            visualLibrary = NativeEditorUITestFixtures.speakerBackgroundVisuals
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
            var clock = visualPreparation
            clock.observe(library.assets, now: visualAutoRetryClock())
            // Publishes only real changes: every publish re-renders the editor.
            if clock != visualPreparation { visualPreparation = clock }
            visualError = nil
            return true
        } catch {
            if !Task.isCancelled { visualError = "Your visuals couldn’t load. Try again. " + error.localizedDescription }
            return false
        }
    }

    /// Whether the Visuals panel should keep polling: an asset is still
    /// preparing (a fresh upload and a reanalyze both restart at `queued`,
    /// which the API reports as `uploaded` until
    /// `pool_asset_queued_status_enabled` is on), or a transient failure still
    /// has an automatic retry to come.
    var visualLibraryNeedsPolling: Bool {
        visualLibrary.contains { $0.preparationStage != nil || visualAutoRetry.needsObservation(of: $0) }
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
        if let cloudEditorUnavailableMessage { return cloudEditorUnavailableMessage }
        return rendersOnDevice && !canRegisterPhoneSources ? "Adding media isn’t available for this edit on this iPhone." : nil
    }

    var canAddTimelineMedia: Bool {
        cloudEditorUnavailableMessage == nil && (canEditTimeline || document.editorState == "empty") && draft.clips.count < Self.maxTimelineClips && (!rendersOnDevice || canRegisterPhoneSources)
    }
    /// KRI-166: why `canAddTimelineMedia` is false, for the quick-add menu's
    /// Video row (which otherwise just greys out). Mirrors the three
    /// conditions above; nil when adding is allowed.
    var addClipUnavailableReason: String? {
        if let cloudEditorUnavailableMessage { return cloudEditorUnavailableMessage }
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
            if uploads.editorPlacements(itemID: target.itemID, variantID: target.variantID, baseGeneration: target.baseGeneration)
                .first(where: { $0.id == recordID })?.status == "failed" { throw APIError.invalidResponse }
            if uploads.shouldWaitForEditorSourceRegistration(recordID) {
                try await Task.sleep(for: .seconds(1))
                continue
            }
            if let api {
                do {
                    let result = try await api.editorSource(itemID: target.itemID, variantID: target.variantID, importID: target.clientImportID)
                    uploads.recordEditorSourceResult(placementID: recordID, response: result)
                    if result.isTerminal { return result }
                } catch {
                    guard Self.isRetryableEditorSourcePoll(error) else { throw error }
                }
            }
            try await Task.sleep(for: .seconds(1))
        }
        uploads.failEditorPlacement(recordID, error: "Preparation is taking longer than expected. Try again.")
        throw APIError.offline
    }

    /// A missing/retired admission is not a preparation state. Keep the upload
    /// for an explicit retry, but don't hide terminal errors behind the lease.
    static func isRetryableEditorSourcePoll(_ error: Error) -> Bool {
        if let error = error as? APIError {
            switch error {
            case .offline: return true
            case .requestFailed(let status, _): return status == 408 || status == 429 || status >= 500
            default: return false
            }
        }
        if let error = error as? URLError {
            return [.networkConnectionLost, .notConnectedToInternet, .timedOut, .cannotConnectToHost, .cannotFindHost].contains(error.code)
        }
        return false
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
            guard let api, let jobID, let variantKey else { throw APIError.invalidResponse }
            // Admission and preview must use the same source index/original.
            // Resolve before adding an undo step or changing local duration.
            let pool = try await api.editorSourcePool(jobID: jobID, variantID: variantKey)
            guard editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true,
                  !Task.isCancelled, document.revision.baseGeneration == placement.target.baseGeneration else { return }
            guard pool.baseGeneration == placement.target.baseGeneration,
                  let admitted = pool.clips.first(where: { $0.clipIndex == index })?.nativeSource,
                  admitted.mediaID == (response.source?["media_id"]?.stringValue ?? response.sourceID) else {
                throw APIError.invalidResponse
            }
            let resolver = sourceResolver ?? NativeEditorSourceResolver(project: BackgroundUploadCoordinator.projectDirectory(threadID ?? projectID), jobID: jobID)
            let needed = Set(timelineClips.compactMap(\.sourceClipIndex) + document.clipAudio.map(\.sourceClipIndex)).union([index])
            let sources = try await resolver.resolve(pool, generation: placement.target.baseGeneration, requiredIndices: needed)
            guard editorImportsActive, mediaUploads?.containsEditorPlacement(placement.id) == true,
                  !Task.isCancelled, document.revision.baseGeneration == placement.target.baseGeneration else { return }
            guard !document.clips.contains(where: { $0.raw["editor_source_placement_id"] == .string(placementID) }) else {
                mediaUploads?.acknowledgeEditorPlacement(placement.id); return
            }
            guard canAddTimelineMedia else { throw APIError.unsupported }
            let latestNeeded = Set(timelineClips.compactMap(\.sourceClipIndex) + document.clipAudio.map(\.sourceClipIndex)).union([index])
            guard latestNeeded.isSubset(of: Set(sources.keys)),
                  let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) else { throw APIError.invalidResponse }
            let compiler = try NativeEditorRenderCompiler(fontDirectory: fonts)
            sourceResolver = resolver; sourcePool = pool; originalsRecoveryPool = pool
            resolvedSources = sources; sourceCompiler = compiler
            sourcePreviewGeneration = placement.target.baseGeneration
            sourcePreviewState = .preparing
            transactDocument(section: .timeline) {
                $0.clips.append(EditorTimelineSlot(id: placementID, clipIndex: index, inS: 0, durationS: min(Self.addedClipDurationS, duration), raw: [
                    "source_duration_s": .number(sourceDuration(response) ?? placement.localDurationS ?? duration + 0.05),
                    "editor_source_placement_id": .string(placementID),
                ]))
            }
            select(.init(kind: .clip, id: placementID))
            mediaUploads?.acknowledgeEditorPlacement(placement.id)
            await sourcePreviewTask?.value
            return
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

    var overlayPlanScope: String? {
        guard let itemID, let jobID, let variantKey else { return nil }
        return [itemID, jobID.uuidString, variantKey].joined(separator: ".")
    }

    var overlayGenerationAPI: (any KriaAPIClient)? { api }

    var canGenerateOverlayPlan: Bool { rendersOnDevice && canImportVisuals }

    var overlayBaseDuration: Double { timelineProjection.baseClipWindows.last?.end ?? 0 }

    func overlayOutputTime(_ baseTime: Double) -> Double { timelineProjection.projectBaseTime(baseTime) }

    func overlayBaseTime(_ outputTime: Double) -> Double? {
        let base = timelineProjection.unprojectOutputTime(outputTime)
        return abs(timelineProjection.projectBaseTime(base) - outputTime) <= 0.002 ? base : nil
    }

    func overlayDocumentFingerprint() throws -> String {
        try OverlayPlanFingerprint.make(document.encodeSnapshot())
    }

    func requestOverlayPlan(mode: OverlayGenerationMode, prompt: String, tasteNotes: String) async throws -> OverlayPlan {
        guard loadState == .loaded, !isSaving, let api, let itemID, let jobID, let variantKey,
              canGenerateOverlayPlan else { throw APIError.unsupported }
        endTransaction()
        let fingerprint = try overlayDocumentFingerprint()
        let value = try await api.overlayPlan(itemID: itemID, body: OverlayPlanRequest(
            jobID: jobID.uuidString, variantID: variantKey, baseGeneration: document.revision.baseGeneration,
            documentFingerprint: fingerprint, mode: mode, prompt: prompt, tasteNotes: tasteNotes,
            editorSnapshot: document.encodeSnapshot()))
        guard !Task.isCancelled, value.documentFingerprint == fingerprint,
              value.baseGeneration == document.revision.baseGeneration,
              try overlayDocumentFingerprint() == fingerprint,
              value.overlays.count <= 6, Set(value.overlays.map(\.id)).count == value.overlays.count,
              value.overlays.allSatisfy({ $0.isValid(duration: overlayBaseDuration) }) else { throw APIError.invalidResponse }
        return value
    }

    func containsOverlayProposal(_ id: String) -> Bool {
        document.visualBlocks.contains { $0.id == id } || document.textElements.contains { $0.id == id }
    }

    func applyOverlayProposal(_ proposal: OverlayProposal, assetID: String?, plan: OverlayPlan) async throws {
        guard !containsOverlayProposal(proposal.id) else { return }
        guard !isAddingVisual, !isSaving, proposal.isValid(duration: overlayBaseDuration),
              plan.baseGeneration == document.revision.baseGeneration,
              try overlayDocumentFingerprint() == plan.documentFingerprint else { throw APIError.invalidResponse }
        if proposal.kind == .text {
            guard canEdit(.text) else { throw APIError.unsupported }
            let element = EditorTextElement(id: proposal.id, text: proposal.text, startS: proposal.startS, endS: proposal.endS,
                raw: ["font_family": .string("Inter"), "size_px": .number(540 * proposal.scale), "color": .string("#FFFFFF"),
                      "x_frac": .number(proposal.xFrac), "y_frac": .number(proposal.yFrac), "position": .string("custom"),
                      "max_width_frac": .number(proposal.scale), "wrap_lines": .bool(false), "stroke_width": .number(3), "effect": .string("none")])
            transactDocument(section: .text) { $0.textElements.append(element) }
            return
        }
        guard canImportVisuals, document.visualBlocks.count < 20, let api, let itemID, let assetID,
              let sourceResolver else { throw APIError.unsupported }
        isAddingVisual = true
        defer { isAddingVisual = false }
        let library = try await api.visuals(itemID: itemID)
        guard let asset = library.assets.first(where: { $0.id == assetID && $0.status == "ready" && $0.kind == "image" }),
              let originalURL = asset.originalMediaURL else { throw APIError.invalidResponse }
        var admittedPath = asset.gcsPath
        if rendersOnDevice {
            guard let originalTarget = editorSourceTarget(kind: .visual) else { throw APIError.unsupported }
            let stableID = proposal.sourceRegistrationID(assetID: asset.id)
            let target = EditorSourceRegistrationTarget(itemID: originalTarget.itemID, variantID: originalTarget.variantID,
                clientImportID: stableID, baseGeneration: originalTarget.baseGeneration,
                guidedRevisionNumber: originalTarget.guidedRevisionNumber, sourceKind: .visual)
            var response = try await api.registerEditorSource(target, sourceID: asset.id)
            for _ in 0..<120 where response.status == "preparing" {
                try Task.checkCancellation()
                try await Task.sleep(for: .seconds(1))
                response = try await api.editorSource(itemID: target.itemID, variantID: target.variantID, importID: stableID)
            }
            guard response.status == "ready" else { throw APIError.invalidResponse }
            admittedPath = response.source?["gcs_path"]?.stringValue ?? asset.gcsPath
        }
        guard let admittedPath, !admittedPath.isEmpty else { throw APIError.invalidResponse }
        let source = try await sourceResolver.resolveMedia(id: asset.id, url: originalURL, generation: plan.baseGeneration)
        try Task.checkCancellation()
        guard plan.baseGeneration == document.revision.baseGeneration,
              try overlayDocumentFingerprint() == plan.documentFingerprint, canImportVisuals,
              document.visualBlocks.count < 20 else { throw APIError.invalidResponse }
        guard !containsOverlayProposal(proposal.id) else { return }
        guard var block = NativeVisualAuthoring.plannedOverlay(asset: asset, proposal: proposal,
            z: Int(document.visualBlocks.compactMap { $0.raw["z"]?.numberValue }.max() ?? 0) + 1) else { throw APIError.invalidResponse }
        block.raw["src_gcs_path"] = .string(admittedPath)
        authoredVisualSources["visual:" + proposal.id + ":" + proposal.id] = source
        transactDocument(section: .visualBlocks) { $0.visualBlocks.append(block) }
        await prepareSourcePreview()
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

    // MARK: Speaker background (KRI-1118)

    /// The Background tool exists only when the server advertises `speaker_background` for this variant (a
    /// device-rendered Talk-to-camera edit on a build that speaks protocol 27, with the kill switch on).
    /// Never inferred from the lane: a variant with a background but no capability is read-only.
    var canEditSpeakerBackground: Bool {
        cloudEditorUnavailableMessage == nil && canEditSection(.speakerBackground)
    }
    var speakerBackground: EditorSpeakerBackground? { document.speakerBackground }

    /// A Talking edit's live preview hydrates its source clip once any phone lane is editable: cards,
    /// sounds, or (KRI-1118) the speaker background, which composites behind that clip.
    var phoneTalkingLanesEditable: Bool { canEdit(.mediaOverlays) || canEdit(.soundEffects) || canEdit(.speakerBackground) }

    /// Puts one photo or video from the item's Visuals pool behind the speaker, replacing any current one.
    /// `sourceStart` is where a video begins playing; a photo ignores it. One undo step.
    @discardableResult
    func setSpeakerBackground(mediaID: String, kind: VisualMediaKind, sourceStart: Double = 0) -> Bool {
        guard canEditSpeakerBackground,
              let value = EditorSpeakerBackground(json: EditorSpeakerBackground(mediaID: mediaID, kind: kind, sourceStartS: sourceStart).json)
        else { return false }
        transactDocument(section: .speakerBackground) { $0.speakerBackground = value }
        return true
    }

    /// Clears the background. A no-op (and no unsaved change) when there is none.
    @discardableResult
    func removeSpeakerBackground() -> Bool {
        guard canEditSpeakerBackground, document.speakerBackground != nil else { return false }
        transactDocument(section: .speakerBackground) { $0.speakerBackground = nil }
        return true
    }

    /// The pool visuals that can sit behind the speaker: ready photos and videos.
    var speakerBackgroundChoices: [CreationVisual] { Self.backgroundChoices(from: visualLibrary) }
    static func backgroundChoices(from library: [CreationVisual]) -> [CreationVisual] {
        library.filter { $0.status == "ready" && VisualMediaKind(rawValue: $0.kind) != nil }
    }

    /// Bytes for the compiler's pinned `visual-<media_id>` asset. The library is the source of truth for the
    /// original URL and storage generation; it is refreshed once if the background was saved before it loaded.
    private func resolveSpeakerBackgroundSource(_ background: EditorSpeakerBackground, resolver: NativeEditorSourceResolver) async throws -> ResolvedEditorSource {
        func match() -> CreationVisual? {
            visualLibrary.first { $0.id == background.mediaID && $0.kind == background.kind.rawValue && $0.status == "ready" }
        }
        if match() == nil { await refreshVisualLibrary() }
        guard let asset = match(), let url = asset.originalMediaURL else {
            throw MediaEngineError.missingAsset(EditorSpeakerBackground.assetID(forMediaID: background.mediaID))
        }
        var resolved = try await resolver.resolveMedia(id: asset.id, url: url, generation: asset.sourceGeneration ?? document.revision.baseGeneration)
        resolved.visualGeneration = asset.sourceGeneration ?? "0"
        return resolved
    }

    // MARK: Drawings (KRI-589)

    /// The Draw tool exists only when the server opens `drawings.add` for this variant (a phone-rendered
    /// variant on a build that speaks protocol 5, with the kill switch on). Never inferred from the lane.
    var canAddDrawing: Bool {
        cloudEditorUnavailableMessage == nil && document.capabilities["drawings.add"]?.editable == true
    }
    var canEditDrawings: Bool { canEditOperation(["drawings.edit"], section: .drawings) }
    var canDeleteDrawings: Bool { canEditOperation(["drawings.delete"], section: .drawings) }
    func drawing(id: String) -> EditorDrawing? { document.drawings.first { $0.id == id } }

    /// Why the next stroke cannot be added to `id` (a new drawing when nil), or nil when it can: the same
    /// caps the server enforces, checked before Done so a scribble is never lost to a 422 on Save.
    func drawingCapMessage(extraPoints: Int = 0, extraStrokes: Int = 0, editing id: String? = nil) -> String? {
        let others = document.drawings.filter { $0.id != id }
        let current = id.flatMap { drawing(id: $0) }
        if id == nil, document.drawings.count >= DrawingLimits.maxDrawings { return "You can add up to \(DrawingLimits.maxDrawings) drawings." }
        if (current?.strokes.count ?? 0) + extraStrokes > DrawingLimits.maxStrokesPerDrawing { return "That’s as many strokes as one drawing holds." }
        if (current?.pointCount ?? 0) + extraPoints > DrawingLimits.maxPointsPerDrawing { return "That drawing is as detailed as it can get." }
        if others.reduce(0, { $0 + $1.pointCount }) + (current?.pointCount ?? 0) + extraPoints > DrawingLimits.maxPointsPerVariant {
            return "This video has as many drawings as it can hold."
        }
        return nil
    }

    /// Adds the strokes drawn in Draw mode as one drawing at the playhead: a 3 s window clamped to the
    /// video, the pen drawing itself on over 1.2 s, centred where the strokes are. One undo step.
    @discardableResult
    func addDrawing(strokes: [DrawingStroke], canvasAspect: Double) -> Bool {
        guard canAddDrawing, !strokes.isEmpty else { return false }
        let editableEnd = timelineClips.map(\.end).max() ?? duration
        guard let window = NativeVisualAuthoring.window(at: currentTime, duration: editableEnd, projection: timelineProjection, preferred: 3, minimum: 0.5),
              drawingCapMessage(extraPoints: strokes.reduce(0) { $0 + $1.points.count }, extraStrokes: strokes.count) == nil else {
            visualError = "A drawing can’t be added at this position."
            return false
        }
        var drawing = EditorDrawing(startS: window.start, endS: window.end, canvasAspect: min(4.9, max(0.21, canvasAspect)), strokes: strokes)
        let center = drawing.strokeCenter
        drawing.centerX = center.x; drawing.centerY = center.y
        drawing.drawDurationS = min(1.2, window.end - window.start)
        guard drawing.isValid else { visualError = "That drawing can’t be saved."; return false }
        transactDocument(section: .drawings) { $0.drawings.append(drawing) }
        // Show it finished: the playhead moves to where the pen lifts, so Done never leaves an empty-looking frame.
        select(.init(kind: .drawing, id: drawing.id), seekToStart: false)
        seek(to: timelineProjection.projectBaseTime(min(drawing.endS - 0.05, drawing.startS + drawing.drawDurationS)))
        return true
    }

    /// "Draw more": new strokes drawn on the preview join an existing drawing. They arrive in preview
    /// coordinates, so they are carried back through the drawing's own move / scale / rotation, and the
    /// drawing's centre is re-pinned so the strokes already there do not shift when the bounding box grows.
    @discardableResult
    func appendDrawingStrokes(id: String, previewStrokes: [DrawingStroke]) -> Bool {
        guard canEditDrawings, !previewStrokes.isEmpty, let index = document.drawings.firstIndex(where: { $0.id == id }),
              drawingCapMessage(extraPoints: previewStrokes.reduce(0) { $0 + $1.points.count }, extraStrokes: previewStrokes.count, editing: id) == nil else { return false }
        var value = document.drawings[index]
        value.append(previewStrokes: previewStrokes)
        guard value.isValid else { return false }
        transactDocument(section: .drawings) { $0.drawings[index] = value }
        return true
    }

    private func mutateDrawing(id: String, operation: String = "edit", _ body: (inout EditorDrawing) -> Void) {
        guard canEditOperation(["drawings.\(operation)", "drawings.edit"], section: .drawings),
              let index = document.drawings.firstIndex(where: { $0.id == id }) else { return }
        var value = document.drawings[index]
        body(&value)
        guard value.isValid, value != document.drawings[index] else { return }
        transactDocument(section: .drawings) { $0.drawings[index] = value }
    }

    func recolorDrawing(id: String, hex: String) {
        mutateDrawing(id: id) { value in value.strokes = value.strokes.map { $0.withColor(hex.uppercased()) } }
    }
    func setDrawingEntrance(id: String, entrance: DrawingEntrance) { mutateDrawing(id: id) { $0.entrance = entrance } }
    func setDrawingExit(id: String, exit: DrawingExit) { mutateDrawing(id: id) { $0.exit = exit } }
    func setDrawingDrawDuration(id: String, seconds: Double) {
        guard seconds.isFinite else { return }
        mutateDrawing(id: id) { $0.drawDurationS = min($0.endS - $0.startS, min(DrawingLimits.maxDrawDuration, max(0.2, (seconds * 20).rounded() / 20))) }
    }
    func setDrawingTransform(id: String, centerX: Double, centerY: Double, scale: Double, rotationDeg: Double) {
        guard [centerX, centerY, scale, rotationDeg].allSatisfy(\.isFinite) else { return }
        mutateDrawing(id: id) { value in
            value.centerX = min(1, max(0, centerX)); value.centerY = min(1, max(0, centerY))
            value.scale = min(3, max(0.2, scale)); value.rotationDeg = EditorDrawing.wrappedRotation(rotationDeg)
        }
    }
    func setDrawingTiming(id: String, startS: Double? = nil, endS: Double? = nil) {
        guard canEditOperation(["drawings.trim", "drawings.edit"], section: .drawings),
              let index = document.drawings.firstIndex(where: { $0.id == id }) else { return }
        transactDocument(section: .drawings) { document in
            var value = document.drawings[index]
            if let startS { value.startS = max(0, startS) }
            if let endS { value.endS = endS }
            if value.endS - value.startS < DrawingLimits.minDuration { value.endS = value.startS + DrawingLimits.minDuration }
            value.drawDurationS = min(max(0.2, value.drawDurationS), max(0.2, value.endS - value.startS))
            document.drawings[index] = value
        }
    }

    /// The preview shows a drawing mid-gesture without a recompile: only the portable layers are swapped
    /// in the live composition. The document is committed once, when the gesture ends.
    func previewDrawingInPlace(_ drawing: EditorDrawing) {
        guard sourcePreviewState == .ready, let preview = sourcePreview, player?.currentItem === preview.preview.playerItem else { return }
        var drawings = document.drawings
        guard let index = drawings.firstIndex(where: { $0.id == drawing.id }) else { return }
        drawings[index] = drawing
        let total = TimelineMath.totalDuration(of: preview.recipe)
        try? preview.updateDrawings(NativeDrawingCompile.layers(drawings, timelineDuration: total))
        if !isPlaying { seek(to: currentTime) }
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
        var item = EditorTimedEffect(id: UUID().uuidString, startS: at, endS: at, pointS: at, kind: "sfx",
            raw: ["sound_effect_id": .string(effect.id), "src_gcs_path": .string(""), "source": .string("user"),
                  "gain": .number(1), "duration_s": durationS.map(JSONValue.number) ?? .null, "label": .string(effect.name)])
        item.syncSoundEffectDisplay()
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
        case .drawing: setDrawingTiming(id: selected.id, startS: start, endS: end)
        default: break
        }
    }

    /// Whether a standalone text bar may be deleted from the editor, and why not.
    /// Caption cues belong to the Captions panel; bars linked to a card go with the
    /// card; lyric lines are generated. Everything else (title, clip labels, closing,
    /// the creator's own text, a phone Voiceover title) drops out of `text_elements`,
    /// which the server treats as a user deletion (full-replacement; guided tombstone
    /// `user_removed`). Only a `read_only` element (the server's kill-switch shape for
    /// the Voiceover title, KRI-465) is blocked.
    enum TextDeletion: Equatable {
        case allowed
        case blocked(String)
        var isAllowed: Bool { self == .allowed }
    }

    func textDeletion(id: String) -> TextDeletion {
        guard let element = document.textElements.first(where: { $0.id == id }) else { return .blocked("This text no longer exists.") }
        if element.isReadOnly { return .blocked("This title can't be edited here yet.") }
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
            guard let element = document.textElements.first(where: { $0.id == selected.id }),
                  !element.isReadOnly else { return false }
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
        case .drawing:
            guard document.drawings.contains(where: { $0.id == selected.id }) else { return false }
            transactDocument(section: .drawings) { document in
                document.drawings.removeAll { $0.id == selected.id }
                appendIfPersisted(EditorDeletion(kind: "drawing", id: selected.id), selection: selected, to: &document)
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
        case .drawing: return cleanDocument.drawings.contains { $0.id == selection.id }
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
        transactDocument(section: .captionMeta) {
            $0.captionMeta[key] = value ?? .null
            if key == "y_frac", var appearance = $0.captionMeta["appearance"]?.objectValue,
               appearance["position_y_frac"] != nil {
                appearance["position_y_frac"] = value ?? .null
                $0.captionMeta["appearance"] = .object(appearance)
            }
        }
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
            if key == "highlight_spoken_word", value == .bool(true) { Self.keepTextMotion(&appearance, in: &$0.captionMeta, canKeep: offersCaptionAnimation) }
            if key == "alignment", appearance["position_x_frac"] != nil {
                appearance["position_x_frac"] = .number(value.stringValue == "left" ? 0.06 : value.stringValue == "right" ? 0.82 : 0.5)
            }
            $0.captionMeta["appearance"] = .object(appearance)
        }
    }

    /// Opening a new display mode explicitly decouples display and highlighting.
    func setCaptionDisplay(_ style: String) {
        guard canEditCaptionAppearance, ["sentence", "word"].contains(style) else { return }
        transactDocument(section: .captionMeta) {
            var appearance = $0.captionMeta["appearance"]?.objectValue ?? [:]
            if appearance["highlight_spoken_word"] == nil { appearance["highlight_spoken_word"] = .bool(false) }
            if style == "word" { Self.keepTextMotion(&appearance, in: &$0.captionMeta, canKeep: offersCaptionAnimation) }
            $0.captionMeta["appearance"] = .object(appearance)
            $0.captionMeta["style"] = .string(style)
        }
    }

    /// A Style > Text motion entrance draws lines whole, which word display and spoken-word highlighting cannot.
    /// Where captions animate, it becomes the all-captions default (drawn per word), so the creator's choice
    /// survives the switch; elsewhere it is dropped as before.
    private static func keepTextMotion(_ appearance: inout [String: JSONValue], in meta: inout [String: JSONValue], canKeep: Bool) {
        guard let phases = appearance.removeValue(forKey: "animation_phases") else { return }
        if canKeep, meta["animation_phases"] == nil, NativeEditorRenderCompiler.captionPhases(phases) != nil {
            meta["animation_phases"] = phases
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

    enum CaptionStyleScope: Equatable { case all, line(String) }
    @Published private(set) var captionPlacementMessage: String?
    @Published var captionGestureChoicePresented = false
    private struct CaptionGestureChange {
        var id: String
        var values: [String: JSONValue]
    }
    private var captionGestureChange: CaptionGestureChange?

    func captionPosition(id: String) -> CGPoint {
        let appearance = document.captionMeta["appearance"]?.objectValue ?? [:]
        let own = appearance["line_overrides"]?.objectValue?[id]?.objectValue ?? [:]
        let row = document.textElements.first { $0.id == id }
        let alignment = appearance["alignment"]?.stringValue ?? "center"
        let fallbackX = alignment == "left" ? 80.0 / 1080 : alignment == "right" ? 1000.0 / 1080 : 0.5
        let legacyCenterY = document.isCueNative && own["position_y_frac"] == nil && appearance["position_y_frac"] == nil
            ? previewSelectionBounds(for: .init(kind: .captionCue, id: id), at: currentTime)?.centerY : nil
        let inheritedX = appearance["position_x_frac"]?.numberValue ?? row.map { Double($0.anchor.x) } ?? fallbackX
        let inheritedY = document.captionMeta["y_frac"]?.numberValue ?? row.map { Double($0.anchor.y) } ?? 0.82
        let centeredY = legacyCenterY ?? inheritedY
        let defaultY = appearance["position_y_frac"]?.numberValue ?? centeredY
        let x = own["position_x_frac"]?.numberValue ?? inheritedX
        let y = own["position_y_frac"]?.numberValue ?? defaultY
        return CGPoint(x: x, y: y)
    }

    func setCaptionGesturePosition(id: String, position: CGPoint) {
        guard position.x.isFinite, position.y.isFinite else { return }
        let values: [String: JSONValue] = ["position_x_frac": .number(min(0.95, max(0.05, position.x))),
                                           "position_y_frac": .number(min(0.90, max(0.10, position.y)))]
        writeCaptionStyle(.line(id), values: values)
        rememberCaptionGesture(id: id, values: values)
    }

    func setCaptionGestureSize(id: String, size: Double) {
        guard size.isFinite else { return }
        let values: [String: JSONValue] = ["size_px": .number(min(160, max(36, size.rounded())))]
        writeCaptionStyle(.line(id), values: values)
        rememberCaptionGesture(id: id, values: values)
    }

    private func rememberCaptionGesture(id: String, values: [String: JSONValue]) {
        guard offersCaptionStyleScope, isDirectManipulating else { return }
        if captionGestureChange?.id != id { captionGestureChange = CaptionGestureChange(id: id, values: [:]) }
        captionGestureChange?.values.merge(values) { _, value in value }
    }

    func finishCaptionGesture(applyToAll: Bool) {
        guard let change = captionGestureChange else { return }
        captionGestureChange = nil
        captionGestureChoicePresented = false
        if applyToAll { writeCaptionStyle(.all, values: change.values) }
        endTransaction()
    }

    var offersCaptionStyleScope: Bool { canEditCaptionAppearance && canEditCaptionMeta && canEdit("caption_style_scope") }

    func captionStyleSize(_ scope: CaptionStyleScope) -> Double {
        if case .line(let id) = scope,
           let size = document.captionMeta["appearance"]?.objectValue?["line_overrides"]?.objectValue?[id]?.objectValue?["size_px"]?.numberValue { return size }
        if case .line(let id) = scope, document.isCueNative,
           let size = document.captionCues.first(where: { $0.id == id })?.raw["size_px"]?.numberValue { return size }
        if let size = document.captionMeta["size_px"]?.numberValue { return size }
        if !document.isCueNative {
            let row = document.textElements.first { row in
                guard row.isCaption else { return false }
                if case .line(let id) = scope { return row.id == id }
                return true
            }
            if let row { return Self.textSize(for: row) }
        }
        return 78
    }

    func setCaptionStyleSize(_ scope: CaptionStyleScope, size: Double) {
        guard offersCaptionStyleScope, size.isFinite else { return }
        let value = JSONValue.number(min(160, max(36, size.rounded())))
        writeCaptionStyle(scope, values: ["size_px": value])
    }

    func placeCaptions(_ scope: CaptionStyleScope, at placement: NativeTextBlockPlacement) {
        guard offersCaptionStyleScope else { return }
        captionPlacementMessage = nil
        let meta = document.captionMeta
        let appearance = meta["appearance"]?.objectValue ?? [:]
        if textMeasurer == nil, let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) {
            textMeasurer = try? NativeEditorRenderCompiler(fontDirectory: fonts)
        }
        let units = document.captionUnits.filter { unit in
            if case .line(let id) = scope { return unit.id == id }
            return true
        }
        let projectedRows = NativeEditorRenderCompiler.projectSentenceCaptions(document.textElements, captionMeta: meta)
        var measuredRows: [EditorTextElement] = []
        for unit in units {
            let row = projectedRows.first { $0.id == unit.id }
            var raw: [String: JSONValue]
            if document.isCueNative {
                raw = unit.raw
                raw["font_family"] = raw["font_family"] ?? meta["font"] ?? .string("TikTok Sans")
                raw["alignment"] = appearance["alignment"] ?? .string("center")
                raw["max_width_frac"] = appearance["max_width_frac"] ?? .number(920.0 / 1080)
            } else if let row {
                raw = NativeEditorRenderCompiler.applyingCaptionMeta(meta, to: row).raw
            } else { return }
            raw["source_params"] = nil
            raw["position"] = .string("custom")
            raw["size_px"] = .number(captionStyleSize(.line(unit.id)))
            let text = document.isCueNative ? unit.text : row?.text ?? unit.text
            measuredRows.append(EditorTextElement(id: unit.id, text: text, startS: 0, endS: 1, raw: raw))
        }
        guard !measuredRows.isEmpty else { return }
        let safe = NativeTextBlockGeometry.safeRect
        var coordinate = 0.5
        var converged = false
        for _ in 0..<8 {
            var left = 0.0, right = 0.0, above = 0.0, below = 0.0
            for var row in measuredRows {
                let x = placement.isHorizontal ? coordinate : Double(captionPosition(id: row.id).x)
                row.raw["x_frac"] = .number(x)
                row.raw["y_frac"] = .number(0.5)
                guard let block = textMeasurer?.textBlockRect(for: row, in: document) else {
                    captionPlacementMessage = "This font could not be measured. The caption has not moved."
                    return
                }
                left = max(left, x - block.minX); right = max(right, block.maxX - x)
                above = max(above, 0.5 - block.minY); below = max(below, block.maxY - 0.5)
            }
            guard left + right <= safe.width, above + below <= safe.height else {
                captionPlacementMessage = "Use a smaller caption size or a common alignment to fit inside the safe area."
                return
            }
            let next: Double
            switch placement {
            case .left: next = safe.minX + left
            case .center: next = 0.5 + (left - right) / 2
            case .right: next = safe.maxX - right
            case .top: next = safe.minY + above
            case .middle: next = 0.5 + (above - below) / 2
            case .bottom: next = safe.maxY - below
            }
            if abs(next - coordinate) < 0.0001 { converged = true; break }
            coordinate = next
        }
        guard converged else {
            captionPlacementMessage = "This caption cannot fit at that position. Try a smaller size."
            return
        }
        writeCaptionStyle(scope, values: placement.isHorizontal
            ? ["position_x_frac": .number(coordinate)] : ["position_y_frac": .number(coordinate)])
    }

    private func writeCaptionStyle(_ scope: CaptionStyleScope, values: [String: JSONValue]) {
        guard offersCaptionStyleScope else { return }
        let ids: Set<String>
        switch scope {
        case .all: ids = []
        case .line(let id):
            guard document.captionUnits.contains(where: { $0.id == id }) else { return }
            ids = document.isCueNative ? [id] : captionLineRowIDs(containing: id)
        }
        let materializeIDs = scope != .all && document.isCueNative && document.captionCues.contains { $0.raw["id"] == nil }
        let clearsCueSizes = scope == .all && values["size_px"] != nil && document.captionCues.contains { $0.raw["size_px"] != nil }
        let changesCues = materializeIDs || clearsCueSizes
        guard !changesCues || canEditCaptionLines else { return }
        transactDocument(sections: changesCues ? [.captionMeta, .captions] : [.captionMeta]) { doc in
            if materializeIDs {
                for index in doc.captionCues.indices { doc.captionCues[index].raw["id"] = .string(doc.captionCues[index].id) }
            }
            var appearance = doc.captionMeta["appearance"]?.objectValue ?? [:]
            if scope == .all {
                var overrides = appearance["line_overrides"]?.objectValue ?? [:]
                for (key, value) in values {
                    if key == "size_px" { doc.captionMeta[key] = value }
                    else { appearance[key] = value }
                    for id in Array(overrides.keys) {
                        var own = overrides[id]?.objectValue ?? [:]
                        own.removeValue(forKey: key)
                        overrides[id] = own.isEmpty ? nil : .object(own)
                    }
                }
                if clearsCueSizes {
                    for index in doc.captionCues.indices { doc.captionCues[index].raw.removeValue(forKey: "size_px") }
                }
                if appearance["line_overrides"] != nil { appearance["line_overrides"] = .object(overrides) }
            } else {
                var overrides = appearance["line_overrides"]?.objectValue ?? [:]
                for id in ids {
                    var own = overrides[id]?.objectValue ?? [:]
                    own.merge(values) { _, value in value }
                    overrides[id] = .object(own)
                }
                appearance["line_overrides"] = .object(overrides)
            }
            doc.captionMeta["appearance"] = .object(appearance)
        }
    }

    // MARK: - KRI-1007 caption animation

    /// What an Animation-tab change applies to: every caption, or one line of Edit captions.
    enum CaptionAnimationScope: Equatable { case all, line(String) }

    /// `editor_capabilities.caption_animation`: absent where the server or this variant cannot keep caption phases.
    var offersCaptionAnimation: Bool { canEdit("caption_animation") }

    func canEditCaptionAnimation(_ scope: CaptionAnimationScope) -> Bool {
        guard offersCaptionAnimation, canEditCaptions else { return false }
        switch scope {
        case .all: return canEditCaptionMeta && (captionAnimationOverrideIDs.isEmpty || canEditCaptionLines)
        case .line(let id): return canEditCaptionLines && document.captionUnits.contains { $0.id == id }
        }
    }

    /// The phases the picker shows as selected, in the renderer's order: the line's own, else the
    /// all-captions default, else today's look (cue captions pop in; a word highlight and text-lane
    /// captions hold still).
    func captionAnimationPhases(_ scope: CaptionAnimationScope) -> [String: JSONValue] {
        let meta = document.captionMeta
        let sweep = meta["style"] == .string("word") || meta["appearance"]?.objectValue?["highlight_spoken_word"] == .bool(true)
        let today: [String: JSONValue] = ["entrance": .string(document.isCueNative && !sweep ? "pop" : "none"),
                                          "exit": .string("none"), "loop": .string("none"), "speed": .number(1)]
        // Style > Text motion (before KRI-1007) stored the all-captions entrance in `appearance`; it stays the
        // effective default until All captions writes `caption_meta.animation_phases`.
        let fallback = meta["animation_phases"]?.objectValue
            ?? meta["appearance"]?.objectValue?["animation_phases"]?.objectValue ?? today
        guard case .line(let id) = scope else { return fallback }
        if document.isCueNative {
            return document.captionCues.first { $0.id == id }?.raw["animation_phases"]?.objectValue ?? fallback
        }
        let line = captionLineRowIDs(containing: id)
        return document.textElements.lazy.filter { line.contains($0.id) }
            .compactMap { $0.raw["animation_phases"]?.objectValue }.first ?? fallback
    }

    func setCaptionPhase(_ scope: CaptionAnimationScope, phase: String, effect: String) {
        guard ["entrance", "exit", "loop"].contains(phase),
              (phase == "loop" ? ["none", "pulse", "bounce", "float"] : TextAnimationPhases.Edge.allCases.map(\.rawValue)).contains(effect) else { return }
        var phases = captionAnimationPhases(scope)
        phases[phase] = .string(effect)
        writeCaptionPhases(scope, phases)
    }

    func setCaptionAnimationSpeed(_ scope: CaptionAnimationScope, speed: Double) {
        guard speed.isFinite else { return }
        var phases = captionAnimationPhases(scope)
        phases["speed"] = .number(min(3, max(0.25, speed)))
        writeCaptionPhases(scope, phases)
    }

    /// Lines with phases of their own. All captions removes them, so every line truly matches.
    var captionAnimationOverrideIDs: [String] {
        document.isCueNative
            ? document.captionCues.filter { $0.raw["animation_phases"] != nil }.map(\.id)
            : document.textElements.filter { $0.isCaption && $0.raw["animation_phases"] != nil }.map(\.id)
    }

    /// The rows drawn as one line with `id` (a text-lane line spans several word rows); the row alone otherwise.
    private func captionLineRowIDs(containing id: String) -> Set<String> {
        let rows = document.textElements
        let line = NativeEditorRenderCompiler.captionLineGroups(rows, captionMeta: document.captionMeta)
            .first { $0.contains { rows[$0].id == id } }
        return Set(line?.map { rows[$0].id } ?? [id])
    }

    private func writeCaptionPhases(_ scope: CaptionAnimationScope, _ phases: [String: JSONValue]) {
        guard canEditCaptionAnimation(scope) else { return }
        let value = JSONValue.object(phases)
        let cueNative = document.isCueNative
        switch scope {
        case .all:
            var sections: Set<EditorSection> = [.captionMeta]
            if !captionAnimationOverrideIDs.isEmpty { sections.insert(cueNative ? .captions : .text) }
            transactDocument(sections: sections) { doc in
                doc.captionMeta["animation_phases"] = value
                if var appearance = doc.captionMeta["appearance"]?.objectValue, appearance["animation_phases"] != nil {
                    appearance["animation_phases"] = nil
                    doc.captionMeta["appearance"] = .object(appearance)
                }
                if cueNative {
                    for index in doc.captionCues.indices { doc.captionCues[index].raw.removeValue(forKey: "animation_phases") }
                } else {
                    for index in doc.textElements.indices where doc.textElements[index].isCaption {
                        doc.textElements[index].raw.removeValue(forKey: "animation_phases")
                    }
                }
            }
        case .line(let id) where cueNative:
            guard let index = document.captionCues.firstIndex(where: { $0.id == id }) else { return }
            transactDocument(section: .captions) { $0.captionCues[index].raw["animation_phases"] = value }
        case .line(let id):
            // The line's first row with phases governs the line (`_group_phases`), so this row keeps the
            // only phases on its line.
            let line = captionLineRowIDs(containing: id)
            transactDocument(section: .text) { doc in
                for index in doc.textElements.indices where line.contains(doc.textElements[index].id) {
                    if doc.textElements[index].id == id {
                        doc.textElements[index].raw["animation_phases"] = value
                    } else {
                        doc.textElements[index].raw.removeValue(forKey: "animation_phases")
                    }
                }
            }
        }
    }

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
            if let atS { effect.pointS = max(0, atS) }
            if let startS { if effect.pointS != nil { effect.pointS = max(0, startS) } else { effect.startS = max(0, startS) } }
            if let endS {
                if let pointS = effect.pointS {
                    let window = Self.soundEffectTrimWindow(effect, minimum: minimumClipDuration)
                    effect.raw["trim_end_s"] = .number(min(window.source, max(window.start + minimumClipDuration, window.start + endS - pointS)))
                } else {
                    effect.endS = endS
                }
            }
            effect.syncSoundEffectDisplay()
            effect.endS = max(effect.startS + minimumClipDuration, effect.endS)
        }
    }
    func setSoundEffectTrim(id: String, trimStartS: Double? = nil, trimEndS: Double? = nil) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.trim", "sfx.trim", "sound_effects.trim", "lanes.sfx"]) { effect in
            let next = Self.clampedSoundEffectTrim(effect, start: trimStartS, end: trimEndS, minimum: Self.sfxMinimumTrim)
            effect.raw["trim_start_s"] = .number(next.start)
            effect.raw["trim_end_s"] = .number(next.end)
            effect.syncSoundEffectDisplay()
        }
    }
    func resetSoundEffectTrim(id: String) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.trim", "sfx.trim", "sound_effects.trim", "lanes.sfx"]) { effect in
            effect.raw["trim_start_s"] = nil
            effect.raw["trim_end_s"] = nil
            effect.syncSoundEffectDisplay()
        }
    }
    static let sfxMinimumTrim = 0.05
    /// Source-sound trim window (seconds into the catalog sound), clamped to `duration_s`
    /// with a minimum kept length.
    static func soundEffectTrimWindow(_ effect: EditorTimedEffect, minimum: Double = sfxMinimumTrim) -> (start: Double, end: Double, source: Double) {
        let trimStart = max(0, number(effect.raw["trim_start_s"]) ?? 0)
        let source = max(minimum, number(effect.raw["duration_s"]) ?? number(effect.raw["trim_end_s"]) ?? trimStart + minimum)
        let start = min(trimStart, source - minimum)
        let end = min(source, max(start + minimum, number(effect.raw["trim_end_s"]) ?? source))
        return (start, end, source)
    }
    /// `nil` start/end keep the current value.
    static func clampedSoundEffectTrim(_ effect: EditorTimedEffect, start: Double?, end: Double?, minimum: Double) -> (start: Double, end: Double) {
        let current = soundEffectTrimWindow(effect, minimum: minimum)
        var s = min(max(0, start ?? current.start), current.source - minimum)
        var e = min(current.source, end ?? current.end)
        if start != nil && end == nil { e = max(e, s + minimum) }
        if end != nil && start == nil { s = min(s, e - minimum) }
        e = max(e, s + minimum)
        return (s, min(e, current.source))
    }
    /// Audition/preview URL for a placed effect: the timeline source pool, else the catalog.
    func soundEffectPreviewURL(for effect: EditorTimedEffect) -> URL? {
        if let asset = sourcePool?.nativeAssets.first(where: { $0.kind == "sound_effect" && $0.id == effect.id }) { return asset.sourceURL }
        guard let catalogID = effect.raw["sound_effect_id"]?.stringValue else { return nil }
        return soundEffectCatalog.first(where: { $0.id == catalogID })?.previewAudioURL
    }
    func setSoundEffectGain(id: String, gain: Double) {
        mutateTimedEffect(kind: .soundEffect, id: id, section: .soundEffects, operationKeys: ["lanes.sfx.gain", "sfx.gain", "sound_effects.gain", "lanes.sfx"]) { $0.raw["gain"] = .number(min(max(0, gain), 1)) }
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

    // MARK: Original (camera) audio

    /// The server advertises the control (`original_audio`): phone-rendered guided edits, including
    /// the creator's song videos. Absent on every other variant, where the Sounds tab shows nothing.
    var hasOriginalAudioControl: Bool { document.capabilities["original_audio"] != nil }
    var canEditOriginalAudio: Bool { document.capabilities["original_audio"]?.editable == true }
    var originalAudioLockedReason: String? { document.capabilities["original_audio"]?.reason }
    var canEditClipAudio: Bool { document.capabilities["clips.audio"]?.editable == true }
    var clipAudioLockedReason: String? { document.capabilities["clips.audio"]?.reason }

    /// The level the camera audio plays at for the whole video, 0...1. An unset level is what the
    /// renderer does by default: silent under the creator's song, the clips' own sound otherwise.
    var originalAudioLevel: Double {
        let songPlays = (deviceSongBed != nil || baseUserSong != nil) && !userSongRemoved
        if !userSongRemoved, let value = document.mix["original_level"]?.numberValue, value.isFinite {
            return min(max(0, value), 1)
        }
        if songPlays { return 0 }
        return sourceAudioPreserved ? 1 : 0
    }

    func setOriginalAudioLevel(_ level: Double) {
        guard canEditOriginalAudio, level.isFinite else { return }
        let clamped = min(max(0, level), 1)
        transactDocument(section: .mix) { $0.mix["original_level"] = .number((clamped * 100).rounded() / 100) }
    }

    /// Whether this clip's own sound is heard right now: the whole-video level is up and the clip is not muted.
    func isClipAudioOn(clipID: String) -> Bool {
        guard let index = clipSlotIndex(clipID) else { return false }
        return originalAudioLevel > 0 && document.clips[index].raw["muted"] != .bool(true)
    }

    /// The per-clip audio button. Turning a clip on while the whole video's original audio is off
    /// raises it to 100% and mutes the other clips, so exactly this clip's sound is heard (one undo
    /// step). Otherwise it flips this clip's mute.
    func toggleClipAudio(clipID: String) {
        guard canEditClipAudio, canEditOriginalAudio, let target = clipSlotIndex(clipID) else { return }
        let turningOn = !isClipAudioOn(clipID: clipID)
        let levelIsOff = originalAudioLevel <= 0
        transactDocument(sections: [.timeline, .mix]) { doc in
            if turningOn && levelIsOff {
                doc.mix["original_level"] = .number(1)
                for index in doc.clips.indices where !doc.clips[index].removed {
                    doc.clips[index].raw["muted"] = .bool(index != target)
                }
            } else {
                doc.clips[target].raw["muted"] = .bool(!turningOn)
            }
        }
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
    /// KRI-306: finished-video shape. `orientation` is "portrait" | "landscape"
    /// (the wire values `PUT .../orientation` accepts); `landscapeFit` is
    /// "fit" (black bars) | "fill" (crop). Each is gated by its own server
    /// capability, which the picker already reflects; the guard keeps a stale
    /// UI from dirtying a closed section.
    func setVideoShape(orientation newOrientation: String? = nil, landscapeFit newFit: String? = nil) {
        if let newOrientation, newOrientation != document.orientation, canEditSection(.orientation) {
            transactDocument(section: .orientation) { $0.orientation = newOrientation }
        }
        if let newFit, newFit != document.effectiveLandscapeFit, canEditSection(.landscapeFit) {
            transactDocument(section: .landscapeFit) { $0.landscapeFit = newFit }
        }
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
        await saveOnce()
        if saveHitNewerVersion {
            // KRI-935: a newer version was saved elsewhere. Load it with the creator's
            // edits on top and send them once more, without asking them to reload.
            saveHitNewerVersion = false
            let reload = await reloadLatestKeepingLocalEdits()
            switch reload {
            case .reloaded:
                // The newest version may already contain everything the creator changed.
                guard hasUnsavedChanges else { saveState = .saved; return }
                await saveOnce()
                if saveHitNewerVersion {
                    saveHitNewerVersion = false
                    saveState = .failed("This edit changed again while saving. Your edits are still here on the newest version. Save again to send them.")
                    return
                }
            case .failed(let error) where error is CancellationError:
                // The editor moved to another job or variant meanwhile; that load owns the state.
                return
            case .alreadyTried, .failed:
                let detail = if case .failed(let error) = reload { " \(error.localizedDescription)" } else { "" }
                saveState = .failed("Your edits are still here, but the newest version couldn’t be loaded.\(detail)")
                return
            }
        }
        // A removal the server already had: the local state was folded, so send what else is pending.
        guard songRemovalAlreadyApplied else { return }
        songRemovalAlreadyApplied = false
        if hasUnsavedChanges { await saveOnce() }
    }

    private func saveOnce() async {
        defer { userSongRetry = nil }
        guard !isSaving, hasUnsavedChanges else { return }
        guard let api, let itemID, let variantKey else { saveState = .failed("Load the project before saving edits."); return }
        isSaving = true; saveState = .saving
        defer { isSaving = false }
        let submittedDocument = document
        // KRI-576: the song window this save asks the server to project the lyric rows against.
        let submittedSongStartS = trueSongWindowStartS
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
            // A committed save proves the connection works: drop the offline hint. Any pending
            // retry stays scheduled so the catch-up read still runs.
            refreshFailureStreak = 0
            if refreshSync == .reconnecting { refreshSync = .idle }
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
            var savedSubmission = submittedDocument
            if let draft = response.draft, acknowledged.contains(.timeline) {
                let savedSlots = EditorDocument(snapshot: draft.snapshot).clips
                if savedSlots.count == submittedDocument.clips.count {
                    for index in savedSlots.indices where submittedDocument.clips[index].id == nil {
                        let submitted = submittedDocument.clips[index]
                        savedSubmission.clips[index].id = savedSlots[index].id
                        // Only replace identity on an unchanged submitted row;
                        // never overwrite edits made during the request.
                        if let current = document.clips.firstIndex(of: submitted) {
                            document.clips[current].id = savedSlots[index].id
                        }
                    }
                }
            }
            acknowledge(acknowledged, generation: response.generation, submittedDocument: savedSubmission)
            if hasPostSubmitEdits {
                let acknowledgedRevision = document.revision
                let rebaseSong = userSongRebase
                undoStack = postSubmitUndo.map { value in
                    var rebased = value
                    rebased.revision = acknowledgedRevision
                    if acknowledged.contains(.userSong) { rebased.userSong = rebaseSong(rebased.userSong) }
                    return rebased
                }
                redoStack.removeAll()
            } else {
                undoStack.removeAll(); redoStack.removeAll()
            }
            userSongRebase = { $0 }
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
            if response.draft == nil, let saveJobID,
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
                        expectedDocumentGeneration: response.generation,
                        songWindowStartS: submittedSongStartS
                    )
                } catch {
                    // A durable save must remain successful even when the
                    // read-after-write reconciliation is temporarily down.
                }
            }
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: response.generation) else { return }
            if response.renderStarted ?? response.ok {
                // The commit is durable even while its render is pending. Keep
                // the acknowledged sections retryable until a matching ready
                // generation is observed.
                pendingRenderRetrySections = acknowledged
                saveState = .previewPending
                await refreshDeviceRender()
                guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: response.generation) else { return }
                pendingDeviceRenderIdentity = deviceRenderKey.flatMap { deviceRenders?.request(for: $0)?.identity }
                previewSupersededGeneration = saveDocumentGeneration
                startPreviewRefresh(generation: response.generation)
            } else {
                pendingRenderRetrySections = acknowledged
                saveState = .renderRetryNeeded(response.renderRecovery ?? "Your edit is saved. Its preview render did not start, so you can retry it safely.")
            }
        } catch APIError.conflict {
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: saveDocumentGeneration) else { return }
            // `save()` loads the newer version and sends this edit again.
            saveHitNewerVersion = true
        } catch EditorSaveError.userSongUnavailable where submittedDocument.userSong?.removed == true {
            // The song is already gone on the server (an earlier removal reached it): that is what the
            // creator asked for. Fold the removal in locally and carry on with whatever else is pending.
            guard saveIdentityMatches(itemID: saveItemID, jobID: saveJobID, variantKey: saveVariantKey, threadID: saveThreadID, documentGeneration: saveDocumentGeneration) else { return }
            foldAcknowledgedUserSong(submittedDocument.userSong)
            let rebaseSong = userSongRebase
            undoStack = undoStack.map { var value = $0; value.userSong = rebaseSong(value.userSong); return value }
            redoStack = redoStack.map { var value = $0; value.userSong = rebaseSong(value.userSong); return value }
            userSongRebase = { $0 }
            changedSections.remove(.userSong); explicitlyDirtySections.remove(.userSong); pendingRenderRetrySections.remove(.userSong)
            refreshDirtyState()
            songRemovalAlreadyApplied = true
            saveState = hasUnsavedChanges ? .idle : .saved
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

    /// Whether the last `saveOnce` was refused because a newer version exists.
    private var saveHitNewerVersion = false
    /// KRI-935: the server generation a preview conflict already reloaded for. A server
    /// that keeps answering 409 for the same generation ends on the Retry card instead of
    /// reloading forever; Retry clears it.
    private var previewConflictReloadGeneration: String?

    private enum LatestReload { case reloaded, alreadyTried, failed(Error) }

    /// KRI-935: load the newest renderer-owned version of this edit without asking the
    /// creator. It becomes the clean base and their unsaved lanes are replayed on top
    /// (`rebaseLocalEdits`). `fencingPreviewConflict` allows one reload per server
    /// generation and always rebuilds the preview, even when the generation is unchanged.
    private func reloadLatestKeepingLocalEdits(fencingPreviewConflict: Bool = false) async -> LatestReload {
        guard !isSaving, let api, let jobID, let variantKey, let itemID else {
            return .failed(APIError.invalidResponse)
        }
        isSaving = true
        defer { isSaving = false }
        do {
            let variant = try await api.editorVariant(jobID: jobID, variantID: variantKey)
            guard self.jobID == jobID, self.variantKey == variantKey else { return .failed(CancellationError()) }
            let generation = variant["render_generation_id"]?.stringValue
                ?? variant["render_finished_at"]?.stringValue
                ?? document.revision.baseGeneration
            if fencingPreviewConflict {
                guard previewConflictReloadGeneration != generation else { return .alreadyTried }
                previewConflictReloadGeneration = generation
                // Already on this version: rebuild the preview and keep the Undo history.
                if generation == cleanDocument.revision.baseGeneration, generation == document.revision.baseGeneration {
                    refreshRebasedSourcePreview(force: true)
                    return .reloaded
                }
            }
            let snapshot = DraftSnapshot(
                draftID: "latest-\(projectID.uuidString)",
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
            rebaseLocalEdits(onto: latestClean, variant: variant, forcePreviewRebuild: fencingPreviewConflict)
            return .reloaded
        } catch {
            return .failed(error)
        }
    }

    /// Make `latestClean` the clean baseline and replay only the locally dirty
    /// sections on top, as one undoable change. Nothing the creator changed is lost.
    private func rebaseLocalEdits(onto latestClean: EditorDocument, variant: [String: JSONValue]?, forcePreviewRebuild: Bool) {
        let localDocument = document
        let localSections = changedSections
        let localExplicitSections = explicitlyDirtySections
        let selected = selection
        var rebased = latestClean
        for section in localSections {
            copy(section, from: localDocument, into: &rebased)
        }
        // Deletion intents travel with the replayed lanes, so Save still sends them.
        for deletion in localDocument.deletions where !rebased.deletions.contains(deletion) {
            rebased.deletions.append(deletion)
        }
        document = rebased
        cleanDocument = latestClean
        changedSections = localSections
        explicitlyDirtySections = localExplicitSections.intersection(localSections)
        redoStack.removeAll()
        configureCapabilities(from: variant)
        refreshRebasedSourcePreview(force: forcePreviewRebuild)
        refreshDirtyState()
        undoStack = hasUnsavedChanges ? [latestClean] : []
        if let selected, selectionExists(selected) { selection = selected } else { selection = nil }
        durationSourcesInvalidated = hasUnsavedChanges && (changedSections.contains(.timeline) || changedSections.contains(.carouselMoment))
        setAuthoritativeDuration(Self.number(variant?["duration_s"]))
        refreshDuration()
    }

    private func transact(section: EditorSection, _ body: (inout EditorDraft) -> Void) {
        var next = draft; body(&next); guard next != draft else { return }
        invalidateDurationSources(for: Set([section]))
        let before = document
        if transactionBaseline == nil { appendUndo(document); redoStack.removeAll() }
        let song = document.userSong
        replace(with: next)
        document.userSong = song
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
        NativeSpeechTimelineProjection.acknowledgeCaptionEdits(&next, from: document)
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

    // MARK: Crop (KRI-44)

    /// The Crop button is offered only when the server EXPLICITLY lists `clips.source_crop`. A variant with no
    /// `clips` capability group (legacy cloud template and montage renders) cannot crop, and `canEditOperation`'s
    /// fall-back to the timeline section would wrongly say yes.
    var serverOffersClipCrop: Bool { document.capabilities["clips.source_crop"] != nil }
    var canCropClips: Bool { document.capabilities["clips.source_crop"]?.editable == true }
    /// Speed is a native-render operation. It must be explicitly advertised;
    /// the editable timeline capability alone is never enough to expose it.
    var serverOffersClipPlaybackRate: Bool { document.capabilities["clips.playback_rate"] != nil }
    var canSetClipPlaybackRate: Bool { document.capabilities["clips.playback_rate"]?.editable == true }
    /// These lanes are stored on the output clock and currently have no
    /// source-anchor projection. Refuse a speed edit while any is present so
    /// it cannot silently detach a creator's timed work from its footage.
    var clipSpeedBlockedReason: String? {
        if GuidedLabelRebase.hasLabels(document.textElements, includeFreeText: ridesGuidedTimeline), !canEditSection(.text) {
            return "Speed isn’t available while this edit’s text timing is locked."
        }
        if !document.captionCues.isEmpty { return "Speed isn’t available while captions are timed to this edit." }
        if !document.mediaOverlays.isEmpty { return "Speed isn’t available while media overlays are timed to this edit." }
        if !document.visualBlocks.isEmpty { return "Speed isn’t available while visuals are timed to this edit." }
        if !document.motionScenes.isEmpty { return "Speed isn’t available while motion is timed to this edit." }
        if !document.cameraEffects.isEmpty { return "Speed isn’t available while camera effects are timed to this edit." }
        if !document.soundEffects.isEmpty { return "Speed isn’t available while sound effects are timed to this edit." }
        if !document.drawings.isEmpty { return "Speed isn’t available while drawings are timed to this edit." }
        if document.carouselMoment != nil { return "Speed isn’t available while a carousel changes this edit’s timing." }
        return nil
    }

    /// A photo can still crop, but has no source timeline to retime.
    func speedBlockedReason(for selection: EditorSelection?) -> String? {
        guard let selection, selection.kind == .clip else { return nil }
        if let found = sourceWindowSlot(for: selection) {
            let kind = found.slot.raw["media_kind"]?.stringValue ?? found.slot.raw["kind"]?.stringValue
            if kind == "image" { return "Photos do not have playback speed." }
            if let url = found.clip.sourceClipIndex.flatMap({ resolvedSources?[$0] })?.url, isStill(url) {
                return "Photos do not have playback speed."
            }
        }
        return rendersOnDevice ? clipSpeedBlockedReason : nil
    }

    /// Why Crop is greyed out for the selected clip, or nil when it is available.
    func cropBlockedReason(for selection: EditorSelection?) -> String? {
        guard let selection, selection.kind == .clip else { return "Select a clip to crop it." }
        if !canCropClips { return document.capabilities["clips.source_crop"]?.reason.map(nativeEditorWireLabel) ?? "Crop isn’t available for this edit." }
        if let slotID = timelineClips.first(where: { $0.id.uuidString == selection.id })?.slotID,
           document.clips.first(where: { $0.id == slotID })?.raw["layout"]?.stringValue == "supporting_card" {
            return "Supporting cards keep their own layout."
        }
        return nil
    }

    /// What the crop is shown in: the video canvas.
    var cropOutputAspect: CGFloat {
        EditorDocument.canonicalOrientation(document.orientation) == "landscape" ? 16.0 / 9.0 : 9.0 / 16.0
    }

    /// The real upright frame a crop is drawn on: the clip's source at its source-in time (a photo's own pixels).
    /// Reads the same prepared media the preview plays, so nothing new is downloaded when the preview is ready.
    func cropSourceFrame(for selection: EditorSelection) async -> UIImage? {
        let source: ResolvedEditorSource?
        var time: TimeInterval = 0
        switch selection.kind {
        case .clip:
            guard let clip = timelineClips.first(where: { $0.id.uuidString == selection.id }) else { return nil }
            source = clip.sourceClipIndex.flatMap { resolvedSources?[$0] }
            time = clip.trimIn
        case .mediaOverlay: source = resolvedMedia["overlay:" + selection.id]
        case .visualBlock: source = resolvedMedia["visual:" + selection.id] ?? authoredVisualSources["visual:" + selection.id]
        default: source = nil
        }
        guard let url = source?.url else { return nil }
        return await KriaCropFrameLoader.image(at: url, time: time)
    }

    // MARK: Source window / Range (KRI-717)

    /// Everything the Range sheet needs to draw and edit one clip's source window.
    struct SourceWindowContext: Equatable {
        var window: KriaSourceWindowGeometry.Window
        var rate: Double
        /// Nil when the server did not say; the sheet then asks the file itself.
        var sourceDuration: Double?
        var url: URL
    }

    private var stillImageURLs: [URL: Bool] = [:]
    private func isStill(_ url: URL) -> Bool {
        if let known = stillImageURLs[url] { return known }
        let value = NativeDownloadedMedia.isStillImage(url)
        stillImageURLs[url] = value
        return value
    }

    func sourceWindowSlot(for selection: EditorSelection?) -> (slot: EditorTimelineSlot, clip: EditorClip)? {
        guard let selection, selection.kind == .clip,
              let clip = timelineClips.first(where: { $0.id.uuidString == selection.id }),
              let slot = document.clips.first(where: { $0.id == clip.slotID }) else { return nil }
        return (slot, clip)
    }

    /// Why Range is greyed out for the selected clip, or nil when it is available.
    func sourceWindowBlockedReason(for selection: EditorSelection?) -> String? {
        guard let found = sourceWindowSlot(for: selection) else { return "Select a clip to choose its range." }
        if !canEditTimeline || !canEdit(.timeline) { return "Clip edits are locked for this render." }
        let kind = found.slot.raw["media_kind"]?.stringValue ?? found.slot.raw["kind"]?.stringValue
        if kind == "image" { return "Photos have no range to choose." }
        if let url = found.clip.sourceClipIndex.flatMap({ resolvedSources?[$0] })?.url, isStill(url) { return "Photos have no range to choose." }
        return nil
    }

    /// Output seconds the slot lasts, as the render compiler reads it (`durationS`, else the projected span).
    func sourceWindowLength(slot: EditorTimelineSlot, clip: EditorClip) -> Double {
        max(minimumClipDuration, slot.durationS ?? (clip.trimOut - clip.trimIn))
    }

    /// The playback rate the clip really plays at, derived the way the render compiler and the server do: an
    /// explicit `playback_rate`, else projected source span / slot length. That covers slowed narrated clips
    /// (`native_source_span_s`, no `playback_rate`) and older retimes that live only in the source span.
    func effectiveSourceRate(slot: EditorTimelineSlot, clip: EditorClip) -> Double {
        if let explicit = slot.raw["playback_rate"]?.numberValue, explicit.isFinite, explicit > 0 { return explicit }
        let derived = (clip.trimOut - clip.trimIn) / sourceWindowLength(slot: slot, clip: clip)
        return derived.isFinite && derived > 0 ? derived : 1
    }

    func sourceWindowContext(for selection: EditorSelection?) -> SourceWindowContext? {
        guard let found = sourceWindowSlot(for: selection),
              let url = found.clip.sourceClipIndex.flatMap({ resolvedSources?[$0] })?.url else { return nil }
        return SourceWindowContext(
            window: .init(start: max(0, found.slot.inS), length: sourceWindowLength(slot: found.slot, clip: found.clip)),
            rate: effectiveSourceRate(slot: found.slot, clip: found.clip),
            sourceDuration: found.clip.sourceDuration,
            url: url
        )
    }

    /// Writes a window from the Range sheet. A window the server would refuse (`TIMELINE_SOURCE_BOUNDS`) is
    /// ignored rather than staged, so one bad drag can never block Save. Wrap a whole drag in
    /// `beginTransaction()` / `endTransaction()` for a single undo step.
    @discardableResult
    func setSourceWindow(for selection: EditorSelection, _ window: KriaSourceWindowGeometry.Window, rate: Double, sourceDuration: Double?) -> Bool {
        guard sourceWindowBlockedReason(for: selection) == nil, let found = sourceWindowSlot(for: selection) else { return false }
        let current = KriaSourceWindowGeometry.Window(start: max(0, found.slot.inS), length: sourceWindowLength(slot: found.slot, clip: found.clip))
        // A slot that already overruns its footage may still shrink toward a valid window, even if it is not valid yet.
        let shrinks = window.start >= 0 && window.length >= KriaSourceWindowGeometry.minimumLength - 1e-9
            && KriaSourceWindowGeometry.sourceEnd(window, rate: rate) <= KriaSourceWindowGeometry.sourceEnd(current, rate: rate) + 1e-9
        guard KriaSourceWindowGeometry.isWithinBounds(window, rate: rate, sourceDuration: sourceDuration) || shrinks else { return false }
        let moved = abs(window.start - current.start) > 1e-6, resized = abs(window.length - current.length) > 1e-6
        guard moved || resized else { return true }
        // A pure slide writes `in_s` alone, so a beat-bound slot keeps `duration_beats`; a new length is a fixed length.
        mutateClip(clipID: selection.id) { slot in
            slot.inS = max(0, window.start)
            if moved { slot.raw["source_start_locked"] = .bool(true) }
            if resized { slot.durationS = max(minimumClipDuration, window.length); slot.durationBeats = nil }
            if slot.raw["source_end_s"] != nil {
                slot.raw["source_end_s"] = .number(KriaSourceWindowGeometry.sourceEnd(window, rate: rate))
            }
        }
        return true
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
        for (index, item) in document.textElements.enumerated() where !item.isReadOnly { layers.append((EditorSelection(kind: .text, id: item.id), Self.number(item.raw["z"] ?? item.raw["z_index"]) ?? 300 + Double(index), 0)) }
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
        case .drawing: return .drawings
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
              // KRI-589: a drawing's window moves and trims under the server's `drawings.trim` / `drawings.edit`.
              canEditOperation(section == .drawings ? ["drawings.trim", "drawings.edit"] : ["\(section.rawValue).\(operation)", section.rawValue], section: section),
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
        case .drawing: guard let item = document.drawings.first(where: { $0.id == selection.id }) else { return nil }; return (item.startS, item.endS)
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
            guard let index = document.textElements.firstIndex(where: { $0.id == selection.id }),
                  !document.textElements[index].isReadOnly else { return }
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
        case .drawing:
            guard let index = document.drawings.firstIndex(where: { $0.id == selection.id }) else { return }
            document.drawings[index].startS = start
            document.drawings[index].endS = end
            // The pen can never take longer than the window it draws in.
            document.drawings[index].drawDurationS = min(max(0.2, document.drawings[index].drawDurationS), max(0.2, end - start))
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
        let window = Self.soundEffectTrimWindow(effect, minimum: minimumClipDuration)
        switch edge {
        case .leading:
            let room = effect.pointS ?? .greatestFiniteMagnitude
            let shift = min(max(translation, max(-window.start, -room)), window.end - minimumClipDuration - window.start)
            effect.raw["trim_start_s"] = .number(window.start + shift)
            if let pointS = effect.pointS { effect.pointS = pointS + shift }
        case .trailing:
            effect.raw["trim_end_s"] = .number(max(window.start + minimumClipDuration, min(window.source, window.end + translation)))
        }
        effect.syncSoundEffectDisplay()
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
        case .drawings: return ["drawings", "lanes.drawings"]
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
        if cloudEditorUnavailableMessage != nil { return false }
        if let capability = capability(for: section) { return capability.editable }
        switch section {
        case .timeline: return canEditTimeline
        case .text: return canEditText
        case .captions, .captionMeta: return canEditCaptions
        case .mix, .music, .backgroundMusic: return canEditMix
        // KRI-589: only the per-operation `drawings.*` capabilities open drawings.
        // KRI-1118: only the server's `speaker_background` capability opens the background.
        case .soundEffects, .mediaOverlays, .visualBlocks, .motionScenes, .cameraEffects, .drawings,
             .speakerBackground, .carouselMoment, .lyrics, .title: return false
        // KRI-306: the server capability decides; with no capability the section stays closed.
        case .orientation, .landscapeFit: return false
        // KRI-428: only the per-operation `user_song.*` capabilities open it.
        case .userSong: return false
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
            case .landscapeFit: differs = document.effectiveLandscapeFit != cleanDocument.effectiveLandscapeFit
            case .soundEffects: differs = document.soundEffects != cleanDocument.soundEffects
            case .mediaOverlays: differs = document.mediaOverlays != cleanDocument.mediaOverlays
            case .visualBlocks: differs = document.visualBlocks != cleanDocument.visualBlocks
            case .motionScenes: differs = document.motionScenes != cleanDocument.motionScenes || document.motionRuntimeHash != cleanDocument.motionRuntimeHash
            case .cameraEffects: differs = document.cameraEffects != cleanDocument.cameraEffects
            case .drawings: differs = document.drawings != cleanDocument.drawings
            case .speakerBackground: differs = document.speakerBackground != cleanDocument.speakerBackground
            case .carouselMoment: differs = document.carouselMoment != cleanDocument.carouselMoment
            case .title: differs = document.title != cleanDocument.title
            case .userSong: differs = document.userSong != cleanDocument.userSong
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
        changedSections.contains(.timeline) && canEditSection(.text)
            && GuidedLabelRebase.hasLabels(document.textElements, includeFreeText: isGuidedStory)
    }

    /// Only a guided story's server skips its own text projection when the Save carries the text
    /// lane, so only its timeline commits always send text. Every other archetype sends text when
    /// the rebase actually changed it (the `.text` section is then dirty).
    private var isGuidedStory: Bool { previewVariant["resolved_archetype"] == .string("guided_story") }

    /// Archetypes whose text is timed by speech (captions, the talking cut), not by a clip timeline.
    /// Their text keeps its own rules; the speech compiler re-times it.
    private static let speechTimedArchetypes: Set<String> = ["subtitled", "narrated", "talking_head"]

    /// A clip-timeline video times EVERY text on its absolute output seconds, so any clip-timing
    /// edit (delete, trim, extend, reorder) must re-time all of it (chat / creator text, group
    /// titles, `montage-text-*`), not just the per-clip labels, or the render compiler skips
    /// whatever now starts past the end. That holds for a guided story AND for every montage-family
    /// edit: the phone montage writers, voiceover montages, day vlog, single hero, and montage
    /// variants that carry no `resolved_archetype` at all (KRI-715). Speech-timed archetypes opt out.
    private var ridesGuidedTimeline: Bool {
        guard let archetype = previewVariant["resolved_archetype"]?.stringValue, !archetype.isEmpty else { return true }
        return !Self.speechTimedArchetypes.contains(archetype)
    }

    /// Source media ids by `clip_index`, for slots that do not carry a `media_id` themselves
    /// (the server resolves them from the guided revision's `sources[clip_index]`).
    private var mediaIDsByClipIndex: [Int: String] {
        var result: [Int: String] = [:]
        // The variant's own clip pool names every clip's media (resolved or not); the resolved
        // source pool only fills in what it lacks.
        for clip in previewVariant["user_timeline"]?.objectValue?["clips"]?.arrayValue ?? [] {
            if let object = clip.objectValue, let index = object["clip_index"]?.numberValue,
               let mediaID = object["media_id"]?.stringValue, !mediaID.isEmpty { result[Int(index)] = mediaID }
        }
        for clip in sourcePool?.clips ?? [] where result[clip.clipIndex] == nil {
            if let mediaID = clip.nativeSource?.mediaID { result[clip.clipIndex] = mediaID }
        }
        return result
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
        let speakerBackgroundObject = Self.object(value["speaker_background"])
        var captionMeta = object("caption_meta", .captionMeta)
        if let y = captionMeta?["y_frac"]?.numberValue, !(0.30...0.90).contains(y),
           captionMeta?["appearance"]?.objectValue?["position_y_frac"] != nil {
            // Upper corners use the resolved appearance geometry; the legacy
            // position field remains within its existing server contract.
            captionMeta?.removeValue(forKey: "y_frac")
        }
        return EditorCommitRequest(
            timelineSlots: array("timeline_slots", .timeline),
            // A guided timeline commit always carries the rebased label lane, like the server's
            // copilot path: the server then treats text as authored and skips its own
            // (right-biased, label-unaware) projection.
            textElements: changedSections.contains(.text) || carriesGuidedLabelsWithTimeline
                ? Self.array(value["text_elements"]) : nil,
            captionCues: array("caption_cues", .captions),
            captionMeta: captionMeta,
            mix: changedSections.contains(.mix) ? commitMix(Self.object(value["mix"]) ?? Self.object(value["audio_mix"]) ?? [:]) : nil,
            musicTrackID: changedSections.contains(.music) ? value["music_track_id"]?.stringValue : nil,
            removeMusic: changedSections.contains(.music) && document.music == nil,
            musicWindow: changedSections.contains(.music) && document.music != nil
                ? EditorCommitMusicWindow(startS: Self.number(musicObject?["start_s"]) ?? document.music?.startS ?? 0, alignment: EditorMusicAlignment(rawValue: musicObject?["alignment"]?.stringValue ?? document.music?.alignment ?? "preserve_cuts") ?? .preserveCuts) : nil,
            backgroundMusic: changedSections.contains(.backgroundMusic) ? (backgroundObject.map { EditorCommitBackgroundMusic(trackID: $0["track_id"]?.stringValue, enabled: Self.bool($0["enabled"]) ?? true, startS: Self.number($0["start_s"]), endS: Self.number($0["end_s"]), gainDB: Self.number($0["gain_db"]), muted: Self.bool($0["muted"]) ?? false) } ?? EditorCommitBackgroundMusic(enabled: false)) : nil,
            userSong: changedSections.contains(.userSong) ? commitUserSong() : nil,
            lyrics: changedSections.contains(.lyrics) ? (lyricsObject.map { EditorCommitLyrics(enabled: Self.bool($0["enabled"]), lineOverrides: Self.object($0["line_overrides"])) } ?? EditorCommitLyrics(enabled: false)) : nil,
            orientation: changedSections.contains(.orientation) ? value["orientation"]?.stringValue : nil,
            landscapeFit: changedSections.contains(.landscapeFit) ? value["landscape_fit"]?.stringValue : nil,
            soundEffects: array("sound_effects", .soundEffects),
            mediaOverlays: array("media_overlays", .mediaOverlays),
            visualBlocks: array("visual_blocks", .visualBlocks),
            motionScenes: array("motion_scenes", .motionScenes),
            motionRuntimeHash: changedSections.contains(.motionScenes) ? document.motionRuntimeHash : nil,
            cameraEffects: array("camera_effects", .cameraEffects),
            drawings: array("drawings", .drawings),
            carouselMoment: changedSections.contains(.carouselMoment) ? (carouselObject.map(EditorCarouselMomentPatch.replace) ?? .remove) : .omitted,
            speakerBackground: changedSections.contains(.speakerBackground) ? (speakerBackgroundObject.map(EditorSpeakerBackgroundPatch.replace) ?? .remove) : .omitted,
            title: changedSections.contains(.title) ? value["title"]?.stringValue : nil,
            guidedRevisionNumber: guidedRevisionNumber,
            editorStateVersion: 2,
            deletions: document.deletions.isEmpty ? nil : document.deletions,
            baseGeneration: baseGeneration
        )
    }

    /// A guided edit has no catalog-music level to set (the server refuses a `music_level` there), so
    /// its Save carries only the footage's own `original_level`.
    private func commitMix(_ mix: [String: JSONValue]) -> [String: JSONValue] {
        guard document.capabilities["original_audio"] != nil, document.capabilities["mix"]?.editable != true else { return mix }
        return mix.filter { $0.key == "original_level" }
    }

    /// What Save sends for the song: the unsaved edits (or, on a render retry, the acknowledged volume /
    /// start), with the start re-clamped to the current video length so the server never rejects it.
    private func commitUserSong() -> EditorCommitUserSong? {
        guard let pending = document.userSong ?? userSongRetry else { return nil }
        return reclamped(pending).commit
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
        if sections.orientation { result.insert(.orientation) }; if sections.landscapeFit { result.insert(.landscapeFit) }; if sections.soundEffects { result.insert(.soundEffects) }
        if sections.mediaOverlays { result.insert(.mediaOverlays) }; if sections.visualBlocks { result.insert(.visualBlocks) }
        if sections.motionScenes { result.insert(.motionScenes) }; if sections.cameraEffects { result.insert(.cameraEffects) }; if sections.drawings { result.insert(.drawings) }
        if sections.carouselMoment { result.insert(.carouselMoment) }; if sections.title { result.insert(.title) }
        // KRI-1118: an accepted background is saved even if the server does not echo the section (a rejected
        // one answers 422 and never reaches here), the same stance as the song.
        if sections.speakerBackground || submittedSections.contains(.speakerBackground) { result.insert(.speakerBackground) }
        // KRI-428: a user_song the server accepted is saved even if it does not echo the section
        // (a rejected one answers 422 and never reaches here).
        if sections.userSong || submittedSections.contains(.userSong) { result.insert(.userSong) }
        return result.intersection(submittedSections)
    }

    private func acknowledge(_ sections: Set<EditorSection>, generation: String, submittedDocument: EditorDocument) {
        guard !sections.isEmpty else { return }
        if document.revision.baseGeneration != generation {
            finishedRenderIsCurrent = false
            finishedRenderRecoveryTask?.cancel()
            finishedRenderRecoveryInFlight = false
        }
        document.revision.baseGeneration = generation
        let acknowledgedRevision = document.revision
        // KRI-576: the cuts this save carried now ARE the clean clips, so a lip-sync song start they moved stops
        // reading as an unsaved shift; remember it, because the loaded variant's window stays stale until reload.
        if sections.contains(.timeline) {
            songCaptionLipsyncCarryS += lipsyncStartShift(of: submittedDocument, against: cleanDocument)
        }
        for section in sections where section != .userSong {
            copy(section, from: submittedDocument, into: &cleanDocument)
        }
        if sections.contains(.userSong) { foldAcknowledgedUserSong(submittedDocument.userSong) }
        cleanDocument.revision = acknowledgedRevision
        changedSections.subtract(sections)
        explicitlyDirtySections.subtract(sections)
        refreshDirtyState()
    }

    /// The server accepted these song edits: they become part of the saved song, the unsaved state
    /// starts empty again, and any unsaved edit made while the save was in flight is re-expressed
    /// against the new saved song (so "drag back to the old value" is a real change).
    private func foldAcknowledgedUserSong(_ submitted: EditorUserSongState?) {
        guard let submitted else { return }
        let oldSaved = savedUserSong
        acknowledgedUserSong = EditorUserSongState.merged(acknowledgedUserSong, submitted)
        userSongRebase = { post in
            if post == submitted { return nil }
            if post?.removed == true { return post }
            guard let oldSaved, !submitted.removed else { return nil }
            var rebased = EditorUserSongState()
            let volume = post?.volume ?? oldSaved.volume, savedVolume = submitted.volume ?? oldSaved.volume
            if abs(volume - savedVolume) > 0.0005 { rebased.volume = volume }
            let start = post?.windowStartS ?? oldSaved.windowStartS, savedStart = submitted.windowStartS ?? oldSaved.windowStartS
            if abs(start - savedStart) > 0.0005 { rebased.windowStartS = start }
            // KRI-561: the creator's end is nil (none) or an absolute second; re-express it against the new saved end.
            let end = oldSaved.creatorEnd(applying: post), savedEnd = oldSaved.creatorEnd(applying: submitted)
            let endDiffers = (end == nil) != (savedEnd == nil) || abs((end ?? 0) - (savedEnd ?? 0)) > 0.0005
            if endDiffers, let duration = oldSaved.durationS { rebased.windowEndS = end ?? duration }
            return rebased.isEmpty ? nil : rebased
        }
        document.userSong = userSongRebase(document.userSong)
        cleanDocument.userSong = nil
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
        case .landscapeFit: baseline.landscapeFit = submitted.landscapeFit
        case .soundEffects: baseline.soundEffects = submitted.soundEffects
        case .mediaOverlays: baseline.mediaOverlays = submitted.mediaOverlays
        case .visualBlocks: baseline.visualBlocks = submitted.visualBlocks
        case .motionScenes:
            baseline.motionScenes = submitted.motionScenes
            baseline.motionRuntimeHash = submitted.motionRuntimeHash
        case .cameraEffects: baseline.cameraEffects = submitted.cameraEffects
        case .drawings: baseline.drawings = submitted.drawings
        case .speakerBackground: baseline.speakerBackground = submitted.speakerBackground
        case .carouselMoment: baseline.carouselMoment = submitted.carouselMoment
        case .title: baseline.title = submitted.title
        case .userSong: baseline.userSong = submitted.userSong
        }
    }
    private func reflow(_ clips: inout [EditorClip], from index: Int) {
        guard !clips.isEmpty else { return }
        let start = min(max(0, index), clips.count - 1)
        for i in start..<clips.count { let length = max(minimumClipDuration, clips[i].end - clips[i].start); let previousEnd = i == 0 ? 0 : clips[i - 1].end; clips[i].start = previousEnd; clips[i].end = previousEnd + length }
    }
    /// Retimes the text that rides the guided timeline (clip labels, titles and, on a guided
    /// story, every other text: see `GuidedLabelRebase`) after any change to clip
    /// timing (trim, extend, reorder, delete, transition overlap, add). Runs inside the
    /// same transaction as the timeline edit, so one Undo reverts both, and the text lane
    /// then differs from the clean document and is saved alongside `timeline_slots`.
    /// No-op unless a window actually moved and the text lane is editable.
    private func rebaseGuidedLabels(_ next: inout EditorDocument, from previous: EditorDocument) {
        if NativeSpeechTimelineProjection.rebase(&next, from: previous) { return }
        let freeText = ridesGuidedTimeline
        guard GuidedLabelRebase.hasLabels(next.textElements, includeFreeText: freeText), canEditSection(.text),
              previous.clips != next.clips else { return }
        let media = mediaIDsByClipIndex
        guard GuidedLabelRebase.windows(of: previous.clips, mediaByClipIndex: media)
                != GuidedLabelRebase.windows(of: next.clips, mediaByClipIndex: media) else { return }
        let before = Dictionary(next.textElements.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        next.textElements = GuidedLabelRebase.rebase(
            next.textElements, oldSlots: previous.clips, newSlots: next.clips,
            mediaByClipIndex: media, includeFreeText: freeText
        ).map { element in
            // A text the edit did not move stays byte-identical: stamping its segment_id alone
            // would mark the text lane dirty (and send it) for a clip edit that never touched it.
            guard let original = before[element.id],
                  abs(original.startS - element.startS) < 0.000_001,
                  abs(original.endS - element.endS) < 0.000_001 else { return element }
            return original
        }
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
        // window. A failed source preview must keep this fence too: an older
        // render cannot represent the saved document or replace its error.
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

    /// Release builds otherwise learn nothing when the live preview cannot be built (only DEBUG logs it), so a
    /// creator who is silently left on the finished video leaves no trace. Type, case and numeric code only
    /// (see `NativePreviewDiagnostics.previewFailure`). A failed player item reports itself, and a cancelled
    /// rebuild is not a failure.
    private func reportPreviewFailure(_ error: Error) {
        if error is CancellationError || error is NativeEditorPlaybackFailure { return }
        let report = NativePreviewDiagnostics.previewFailure(error: error, sourceState: sourcePreviewStateLabel)
        guard let api, let jobID else { return }
        Task { try? await api.reportPlaybackFailure(jobID: jobID, report: report) }
    }

    private func failSourcePreview(_ error: Error, reloadsLatestOnConflict: Bool = true) {
        reportPreviewFailure(error)
        sourcePreviewFailureIsPermanent = Self.isPermanentSourcePreviewFailure(error)
        let finishedWasCurrent = finishedRenderIsCurrent
        restoreFinishedRenderFallback()
        if (reloadsLatestOnConflict && reloadLatestAfterPreviewConflict(error)) || retrySourcePreviewQuietly(after: error) {
            // KRI-535: while the quiet rebuild runs, a render older than a staged edit stays hidden.
            finishedRenderIsCurrent = finishedWasCurrent
            if canDisplayCurrentPlayer { startPendingPlaybackIfPossible() }
            return
        }
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

    /// KRI-935: the preview's inputs belong to a newer version than the loaded edit. Load that
    /// version (keeping unsaved lanes) and rebuild the preview, instead of asking the creator to
    /// close and reopen the editor. Fenced to one reload per server generation.
    private func reloadLatestAfterPreviewConflict(_ error: Error) -> Bool {
        // A save whose render is still pending owns the next generation; its preview refresh follows it.
        guard (error as? APIError) == .conflict, api != nil, let jobID, variantKey != nil,
              pendingPreviewGeneration == nil else { return false }
        sourcePreviewState = .preparing
        NativePreviewDiagnostics.record("source-preview-conflict-reload")
        let sequence = sourcePreviewSequence
        Task { @MainActor [weak self] in
            // The editor's own load (or a Save) is still finishing; reload once it settles.
            for _ in 0..<200 {
                guard let self, self.isSaving else { break }
                try? await Task.sleep(for: .milliseconds(50))
            }
            // Another job, or a newer preview build, owns the state now.
            guard let self, !Task.isCancelled, self.jobID == jobID, self.sourcePreviewSequence == sequence else { return }
            guard !self.isSaving else { return self.failSourcePreview(error, reloadsLatestOnConflict: false) }
            switch await self.reloadLatestKeepingLocalEdits(fencingPreviewConflict: true) {
            case .reloaded:
                return
            case .failed(let failure) where failure is CancellationError:
                return
            case .alreadyTried:
                self.failSourcePreview(error, reloadsLatestOnConflict: false)
            case .failed(let failure):
                self.failSourcePreview(failure, reloadsLatestOnConflict: false)
            }
        }
        return true
    }

    /// KRI-587: right after the phone's own export, AVFoundation can briefly refuse to build or play the
    /// editable composition. Rebuild it once, quietly, before showing the fallback card. Meanwhile the
    /// finished render plays when it is current, and "Preparing preview" shows when it is not.
    private func retrySourcePreviewQuietly(after error: Error) -> Bool {
        guard !sourcePreviewQuietRetryUsed, Self.isTransientSourcePreviewFailure(error),
              api != nil, let jobID, variantKey != nil else { return false }
        sourcePreviewQuietRetryUsed = true
        sourcePreviewState = .preparing
        NativePreviewDiagnostics.record("source-preview-quiet-retry", fields: ["error": String((error as NSError).code)])
        let sequence = sourcePreviewSequence
        let delay = sourcePreviewQuietRetryDelay
        sourcePreviewQuietRetryTask?.cancel()
        sourcePreviewQuietRetryTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: delay)
            // A newer rebuild (an edit, another job) already owns the preview state.
            guard let self, !Task.isCancelled, self.jobID == jobID, self.sourcePreviewSequence == sequence else { return }
            await self.prepareSourcePreview()
        }
        return true
    }

    /// AVFoundation refusing the composition for the moment, not a problem with the edit itself:
    /// building the same document again can work.
    static func isTransientSourcePreviewFailure(_ error: Error) -> Bool {
        if case NativeEditorPlaybackFailure.liveItem = error { return true }
        return (error as NSError).domain == AVFoundationErrorDomain
    }

    /// KRI-626: Apple's recovery after `mediaServicesWereReset` is to reconfigure the audio session and
    /// rebuild every player and composition. The old ones never play again, so recovery budgets that
    /// were spent on failures from before the reset start over.
    func handleMediaServicesReset() {
        NativePreviewDiagnostics.record("media-services-reset", fields: ["state": sourcePreviewStateLabel])
        startPlaybackRecoveryEpisode()
        finishedRenderRecoveryTask?.cancel(); finishedRenderRecoveryInFlight = false
        sourcePreviewQuietRetryTask?.cancel()
        let wantsPlayback = isPlaying || pendingPlayRequest
        player?.pause()
        isPlaying = false
        failureHandledItem = nil
        activatePreviewAudio()
        if player != nil, player === finishedRenderPlayer, let url = playableDeviceOutputURL ?? finishedRenderURL {
            reinstallFinishedRender(url: url, preferredDuration: finishedRenderDuration)
        }
        pendingPlayRequest = wantsPlayback
        guard api != nil, jobID != nil, variantKey != nil, sourcePreviewState != .idle else {
            startPendingPlaybackIfPossible()
            return
        }
        Task { @MainActor [weak self] in await self?.prepareSourcePreview() }
    }

    /// `AVErrorMediaServicesWereReset`, or the expired-audio-session error (`'!ses'`) the dead session
    /// leaves behind on the next item. Both clear once the session is set up again.
    static func isMediaServicesResetFailure(_ error: Error?) -> Bool {
        guard let error = error as NSError? else { return false }
        let expiredSession = 561210739 // '!ses'
        if error.domain == AVFoundationErrorDomain, error.code == AVError.mediaServicesWereReset.rawValue { return true }
        let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError
        return error.code == expiredSession || underlying?.code == expiredSession
    }

    /// A new render, or the creator's Retry, is a fresh start for every automatic recovery.
    private func startPlaybackRecoveryEpisode() {
        finishedRenderRetryCount = 0
        sourcePreviewQuietRetryUsed = false
    }

    /// The canvas holds the finished render and it can no longer play.
    private var finishedRenderIsDead: Bool {
        guard let player, player === finishedRenderPlayer else { return false }
        return finishedRenderFailed || player.currentItem?.status == .failed
    }

    /// This phone's copy of its own render, when the file is still there.
    private var playableDeviceOutputURL: URL? {
        guard let deviceOutputURL, FileManager.default.fileExists(atPath: deviceOutputURL.path) else { return nil }
        return deviceOutputURL
    }

    /// Swaps in a new finished-render player without changing whether that render is current.
    private func reinstallFinishedRender(url: URL, preferredDuration: TimeInterval?) {
        let isCurrent = finishedRenderIsCurrent
        installFinishedRenderPlayer(url: url, preferredDuration: preferredDuration)
        finishedRenderIsCurrent = isCurrent
    }

    /// The Retry and Try again buttons: every automatic recovery starts over, a dead finished render is
    /// reloaded, then the editable preview is rebuilt.
    func retryPreview() async {
        startPlaybackRecoveryEpisode()
        // A Retry after a conflict may reload the newest version once more.
        previewConflictReloadGeneration = nil
        if finishedRenderIsDead, let url = playableDeviceOutputURL ?? finishedRenderURL {
            finishedRenderRecoveryTask?.cancel(); finishedRenderRecoveryInFlight = false
            reinstallFinishedRender(url: url, preferredDuration: finishedRenderDuration)
        }
        await prepareSourcePreview()
    }

    /// One place for "the item AVPlayer is holding cannot play". Reports the error identity from every
    /// build, then recovers: a failed editable preview hands over to the finished render, and a failed
    /// finished render is retried (KRI-587) before the failure is surfaced.
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
        // KRI-626: a media-services reset leaves the audio session expired; retrying on it fails again.
        if Self.isMediaServicesResetFailure(error) { activatePreviewAudio() }
        switch kind {
        case .live:
            pendingPlayRequest = wantsPlayback
            failSourcePreview(NativeEditorPlaybackFailure.liveItem)
        case .finished:
            // An obsolete render may still be retained for recovery, but it is
            // hidden. Its failures cannot replace the current preview's state or
            // start a retry loop against a generation the server no longer serves.
            guard finishedRenderIsCurrent, !hasUnsavedChanges, chatStagedDocument == nil else {
                finishedRenderFailed = true
                return
            }
            pendingPlayRequest = wantsPlayback
            recoverFinishedRender(failedPlayer: failedPlayer)
        }
    }

    /// KRI-587: a finished render that fails is retried a few times, spaced out, before anything is
    /// said. Each retry prefers this phone's own copy of a phone render, then a fresh server link, and
    /// only then the same link again.
    private func recoverFinishedRender(failedPlayer: AVPlayer) {
        guard finishedRenderRetryCount < finishedRenderRetryDelays.count else {
            surfaceUnplayableFinishedRender()
            return
        }
        let delay = finishedRenderRetryDelays[finishedRenderRetryCount]
        finishedRenderRetryCount += 1
        let attempt = finishedRenderRetryCount
        let failedURL = (failedPlayer.currentItem?.asset as? AVURLAsset)?.url
        finishedRenderRecoveryTask?.cancel()
        finishedRenderRecoveryInFlight = true
        finishedRenderRecoveryTask = Task { @MainActor [weak self, weak failedPlayer] in
            try? await Task.sleep(for: delay)
            // Whoever cancelled this (a newer failure, Retry, another job) owns the in-flight flag.
            guard let self, !Task.isCancelled else { return }
            let next = failedPlayer != nil && self.player === failedPlayer
                ? await self.finishedRenderRetryTarget(after: failedURL) : nil
            guard !Task.isCancelled else { return }
            self.finishedRenderRecoveryInFlight = false
            // The canvas moved on (the editable preview took over, another job loaded) while this waited.
            guard let failedPlayer, self.player === failedPlayer else { return }
            guard let next else { self.surfaceUnplayableFinishedRender(); return }
            NativePreviewDiagnostics.record("finished-render-retry", fields: ["attempt": String(attempt), "source": next.source])
            self.reinstallFinishedRender(url: next.url, preferredDuration: self.finishedRenderDuration)
            self.startPendingPlaybackIfPossible()
        }
    }

    private func finishedRenderRetryTarget(after failed: URL?) async -> (url: URL, source: String)? {
        func untried(_ url: URL?) -> URL? {
            guard let url, url != failed else { return nil }
            return !url.isFileURL || FileManager.default.fileExists(atPath: url.path) ? url : nil
        }
        if let device = untried(playableDeviceOutputURL) { return (device, "device") }
        if let finished = untried(finishedRenderURL) { return (finished, "finished") }
        if let api, let jobID, let fresh = try? await api.playbackURL(jobID: jobID) { return (fresh, "fresh") }
        // Nothing new to try: the same link again still beats giving up after a moment's failure,
        // unless it is a file that simply isn't there.
        if let failed, !failed.isFileURL || FileManager.default.fileExists(atPath: failed.path) { return (failed, "same") }
        return nil
    }

    private func surfaceUnplayableFinishedRender() {
        // The retry may have started before a save or staged edit made this
        // player obsolete. Its delayed completion cannot own the new error.
        guard finishedRenderIsCurrent, !hasUnsavedChanges, chatStagedDocument == nil else { return }
        // An editable preview still building will take over by itself; only a settled state needs a message.
        guard sourcePreviewState != .preparing else { return }
        let message = Self.sourcePreviewMessage(for: NativeEditorPlaybackFailure.finishedItem)
        if sourcePreviewState != .failed(message) { stateBeforeFinishedRenderFailure = sourcePreviewState }
        finishedRenderFailed = true
        sourcePreviewState = .failed(message)
        pendingPlayRequest = false
        NativePreviewDiagnostics.record("finished-render-unplayable", fields: ["attempts": String(finishedRenderRetryCount)])
    }

    /// A player item that loads ends the finished render's failure streak. When it is the finished
    /// render after its failure was surfaced, the "couldn't be loaded" message goes with it (KRI-587).
    private func playerItemBecameReady() {
        finishedRenderRetryCount = 0
        guard finishedRenderFailed else { return }
        finishedRenderFailed = false
        if sourcePreviewState == .failed(Self.sourcePreviewMessage(for: NativeEditorPlaybackFailure.finishedItem)) {
            sourcePreviewState = stateBeforeFinishedRenderFailure ?? .idle
        }
        stateBeforeFinishedRenderFailure = nil
        NativePreviewDiagnostics.record("finished-render-recovered")
    }

    private func restoreFinishedRenderFallback() {
        guard let finishedRenderURL, finishedRenderIsCurrent,
              !hasUnsavedChanges, chatStagedDocument == nil else { return }
        guard player === finishedRenderPlayer else {
            installFinishedRenderPlayer(url: finishedRenderURL, preferredDuration: finishedRenderDuration)
            return
        }
        // KRI-587: a finished render that died while the editable preview was building (its message was
        // held back) would otherwise stay a black box under the fallback card. A retry on its way wins.
        guard finishedRenderIsDead, !finishedRenderRecoveryInFlight else { return }
        finishedRenderRetryCount = 0
        reinstallFinishedRender(url: playableDeviceOutputURL ?? finishedRenderURL, preferredDuration: finishedRenderDuration)
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
                else if status == .readyToPlay, self.player?.currentItem === item { self.playerItemBecameReady() }
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
        // The KVO hop reads `timeControlStatus` on a later main-actor turn, and AVPlayer can still say
        // `.playing` for a beat after `pause()` has zeroed `rate` (KRI-1087). `rate` is the intent, so a
        // .playing report at rate 0 is stale: it must not turn an explicit pause back into playback.
        let nextIsPlaying = status == .playing && (player?.rate ?? 0) != 0
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

    /// `capabilities` is a read the caller already started; without one this makes its own.
    private func applyCloudReadOnlyIfNeeded(
        api: any KriaAPIClient, variant: [String: JSONValue]?, capabilities: Task<CreationCapabilities?, Never>? = nil
    ) async {
        guard variant != nil else {
            cloudEditorUnavailableMessage = nil
            return
        }
        let loaded: CreationCapabilities?
        if let capabilities { loaded = await capabilities.value } else { loaded = try? await api.creationCapabilities() }
        if let loaded { lastPhoneRenderingCapabilities = loaded.phoneRendering ?? .disabled }
        let creationMode = loaded?.creationMode
        if creationMode == .deviceOnly, !rendersOnDevice {
            let message = "This video was rendered in the cloud and can’t be changed on this iPhone. You can still play its existing video."
            cloudEditorUnavailableMessage = message
            canEditTimeline = false
            canEditText = false
            canEditCaptions = false
            canEditMix = false
        } else {
            cloudEditorUnavailableMessage = nil
        }
    }

    private func configureCapabilities(from variant: [String: JSONValue]?) {
        previewVariant = variant ?? [:]
        // The variant is the server's word on the song: what it lists already includes saved edits.
        acknowledgedUserSong = nil
        // KRI-576: its lyric rows were projected for that same window.
        songCaptionWindowStartS = NativeUserSong(variant: previewVariant)?.windowStartS
        songCaptionLipsyncCarryS = 0
        rendersOnDevice = variant?["render_destination"]?.stringValue == "device"
        guidedRevisionNumber = Self.number(variant?["editor_revision_number"]).flatMap { $0 >= 1 && $0 <= Double(Int32.max) ? Int($0) : nil }
        configureEditingPermissions(from: variant)
    }

    private func refreshEditingPermissions(from variant: [String: JSONValue]?, document latest: EditorDocument) {
        guard cloudEditorUnavailableMessage == nil, document.capabilities != latest.capabilities else { return }
        func refreshed(_ value: EditorDocument) -> EditorDocument {
            var value = value
            value.capabilities = latest.capabilities
            return value
        }
        document = refreshed(document)
        cleanDocument = refreshed(cleanDocument)
        undoStack = undoStack.map(refreshed)
        redoStack = redoStack.map(refreshed)
        if let transactionBaseline { self.transactionBaseline = refreshed(transactionBaseline) }
        if var activeTrim {
            activeTrim.baseline = refreshed(activeTrim.baseline)
            activeTrim.redoBaseline = activeTrim.redoBaseline.map(refreshed)
            self.activeTrim = activeTrim
        }
        if var activeTimedEdit {
            activeTimedEdit.baseline = refreshed(activeTimedEdit.baseline)
            activeTimedEdit.redoBaseline = activeTimedEdit.redoBaseline.map(refreshed)
            self.activeTimedEdit = activeTimedEdit
        }
        previewVariant["editor_capabilities"] = variant?["editor_capabilities"]
        configureEditingPermissions(from: variant)
    }

    private func configureEditingPermissions(from variant: [String: JSONValue]?) {
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

    /// The generation the last save started from. A ready read of it is the old video, never "newer".
    private var previewSupersededGeneration: String?
    /// Poll cadence and how many answered polls to wait before calling the preview slow. Internal for tests.
    var previewPollInterval: Duration = .seconds(1)
    var previewPollBudget = 300

    private func startPreviewRefresh(generation: String) {
        previewRefreshTask?.cancel()
        pendingPreviewGeneration = generation
        if surfaceDeviceRenderFailure() { return }
        guard let api, let jobID, let variantKey else {
            saveState = .previewFailed("Your edit is saved, but this device cannot check the new preview yet.")
            return
        }
        let savedFrom = previewSupersededGeneration
        let interval = previewPollInterval
        let budget = previewPollBudget
        previewRefreshTask = Task { [weak self] in
            // Only answered polls spend the budget: offline or a 5xx just waits longer between tries.
            var answered = 0
            var unreachable = 0
            var newerGeneration: String?
            var newerSightings = 0
            while answered < budget {
                guard !Task.isCancelled else { return }
                let backoff = unreachable == 0 ? interval : min(interval * (1 << min(unreachable, 4)), .seconds(15))
                try? await Task.sleep(for: backoff)
                guard !Task.isCancelled else { return }
                let variant: [String: JSONValue]
                do {
                    variant = try await api.editorVariant(jobID: jobID, variantID: variantKey)
                } catch {
                    switch Self.refreshFailure(error) {
                    case .cancelled:
                        return
                    case .appGate:
                        // Sign-in or update is shown app-wide; nothing to poll for until then.
                        return
                    case let .transient(retryAfter):
                        unreachable += 1
                        if let retryAfter { try? await Task.sleep(for: .seconds(min(retryAfter, 60))) }
                    case .readConflict, .targetLost, .runtimeUnavailable, .other:
                        answered += 1
                    }
                    continue
                }
                unreachable = 0
                answered += 1
                guard !Task.isCancelled else { return }
                guard let self, self.pendingPreviewGeneration == generation else { return }
                // Device failures are reported on the authoritative variant;
                // only reconcile the local renderer for that terminal status.
                // Save-time reconciliation already covers the normal path,
                // while this branch lets a later server failure reach the
                // device retry affordance without doing a full reconcile on
                // every poll tick. A reconcile skipped for unreadable
                // capabilities is retried here too.
                let variantGeneration = variant["render_generation_id"]?.stringValue ?? variant["render_finished_at"]?.stringValue
                let refreshDevice = self.deviceRenderKey != nil && (
                    self.deviceReconcilePending
                    || variant["render_status"]?.stringValue == "needs_attention"
                    || (variant["render_status"]?.stringValue == "ready" && variantGeneration != generation)
                )
                if refreshDevice {
                    await self.refreshDeviceRender()
                    guard !Task.isCancelled, self.pendingPreviewGeneration == generation else { return }
                }
                if self.applyPreviewVariant(variant, generation: generation) { return }
                // The server moved past this save: another device saved on top of it, or this
                // iPhone published under a request the identity fence no longer recognises. Waiting
                // for this generation can never end, so follow the newer one once it reads the same
                // twice (the fresh reconcile above has had its chance to match the fence).
                guard variant["render_status"]?.stringValue == "ready",
                      let current = variant["render_generation_id"]?.stringValue, !current.isEmpty,
                      current != generation, current != savedFrom else {
                    newerGeneration = nil; newerSightings = 0
                    continue
                }
                newerSightings = newerGeneration == current ? newerSightings + 1 : 1
                newerGeneration = current
                guard newerSightings >= 2 else { continue }
                self.pendingPreviewGeneration = current
                if self.applyPreviewVariant(variant, generation: current) { return }
                self.pendingPreviewGeneration = generation
            }
            guard !Task.isCancelled, let self, self.pendingPreviewGeneration == generation else { return }
            // Never reuse a read error's copy here: "This edit changed elsewhere" is a save conflict.
            self.saveState = .previewFailed("Your edit is saved, but the preview is taking longer than expected. Try checking again.")
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
        if surfaceDeviceRenderFailure() { return true }
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
            expectedThreadID: threadID,
            songWindowStartS: NativeUserSong(variant: variant)?.windowStartS
        )
        if let key = deviceRenderKey,
           let presentation = deviceRenders?.presentations[key],
           presentation.requiresServerRetry {
            saveState = .deviceRenderRetryNeeded(Self.deviceRenderRetryMessage(presentation))
            return true
        }
        let status = variant["render_status"]?.stringValue
        if status == "ready", let output = variant["output_url"]?.stringValue, let url = URL(string: output) {
            if !rebaseCleanDraft(from: variant) {
                rebaseGenerationPreservingLocalEdits(currentGeneration)
            }
            // A new render: the phone's copy of the previous one is no longer the video to retry.
            deviceOutputURL = nil
            startPlaybackRecoveryEpisode()
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
        expectedDocumentGeneration: String? = nil,
        songWindowStartS: Double? = nil
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
        if currentWasClean {
            document.textElements = authoritativeText
            // KRI-576: the adopted lyric rows sit at the window the server projected them for, so the preview
            // shift restarts from there (no double shift). Text edited meanwhile keeps the old rows, so the old
            // anchor stays.
            if let songWindowStartS { songCaptionWindowStartS = songWindowStartS }
        }

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
        // A change-required or outdated-app reason: a retry re-pins the same recipe and
        // this iPhone declines it again. The banner offers chat or Update Kria instead.
        guard [.retry, .resume, .renderNow].contains(deviceRenderNextStep) else { return }
        if deviceRenders.isPausedByCreator(key) {
            // This tap is the creator's typed Resume; only it may release a durable Stop. A server that
            // recorded Stop as a failure also needs the /retry that lifts its pause, which the typed
            // retry mints (or turns into a plain Resume while a durable intent still holds the render).
            if deviceRenders.presentations[key]?.requiresServerRetry == true { _ = await deviceRenders.retryNeedsAttentionFromUser(key) }
            else { await deviceRenders.resumeFromUser(key) }
        } else if deviceRenders.presentations[key]?.requiresServerRetry == true {
            let retried = await deviceRenders.retryNeedsAttention(key)
            guard retried else { return }
        }
        guard !Task.isCancelled, pendingPreviewGeneration == generation, deviceRenderKey == key else { return }
        await refreshDeviceRender(retry: true)
        guard !Task.isCancelled, pendingPreviewGeneration == generation, deviceRenderKey == key else { return }
        pendingDeviceRenderIdentity = deviceRenders.request(for: key)?.identity
        saveState = .previewPending
        startPreviewRefresh(generation: generation)
    }

    /// Saving the edit and producing its video are separate outcomes. A local
    /// preparation failure (including a failed status fetch) must leave a useful
    /// retry instead of polling the unchanged awaiting_device variant forever.
    @discardableResult
    private func surfaceDeviceRenderFailure() -> Bool {
        guard let key = deviceRenderKey, let presentation = deviceRenders?.presentations[key],
              Self.deviceRenderNeedsRetry(presentation) else { return false }
        saveState = .deviceRenderRetryNeeded(Self.deviceRenderRetryMessage(presentation))
        return true
    }

    func retryRender() async {
        guard !isSaving, !pendingRenderRetrySections.isEmpty else { return }
        explicitlyDirtySections.formUnion(pendingRenderRetrySections)
        // Re-send an acknowledged volume / start so the render restarts; never a removal.
        if pendingRenderRetrySections.contains(.userSong), let saved = acknowledgedUserSong, !saved.removed { userSongRetry = saved }
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

// Declared here, in the scanned session file, so the editor capability parity test sees every extension.
extension NativeEditorSession: NativeEditorExportSource {}
