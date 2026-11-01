import Foundation
import KriaMediaEngine

struct NativeEditorRenderProgram: Sendable {
    let recipe: KriaMediaEngine.EditRecipe
    let assetURLs: [String: URL]
}

enum NativeEditorRenderError: Error, Equatable {
    case unsupportedLane(String)
    case missingSpeechCaptionTiming
    case missingFont(String)
    case missingSource(Int)
    /// No clip reached the video track, so nothing would play under the
    /// document's cards and text.
    case missingVideoTrack
    /// More text layers than one recipe may carry even after captions fall back to one layer per line.
    /// Rebuilding the same document hits the same limit, so it is a permanent failure, not a retry.
    case tooManyTextLayers
}

/// Compiles the public editor document. Internal backend assembly state never
/// participates in local rendering, and an unknown lane cannot silently vanish.
@MainActor final class NativeEditorRenderCompiler {
    private let fontURLs: [String: URL]
    private let fontAliases: [String: String]
    private let fontInstances: [String: [String: Double]]
    private let glyphFallback: NativeGlyphFallback
    private let handwritingLayout: AuthoredHandwritingLayout
    private var fontAssets: [String: MediaAsset] = [:]

    /// The `audioSources` key of the creator's own song. Catalog tracks are keyed by their id, so this
    /// can never be a plain word like "song" that a track id could equal.
    static let songSourceKey = "user_song"
    private static let songTrackID = NativeEditorSongBed.trackID
    /// Mirrors `CAPTION_BAND_TOP_FRAC` (app/pipeline/phone_subtitled_lanes.py): a PiP card's centre never goes
    /// below it, so a card stays out of the caption band.
    static let deviceCardMaxYFraction = 0.62
    /// The engine's text-layer cap (`EditRecipe.validate`). A recipe over it is refused whole.
    static let maxTextLayers = KriaMediaEngine.EditRecipe.maxTextLayers
    /// Mirrors `_MIN_CUTAWAY_S` (app/pipeline/phone_subtitled_plan.py): a shorter footage window reads as a flicker.
    static let minimumTalkingCutawayS = 0.5

    init(fontDirectory: URL) throws {
        handwritingLayout = try AuthoredHandwritingLayout(url: fontDirectory.appendingPathComponent("handwriting-strokes.json"))
        // Last-wins, like the backend: the registry repeats "Outfit" (see NativeFontRegistry).
        guard let registry = NativeFontRegistry.entries(from: try Data(contentsOf: fontDirectory.appendingPathComponent("font-registry.json"))) else {
            throw NativeEditorRenderError.missingFont("font-registry.json")
        }
        fontAliases = registry.mapValues(\.file)
        fontInstances = try JSONDecoder().decode([String: [String: Double]].self, from: Data(contentsOf: fontDirectory.appendingPathComponent("native-font-instances.json")))
        let urls = Dictionary(uniqueKeysWithValues: try FileManager.default.contentsOfDirectory(
            at: fontDirectory, includingPropertiesForKeys: nil
        ).filter { ["ttf", "otf"].contains($0.pathExtension.lowercased()) }.map { ($0.lastPathComponent, $0) })
        fontURLs = urls
        let aliases = fontAliases
        // Same lookup order as `resolveFont`.
        glyphFallback = NativeGlyphFallback(fontDirectory: fontDirectory) { family in
            [aliases[family], family + ".ttf", family + "-Regular.ttf", family + ".otf"].compactMap { $0 }.lazy.compactMap { urls[$0] }.first
        }
    }

