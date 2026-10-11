import Foundation
import KriaMediaEngine

/// The creator's own song on a montage (KRI-374), decoded leniently from the
/// additive `user_song` variant field. Missing or malformed data is `nil`, so
/// every other variant (and an older server) behaves exactly as before.
struct NativeUserSong: Equatable, Sendable {
    enum Mode: String, Sendable {
        case background
        case lipsync

        var label: String {
            switch self {
            case .background: "Background"
            case .lipsync: "Lip-sync · master audio"
            }
        }
    }

    let title: String?
    let mode: Mode
    let durationS: Double?
    let windowStartS: Double
    let windowEndS: Double
    /// Song level 0...1 the recipe plays at. Additive on the wire (KRI-428): a
    /// missing or malformed value reads as full volume, like an older server.
    let volume: Double
    /// Lip-sync only (KRI-561): each pinned take's song start, `song_time = take_time + delta`, keyed by
    /// `media_id`. May be negative. Empty on a background song and on an older server.
    let takes: [String: Double]
    /// Absolute song second where the CREATOR stopped a background song, or nil while the song just follows
    /// the video. The wire only reports the effective `window_end_s`, so it is inferred: an end that falls
    /// short of `min(start + video, song end)` can only be the creator's. The song's own end, and the video's
    /// end, are the natural ones.
    let creatorEndS: Double?
    /// The video length the saved window was computed for (the variant's `duration_s`), when known.
    let videoLengthS: Double?

    init(title: String?, mode: Mode, durationS: Double?, windowStartS: Double, windowEndS: Double, volume: Double = 1,
         takes: [String: Double] = [:], creatorEndS: Double? = nil, videoLengthS: Double? = nil) {
        self.title = title; self.mode = mode; self.durationS = durationS
        self.windowStartS = windowStartS; self.windowEndS = windowEndS; self.volume = volume
        self.takes = takes; self.creatorEndS = creatorEndS; self.videoLengthS = videoLengthS
    }

    /// A creator end is told apart from a natural one by more than this (rounding noise on the wire).
    static let creatorEndTolerance = 0.05

    /// The creator's end, if the saved window ends short of what the video and the song would give.
    static func inferredCreatorEnd(mode: Mode, start: Double, end: Double, durationS: Double?, videoLengthS: Double?) -> Double? {
        guard mode == .background, let videoLengthS, videoLengthS.isFinite, videoLengthS > 0 else { return nil }
        var natural = start + videoLengthS
        if let durationS { natural = min(natural, durationS) }
        return end < natural - creatorEndTolerance ? end : nil
    }

