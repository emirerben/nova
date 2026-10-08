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

    init(title: String?, mode: Mode, durationS: Double?, windowStartS: Double, windowEndS: Double, volume: Double = 1) {
        self.title = title; self.mode = mode; self.durationS = durationS
        self.windowStartS = windowStartS; self.windowEndS = windowEndS; self.volume = volume
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
        self.durationS = raw["duration_s"]?.numberValue.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        self.windowStartS = start
        self.windowEndS = end
        self.volume = raw["volume"]?.numberValue.flatMap { $0.isFinite && $0 >= 0 && $0 <= 1 ? $0 : nil } ?? 1
    }

    /// Length of the played window as the server last saved it.
    var windowLengthS: Double { windowEndS - windowStartS }

    /// Fewest seconds of song a start may leave (KRI-457). The song plays while it has time left, so a
    /// start can sit anywhere up to `durationS - minPlayableS`.
    static let minPlayableS = 1.0

    /// The song as the editor currently shows it: the server's values with the user's unsaved volume /
    /// start applied. Nil once the user removed it. With `videoLength` the window follows the video as it
    /// is NOW (extended, trimmed): it ends where the video ends or the song does, whichever comes first.
    func applying(_ edit: EditorUserSongState?, videoLength: Double? = nil) -> NativeUserSong? {
        if edit?.removed == true { return nil }
        guard edit != nil || videoLength != nil else { return self }
        let start = edit?.windowStartS ?? windowStartS
        var end = start + (videoLength ?? windowLengthS)
        if let durationS { end = min(end, durationS) }
        return NativeUserSong(title: title, mode: mode, durationS: durationS,
                              windowStartS: start, windowEndS: max(end, start),
                              volume: edit?.volume ?? volume)
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

    init(assetID: String, sourceStart: TimeInterval = 0, sourceDuration: TimeInterval = .infinity,
         volume: Double = 1, fadeIn: TimeInterval? = nil, fadeOut: TimeInterval? = nil) {
        self.assetID = assetID
        self.sourceStart = sourceStart
        self.sourceDuration = sourceDuration
        self.volume = volume
        self.fadeIn = fadeIn
        self.fadeOut = fadeOut
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
    static let lipSyncLockCopy = "Lip-sync keeps the song where you filmed it."
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

    var startLabel: String { "Starts at \(NativeEditorYourSong.timecode(startS))" }
}
