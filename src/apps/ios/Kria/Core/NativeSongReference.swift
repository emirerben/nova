import Foundation

/// A song section selected for a social-app sound search. It is intentionally
/// separate from editor audio lanes: reference-only videos export without it.
struct NativeSongReference: Equatable, Sendable {
    let trackID: String
    let title: String
    let artist: String?
    let startS: TimeInterval
    let endS: TimeInterval

    init?(variant: [String: JSONValue]) {
        guard let raw = variant["song_reference"]?.objectValue,
              let trackID = raw["track_id"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !trackID.isEmpty,
              let title = raw["title"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty,
              let start = raw["start_s"]?.numberValue,
              let end = raw["end_s"]?.numberValue,
              start.isFinite, end.isFinite, start >= 0, end > start else { return nil }
        self.trackID = trackID
        self.title = title
        let artist = raw["artist"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.artist = artist?.isEmpty == false ? artist : nil
        self.startS = start
        self.endS = end
    }

    var timeRange: String { "\(Self.timecode(startS)) – \(Self.timecode(endS))" }
    var copyText: String { "\(title)\(artist.map { " by \($0)" } ?? ""), \(timeRange)" }

    func withEndS(_ endS: TimeInterval) -> NativeSongReference {
        NativeSongReference(trackID: trackID, title: title, artist: artist, startS: startS, endS: endS)
    }

    private init(trackID: String, title: String, artist: String?, startS: TimeInterval, endS: TimeInterval) {
        self.trackID = trackID
        self.title = title
        self.artist = artist
        self.startS = startS
        self.endS = endS
    }

    /// Round before splitting the clock so a carry at 59.9995 seconds is
    /// represented unambiguously as the next minute.
    static func timecode(_ seconds: TimeInterval) -> String {
        let milliseconds = max(0, Int((seconds * 1_000).rounded(.toNearestOrAwayFromZero)))
        let minutes = milliseconds / 60_000
        let remainder = milliseconds % 60_000
        return String(format: "%02d:%02d.%03d", minutes, remainder / 1_000, remainder % 1_000)
    }
}

enum NativeEditorSongReferencePresentation: Equatable, Sendable {
    case reference(NativeSongReference)
    case saveToUpdateTiming

    static func make(reference: NativeSongReference, baselineDuration: TimeInterval?, currentDuration: TimeInterval, durationChanged: Bool) -> Self {
        guard durationChanged,
              let baselineDuration,
              baselineDuration.isFinite, baselineDuration > 0,
              currentDuration.isFinite, currentDuration >= 0 else { return .reference(reference) }
        let difference = currentDuration - baselineDuration
        guard abs(difference) > 0.001 else { return .reference(reference) }
        guard difference < 0 else { return .saveToUpdateTiming }
        return .reference(reference.withEndS(reference.startS + currentDuration))
    }
}

enum NativeMusicPlaybackMode: String, Equatable, Sendable {
    case embedded
    case referenceOnly = "reference_only"

    init(variant: [String: JSONValue]) {
        self = variant["music_playback_mode"]?.stringValue == Self.referenceOnly.rawValue ? .referenceOnly : .embedded
    }
}