    init?(variant: [String: JSONValue]) {
        guard let raw = variant["user_song"]?.objectValue,
              let mode = raw["mode"]?.stringValue.flatMap(Mode.init(rawValue:)),
              let start = raw["window_start_s"]?.numberValue,
              let end = raw["window_end_s"]?.numberValue,
              start.isFinite, end.isFinite, start >= 0, end > start else { return nil }
        let title = raw["title"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.title = title?.isEmpty == false ? title : nil
        self.mode = mode
        let duration = raw["duration_s"]?.numberValue.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        self.durationS = duration
        self.windowStartS = start
        self.windowEndS = end
        self.volume = raw["volume"]?.numberValue.flatMap { $0.isFinite && $0 >= 0 && $0 <= 1 ? $0 : nil } ?? 1
        var takes: [String: Double] = [:]
        for (mediaID, delta) in raw["takes"]?.objectValue ?? [:] {
            if let value = delta.numberValue, value.isFinite { takes[mediaID] = value }
        }
        self.takes = mode == .lipsync ? takes : [:]
        let video = variant["duration_s"]?.numberValue.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        self.videoLengthS = video
        // Only a server that offers `user_song.trim` can have a creator end (an older one always follows the
        // video, so a window short of it is a rendering detail, never the creator's choice).
        let offersTrim = variant["editor_capabilities"]?.objectValue?["user_song"]?.objectValue?["trim"] != nil
        self.creatorEndS = offersTrim
            ? Self.inferredCreatorEnd(mode: mode, start: start, end: end, durationS: duration, videoLengthS: video)
            : nil
    }

    /// Length of the played window as the server last saved it.
    var windowLengthS: Double { windowEndS - windowStartS }

    /// Fewest seconds of song a start may leave (KRI-457). The song plays while it has time left, so a
    /// start can sit anywhere up to `durationS - minPlayableS`.
    static let minPlayableS = 1.0

    /// The creator end after `edit`: its `window_end_s` wins (the song's own length means "no end"),
    /// otherwise the saved one stays.
    func creatorEnd(applying edit: EditorUserSongState?) -> Double? {
        guard let value = edit?.windowEndS else { return creatorEndS }
        if let durationS, value >= durationS - 0.001 { return nil }
        return value
    }

    /// The song as the editor currently shows it: the server's values with the user's unsaved volume /
    /// start / end applied. Nil once the user removed it. With `videoLength` the window follows the video as
    /// it is NOW (extended, trimmed): it ends where the video ends, the song does, or the creator stopped it,
    /// whichever comes first.
    func applying(_ edit: EditorUserSongState?, videoLength: Double? = nil) -> NativeUserSong? {
        if edit?.removed == true { return nil }
        guard edit != nil || videoLength != nil else { return self }
        let start = edit?.windowStartS ?? windowStartS
        let creatorEnd = mode == .background ? creatorEnd(applying: edit) : nil
        // With a creator end the saved window length is the creator's, not the video's, so the video length
        // has to come from the variant. Without one the saved window length is the old rule.
        let length = videoLength ?? (creatorEndS != nil || creatorEnd != nil ? videoLengthS : nil) ?? windowLengthS
        var end = start + length
        if let durationS { end = min(end, durationS) }
        if let creatorEnd { end = min(end, creatorEnd) }
        return NativeUserSong(title: title, mode: mode, durationS: durationS,
                              windowStartS: start, windowEndS: max(end, start),
                              volume: edit?.volume ?? volume, takes: takes,
                              creatorEndS: creatorEnd, videoLengthS: videoLengthS)
    }

    /// The same song with its window moved by `seconds` (lip-sync: the singer's position after a trim).
    func shifted(by seconds: Double) -> NativeUserSong {
        guard seconds != 0 else { return self }
        let start = max(0, windowStartS + seconds)
        return NativeUserSong(title: title, mode: mode, durationS: durationS, windowStartS: start,
                              windowEndS: start + windowLengthS, volume: volume, takes: takes,
                              creatorEndS: creatorEndS, videoLengthS: videoLengthS)
    }
}

extension NativeUserSong {
    /// The latest start: it must leave `minPlayableS` of song. Nil when the song length is unknown.
    static func maxStart(for song: NativeUserSong) -> Double? {
        guard let songDuration = song.durationS else { return nil }
        return max(0, songDuration - minPlayableS)
    }

    /// A pending start re-clamped to the song as it is NOW: it must leave a second of song, and (KRI-561) a
    /// second before the creator's end, so the server never refuses it.
    static func reclamped(_ state: EditorUserSongState, base song: NativeUserSong,
                          acknowledged: EditorUserSongState?) -> EditorUserSongState {
        var state = state
        guard !state.removed, var start = state.windowStartS else { return state }
        if let limit = maxStart(for: song) { start = min(start, limit) }
        let context = EditorUserSongState.merged(acknowledged, state) ?? state
        if let end = song.creatorEnd(applying: context) { start = min(start, max(0, end - minPlayableS)) }
        state.windowStartS = start
        return state
    }

