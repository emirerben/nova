import Foundation

/// Trims a lip-sync song by cutting the video (KRI-561).
///
/// A lip-sync song is the master clock: each take sits at `song_time = take_time + delta`, so the song
/// cannot be trimmed with a field. Choosing a song range `[start, end]` instead means keeping only the
/// stretch of the video that plays it: cuts wholly outside the range are dropped, the cuts straddling its
/// edges lose their head / tail, and everything after the new start ripples left. Kept footage keeps its
/// source window, except a straddling cut, whose in-point moves by exactly as much as its output start does
/// (so every pinned take still hits `delta + source_start - output_start == song start`). The server then
/// derives the new song start from those cuts, and the song end is always start + video length.
///
/// This is only the arithmetic on the timeline's own slots. The session applies it through the same
/// document transaction, tombstones and deletions a clip removal uses.
enum NativeLipsyncSongTrim {
    /// The video may not be trimmed below this (the server refuses a shorter montage).
    static let minimumTotalS = 3.0
    /// Boundaries closer than this are the same boundary.
    static let epsilon = 0.001

    /// One surviving cut: where its footage window starts and how long it plays.
    struct Kept: Equatable {
        let index: Int
        let inS: Double
        let durationS: Double
        /// The cut lost its head and / or tail. An untouched cut keeps its slot exactly as it is.
        let trimmed: Bool
    }

    struct Plan: Equatable {
        /// Indices (into the slots passed in) of the cuts that are dropped entirely.
        let dropped: [Int]
        /// The cuts that remain, in timeline order. `inS` / `durationS` are set for every one of them, but
        /// only a straddler differs from its slot.
        let kept: [Kept]
        /// How far the video's start moved: the old output time that is now 0.
        let originS: Double
        /// The old output time that is now the end of the video.
        let endS: Double
        var totalS: Double { endS - originS }
    }

    /// `t0` / `t1` are output seconds of the CURRENT timeline (0 = the first cut). Nil when the trim would
    /// leave under `minimumTotalS`, leave nothing, or change nothing.
    static func plan(slots: [EditorTimelineSlot], t0: Double, t1: Double, minimumClipS: Double) -> Plan? {
        let projection = NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil)
        let windows = projection.baseClipWindows
        guard let total = windows.last?.end, t0.isFinite, t1.isFinite else { return nil }
        var head = max(0, t0), tail = min(total, t1)
        guard tail > head else { return nil }

        // A straddler whose remainder would be shorter than the editor's minimum clip is not kept: the
        // edge snaps to its far boundary instead (the shortest remainder is the one inside the range).
        for window in windows where window.start < head - epsilon && window.end > head + epsilon {
            if window.end - head < minimumClipS { head = window.end }
        }
        for window in windows.reversed() where window.start < tail - epsilon && window.end > tail + epsilon {
            if tail - window.start < minimumClipS { tail = window.start }
        }
        guard tail - head >= minimumTotalS - epsilon else { return nil }

        var dropped: [Int] = []
        var kept: [Kept] = []
        for window in windows {
            guard window.end > head + epsilon, window.start < tail - epsilon else {
                dropped.append(window.sourceIndex)
                continue
            }
            let slot = slots[window.sourceIndex]
            let headCut = max(0, head - window.start)
            let tailCut = max(0, window.end - tail)
            let duration = (window.end - window.start) - headCut - tailCut
            guard duration > epsilon else { dropped.append(window.sourceIndex); continue }
            let trimmed = headCut > epsilon || tailCut > epsilon
            kept.append(Kept(index: window.sourceIndex, inS: trimmed ? millis(slot.inS + headCut) : slot.inS,
                             durationS: trimmed ? millis(duration) : duration, trimmed: trimmed))
        }
        guard !kept.isEmpty, !(dropped.isEmpty && head <= epsilon && tail >= total - epsilon) else { return nil }
        return Plan(dropped: dropped, kept: kept, originS: head, endS: tail)
    }

    private static func millis(_ value: Double) -> Double { (value * 1000).rounded() / 1000 }

    /// The cuts as the song-start derivation reads them, in timeline order.
    static func cuts(of slots: [EditorTimelineSlot]) -> [NativeLipsyncSongAnchor.Cut] {
        NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil).baseClipWindows.map { window in
            NativeLipsyncSongAnchor.Cut(clipIndex: slots[window.sourceIndex].clipIndex, inS: slots[window.sourceIndex].inS,
                                        outputStartS: window.start)
        }
    }
}