    /// `deviceCaptions`: the variant renders on the phone, whose caption
    /// compiler (`phone_captions.py`) places every caption cue in the Talking
    /// frame (lower-third safe zone, pop-in) whatever the format. The preview
    /// follows it so a phone Narrated caption shows where the phone burns it
    /// (KRI-280); cloud Narrated keeps its low burn position.
    ///
    /// `skippedMediaOverlayIDs`: `overlay:<id>` keys of cards whose media failed to load; they are left out of the
    /// preview recipe only.
    ///
    /// `songCaptionShift` (KRI-576): re-times the song's lyric caption rows for the live preview only; nil leaves
    /// every row exactly as the document has it.
    ///
    /// `talkingCutaways` (KRI-719): a multi-clip Talking edit's muted full-frame footage windows, drawn from
    /// `sources` like the speakers. Server state, not document content: preview only.
    func compile(document: EditorDocument, clips: [EditorClip], items: [NativeEditorTimelineItem],
                 sources: [Int: ResolvedEditorSource], audioSources: [String: ResolvedEditorSource] = [:], mediaSources: [String: ResolvedEditorSource] = [:],
                 referenceOnlyMusic: Bool = false, sourceAudioPreserved: Bool = true,
                 songBed: NativeEditorSongBed? = nil,
                 deviceCaptions: Bool = false,
                 guidedStory: Bool = false,
                 songCaptionShift: NativeSongCaptionShift? = nil,
                 skippedMediaOverlayIDs: Set<String> = [],
                 talkingCutaways: [NativePhoneTalkingSource.Cutaway] = []) throws -> NativeEditorRenderProgram {
        if NativeSpeechTimelineProjection.requiresCaptionTiming(document) {
            throw NativeEditorRenderError.missingSpeechCaptionTiming
        }
        for (name, populated) in [
            ("carousel", document.carouselMoment != nil),
            ("speaker background", document.unreadableSpeakerBackground),
        ] where populated { throw NativeEditorRenderError.unsupportedLane(name) }
        guard document.opaqueRecords.values.allSatisfy(\.isEmpty) else {
            #if DEBUG
            NativePreviewDiagnostics.record("unparsed-lanes", fields: Dictionary(uniqueKeysWithValues:
                document.opaqueRecords.map { (String(describing: $0.key), String($0.value.count)) }))
            #endif
            throw NativeEditorRenderError.unsupportedLane("unknown")
        }
        let canvas = Self.canvas(for: document)
        var assets: [String: MediaAsset] = [:]
        var references: [String: RenderAssetReference] = [:]
        var urls: [String: URL] = [:]
        var video: [TimelineClip] = []
        /// Each video clip's camera level if no song plays. The creator's song mutes the camera only
        /// while a song bed is actually applied (KRI-428); removing the song, or a bed that cannot
        /// play, restores these levels.
        var cameraLevelWithoutSong: [Double] = []
        let requestedOriginalGain = document.mix["original_level"]?.numberValue ?? 1
        let originalGain = requestedOriginalGain.isFinite ? min(max(0, requestedOriginalGain), 1) : 1
        let hasExplicitOriginalGain = document.mix["original_level"] != nil
        let activeSlots = document.clips.filter { !$0.removed }
        // Voiceover renders normally silence every camera clip. A new server
        // projection may opt one or more slots back in with this field: it is
        // the clip-level half of the mix, while `originalGain` below remains
        // the global creator-controlled bed gain. Its *presence* is the
        // feature gate so older narrated documents stay byte-for-byte silent.
        let hasExplicitNarratedSourceAudio = activeSlots.contains {
            $0.raw["source_audio_volume"]?.numberValue != nil
        }
        // The creator's song replaces the camera audio unless they explicitly turned the original
        // audio up (Sounds > Original audio): then the camera plays WITH the song at that level
        // (the song keeps its own level). An unset level, or an explicit 0, keeps the camera silent.
        let creatorHearsCameraWithSong = hasExplicitOriginalGain && originalGain > 0
        func slot(for clip: EditorClip, index: Int) -> EditorTimelineSlot? {
            if let id = clip.slotID, let value = activeSlots.first(where: { $0.id == id }) { return value }
            return activeSlots.indices.contains(index) ? activeSlots[index] : nil
        }
        for (index, clip) in clips.enumerated() {
            guard let sourceIndex = clip.sourceClipIndex, let source = sources[sourceIndex],
                  let fingerprint = source.asset.fingerprint else {
                throw NativeEditorRenderError.missingSource(clip.sourceClipIndex ?? index)
            }
            let id = "source-\(sourceIndex)"
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint),
                                                   source: .original(mediaID: source.mediaID))
            urls[id] = source.url
            let authoredSlot = slot(for: clip, index: index)
            guard authoredSlot?.raw["layout"]?.stringValue != "supporting_card" else {
                throw NativeEditorRenderError.unsupportedLane("supporting card")
            }
            guard authoredSlot?.lookAdjustments == nil || authoredSlot?.lookAdjustments?.isEmpty == true,
                  [nil, "none", "golden_hour"].contains(authoredSlot?.lookPreset) else {
                throw NativeEditorRenderError.unsupportedLane("look")
            }
            var transition: Transition?
            if index > 0, let previous = slot(for: clips[index - 1], index: index - 1),
               previous.transitionAfter != "cut" {
                let kind: Transition.Kind
                switch previous.transitionAfter {
                case "crossfade": kind = .crossfade
                case "dip_to_black": kind = .fadeBlack
                case "flash": kind = .fadeWhite
                default: throw NativeEditorRenderError.unsupportedLane("transition")
                }
                // NativeEditorInteraction.transitionOverlap already decided how
                // much (if any) of these two clips overlaps -- it floors any
                // overlap under 0.1s to zero, which `timelineClips` bakes into
                // `clips[index-1].end`/`clip.start` as an exact abut. A
                // transition built from the slot's *requested* duration
                // instead of that projected geometry can claim overlap that
                // doesn't exist, which Composition then rejects outright
                // (KRI-164). The projection owns the overlap; only emit a
                // transition when it actually left room for one.
                let overlap = clips[index - 1].end - clip.start
                if overlap > 0.000_001 { transition = Transition(kind: kind, duration: overlap) }
            }
            let explicitRate = authoredSlot?.raw["playback_rate"]?.numberValue
            if let explicitRate {
                guard NarratedRateGate.acceptsExplicit(explicitRate, slot: authoredSlot) else {
                    NarratedRateGate.reject("explicit-rate", index: index, rate: explicitRate, slot: authoredSlot)
                    throw RecipeError.invalidTimeline
                }
            }
            let outputDuration = clip.end - clip.start
            // Playback rate consumes a different source span while the slot's
            // timeline duration remains authoritative. If the source ends,
            // retain its last decoded frame for the remaining clock time; this
            // keeps narration and other audio tracks on their original clock.
            var sourceStart = clip.trimIn
            var sourceSpan = max(0, clip.trimOut - clip.trimIn)
            if let selectedEnd = authoredSlot?.raw["source_end_s"]?.numberValue,
               selectedEnd.isFinite, selectedEnd > sourceStart {
                sourceSpan = selectedEnd - sourceStart
            }
            let authoredMovingDuration = max(0, authoredSlot?.durationS ?? outputDuration)
            var requestedRate = explicitRate ?? sourceSpan / authoredMovingDuration
            // KRI-828: a creator's freeze frame. The slot's window plays at 1x and its last
            // frame holds for `freeze_hold_s` (inside the slot's duration); never a slow rate
            // derived from a source span shorter than the slot.
            // `freeze_frame_s` is the frozen frame's source time, which editor trims never
            // rewrite: a trim of the slot's end shortens the hold on the same frame and a
            // trim of its start shortens the motion before it, exactly like the server's
            // Save (`build_guided_segments`). A slot that no longer reaches that frame with
            // room to move (the leading half of a split) plays with no hold.
            var freezeHold = authoredSlot?.raw["freeze_hold_s"]?.numberValue ?? 0
            if freezeHold > 0, let frame = authoredSlot?.raw["freeze_frame_s"]?.numberValue, frame.isFinite {
                let motion = frame - sourceStart
                let hold = ((authoredMovingDuration - motion) * 30).rounded() / 30
                freezeHold = motion >= 0.1 - 0.000_001 && hold >= 1.0 / 30 - 0.000_001 ? hold : 0
            }
            let freezes = explicitRate == nil && freezeHold.isFinite && freezeHold > 0
                && authoredMovingDuration - freezeHold > 0
            if freezes {
                requestedRate = 1
                sourceSpan = authoredMovingDuration - freezeHold
            }
            // Narration and legacy projections can intentionally carry a
            // shorter moving span plus a hold. Only an unchanged authored
            // source window uses the duration-preserving render projection.
            let isAuthoredWindow = authoredSlot?.raw["native_source_span_s"] == nil
                && authoredSlot?.raw["source_end_s"] == nil
                && abs(outputDuration - authoredMovingDuration) < 0.000_001
                && abs(sourceStart - (authoredSlot?.inS ?? sourceStart)) < 0.000_001
            if isAuthoredWindow, !freezes, let authoredSlot,
               let available = source.asset.duration ?? clip.sourceDuration {
                let authoredRate = explicitRate ?? 1
                let requestedSpan = authoredMovingDuration * authoredRate
                guard available.isFinite, authoredSlot.inS.isFinite, authoredRate.isFinite,
                      available > 0, authoredSlot.inS >= 0 else { throw RecipeError.invalidTimeline }
                // Saved-draft end-minus-start arithmetic can exceed the exact
                // source endpoint by a few floating-point ULPs. Match the
                // surrounding microsecond geometry checks without admitting a
                // genuinely longer source window.
                let sourceWindowToleranceS = 0.000_001
                if authoredSlot.inS + requestedSpan > available + sourceWindowToleranceS {
                    guard requestedSpan <= available + sourceWindowToleranceS,
                          authoredSlot.raw["source_start_locked"] != .bool(true) else { throw RecipeError.invalidTimeline }
                    requestedRate = authoredRate
                    sourceSpan = requestedSpan
                    sourceStart = max(0, available - requestedSpan)
                }
            }
            guard NarratedRateGate.acceptsDerived(requestedRate, sourceSpan: sourceSpan, slot: authoredSlot),
                  outputDuration > 0, sourceSpan > 0 else {
                NarratedRateGate.reject("derived-rate", index: index, rate: requestedRate, slot: authoredSlot, sourceSpan: sourceSpan)
                throw RecipeError.invalidTimeline
            }
            // The timeline owns output duration. At slow rates consume only
            // the source span that fits that clock, then hold the last frame.
            let consumedSourceDuration = min(sourceSpan, outputDuration * requestedRate)
            let movingDuration = consumedSourceDuration / requestedRate
            // A sub-microsecond gap is clock noise (the unrounded speech clock), not a held clip: it must not skip the letterbox below.
            let rawHold = outputDuration - movingDuration
            let holdDuration = rawHold > 0.000_001 ? rawHold : 0
            let hasExplicitClipAudio = authoredSlot?.raw["muted"] != nil
            let sourceGain: Double = hasExplicitOriginalGain || hasExplicitClipAudio || sourceAudioPreserved ? 1 : 0
            let narratedSourceLevel: Double
            if audioSources["narration"] != nil, hasExplicitNarratedSourceAudio {
                let requested = authoredSlot?.raw["source_audio_volume"]?.numberValue ?? 0
                // Selected sources carry unit gains; clip-scoped sound may
                // carry a fractional gain. Clamp rather than fail a preview;
                // AudioMixRecipe.originalVolume applies the global
                // original-audio gain exactly once after this value.
                narratedSourceLevel = requested.isFinite ? min(max(0, requested), 1) : 0
            } else {
                narratedSourceLevel = audioSources["narration"] != nil ? 0 : sourceGain
            }
            let cameraLevel: Double = clip.muted || authoredSlot?.raw["muted"] == .bool(true) ? 0 : narratedSourceLevel
            cameraLevelWithoutSong.append(cameraLevel)
            video.append(TimelineClip(id: clip.slotID ?? clip.id.uuidString, sourceAssetID: id,
                sourceStart: sourceStart, sourceDuration: consumedSourceDuration, timelineStart: clip.start,
                rate: requestedRate, transition: transition,
                volume: audioSources[Self.songSourceKey] != nil && !creatorHearsCameraWithSong ? 0 : cameraLevel, look: authoredSlot?.lookPreset == "golden_hour" ? .goldenHour : nil,
                holdDuration: holdDuration > 0 ? holdDuration : nil,
                sourceCrop: try Self.sourceCrop(authoredSlot?.raw["source_crop"])))
            // KRI-306: letterbox a sideways clip when the creator chose black
            // bars. Cropped and graded clips keep the engine's cover-fill: the
            // engine rejects a look combined with a non-identity transform, and
            // the backend refuses that combination for the same reason.
            // Guided stories apply their authored fit before adding a final-frame hold
            // (`phone_guided_plan`); other formats retain `_fit_eligible` behavior.
            if document.previewLetterboxesSidewaysClips, canvas.height > canvas.width, (guidedStory || holdDuration <= 0),
               authoredSlot?.lookPreset != "golden_hour", video[video.count - 1].sourceCrop == nil {
                video[video.count - 1].transform = Self.fitTransform(display: source.asset.naturalSize, canvas: canvas, landscapeFit: "fit")
            }
            // KRI-1136: the creator's tilt turns the clip about the canvas centre AFTER the letterbox scale, so a
            // tilted sideways clip keeps its bars. A graded clip stays upright, like the server's refusal.
            if video[video.count - 1].look == nil, let tilt = try Self.clipTilt(authoredSlot?.raw["tilt"]) {
                video[video.count - 1].transform.rotationDegrees = tilt.recipeRotationDegrees
                video[video.count - 1].backdrop = tilt.recipeBackdrop
            }
        }
        // The video track is the composition's clock. Without it text and
        // captions clip to nothing, yet an unclamped media overlay still makes
        // the recipe valid, and the editor would play that card over a black
        // canvas instead of falling back to the finished MP4.
        // No editor document legitimately previews without a video clip.
        guard !video.isEmpty else { throw NativeEditorRenderError.missingVideoTrack }
        var audioTracks: [TimelineTrack] = []
        let total = video.map { $0.timelineStart + $0.duration }.max() ?? 0
        func addAudio(id: String, lane: String, start: Double, length: Double, gain: Double, timelineStart: Double = 0, catalog: RenderAssetCatalog = .music) throws {
            guard length > 0, let source = audioSources[id], let fingerprint = source.asset.fingerprint,
                  let available = source.asset.duration, start >= 0, start + length <= available + 0.01 else {
                #if DEBUG
                NativePreviewDiagnostics.record("audio-range-unavailable", fields: ["lane": lane == "music" ? "music" : lane == "background-music" ? "background-music" : "sfx", "present": String(audioSources[id] != nil), "start": String(start), "length": String(length), "available": String(audioSources[id]?.asset.duration ?? -1)])
                #endif
                throw MediaEngineError.missingAsset(id)
            }
            let alias = "audio-\(id)"
            assets[alias] = MediaAsset(id: alias, relativePath: alias, fingerprint: fingerprint, duration: available)
            references[alias] = RenderAssetReference(id: alias, fingerprint: try RenderFingerprint(fingerprint),
                source: .library(catalog: catalog, catalogID: id, generation: fingerprint.hex))
            urls[alias] = source.url
            audioTracks.append(TimelineTrack(id: lane, kind: .audio, clips: [
                TimelineClip(id: lane, sourceAssetID: alias, sourceStart: start, sourceDuration: length, timelineStart: timelineStart, volume: gain)
            ]))
        }
        if let narration = audioSources["narration"] {
            guard let fingerprint = narration.asset.fingerprint, let available = narration.asset.duration,
                  available.isFinite, available > 0 else { throw MediaEngineError.missingAsset("narration") }
            #if DEBUG
            NativePreviewDiagnostics.record("narration-range", fields: ["available": String(available), "timeline": String(total)])
            #endif
            let id = "narration"
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint, duration: available)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: narration.mediaID))
            urls[id] = narration.url
            audioTracks.append(TimelineTrack(id: id, kind: .audio, clips: [
                TimelineClip(id: id, sourceAssetID: id, sourceStart: 0, sourceDuration: min(total, available), timelineStart: 0, volume: 1)
            ]))
        }
        // The creator's own song (KRI-374) is the project's soundtrack, played from the pinned
        // device recipe's `song` clip. Camera audio is forced to 0 above while a song source is
        // present unless the creator explicitly set `mix.original_level` above 0 (then the clip's
        // own `muted` flag decides), so by default it never leaks over the song. A caller that removed the song passes no
        // song source at all.
        if let song = audioSources[Self.songSourceKey] {
            guard let fingerprint = song.asset.fingerprint, let available = song.asset.duration,
                  available.isFinite, available > 0 else { throw MediaEngineError.missingAsset(Self.songTrackID) }
            let bed = songBed ?? NativeEditorSongBed(assetID: song.mediaID)
            let start = min(max(0, bed.sourceStart), available)
            // The song plays while it has time left and stops where it ends (KRI-457). The bed's own duration
            // was only the window the last saved recipe had, so it never caps a longer video.
            // KRI-561: the creator may have stopped the music earlier (absolute song second `endS`).
            let natural = min(total, available - start)
            let creatorEndApplies = bed.endS.map { $0 - start < natural - 0.05 } ?? false
            let length = creatorEndApplies ? max(0, (bed.endS ?? 0) - start) : natural
            if length > 0 {
                let id = Self.songTrackID
                assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint, duration: available)
                references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: song.mediaID))
                urls[id] = song.url
                var clip = TimelineClip(id: id, sourceAssetID: id, sourceStart: start, sourceDuration: length,
                                        timelineStart: 0, volume: bed.volume)
                clip.audioFadeIn = bed.fadeIn.map { min($0, length / 2) }
                // The server fades a creator-stopped song out over 1.5 s; a song that merely runs out keeps the
                // recipe's own fade.
                clip.audioFadeOut = creatorEndApplies
                    ? min(NativeEditorSongBed.creatorEndFadeOut, length / 2)
                    : bed.fadeOut.map { min($0, length / 2) }
                audioTracks.append(TimelineTrack(id: id, kind: .audio, clips: [clip]))
            } else {
                // No song plays (its window is past the end of the file): the camera is not muted for nothing.
                for index in video.indices { video[index].volume = cameraLevelWithoutSong[index] }
            }
        }
        // The rendered narration source already includes its approved music bed.
        if !referenceOnlyMusic, let music = document.music, audioSources["narration"] == nil, audioSources[Self.songSourceKey] == nil {
            guard music.alignment == nil || music.alignment == "preserve_cuts" else {
                throw NativeEditorRenderError.unsupportedLane("beat alignment")
            }
            try addAudio(id: music.trackID, lane: "music", start: music.startS, length: total,
                         gain: document.mix["music_level"]?.numberValue ?? 1)
        }
        if !referenceOnlyMusic, let bed = document.backgroundMusic, bed.enabled, !bed.muted, let id = bed.trackID {
            let start = bed.startS ?? 0
            let length = min(total, max(0, (bed.endS ?? (start + total)) - start))
            try addAudio(id: id, lane: "background-music", start: start, length: length, gain: pow(10, (bed.gainDB ?? -18) / 20))
        }
        for effect in document.soundEffects {
            let id = "sfx:" + effect.id
            guard let source = audioSources[id], let available = source.asset.duration,
                  let item = items.first(where: { $0.kind == .soundEffect && $0.id == effect.id }) else {
                throw MediaEngineError.missingAsset(id)
            }
            let start = effect.raw["trim_start_s"]?.numberValue ?? 0
            let end = min(available, effect.raw["trim_end_s"]?.numberValue ?? available)
            let length = min(end - start, total - item.start)
            if length > 0 {
                try addAudio(id: id, lane: id, start: start, length: length,
                    gain: effect.raw["gain"]?.numberValue ?? 1, timelineStart: item.start, catalog: .soundEffect)
            }
        }
        // KRI-282 clip-audio lane: speech excerpts lifted from one clip's own audio
        // and played over other clips' visuals. They share one audio track so
        // back-to-back excerpts of a take crossfade (AudioCutHandles.plan). An
        // unreadable row refuses the render instead of silently dropping speech.
        guard document.unreadableClipAudio == 0 else { throw NativeEditorRenderError.unsupportedLane("clip audio") }
        var clipAudioWindows: [(start: Double, end: Double)] = []
        var excerptClips: [TimelineClip] = []
        for excerpt in document.clipAudio.sorted(by: { ($0.startS, $0.id) < ($1.startS, $1.id) }) {
            guard let source = sources[excerpt.sourceClipIndex], let fingerprint = source.asset.fingerprint else {
                throw NativeEditorRenderError.missingSource(excerpt.sourceClipIndex)
            }
            let sourceSpan = excerpt.sourceEndS - excerpt.sourceStartS
            guard [excerpt.sourceStartS, excerpt.sourceEndS, excerpt.startS, excerpt.gain].allSatisfy(\.isFinite),
                  excerpt.sourceStartS >= 0, sourceSpan > 0, excerpt.startS >= 0, (0...2).contains(excerpt.gain),
                  (excerpt.fadeInS ?? 0) >= 0, (excerpt.fadeOutS ?? 0) >= 0,
                  source.asset.duration.map({ excerpt.sourceEndS <= $0 + 0.01 }) ?? true else { throw RecipeError.invalidTimeline }
            let length = min(sourceSpan, total - excerpt.startS)
            guard length > 0.001 else { continue }
            let id = "source-\(excerpt.sourceClipIndex)"
            if assets[id] == nil {
                assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
                references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
                urls[id] = source.url
            }
            var clip = TimelineClip(id: "clip-audio:" + excerpt.id, sourceAssetID: id, sourceStart: excerpt.sourceStartS, sourceDuration: length,
                                    timelineStart: excerpt.startS, volume: excerpt.gain)
            clip.audioFadeIn = excerpt.fadeInS; clip.audioFadeOut = excerpt.fadeOutS
            excerptClips.append(clip)
            clipAudioWindows.append((excerpt.startS, excerpt.startS + length))
        }
        if !excerptClips.isEmpty { audioTracks.append(TimelineTrack(id: "clip-audio", kind: .audio, clips: excerptClips)) }
        var overlays: [TimelineClip] = []
        // KRI-719 footage cutaways go first: the server's `talking-head-cutaways` track sits right above the
        // speakers and below every card (`compile_phone_subtitled_plan`). Same clamps as its
        // `_compile_cutaway_track`: never past the speakers, never past the footage (no frozen last frame),
        // and a window left under half a second is dropped.
        for (index, cutaway) in talkingCutaways.enumerated() {
            guard let source = sources[cutaway.clipIndex], let fingerprint = source.asset.fingerprint,
                  let available = source.asset.duration else { throw NativeEditorRenderError.missingSource(cutaway.clipIndex) }
            let end = min(cutaway.end, total, cutaway.start + max(0, available - cutaway.sourceStart))
            guard end - cutaway.start >= Self.minimumTalkingCutawayS else { continue }
            let id = "source-\(cutaway.clipIndex)"
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
            urls[id] = source.url
            overlays.append(TimelineClip(id: "cutaway-\(index)", sourceAssetID: id, sourceStart: cutaway.sourceStart,
                sourceDuration: end - cutaway.start, timelineStart: cutaway.start, transform: .identity, volume: 0))
        }
        // A device-rendered variant is finished from the server's pinned recipe, which draws every card BELOW
        // the text layers (no server writer ever sets `overlay_above_text`), full-screen cutaways below PiP
        // cards, and clamps PiP cards out of the caption band (`phone_subtitled_plan._compile_overlay_track`,
        // `_compile_fullscreen_track`). The preview follows it so what the creator sees is what exports.
        // Cloud variants keep the legacy ffmpeg order: cards above text, in z order.
        let cardsFollowDeviceRecipe = deviceCaptions
        func isFullscreenCard(_ card: EditorTimedEffect) -> Bool { card.raw["display_mode"]?.stringValue == "fullscreen" }
        let orderedCards: [EditorTimedEffect]
        if cardsFollowDeviceRecipe {
            func z(_ card: EditorTimedEffect) -> Double { card.raw["z"]?.numberValue ?? 0 }
            let fullscreenCards = document.mediaOverlays.filter(isFullscreenCard)
                .sorted { ($0.startS, $0.endS, $0.id) < ($1.startS, $1.endS, $1.id) }
            let pipCards = document.mediaOverlays.filter { !isFullscreenCard($0) }
                .sorted { (z($0), $0.startS, $0.id) < (z($1), $1.startS, $1.id) }
            orderedCards = fullscreenCards + pipCards
        } else {
            orderedCards = document.mediaOverlays.sorted(by: { ($0.raw["z"]?.numberValue ?? 0) < ($1.raw["z"]?.numberValue ?? 0) })
        }
        for overlay in orderedCards {
            let id = "overlay:" + overlay.id
            // A card whose photo or video could not be loaded stays in the document (and the saved edit) but
            // is not drawn: one bad file must not take the whole live preview down. Only a card the session
            // explicitly gave up on is skipped; a card that was never offered still throws.
            if skippedMediaOverlayIDs.contains(id), mediaSources[id] == nil { continue }
            guard let source = mediaSources[id], let fingerprint = source.asset.fingerprint, let size = source.asset.naturalSize,
                  let item = items.first(where: { $0.kind == .mediaOverlay && $0.id == overlay.id }), item.end > item.start else {
                throw MediaEngineError.missingAsset(id)
            }
            // "fade" is the phone Talking-to-camera card fade (KRI-182 design:
            // SubtitledOverlayCard.fade <=> entrance/exit "fade"; the server
            // projection still sends "none" until MediaOverlay accepts it). It
            // renders on the same 0.15 s curve the pinned device recipe uses
            // for that card (`VisualMediaPlacement.fadeEnvelope`).
            guard [nil, "none", "dissolve-out", "fade"].contains(overlay.raw["exit_token"]?.stringValue) else {
                throw NativeEditorRenderError.unsupportedLane("media dissolve")
            }
            guard [nil, "none", "pop_in", "fade"].contains(overlay.raw["entrance_token"]?.stringValue) else {
                throw NativeEditorRenderError.unsupportedLane("media entrance")
            }
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
            urls[id] = source.url
            let editorStyle = try Self.visualEditorStyle(overlay.raw["editor_style"])
            let fadeIn = overlay.raw["entrance_token"] == .string("fade")
            let fadeOut = overlay.raw["exit_token"] == .string("fade")
            let fullscreen = overlay.raw["display_mode"]?.stringValue == "fullscreen"
            let width = Double(canvas.width), height = Double(canvas.height)
            let coverWidth = size.width * max(width / size.width, height / size.height)
            let cardWidth = (width * (overlay.raw["scale"]?.numberValue ?? 0.35)).rounded(.toNearestOrEven)
            let fallbackY: Double = switch overlay.raw["position"]?.stringValue {
            case "top": 0.18
            case "bottom": 0.82
            default: 0.5
            }
            let x = overlay.raw["x_frac"]?.numberValue ?? 0.5
            let rawY = overlay.raw["y_frac"]?.numberValue ?? fallbackY
            // Server: `y_frac = min(card.y_frac, CAPTION_BAND_TOP_FRAC)` for PiP cards (full-screen has no geometry).
            let y = cardsFollowDeviceRecipe && !fullscreen ? min(rawY, Self.deviceCardMaxYFraction) : rawY
            let transform: MediaTransform = fullscreen ? .identity : MediaTransform(scale: cardWidth / coverWidth,
                positionX: (x * width).rounded(.toNearestOrEven) - width / 2,
                positionY: (y * height).rounded(.toNearestOrEven) - height / 2)
            let window = item.end - item.start
            let sourceStart = source.asset.duration == nil ? 0 : (overlay.raw["clip_trim_start_s"]?.numberValue ?? 0)
            let sourceEnd = source.asset.duration.map { min($0, overlay.raw["clip_trim_end_s"]?.numberValue ?? $0) }
            let playbackRate = overlay.raw["playback_rate"]?.numberValue ?? 1
            guard playbackRate.isFinite, (0.25...4).contains(playbackRate) else { throw RecipeError.invalidTimeline }
            let moving = min(window * playbackRate, sourceEnd.map { $0 - sourceStart } ?? window * playbackRate)
            guard moving > 0 else { throw RecipeError.invalidTimeline }
            overlays.append(TimelineClip(id: id, sourceAssetID: id, sourceStart: sourceStart, sourceDuration: moving,
                timelineStart: item.start, rate: playbackRate, transform: transform, volume: 0, holdDuration: max(0, window - moving / playbackRate),
                overlayAboveText: cardsFollowDeviceRecipe ? nil : true, overlayPopIn: !fullscreen && overlay.raw["entrance_token"] == .string("pop_in"),
                overlayPreserveAlpha: !fullscreen && source.preserveAlpha,
                // Only an editor_style moves a card onto the placement path.
                // That path positions media differently from `transform`
                // (no source-crop cover correction, unrounded y), so a fade
                // must not route a plain card there. A plain card fades
                // through `overlayFadeIn`/`overlayFadeOut`, which scale its
                // opacity and leave `transform` untouched; a styled card
                // fades through its placement, one fade source per clip.
                visualPlacement: editorStyle.map { VisualMediaPlacement(order: 0, contain: $0.fitMode == "contain", zoom: $0.zoom,
                    widthFraction: fullscreen ? nil : overlay.raw["scale"]?.numberValue ?? 0.35, xFraction: x, yFraction: y,
                    windowStart: item.start, windowEnd: item.end, fadeIn: fadeIn, fadeOut: fadeOut, editorStyle: $0) },
                overlayDissolveSeed: overlay.raw["exit_token"]?.stringValue == "dissolve-out" ? UInt32(211 + (document.mediaOverlays.firstIndex(where: { $0.id == overlay.id }) ?? 0) * 53) : nil,
                sourceCrop: try Self.sourceCrop(overlay.raw["source_crop"]),
                overlayFadeIn: editorStyle == nil && fadeIn ? true : nil,
                overlayFadeOut: editorStyle == nil && fadeOut ? true : nil))
        }
        func registerFont(_ font: URL) throws -> String {
            let fontID = "font-\(font.lastPathComponent)"
            if fontAssets[fontID] == nil {
                fontAssets[fontID] = MediaAsset(id: fontID, relativePath: fontID,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: font))
            }
            let asset = fontAssets[fontID]!
            assets[fontID] = asset
            references[fontID] = RenderAssetReference(id: fontID, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: .library(catalog: .font, catalogID: font.lastPathComponent, generation: asset.fingerprint!.hex))
            urls[fontID] = font
            return fontID
        }
        // Container duration is rounded independently from authored cut sums.
        // Bound text to the actual composition without changing persisted timing.
        let items = items.map { item in
            guard item.kind == .text || item.kind == .captionCue else { return item }
            return NativeEditorTimelineItem(selection: item.selection, start: item.start,
                end: min(item.end, total), zIndex: item.zIndex, sourceIndex: item.sourceIndex)
        }
        // KRI-110: guided-story captions persist as caption_cue-tagged text
        // elements, not caption_cues rows, so the styled-caption block below
        // (which is cue-native only) never sees them and they'd otherwise
        // render with their raw, unstyled fields regardless of caption_meta.
        // Patch caption_meta onto their raw fields before the generic
        // per-element styling reads them — mirrors the backend's
        // _apply_guided_caption_meta (guided_story.py), which does the exact
        // same thing server-side: overlay style fields onto the ordinary
        // TextElement rather than routing through a separate caption
        // renderer. Per-word highlighting/karaoke ("style": "word") has no
        // equivalent in the generic per-element path and is not covered here
        // — see the render-compiler TODOS.md follow-up.
        let captionMeta = document.captionMeta
        let captionsGloballyDisabled = captionMeta["enabled"] == .bool(false)
        var text: [PortableTextLayer] = []
        // This is a render projection only: the editor document continues to
        // contain one receipt-backed element per spoken word, with its original
        // id and timing. Repeating the sentence on those adjacent intervals
        // keeps it continuously visible without inventing a replacement cue.
        // KRI-576: lyric rows follow the song window as it is NOW (preview only). The shift works on a copy;
        // a row it hides is `removed` here, so the interval check below never sees a reversed window.
        let timedTextElements = songCaptionShift.map { $0.apply(to: document.textElements) } ?? document.textElements
        let renderedTextElements = Self.stampingCaptionLinePhases(
            Self.projectSentenceCaptions(timedTextElements, captionMeta: captionMeta),
            source: timedTextElements, captionMeta: captionMeta
        )
        // KRI-785: one glyph-safe face per requested family across the video (the
        // union of its texts), like the cloud's `glyph_safe_overlays`, so a Turkish
        // lyric track never flips fonts line by line.
        let glyphSafeFamilies = Self.glyphSafeFamilyTexts(renderedTextElements, captionMeta: captionMeta)
        for rawElement in renderedTextElements where rawElement.raw["enabled"] != .bool(false) && rawElement.raw["removed"] != .bool(true) {
            if rawElement.isCaption {
                if captionsGloballyDisabled { continue }
                // Talking/subtitled variants carry their captions twice: as
                // caption_cues rows and, for the editor's text lane, as
                // caption_cue-tagged text elements mirroring the same sentences
                // (the API's `_base_text_elements_for_variant` CAPTION path).
                // The cue-native block below renders the cues, so skip the
                // mirrors here; both firing burned every sentence twice, once
                // mid-frame across the speaker's face (KRI-172 render 1aff3f03).
                // The session's timeline has no item for a mirror either, so
                // this skip must stay ahead of the item lookup below, which
                // throws on a missing item.
                if document.isCaptionCueMirror(rawElement) { continue }
            }
            let element = rawElement.isCaption
                ? Self.applyingCaptionMeta(captionMeta, to: rawElement) : rawElement
            // Read-only text (the server's kill-switch shape for a phone
            // Voiceover title, KRI-455/465) has no timeline item, since nothing
            // may select or move it: it draws over its own window, exactly where
            // the server pinned it in the export. An editable title has a
            // timeline item and takes the ordinary branch.
            guard let authoredItem = rawElement.isReadOnly
                ? NativeEditorTimelineItem(selection: EditorSelection(kind: .text, id: element.id),
                    start: element.startS, end: min(element.endS, total))
                : items.first(where: { $0.kind == .text && $0.id == element.id }) else {
                throw RecipeError.invalidTimeline
            }
            // Sentence projection may extend a caption through the silent gap
            // before its next spoken word. This uses only the copied render
            // element; the timeline item and persisted document stay pinned to
            // the canonical word interval.
            let item = rawElement.isCaption
                ? NativeEditorTimelineItem(selection: authoredItem.selection, start: element.startS,
                    end: min(max(element.endS, element.startS + 0.01), total), zIndex: authoredItem.zIndex, sourceIndex: authoredItem.sourceIndex)
                : authoredItem
            // Nothing to draw is not a broken edit. A caption or title passes
            // through the empty string while its field is being retyped;
            // failing the whole composition for that fell the canvas back to
            // the stale finished render (KRI-110). Mirrors the caption-cue
            // path below and the staged pendingText handling in the session.
            if element.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
            // Trimming the video can leave authored text entirely beyond the
            // output. Keep that record for undo, but don't compile an empty or
            // reversed render interval after clipping its end to the new cut.
            guard item.start.isFinite, item.end.isFinite else { throw RecipeError.invalidTimeline }
            if item.start >= total { continue }
            guard item.end > item.start else { throw RecipeError.invalidTimeline }
            // Placement and the default face match the server burn (cloud and
            // phone recipes alike); see `EditorTextElement.anchor(of:)`.
            let raw = element.raw
            let displayText = Self.displayText(element)
            // KRI-785: a face missing a letter of this family's texts sets the whole
            // run in its glyph-fallbacks.json partner, like the cloud renderer.
            let family = glyphFallback.family(element.fontFamily, for: glyphSafeFamilies[element.fontFamily] ?? displayText)
            let font = try resolveFont(family)
            let fontID = try registerFont(font)
            let legacy = raw["effect"]?.stringValue ?? "none"
            if raw["theme_transition"]?.objectValue != nil {
                throw NativeEditorRenderError.unsupportedLane("text scene compositing")
            }
            // Text behind people: compiled as the layer's `behindSubject`; the engine occludes it with a
            // per-frame person mask (`NativeSubjectMatte`) and paints plain text whenever the mask is unsane.
            // Whether this phone may RENDER it is the server's call (capability `behindSubject`, protocol 21).
            let behindSubject: Bool? = raw["behind_subject"] == .bool(true) ? true : nil
            guard raw["animation_phases"] != nil || ["none", "static", "fade-in", "pop-in", "slide-in", "typewriter", "scale-up", "slide-up", "slide-down", "bounce", "dissolve-out", "stream-in", "smooth-type", "handwriting", "ink-reveal"].contains(legacy) else {
                throw NativeEditorRenderError.unsupportedLane("text effect")
            }
            let phases = try JSONDecoder().decode(TextAnimationPhases.self,
                from: JSONEncoder().encode(NativeEditorSession.textPhases(for: element)))
            let style = try authoredStyle(for: element, fontID: fontID, family: family)
            let explicitPhases = raw["animation_phases"]?.objectValue != nil
            if legacy == "handwriting" && !explicitPhases {
                var handwriting = try handwritingLayout.compile(id: element.id, text: displayText, start: item.start, end: item.end,
                    style: style, canvas: canvas, motion: Self.normalizedMotion(effect: legacy, raw: raw["motion"]),
                    lineSpacing: raw["line_spacing"]?.numberValue ?? 1.15)
                handwriting.behindSubject = behindSubject
                text.append(handwriting)
                continue
            }
            var layout = try AuthoredTextLayout.compile(id: element.id, text: displayText,
                start: item.start, end: item.end, style: style, fontURL: font, canvas: canvas,
                phases: explicitPhases ? phases : nil,
                legacyEffect: [.typewriter, .streamIn, .smoothType, .inkReveal].contains(PortableTextLayer.Effect(rawValue: legacy) ?? .none) ? PortableTextLayer.Effect(rawValue: legacy)! : .none,
                motion: explicitPhases ? nil : try Self.normalizedMotion(effect: legacy, raw: raw["motion"]))
            layout.behindSubject = behindSubject
            if explicitPhases || ["typewriter", "stream-in", "smooth-type", "ink-reveal"].contains(legacy) {
                text.append(layout)
            } else {
                let legacyEffect = PortableTextLayer.Effect(rawValue: legacy) ?? .none
                text.append(PortableTextLayer(id: layout.id, start: layout.start, end: layout.end,
                    anchorX: layout.anchorX, anchorY: layout.anchorY, rotationDegrees: layout.rotationDegrees,
                    runs: layout.runs, effect: legacyEffect,
                    motion: try Self.normalizedMotion(effect: legacy, raw: raw["motion"]),
                    dissolveSeed: legacy == "dissolve-out" ? UInt32(101 + (document.textElements.firstIndex(where: { $0.id == element.id }) ?? 0) * 37) : nil,
                    background: layout.background,
                    // `layout` was compiled as a plain effect; the real effect decides whether the arc survives.
                    bend: PortableTextLayer.bendSupported(effect: legacyEffect) ? layout.bend : nil,
                    behindSubject: behindSubject))
            }
        }
        if !document.captionCues.isEmpty && document.captionMeta["enabled"] != .bool(false) {
            let meta = document.captionMeta
            let talkingCaptionFrame = document.editFormat == "subtitled" || deviceCaptions
            let captionFont = ["Inter": "Inter-Bold", "Fraunces": "Fraunces-Bold", "Space Grotesk": "SpaceGrotesk-Bold"]
            let family = meta["font"]?.stringValue ?? "TikTok Sans"
            let font = try resolveFont(captionFont[family] ?? family)
            let fontID = try registerFont(font)
            let wordStyle = meta["style"]?.stringValue == "word"
            let appearance = meta["appearance"]?.objectValue ?? [:]
            let explicitHighlight = appearance["highlight_spoken_word"]?.boolValue
            let captionAlignment = AuthoredTextLayout.Alignment(rawValue: appearance["alignment"]?.stringValue ?? "center") ?? .center
            let captionX = appearance["position_x_frac"]?.numberValue ?? (captionAlignment == .left ? 80.0 / 1080 : captionAlignment == .right ? 1000.0 / 1080 : 0.5)
            let defaultCaptionStyle = AuthoredTextLayout.Style(fontAssetID: fontID, size: meta["size_px"]?.numberValue ?? 78,
                widthFraction: appearance["max_width_frac"]?.numberValue ?? 920.0 / 1080, xFraction: captionX, yFraction: appearance["position_y_frac"]?.numberValue ?? meta["y_frac"]?.numberValue ?? (talkingCaptionFrame ? 0.82 : 1740.0 / 1920),
                alignment: captionAlignment,
                color: try ink(meta["color"], fallback: "#FFFFFF"), stroke: try ink(appearance["stroke_color"], fallback: "#000000"), strokeWidth: meta["stroke_width"]?.numberValue ?? 4,
                shadows: meta["shadow_enabled"] == .bool(false) ? [] : [TextBlurLayer(color: try ink(appearance["shadow_color"], fallback: "#000000", alpha: appearance["shadow_opacity"]?.numberValue ?? 0.5), sigma: 0, dx: 1, dy: 1)],
                bottomAligned: appearance["position_y_frac"] == nil, fontVariations: fontInstances[family] ?? [:])
            // KRI-1007: a cue's own phases win over the all-captions default (`phone_captions.compile_caption_layers`).
            let defaultCaptionPhases = Self.captionPhases(meta["animation_phases"])
            var previousWordEnd = 0.0
            // KRI-202: two caption_cues rows can reach the client already
            // overlapping in time (the cloud's own de-overlap guard,
            // `phone_captions._prepare_cues`, runs on a compiled copy the
            // native editor's live document never passes through). Burning
            // both unclamped stacks two caption rows on the same frame.
            // Clamp each rendered cue's end to the next rendered cue's start,
            // mirroring that cloud guard, before compiling any layer below.
            let renderedCues = document.captionCues
                .filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
                .sorted { $0.startS == $1.startS ? $0.id < $1.id : $0.startS < $1.startS }
            var clampedCueEnd: [String: Double] = [:]
            for index in renderedCues.indices {
                clampedCueEnd[renderedCues[index].id] = index + 1 < renderedCues.count
                    ? min(renderedCues[index].endS, renderedCues[index + 1].startS)
                    : renderedCues[index].endS
            }
            for cue in document.captionCues where !cue.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                let own = appearance["line_overrides"]?.objectValue?[cue.id]?.objectValue ?? [:]
                var captionStyle = defaultCaptionStyle
                let cueFamily = cue.raw["font_family"]?.stringValue ?? family
                let font = try resolveFont(captionFont[cueFamily] ?? cueFamily)
                captionStyle.fontAssetID = try registerFont(font)
                captionStyle.fontVariations = fontInstances[cueFamily] ?? [:]
                if let size = cue.raw["size_px"]?.numberValue { captionStyle.size = size }
                if let color = cue.raw["text_color"] { captionStyle.color = try ink(color, fallback: "#FFFFFF") }
                if let width = cue.raw["stroke_width"]?.numberValue { captionStyle.strokeWidth = width }
                if let enabled = cue.raw["shadow_enabled"]?.boolValue {
                    captionStyle.shadows = enabled ? [TextBlurLayer(color: try ink(appearance["shadow_color"], fallback: "#000000", alpha: appearance["shadow_opacity"]?.numberValue ?? 0.5), sigma: 0, dx: 1, dy: 1)] : []
                }
                if let size = own["size_px"]?.numberValue { captionStyle.size = size }
                if let x = own["position_x_frac"]?.numberValue { captionStyle.xFraction = x }
                if let y = own["position_y_frac"]?.numberValue { captionStyle.yFraction = y; captionStyle.bottomAligned = false }
                guard let item = items.first(where: { $0.kind == .captionCue && $0.id == cue.id }),
                      item.start.isFinite, item.end.isFinite else {
                    throw RecipeError.invalidTimeline
                }
                if item.start >= total { continue }
                guard item.end > item.start else { throw RecipeError.invalidTimeline }
                let cueEnd = min(item.end, clampedCueEnd[cue.id] ?? item.end)
                if cueEnd <= item.start { continue }
                let phases = Self.captionPhases(cue.raw["animation_phases"]) ?? defaultCaptionPhases
                if wordStyle || explicitHighlight == true {
                    let tokens = cue.text.split(whereSeparator: \.isWhitespace).map(String.init)
                    let wordValues: [JSONValue]
                    if case .array(let values) = cue.raw["words"] { wordValues = values } else { wordValues = [] }
                    let stored = wordValues.compactMap { $0.objectValue }
                    let matches = stored.map { $0["text"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "" } == tokens
                    var windows: [(Double, Double)] = []
                    let scale = (cueEnd - item.start) / max(0.01, cue.endS - cue.startS)
                    var synthesizedEnd = 0.0
                    for index in tokens.indices {
                        if matches, let begin = stored[index]["start_s"]?.numberValue, let end = stored[index]["end_s"]?.numberValue {
                            windows.append((item.start + (begin - cue.startS) * scale, item.start + (end - cue.startS) * scale))
                        } else {
                            let next = max(Double(index + 1) * (cueEnd - item.start) / Double(tokens.count), synthesizedEnd + 0.05)
                            windows.append((item.start + synthesizedEnd, item.start + next))
                            synthesizedEnd = next
                        }
                    }
                    var starts: [Double] = []
                    for index in windows.indices {
                        let start = max(0, windows[index].0, previousWordEnd)
                        let next = index + 1 < windows.count ? windows[index + 1].0 : windows[index].1
                        previousWordEnd = max(start + 0.01, next)
                        starts.append(start)
                    }
                    guard let first = starts.first, first < total else { continue }
                    // `Display: Word` with an explicit highlight setting shows ONE WORD AT A TIME. It is one layer per
                    // cue (`KaraokeContent.singleWord`), not one per word, so a long talk stays far under the engine's
                    // text-layer cap and the preview builds quickly. A word too long to stay on one line falls back
                    // to the cue's full-line karaoke layer.
                    if wordStyle, let highlighted = explicitHighlight {
                        let layerEnd = min(total, min(cueEnd, previousWordEnd))
                        if layerEnd > first,
                           let singles = try AuthoredTextLayout.compileSingleWords(id: "caption-" + cue.id, text: cue.text,
                               start: first, end: layerEnd, starts: starts.map { $0 - first },
                               highlight: highlighted ? try ink(meta["highlight_color"], fallback: "#C5F82A") : captionStyle.color,
                               style: captionStyle, fontURL: font, canvas: canvas) {
                            // A word-highlight line keeps its sweep; the phases move the whole line.
                            text.append(contentsOf: singles.map { Self.layer($0, phases: phases) })
                            continue
                        }
                        if layerEnd <= first { continue }
                    }
                    text.append(Self.layer(try AuthoredTextLayout.compileHighlightedWords(id: "caption-" + cue.id, text: cue.text,
                        start: first, end: min(total, explicitHighlight == nil ? previousWordEnd : min(cueEnd, previousWordEnd)), starts: starts.map { $0 - first },
                        highlight: explicitHighlight == false ? captionStyle.color : try ink(meta["highlight_color"], fallback: "#C5F82A"),
                        style: captionStyle, fontURL: font, canvas: canvas), phases: phases))
                } else if let phases {
                    text.append(try AuthoredTextLayout.compile(id: "caption-" + cue.id, text: cue.text,
                        start: item.start, end: cueEnd, style: captionStyle, fontURL: font, canvas: canvas, phases: phases))
                } else {
                    let captionPhases = try appearance["animation_phases"].map {
                        try JSONDecoder().decode(TextAnimationPhases.self, from: JSONEncoder().encode($0))
                    }
                    let layout = try AuthoredTextLayout.compile(id: "caption-" + cue.id, text: cue.text,
                        start: item.start, end: cueEnd, style: captionStyle, fontURL: font, canvas: canvas, phases: captionPhases)
                    if captionPhases != nil { text.append(layout); continue }
                    text.append(PortableTextLayer(id: layout.id, start: layout.start, end: layout.end,
                        anchorX: layout.anchorX, anchorY: layout.anchorY, rotationDegrees: 0, runs: layout.runs,
                        effect: appearance["position_y_frac"] != nil ? .popIn : talkingCaptionFrame ? .captionPop : .none))
                }
            }
        }
        var visualFills: [VisualCanvasFill] = []
        var muteWindows: [AudioMuteWindow] = []
        let structured = document.visualBlocks.filter { $0.kind != "media" }
        let mediaBlocks = document.visualBlocks.filter { $0.kind == "media" }.sorted {
            let a = $0.raw["z"]?.numberValue ?? 0, b = $1.raw["z"]?.numberValue ?? 0
            return a == b ? ($0.startS == $1.startS ? $0.id < $1.id : $0.startS < $1.startS) : a < b
        }
        for (index, block) in (structured + mediaBlocks).enumerated() {
            let item = items.first(where: { $0.kind == .visualBlock && $0.id == block.id })
            let start = item?.start ?? block.startS, end = min(total, item?.end ?? block.endS)
            guard end > start else { throw RecipeError.invalidTimeline }
            let policy = block.raw["audio_policy"]?.objectValue ?? [:]
            let sfxIDs = Set(document.soundEffects.map { "sfx:" + $0.id })
            var muted: [String] = []
            if policy["base"]?.stringValue == "mute" { muted += video.map(\.id) + audioTracks.flatMap(\.clips).filter { !sfxIDs.contains($0.id) }.map(\.id) }
            if policy["sfx"]?.stringValue == "mute" { muted += audioTracks.flatMap(\.clips).filter { sfxIDs.contains($0.id) }.map(\.id) }
            if !muted.isEmpty { muteWindows.append(AudioMuteWindow(start: start, end: end, clipIDs: muted)) }
            let order = index + 1
            let fadeIn = block.raw["transition_in"]?.stringValue == "fade"
            let fadeOut = block.raw["transition_out"]?.stringValue == "fade"
            func addShot(_ shot: [String: JSONValue], isMedia: Bool = false) throws {
                guard let shotID = shot["id"]?.stringValue,
                      let source = mediaSources["visual:" + block.id + ":" + shotID],
                      let fingerprint = source.asset.fingerprint else { throw MediaEngineError.missingAsset("visual:" + block.id) }
                let alias = "visual-" + block.id + "-" + shotID
                assets[alias] = MediaAsset(id: alias, relativePath: alias, fingerprint: fingerprint)
                references[alias] = RenderAssetReference(id: alias, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
                urls[alias] = source.url
                let image = shot[isMedia ? "media_kind" : "kind"]?.stringValue == "image"
                let shotStart = start + (isMedia ? 0 : shot["start_offset_s"]?.numberValue ?? 0)
                let timelineLength = min(end - shotStart, isMedia ? end - start : shot["duration_s"]?.numberValue ?? end - start)
                let sourceStart = image ? 0 : shot["trim_start_s"]?.numberValue ?? 0
                let playbackRate = shot["playback_rate"]?.numberValue ?? 1
                guard playbackRate.isFinite, (0.25...4).contains(playbackRate), timelineLength > 0 else { throw RecipeError.invalidTimeline }
                let length = image ? timelineLength : min(timelineLength * playbackRate, max(0, (source.asset.duration ?? 0) - sourceStart))
                guard image || length > 0 else { throw RecipeError.invalidTimeline }
                let crop = shot[isMedia ? "transform" : "crop"]?.objectValue ?? [:]
                let motion = VisualMediaPlacement.Motion(rawValue: shot["motion"]?.stringValue ?? "none")
                guard let motion else { throw NativeEditorRenderError.unsupportedLane("visual motion") }
                let placement = VisualMediaPlacement(order: order,
                    contain: isMedia && (crop["fit_mode"]?.stringValue ?? "contain") == "contain",
                    focalX: crop[isMedia ? "focal_x" : "x_frac"]?.numberValue ?? 0.5,
                    focalY: crop[isMedia ? "focal_y" : "y_frac"]?.numberValue ?? 0.5,
                    zoom: crop[isMedia ? "zoom" : "scale"]?.numberValue ?? 1,
                    preCrop: !isMedia && (image || motion != .none), motion: motion,
                    widthFraction: isMedia && shot["display_mode"]?.stringValue == "overlay" ? shot["scale"]?.numberValue ?? 0.35 : nil,
                    xFraction: shot["x_frac"]?.numberValue ?? 0.5, yFraction: shot["y_frac"]?.numberValue ?? 0.5,
                    windowStart: start, windowEnd: end, fadeIn: fadeIn, fadeOut: fadeOut, editorStyle: try Self.visualEditorStyle(block.raw["editor_style"]))
                overlays.append(TimelineClip(id: alias, sourceAssetID: alias, sourceStart: sourceStart, sourceDuration: length,
                    timelineStart: shotStart, rate: image ? 1 : playbackRate, volume: 0,
                    holdDuration: image ? nil : max(0, timelineLength - length / playbackRate), visualPlacement: placement,
                    sourceCrop: try Self.sourceCrop(shot["source_crop"])))
            }
            switch block.kind {
            case "media": try addShot(block.raw.merging(["id": .string(block.id)]) { _, new in new }, isMedia: true)
            case "montage":
                guard case .array(let shots) = block.raw["shots"] else { throw RecipeError.invalidTimeline }
                for shot in shots { guard let fields = shot.objectValue else { throw RecipeError.invalidTimeline }; try addShot(fields) }
            case "text_card":
                guard let background = block.raw["background"]?.objectValue else { throw RecipeError.invalidTimeline }
                if background["type"]?.stringValue == "asset" {
                    guard let shot = background["shot"]?.objectValue else { throw RecipeError.invalidTimeline }
                    try addShot(shot)
                } else {
                    guard let kind = VisualCanvasFill.Kind(rawValue: background["type"]?.stringValue ?? "") else { throw RecipeError.invalidTimeline }
                    visualFills.append(VisualCanvasFill(id: "fill-" + block.id, start: start, end: end, order: order, kind: kind,
                        color: try ink(background[kind == .gradient ? "from" : "color"], fallback: "#111111"),
                        endColor: kind == .gradient ? try ink(background["to"], fallback: "#111111") : nil,
                        angle: background["angle_deg"]?.numberValue ?? 180, blurRadius: background["blur_px"]?.numberValue ?? 24,
                        fadeIn: fadeIn, fadeOut: fadeOut))
                }
            default: throw NativeEditorRenderError.unsupportedLane("visual block")
            }
        }
        let cameraPulses = try document.cameraEffects.map { effect -> CameraPulse in
            guard [nil, "semantic_crop_pulse"].contains(effect.raw["token"]?.stringValue),
                  [nil, "sine_pulse"].contains(effect.raw["easing"]?.stringValue),
                  let item = items.first(where: { $0.kind == .cameraEffect && $0.id == effect.id }) else {
                throw NativeEditorRenderError.unsupportedLane("camera effect")
            }
            return CameraPulse(id: effect.id, start: item.start, end: item.end, intensity: effect.raw["intensity"]?.numberValue ?? 0.04)
        }
        var motionProgram: MotionSceneProgram?
        if !document.motionScenes.isEmpty {
            guard let hash = document.motionRuntimeHash else { throw NativeEditorRenderError.unsupportedLane("motion runtime") }
            var imageAssets: [String: String] = [:]
            let instances: [[String: JSONValue]] = try document.motionScenes.map { scene in
                guard let item = items.first(where: { $0.kind == .motionScene && $0.id == scene.id }) else { throw RecipeError.invalidTimeline }
                var raw = scene.raw
                raw["start_frame"] = .number((item.start * 30).rounded())
                raw["end_frame_exclusive"] = .number((item.end * 30).rounded())
                if case .object(var params) = raw["params"], case .array(let refs) = params["assets"] {
                    params["assets"] = .array(try refs.map { ref in
                        guard case .object(let fields) = ref, let assetID = fields["asset_id"]?.stringValue,
                              let source = mediaSources["motion:" + assetID], let fingerprint = source.asset.fingerprint else {
                            throw NativeEditorRenderError.unsupportedLane("motion image")
                        }
                        let id = "motion-" + assetID
                        assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
                        references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
                        urls[id] = source.url; imageAssets[assetID] = id
                        return .object(["asset_id": .string(assetID)])
                    })
                    raw["params"] = .object(params)
                }
                return raw
            }
            motionProgram = MotionSceneProgram(instances: try JSONDecoder().decode([MotionSceneValue].self, from: JSONEncoder().encode(instances)),
                runtimeHash: hash, fontAssetID: try registerFont(resolveFont("Inter")), imageAssetIDs: imageAssets)
        }
        // The excerpt is the speech in its window: silence the visuals' own
        // sound there so the two never double up.
        for window in clipAudioWindows {
            let under = video.filter { $0.volume > 0 && $0.timelineStart < window.end && $0.timelineStart + $0.duration > window.start }.map(\.id)
            if !under.isEmpty, muteWindows.count < 100 { muteWindows.append(AudioMuteWindow(start: window.start, end: window.end, clipIDs: under)) }
        }
        // KRI-1118: the Talk-to-camera background is one engine layer plus a pinned pool-visual asset.
        var speakerBackground: SpeakerBackgroundLayer?
        if let background = document.speakerBackground {
            let layer = background.layer
            guard let source = mediaSources[EditorSpeakerBackground.sourceKey(forMediaID: background.mediaID)],
                  let fingerprint = source.asset.fingerprint else { throw MediaEngineError.missingAsset(layer.assetID) }
            assets[layer.assetID] = MediaAsset(id: layer.assetID, relativePath: layer.assetID, fingerprint: fingerprint)
            references[layer.assetID] = RenderAssetReference(id: layer.assetID, fingerprint: try RenderFingerprint(fingerprint),
                source: .visual(visualID: background.mediaID, generation: source.visualGeneration ?? "0", mediaKind: background.kind))
            urls[layer.assetID] = source.url
            speakerBackground = layer
        }
        // Past the cap `recipe.validate()` would throw a bare `invalidTimeline`, which reads as a transient failure.
        guard text.count <= Self.maxTextLayers else { throw NativeEditorRenderError.tooManyTextLayers }
        var recipe = KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas,
            assets: assets.values.sorted { $0.id < $1.id }, tracks: [TimelineTrack(id: "video", kind: .video, clips: video)] + (overlays.isEmpty ? [] : [TimelineTrack(id: "overlays", kind: .overlay, clips: overlays)]) + audioTracks,
            audio: AudioMixRecipe(originalVolume: originalGain, muteWindows: muteWindows),
            assetManifest: RenderAssetManifest(assets: references.values.sorted { $0.id < $1.id }), textLayers: text, cameraPulses: cameraPulses, motionScenes: motionProgram, visualFills: visualFills, speakerBackground: speakerBackground)
        // KRI-589: drawings follow the timeline the cuts left them (the projected window), then take the
        // same clamp the server's `compile_drawing_layers` applies; both are pinned by compile_v1.json.
        recipe.drawingLayers = NativeDrawingCompile.layers(document.drawings.map { drawing in
            var placed = drawing
            if let item = items.first(where: { $0.kind == .drawing && $0.id == drawing.id }) { placed.startS = item.start; placed.endS = item.end }
            return placed
        }, timelineDuration: TimelineMath.totalDuration(of: recipe))
        try recipe.validate()
        return NativeEditorRenderProgram(recipe: recipe, assetURLs: urls)
    }

    static func visualEditorStyle(_ raw: JSONValue?) throws -> VisualEditorStyle? {
        guard let raw, raw != .null else { return nil }
        let style = try JSONDecoder().decode(VisualEditorStyle.self, from: JSONEncoder().encode(raw))
        try style.validate()
        return style
    }

    /// Mirrors the backend's `phone_recipe_shared.fit_transform`. The engine
    /// cover-fills each main-track clip and then scales about the canvas centre
    /// over black, so a sideways clip needs `contain / cover` to end up whole
    /// with bars (0.31640625 for 1920x1080 into 1080x1920). Portrait, square,
    /// unknown-size and "fill" are the identity, which keeps those previews
    /// byte-identical. `display` is the oriented (post-rotation) size.
    static func fitTransform(display: MediaSize?, canvas: KriaMediaEngine.Canvas, landscapeFit: String) -> MediaTransform {
        guard landscapeFit == "fit", canvas.height > canvas.width,
              let display, display.width > display.height, display.height > 0 else { return .identity }
        let canvasWidth = Double(canvas.width), canvasHeight = Double(canvas.height)
        let contain = min(canvasWidth / display.width, canvasHeight / display.height)
        let cover = max(canvasWidth / display.width, canvasHeight / display.height)
        return MediaTransform(scale: contain / cover)
    }

    static func clipTilt(_ raw: JSONValue?) throws -> ClipTilt? {
        do { return try ClipTilt(row: raw) } catch { throw RecipeError.invalidTimeline }
    }

    static func sourceCrop(_ raw: JSONValue?) throws -> NormalizedSourceRect? {
        guard let raw, raw != .null else { return nil }
        guard let object = raw.objectValue,
              let x = object["x"]?.numberValue, let y = object["y"]?.numberValue,
              let width = object["width"]?.numberValue, let height = object["height"]?.numberValue else {
            throw RecipeError.invalidTimeline
        }
        let crop = NormalizedSourceRect(x: x, y: y, width: width, height: height)
        try crop.validate()
        return crop
    }

    /// Matches text_motion_v2.normalize_text_motion; absent v2 motion retains legacy timing.
    static func normalizedMotion(effect: String, raw: JSONValue?) throws -> TextMotionParameters? {
        guard let values = raw?.objectValue, values["version"]?.numberValue == 2 else { return nil }
        let smooth = effect == "smooth-type"
        var normalized: [String: JSONValue] = [:]
        func number(_ source: String, _ target: String, _ fallback: Double, _ low: Double, _ high: Double) {
            let value = values[source]?.numberValue ?? fallback
            normalized[target] = .number(min(high, max(low, value.isFinite ? value : fallback)))
        }
        func choice(_ source: String, _ target: String, _ fallback: String, _ allowed: [String]) {
            let value = values[source]?.stringValue ?? fallback
            normalized[target] = .string(allowed.contains(value) ? value : fallback)
        }
        number("speed", "speed", 1, 0.25, 4)
        number("intensity", "intensity", smooth ? 0.7 : 1, 0, 1)
        number("stagger_ms", "staggerMs", smooth ? 45 : 0, 0, 250)
        number("travel_px", "travelPx", smooth ? 18 : (["slide-up", "slide-down"].contains(effect) ? 220 : 0), 0, 600)
        number("overshoot", "overshoot", ["pop-in", "bounce"].contains(effect) ? 0.15 : 0, 0, 1)
        number("blur_px", "blurPx", smooth ? 4 : 0, 0, 12)
        number("cursor_blink_ms", "cursorBlinkMs", 500, 100, 2000)
        number("hold_s", "holdS", 1, 0, 3600)
        number("exit_s", "exitS", effect == "dissolve-out" ? 1 : 0, 0, 2)
        number("reveal_ramp_ms", "revealRampMs", 120, 40, 400)
        choice("easing", "easing", "ease-out-cubic", ["linear", "ease-out-cubic", "ease-in-out-cubic"])
        choice("order", "order", "forward", ["forward", "reverse", "center-out"])
        choice("direction", "direction", effect == "slide-down" ? "down" : (smooth || effect == "slide-up" ? "up" : "none"), ["none", "up", "down", "left", "right"])
        choice("cursor_style", "cursorStyle", effect == "stream-in" ? "bar" : "none", ["none", "bar", "block", "underscore"])
        return try JSONDecoder().decode(TextMotionParameters.self, from: JSONEncoder().encode(normalized))
    }

    private static func displayText(_ element: EditorTextElement) -> String {
        KriaTextCase.apply(element.text, element.raw["text_case"]?.stringValue)
    }

    static func projectSentenceCaptions(
        _ elements: [EditorTextElement], captionMeta: [String: JSONValue]
    ) -> [EditorTextElement] {
        guard captionMeta["style"] == .string("sentence") else { return elements }
        var projected = elements
        for sentence in sentenceGroups(elements) {
            let display = sentence.map { projected[$0].text.trimmingCharacters(in: .whitespacesAndNewlines) }
                .joined(separator: " ")
            for (position, index) in sentence.enumerated() {
                projected[index].text = display
                // Python's render-only projection holds each sentence display
                // to the next word's start. This eliminates a micro-gap while
                // preserving the final word's real spoken end.
                if sentence.indices.contains(position + 1) {
                    projected[index].endS = projected[sentence[position + 1]].startS
                }
            }
        }
        return projected
    }

    /// The visible caption rows of each spoken sentence, as indices into `elements`, in time order.
    private static func sentenceGroups(_ elements: [EditorTextElement]) -> [[Int]] {
        let captionIndices = elements.indices.filter { elements[$0].isCaption }.sorted {
            if elements[$0].startS != elements[$1].startS {
                return elements[$0].startS < elements[$1].startS
            }
            return elements[$0].id < elements[$1].id
        }
        var groups: [[Int]] = []
        var sentence: [Int] = []
        func emitSentence() {
            guard !sentence.isEmpty else { return }
            groups.append(sentence)
            sentence.removeAll()
        }
        for index in captionIndices {
            let spoken = elements[index].text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !spoken.isEmpty else { continue }
            let punctuated = spoken.range(of: #"[.!?]+["'’”\)\]]*$"#, options: .regularExpression) != nil
            // KRI-569: song lyrics carry no terminal punctuation. The server marks the
            // last word of each lyric line with `source_params.line_end`; narration rows
            // never carry it, so only lyric lines are split by it.
            let terminal = punctuated || isLineEnd(elements[index])
            guard elements[index].raw["enabled"] != .bool(false), elements[index].raw["removed"] != .bool(true) else {
                // A hidden word must not leak into the display. Its terminal
                // punctuation still separates the adjacent visible sentences.
                if terminal { emitSentence() }
                continue
            }
            sentence.append(index)
            // The terminal mark has to end the token, so "172.5" is not a
            // sentence boundary while `Done.”` is.
            if terminal {
                emitSentence()
            }
        }
        emitSentence()
        return groups
    }

    private static func isLineEnd(_ element: EditorTextElement) -> Bool {
        element.raw["source_params"]?.objectValue?["line_end"] == .bool(true)
    }

    /// KRI-1007: the displayed lines of text-lane (guided, song, montage) captions, as indices into
    /// `elements`. Sentence display splits as `projectSentenceCaptions` draws it; any other display uses the
    /// server's `guided_caption_presentation._legacy_groups`.
    static func captionLineGroups(_ elements: [EditorTextElement], captionMeta: [String: JSONValue]) -> [[Int]] {
        if captionMeta["style"] == .string("sentence") { return sentenceGroups(elements) }
        let rows = elements.indices.filter {
            elements[$0].isCaption && elements[$0].raw["enabled"] != .bool(false) && elements[$0].raw["removed"] != .bool(true)
                && !elements[$0].text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }.sorted { (elements[$0].startS, elements[$0].id) < (elements[$1].startS, elements[$1].id) }
        var groups: [[Int]] = []
        for index in rows {
            let text = elements[index].text.trimmingCharacters(in: .whitespacesAndNewlines)
            if let group = groups.last, let last = group.last {
                let combined = (group.map { elements[$0].text.trimmingCharacters(in: .whitespacesAndNewlines) } + [text]).joined(separator: " ")
                let previous = elements[last].text.replacingOccurrences(of: #"\s+$"#, with: "", options: .regularExpression)
                let splits = elements[index].startS - elements[last].endS > 0.35
                    || [".", "!", "?"].contains(where: previous.hasSuffix) || isLineEnd(elements[last])
                    || combined.split(whereSeparator: \.isWhitespace).count > 6 || combined.count > 42
                if !splits { groups[groups.count - 1].append(index); continue }
            }
            groups.append([index])
        }
        return groups
    }

    /// KRI-1007, the server's `_group_phases` + `_stamp_line_phases`: a line's first row with its own
    /// phases governs the whole displayed line, else `caption_meta.animation_phases`. The line's first row
    /// enters, its last exits, every row loops; under Display Word every row is a word of its own and plays all three. Phases that animate nothing, or that do not parse, leave the
    /// line as it is drawn today. `groups` index both arrays, which share their order.
    /// Without either, the older `appearance.animation_phases` (Style > Text motion) draws the line as one
    /// row, unless a word display or spoken-word highlight is on (`guided_caption_presentation`).
    private static func stampingCaptionLinePhases(_ rendered: [EditorTextElement], source: [EditorTextElement],
                                                  captionMeta: [String: JSONValue]) -> [EditorTextElement] {
        var stamped = rendered
        for index in stamped.indices where stamped[index].isCaption {
            stamped[index].raw.removeValue(forKey: "animation_phases")
        }
        let appearance = captionMeta["appearance"]?.objectValue ?? [:]
        let appearancePhases = captionMeta["style"] != .string("word") && appearance["highlight_spoken_word"] != .bool(true)
            ? appearance["animation_phases"].flatMap { captionPhases($0) == nil ? nil : $0 } : nil
        var hidden = Set<Int>()
        let wordDisplay = captionMeta["style"] == .string("word")
        for group in captionLineGroups(source, captionMeta: captionMeta) {
            guard let phases = captionLinePhases(group.map { source[$0] }, captionMeta: captionMeta) else {
                guard let appearancePhases, let first = group.first, let last = group.last else { continue }
                stamped[first].text = group.map { source[$0].text.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .joined(separator: " ")
                stamped[first].endS = source[last].endS
                stamped[first].raw["animation_phases"] = appearancePhases
                hidden.formUnion(group.dropFirst())
                continue
            }
            for (position, index) in group.enumerated() {
                var row = phases
                if position > 0 && !wordDisplay { row.entrance = .none }
                if position < group.count - 1 && !wordDisplay { row.exit = .none }
                if row.entrance == .none, row.exit == .none, row.loop == .none { continue }
                stamped[index].raw["animation_phases"] = .object([
                    "entrance": .string(row.entrance.rawValue), "exit": .string(row.exit.rawValue),
                    "loop": .string(row.loop.rawValue), "speed": .number(row.speed),
                ])
            }
        }
        return stamped.enumerated().filter { !hidden.contains($0.offset) }.map(\.element)
    }

    /// The phases one displayed text-lane caption line draws with, or nil for today's static line.
    static func captionLinePhases(_ line: [EditorTextElement], captionMeta: [String: JSONValue]) -> TextAnimationPhases? {
        let own = line.lazy.compactMap { row -> JSONValue? in
            guard let value = row.raw["animation_phases"], value != .null else { return nil }
            return value
        }.first
        guard let phases = captionPhases(own ?? captionMeta["animation_phases"]),
              phases.entrance != .none || phases.exit != .none || phases.loop != .none else { return nil }
        return phases
    }

    /// A valid `animation_phases` object, else nil: the server's `phone_captions._phases_or_none`.
    static func captionPhases(_ value: JSONValue?) -> TextAnimationPhases? {
        guard let object = value?.objectValue, let data = try? JSONEncoder().encode(object) else { return nil }
        return try? JSONDecoder().decode(TextAnimationPhases.self, from: data)
    }

    private static func layer(_ layer: PortableTextLayer, phases: TextAnimationPhases?) -> PortableTextLayer {
        var layer = layer
        if let phases { layer.animationPhases = phases }
        return layer
    }

    /// KRI-110: overlay caption_meta styling onto a caption_cue-tagged text
    /// element's raw fields, so the generic per-element text pass (which
    /// reads exactly these keys — `color`, `stroke_color`, `size_px`, etc.)
    /// renders it styled without a separate caption code path. Mirrors the
    /// backend's `_apply_guided_caption_meta`, field for field. Per-word
    /// highlight/karaoke has no generic-path equivalent and is intentionally
    /// not covered — the caption keeps its base style in that mode.
    static func applyingCaptionMeta(_ meta: [String: JSONValue], to element: EditorTextElement) -> EditorTextElement {
        var patched = element
        let appearance = meta["appearance"]?.objectValue ?? [:]
        let own = appearance["line_overrides"]?.objectValue?[element.id]?.objectValue ?? [:]
        let movesX = (own["position_x_frac"] ?? appearance["position_x_frac"])?.numberValue != nil
        let movesY = (own["position_y_frac"] ?? appearance["position_y_frac"] ?? meta["y_frac"])?.numberValue != nil
        if movesX || movesY {
            patched.raw["position"] = .string("custom")
            patched.raw["x_frac"] = .number(element.anchor.x)
            patched.raw["y_frac"] = .number(element.anchor.y)
        }
        if case .string(let alignment)? = appearance["alignment"], ["left", "center", "right"].contains(alignment) {
            patched.raw["alignment"] = .string(alignment)
        }
        if let value = appearance["stroke_color"] { patched.raw["stroke_color"] = value }
        if let value = appearance["shadow_color"] { patched.raw["shadow_color"] = value }
        if let value = appearance["shadow_opacity"] { patched.raw["shadow_opacity"] = value }
        if let value = meta["y_frac"], value.numberValue != nil {
            patched.raw["position"] = .string("custom")
            patched.raw["y_frac"] = value
        }
        if meta["font_set"] == .bool(true), let font = meta["font"] { patched.raw["font_family"] = font }
        if let value = meta["size_px"] { patched.raw["size_px"] = value }
        if let value = own["size_px"] { patched.raw["size_px"] = value }
        for (source, target) in [("position_x_frac", "x_frac"), ("position_y_frac", "y_frac")] {
            if let value = own[source] ?? appearance[source], value.numberValue != nil {
                patched.raw["position"] = .string("custom")
                patched.raw[target] = value
            }
        }
        if let value = appearance["max_width_frac"] { patched.raw["max_width_frac"] = value }
        if let value = meta["color"] { patched.raw["color"] = value }
        if let value = meta["stroke_width"] { patched.raw["stroke_width"] = value }
        if let value = meta["shadow_enabled"] { patched.raw["shadow_enabled"] = value }
        // The Display choice projects onto the element's entrance exactly as
        // the backend does: "word" pops each cue in, "sentence" holds it
        // static. Per-word highlighting itself is still not mirrored here.
        switch meta["style"] {
        case .string("word"): patched.raw["effect"] = .string("pop-in")
        case .string("sentence"): patched.raw["effect"] = .string("static")
        default: break
        }
        return patched
    }

    /// The layout style of one authored text row. The render loop and the block measurement both read it from
    /// here, so what the editor measures is what the recipe draws.
    private func authoredStyle(for element: EditorTextElement, fontID: String, family: String) throws -> AuthoredTextLayout.Style {
        let raw = element.raw
        var shadows: [TextBlurLayer] = []
        if raw["shadow_enabled"] != .bool(false) {
            if raw["shadow_style"]?.stringValue == "high_visibility" && ["stroke_color", "shadow_color", "shadow_opacity"].allSatisfy({ raw[$0] == nil || raw[$0] == .null }) {
                shadows = [TextBlurLayer(color: try ink(nil, fallback: "#000000", alpha: 115.0 / 255), sigma: 14, dx: 0, dy: 8),
                           TextBlurLayer(color: try ink(nil, fallback: "#000000", alpha: 200.0 / 255), sigma: 3, dx: 0, dy: 2)]
            } else {
                shadows.append(TextBlurLayer(color: try ink(raw["shadow_color"], fallback: "#000000",
                    alpha: ((raw["shadow_opacity"]?.numberValue ?? (160.0 / 255)) * 255).rounded(.toNearestOrEven) / 255), sigma: 12, dx: 0, dy: 6))
            }
        }
        if let strength = raw["glow_strength"]?.numberValue, strength > 0, let glow = raw["glow_color"]?.stringValue {
            for (sigma, alpha) in [(8.0, 120.0), (20.0, 220.0)] {
                shadows.append(TextBlurLayer(color: try ink(.string(glow), fallback: "#FFFFFF",
                    alpha: (alpha * min(1, strength)).rounded(.toNearestOrEven) / 255), sigma: sigma, dx: 0, dy: 0))
            }
        }
        let anchor = element.anchor
        return AuthoredTextLayout.Style(fontAssetID: fontID, size: NativeEditorSession.textSize(for: element),
            widthFraction: raw["max_width_frac"]?.numberValue ?? 0.9,
            xFraction: anchor.x, yFraction: anchor.y,
            rotation: raw["rotation_deg"]?.numberValue ?? 0,
            alignment: AuthoredTextLayout.Alignment(rawValue: raw["alignment"]?.stringValue ?? "center") ?? .center,
            color: try ink(raw["color"], fallback: "#FFFFFF"),
            stroke: try ink(raw["stroke_color"], fallback: "#000000"),
            strokeWidth: raw["stroke_width"]?.numberValue ?? 0, shadows: shadows,
            background: try raw["background_color"]?.stringValue.map { try ink(.string($0), fallback: "#FFFFFF") },
            fontVariations: fontInstances[family] ?? [:], letterSpacing: raw["letter_spacing"]?.numberValue ?? 0, lineSpacing: raw["line_spacing"]?.numberValue ?? 1.15, wrapsLines: raw["wrap_lines"] != .bool(false),
            bend: raw["bend"]?.numberValue ?? 0)
    }

    static func canvas(for document: EditorDocument) -> KriaMediaEngine.Canvas {
        switch EditorDocument.canonicalOrientation(document.orientation) {
        case "landscape": .init(width: 1920, height: 1080)
        case "square": .init(width: 1080, height: 1080)
        default: .init(width: 1080, height: 1920)
        }
    }

    private static func glyphSafeFamilyTexts(_ elements: [EditorTextElement], captionMeta: [String: JSONValue]) -> [String: String] {
        var familyTexts: [String: [String]] = [:]
        for rawElement in elements where rawElement.raw["enabled"] != .bool(false) && rawElement.raw["removed"] != .bool(true) {
            let element = rawElement.isCaption ? Self.applyingCaptionMeta(captionMeta, to: rawElement) : rawElement
            familyTexts[element.fontFamily, default: []].append(Self.displayText(element))
        }
        return familyTexts.mapValues { $0.joined(separator: " ") }
    }

    /// Where the block of `element` is drawn, in canvas fractions, from the same face, style and `compile` call the
    /// recipe uses. Nil when the row draws nothing or its face cannot be laid out.
    func textBlockRect(for rawElement: EditorTextElement, in document: EditorDocument) -> CGRect? {
        let element = rawElement.isCaption ? Self.applyingCaptionMeta(document.captionMeta, to: rawElement) : rawElement
        let text = Self.displayText(element)
        guard element.raw["enabled"] != .bool(false), element.raw["removed"] != .bool(true),
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        let canvas = Self.canvas(for: document)
        let rendered = Self.projectSentenceCaptions(document.textElements, captionMeta: document.captionMeta)
        let glyphSafe = Self.glyphSafeFamilyTexts(rendered, captionMeta: document.captionMeta)
        do {
            let family = glyphFallback.family(element.fontFamily, for: glyphSafe[element.fontFamily] ?? text)
            let font = try resolveFont(family)
            let style = try authoredStyle(for: element, fontID: "measure-" + font.lastPathComponent, family: family)
            let layer = try AuthoredTextLayout.compile(id: element.id, text: text, start: 0, end: 1,
                style: style, fontURL: font, canvas: canvas)
            guard let bounds = AuthoredTextLayout.blockBounds(of: layer, fontURL: font) else { return nil }
            return CGRect(x: bounds.minX / Double(canvas.width), y: bounds.minY / Double(canvas.height),
                          width: bounds.width / Double(canvas.width), height: bounds.height / Double(canvas.height))
        } catch { return nil }
    }

    private func resolveFont(_ family: String) throws -> URL {
        for name in [fontAliases[family], family + ".ttf", family + "-Regular.ttf", family + ".otf"].compactMap({ $0 }) {
            if let url = fontURLs[name] { return url }
        }
        throw NativeEditorRenderError.missingFont(family)
    }

    private func ink(_ value: JSONValue?, fallback: String, alpha: Double = 1) throws -> TextInk {
        let hex = value?.stringValue ?? fallback
        guard hex.count == 7, hex.first == "#", let color = UInt32(hex.dropFirst(), radix: 16) else {
            throw RecipeError.invalidTimeline
        }
        return TextInk(red: Double((color >> 16) & 255) / 255,
                       green: Double((color >> 8) & 255) / 255, blue: Double(color & 255) / 255, alpha: alpha)
    }
}


/// text_case transform. EXACT mirror of `apply_text_case` (api app/agents/_schemas/text_element.py,
/// casing in app/text_fold.py) and `applyTextCase` (web src/lib/overlay-layout.ts); shared fixture
/// tests/fixtures/text-element-parity/text_case.json. KRI-781: Turkish i rules ("istanbul" -> "İSTANBUL",
/// "IŞIK" -> "ışık") apply to the whole text when the language is Turkish, otherwise only to words
/// with a Turkish-only letter (ı İ ğ Ğ ş Ş); every other word is cased exactly as before.
enum KriaTextCase {
    private static let turkishOnlyLetters: Set<Character> = ["ı", "İ", "ğ", "Ğ", "ş", "Ş"]
    private static let nonSpaceRun = try! NSRegularExpression(pattern: #"\S+"#)

    private static func isTurkishLanguage(_ language: String?) -> Bool {
        let code = (language ?? "").lowercased().replacingOccurrences(of: "_", with: "-")
        let base = code.split(separator: "-").first.map(String.init) ?? ""
        return base == "tr" || base == "az"
    }

    private static func hasTurkishLetter(_ text: String) -> Bool {
        text.contains { turkishOnlyLetters.contains($0) }
    }

    static func usesTurkishCase(_ text: String, language: String? = nil) -> Bool {
        isTurkishLanguage(language) || hasTurkishLetter(text)
    }

    private static func trUpper(_ text: String) -> String {
        text.precomposedStringWithCanonicalMapping
            .replacingOccurrences(of: "i", with: "\u{130}")
            .replacingOccurrences(of: "ı", with: "I")
            .uppercased()
    }

    private static func trLower(_ text: String) -> String {
        text.precomposedStringWithCanonicalMapping
            .replacingOccurrences(of: "\u{130}", with: "i")
            .replacingOccurrences(of: "I", with: "ı")
            .lowercased()
    }

    /// Each whitespace-delimited run replaced by `transform(word)`.
    private static func mapWords(_ text: String, _ transform: (String) -> String) -> String {
        var output = text
        for match in nonSpaceRun.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let range = Range(match.range, in: output) else { continue }
            output.replaceSubrange(range, with: transform(String(output[range])))
        }
        return output
    }

    /// Turkish rules for the whole text when the language is Turkish; otherwise only for words
    /// with a Turkish-only letter, so English naming "Beşiktaş" keeps English i's.
    private static func perWord(_ text: String, _ language: String?,
                                turkish: (String) -> String, english: (String) -> String) -> String {
        if isTurkishLanguage(language) { return turkish(text) }
        guard hasTurkishLetter(text) else { return english(text) }
        return mapWords(text) { hasTurkishLetter($0) ? turkish($0) : english($0) }
    }

    static func upper(_ text: String, language: String? = nil) -> String {
        perWord(text, language, turkish: trUpper, english: { $0.uppercased() })
    }

    static func lower(_ text: String, language: String? = nil) -> String {
        perWord(text, language, turkish: trLower, english: { $0.lowercased() })
    }

    /// First character of each whitespace-delimited run upper, the rest lower (never after apostrophes).
    static func title(_ text: String, language: String? = nil) -> String {
        mapWords(text) { word in
            let lang: String? = usesTurkishCase(word, language: language) ? "tr" : nil
            return upper(String(word.prefix(1)), language: lang) + lower(String(word.dropFirst()), language: lang)
        }
    }

    static func apply(_ text: String, _ textCase: String?, language: String? = nil) -> String {
        switch textCase {
        case "upper": upper(text, language: language)
        case "lower": lower(text, language: language)
        case "title": title(text, language: language)
        default: text
        }
    }
}

/// KRI-813: which playback rates the editor preview accepts for a video slot.
///
/// The creator-facing range is 0.25...4 (retime control, explicit `playback_rate`, cards, shots). A phone
/// narrated edit is different: the server's `phone_narrated_plan._fit_step_window` slows short footage to
/// fill each voiceover step with no rate floor (the C7 museum sculpture plays at ~0.165x), and the export
/// engine plays exactly that. The preview must match it exactly -- clamping would freeze-hold the picture
/// and drift it from the narration -- so only narrated-derived rates may go below 0.25x, down to the
/// server's own minimum of `minimumSourceSpanS` source seconds over the step.
enum NarratedRateGate {
    static let creatorRange: ClosedRange<Double> = 0.25...4
    /// Mirrors the server's `_MIN_USABLE_S` / `_EOF_GUARD_S` (phone_narrated_plan.py).
    static let minimumSourceSpanS = 0.05

    /// Only the two narrated hydrations (`NativeNarratedSourceTiming.hydrate`, `NativePhoneNarratedSource.hydrate`)
    /// write `native_source_span_s`, and both run only for `resolved_archetype == "narrated"`.
    /// `native_composite_source` alone is NOT narrated: talking and composite-base seeds set it too.
    static func narratedSpan(_ slot: EditorTimelineSlot?) -> Double? {
        slot?.raw["native_source_span_s"]?.numberValue
    }

    /// An explicit rate keeps the creator range, unless it is a device-recipe narrated seed's own derived
    /// rate (`span / duration`), never a creator-authored value.
    static func acceptsExplicit(_ rate: Double, slot: EditorTimelineSlot?) -> Bool {
        guard rate.isFinite, rate > 0 else { return false }
        if creatorRange.contains(rate) { return true }
        guard rate < creatorRange.lowerBound, slot?.raw["native_composite_source"] == .bool(true),
              let span = narratedSpan(slot), span.isFinite, span >= minimumSourceSpanS - 1e-9,
              let duration = slot?.durationS, duration.isFinite, duration > 0 else { return false }
        // The seed's rate is the server recipe's own. The compiler consumes min(span, duration * rate) source
        // seconds, so device/server duration skew or a creator-lengthened slot holds the last frame (as at 0.25x+).
        return true
    }

    /// The rate the compiler derives from the slot's source window.
    static func acceptsDerived(_ rate: Double, sourceSpan: Double, slot: EditorTimelineSlot?) -> Bool {
        guard rate.isFinite, rate > 0 else { return false }
        if creatorRange.contains(rate) { return true }
        guard rate < creatorRange.lowerBound, narratedSpan(slot) != nil else { return false }
        return sourceSpan.isFinite && sourceSpan >= minimumSourceSpanS - 1e-9
    }

    /// Names the guard that refused a slot so a preview failure says which check fired (KRI-813).
    static func reject(_ guardID: String, index: Int, rate: Double, slot: EditorTimelineSlot?, sourceSpan: Double? = nil) {
        var fields = ["guard": guardID, "index": String(index), "rate": String(rate),
                      "narrated": String(narratedSpan(slot) != nil), "duration": String(slot?.durationS ?? -1)]
        if let sourceSpan { fields["sourceSpan"] = String(sourceSpan) }
        NativePreviewDiagnostics.record("rate-guard", fields: fields)
    }
}