    /// The song as the editor shows it, from explicit inputs (KRI-576: the session and the caption shift share
    /// this one rule). Saved values (`base` plus the `acknowledged` edits) plus the unsaved `draft`; a lip-sync
    /// window follows the singer by `lipsyncShift`; the window follows the video as it is now (`videoLength`).
    static func effective(base: NativeUserSong, acknowledged: EditorUserSongState?, draft: EditorUserSongState?,
                          lipsyncShift: Double, videoLength: Double) -> NativeUserSong? {
        let state = EditorUserSongState.merged(acknowledged, draft)
            .map { reclamped($0, base: base, acknowledged: acknowledged) }
        let anchored = base.mode == .lipsync ? base.shifted(by: lipsyncShift) : base
        return anchored.applying(state, videoLength: videoLength)
    }
}

/// KRI-576: the lyric caption rows (`source_params.origin == "song"`, ids `song-caption-N`) are timed against the
/// song window the server last projected them for. While the creator moves the start or trims a lip-sync take
/// the live preview re-times them to the window as it is NOW, on a COPY handed to the render compiler. The
/// document is never touched, so nothing goes dirty and Save is unaffected; the server re-projects on Save.
///
/// Mirrors `song_captions.window_words` for rows the document already holds: a row moves by `-shiftS`, a row
/// that leaves `[0, windowLengthS - tailCutoffS)` is hidden. KNOWN LIMIT: the preview can shift and hide rows,
/// not add them. Words the new window newly reaches (no row yet, or a `window_hidden` row with stale times)
/// only appear after Save.
struct NativeSongCaptionShift: Equatable, Sendable {
    /// New window start minus the start the rows were timed against, in seconds.
    let shiftS: Double
    /// The window length now (the video, capped by what is left of the song).
    let windowLengthS: Double
    /// The creator removed the song: every lyric row is hidden.
    let songRemoved: Bool

    /// Server constants (`song_captions._TAIL_CUTOFF_S`, `_MIN_WORD_S`).
    static let tailCutoffS = 0.2
    static let minWordS = 0.12
    static let idPrefix = "song-caption-"
    /// Smaller than this is rounding noise, not a move.
    static let tolerance = 0.0005

    static func isSongCaption(_ element: EditorTextElement) -> Bool {
        if element.raw["source_params"]?.objectValue?["origin"] == .string("song") { return true }
        return element.id.hasPrefix(idPrefix)
    }

    func apply(to elements: [EditorTextElement]) -> [EditorTextElement] {
        elements.map { Self.isSongCaption($0) ? shifted($0) : $0 }
    }

    private func rounded(_ value: Double) -> Double { (value * 1000).rounded() / 1000 }

    private func hidden(_ element: EditorTextElement) -> EditorTextElement {
        var copy = element
        copy.raw["removed"] = .bool(true)
        return copy
    }

    private func shifted(_ element: EditorTextElement) -> EditorTextElement {
        // A row the creator (or an earlier window) already removed stays removed.
        if element.raw["removed"] == .bool(true) { return element }
        if songRemoved || windowLengthS <= 0 { return hidden(element) }
        let length = rounded(windowLengthS)
        guard element.startS - shiftS >= -1e-3 else { return hidden(element) }
        let start = max(0, rounded(element.startS - shiftS))
        if start >= length - Self.tailCutoffS { return hidden(element) }
        var end = rounded(min(element.endS - shiftS, length))
        if end - start < Self.minWordS { end = rounded(min(start + Self.minWordS, length)) }
        if end <= start { return hidden(element) }
        var copy = element
        copy.startS = start
        copy.endS = end
        if copy.raw["start_s"] != nil { copy.raw["start_s"] = .number(start) }
        if copy.raw["end_s"] != nil { copy.raw["end_s"] = .number(end) }
        if let timings = copy.raw["word_timings"]?.arrayValue {
            copy.raw["word_timings"] = .array(timings.map { timing in
                guard var row = timing.objectValue,
                      let from = row["start_s"]?.numberValue, let to = row["end_s"]?.numberValue else { return timing }
                row["start_s"] = .number(min(max(0, rounded(from - shiftS)), length))
                row["end_s"] = .number(min(max(0, rounded(to - shiftS)), length))
                return .object(row)
            })
        }
        return copy
    }
}

/// Keeps a lip-sync song on the singer when the creator trims the head of the first cut, or trims the
/// song itself (KRI-561: the video is cut to the chosen song range).
///
/// A lip-sync take sits at `song_time = delta + source_time`. The recipe's song clip starts at
/// `window_start`, which the server derived from the FIRST cut (`source_start - output_start == window - delta`).
/// Pulling that cut's head earlier or later moves its source start but not the recipe's song start, so the
/// singer drifted off the song by exactly the trim (founder export, 0.3 s trim). The song start therefore
/// follows the first cut: `start' = start + (first.source_start - saved first.source_start)`, both cuts
/// sitting at output 0. The server applies the same rule on Save (`guided_story.compile_guided_runtime_plan`).
enum NativeLipsyncSongAnchor {
    /// One cut as the video plays it: which take, where in the take it starts, and where in the video.
    struct Cut: Equatable, Sendable {
        let clipIndex: Int
        let inS: Double
        let outputStartS: Double
    }

