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

/// What the Sounds tab shows for a project with the creator's own song.
/// Read-only: the server rejects audio edits on song variants.
struct NativeEditorYourSong: Equatable, Sendable {
    let title: String
    let window: String
    let mode: String?

    static let fallbackTitle = "Your song"
    static let helperCopy = "This is the song you added. Camera audio is muted so it plays alone."

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