    /// The song start that keeps every PINNED take on the song: each cut of a take votes for
    /// `delta + source_start - output_start` (the server's `window_start_from_pinned_cuts`). B-roll has no
    /// delta and does not vote. Nil when nothing votes or the votes disagree by more than a frame (a
    /// reorder): the caller then falls back to `startShift`.
    static func derivedStart(cuts: [Cut], deltaByClipIndex: [Int: Double]) -> Double? {
        let votes = cuts.compactMap { cut in
            deltaByClipIndex[cut.clipIndex].map { $0 + cut.inS - cut.outputStartS }
        }
        guard let low = votes.min(), let high = votes.max(), votes.allSatisfy(\.isFinite),
              high - low <= frameTolerance else { return nil }
        return votes.reduce(0, +) / Double(votes.count)
    }

    /// Both sides quantize cut sources to the editor frame clock (1/30 s); a smaller move is rounding noise
    /// and leaves the song where the server pinned it.
    static let frameTolerance = 1.0 / 30.0 + 1e-6

    /// Seconds to add to the recipe song start. Zero unless the same source still opens the video and its
    /// head moved by more than one editor frame.
    static func startShift(savedClipIndex: Int?, savedInS: Double?, currentClipIndex: Int?, currentInS: Double?) -> Double {
        guard let savedClipIndex, let savedInS, let currentClipIndex, let currentInS,
              savedClipIndex == currentClipIndex, savedInS.isFinite, currentInS.isFinite else { return 0 }
        let shift = currentInS - savedInS
        return abs(shift) > frameTolerance ? shift : 0
    }
}

/// How the pinned device recipe plays the song under the timeline: the `song`
/// audio track's clip. It is the playback truth for the live preview, and the
/// only source of the row when the server predates `user_song`.
struct NativeEditorSongBed: Equatable, Sendable {
    static let trackID = "song"

    let assetID: String
    let sourceStart: TimeInterval
    let sourceDuration: TimeInterval
    let volume: Double
    let fadeIn: TimeInterval?
    let fadeOut: TimeInterval?
    /// Absolute song second where the creator stopped the music (KRI-561); nil = the song follows the video.
    let endS: TimeInterval?

    /// The server fades out over this long when the creator's end stops the song before both the video and
    /// the song end; a song that merely runs out keeps the recipe's own fade.
    static let creatorEndFadeOut: TimeInterval = 1.5

    init(assetID: String, sourceStart: TimeInterval = 0, sourceDuration: TimeInterval = .infinity,
         volume: Double = 1, fadeIn: TimeInterval? = nil, fadeOut: TimeInterval? = nil, endS: TimeInterval? = nil) {
        self.assetID = assetID
        self.sourceStart = sourceStart
        self.sourceDuration = sourceDuration
        self.volume = volume
        self.fadeIn = fadeIn
        self.fadeOut = fadeOut
        self.endS = endS
    }

    /// Nil when the recipe has no song track, or its clip is not playable.
    init?(recipe: KriaMediaEngine.EditRecipe) {
        guard let clip = recipe.tracks.first(where: { $0.id == Self.trackID && $0.kind == .audio })?.clips.first,
              clip.sourceStart.isFinite, clip.sourceStart >= 0,
              clip.sourceDuration.isFinite, clip.sourceDuration > 0,
              clip.volume.isFinite, clip.volume >= 0 else { return nil }
        self.init(assetID: clip.sourceAssetID, sourceStart: clip.sourceStart, sourceDuration: clip.sourceDuration,
                  volume: clip.volume, fadeIn: clip.audioFadeIn, fadeOut: clip.audioFadeOut)
    }
}

/// What the Sounds tab shows for a project with the creator's own song. The
/// row is display-only; volume, start and remove edits live on the editor
/// document (`EditorUserSongState`) and are gated by `user_song.*` capabilities (KRI-428).
struct NativeEditorYourSong: Equatable, Sendable {
    let title: String
    let window: String
    let mode: String?

    static let fallbackTitle = "Your song"
    static let helperCopy = "This is the song you added. Camera audio is muted so it plays alone, until you turn up Original audio."
    static let songEndsEarlyCopy = "Song ends before the video does."
    static let songStoppedCopy = "Song stops where you set it, before the video ends."
    static let lipSyncLockCopy = "Lip-sync keeps the song where you filmed it."
    static let lipSyncTrimHintCopy = "Trimming cuts your video to match."
    static let removedHelperCopy = "Song removed. Your camera audio plays instead."

    var accessibilitySummary: String {
        [title, window, mode].compactMap { $0 }.joined(separator: ", ")
    }

    /// `userSong` wins; without it (an older server) the recipe's own clip
    /// supplies the window, and the mode is simply not claimed.
    static func make(userSong: NativeUserSong?, bed: NativeEditorSongBed?) -> NativeEditorYourSong? {
        if let userSong {
            return NativeEditorYourSong(
                title: userSong.title ?? fallbackTitle,
                window: windowLabel(start: userSong.windowStartS, end: userSong.windowEndS),
                mode: userSong.mode.label)
        }
        guard let bed, bed.sourceDuration.isFinite else { return nil }
        return NativeEditorYourSong(
            title: fallbackTitle,
            window: windowLabel(start: bed.sourceStart, end: bed.sourceStart + bed.sourceDuration),
            mode: nil)
    }

    static func windowLabel(start: TimeInterval, end: TimeInterval) -> String {
        "Plays \(timecode(start)) – \(timecode(end))"
    }

    /// `m:ss`, rounded to the nearest second.
    static func timecode(_ seconds: TimeInterval) -> String {
        let whole = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }
}

/// What the Sounds tab's song controls show and allow (KRI-428). Values include unsaved edits.
struct NativeEditorYourSongControls: Equatable, Sendable {
    let mode: NativeUserSong.Mode
    /// 0...1.
    let volume: Double
    /// Seconds into the song file where playback starts.
    let startS: Double
    /// How much of the song plays: the video length, or what is left of the song when that is shorter.
    let windowLengthS: Double
    let songDurationS: Double?
    /// The latest start that still leaves `NativeUserSong.minPlayableS` of song; nil when the song length is unknown.
    let maxStartS: Double?
    /// The video is longer than what is left of the song, so the song stops first.
    var songEndsBeforeVideo = false
    let canEditVolume: Bool
    let canEditStart: Bool
    let canRemove: Bool
    /// KRI-561: the server offers `user_song.trim` (absent on an older server => today's UI, exactly).
    var trimOffered = false
    /// `trimOffered` and the edit is open for it.
    var canTrim = false
    /// Where the song stops now (the window's end, creator's or natural).
    var endS: Double = 0
    /// The creator stopped the music before the video or song end.
    var hasCreatorEnd = false
    /// The video as long as it is now.
    var videoLengthS: Double = 0
    /// The bar's span: the whole song for a background song, the saved window for lip-sync (whose
    /// handles can only move inward, since the footage outside it is gone).
    var barStartS: Double = 0
    var barEndS: Double = 0

    var startLabel: String { "Starts at \(NativeEditorYourSong.timecode(startS))" }
    var endLabel: String { "Ends at \(NativeEditorYourSong.timecode(endS))" }
    var rangeLabel: String { NativeEditorYourSong.windowLabel(start: startS, end: endS) }

    /// Background: the lowest end the handle allows at the current start (a second of song).
    var minEndS: Double { startS + NativeUserSong.minPlayableS }
    /// Background: the natural end, where the video or the song stops. The end handle cannot go past it.
    var maxEndS: Double { min(startS + videoLengthS, songDurationS ?? .infinity) }
}
