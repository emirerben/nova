import CoreGraphics
import Foundation

/// Pure geometry and navigation rules behind the song timeline's fixed-playhead layout (KRI-575).
/// The editor's `NativeMiniStrip` positions every lane from a clock (`playheadX + (t - now) * pixelsPerSecond`)
/// and treats panning as seeking; the song timeline does the same, and these are its numbers. No UI types, so the
/// edge cases (ends of the song, repeated "next", NaN input) are unit-testable.
enum SongTimelineMath {
    static let minPixelsPerSecond: CGFloat = 6
    static let maxPixelsPerSecond: CGFloat = 60
    /// The default zoom shows about this much song across the viewport.
    static let defaultVisibleSeconds: CGFloat = 12
    /// A stop within this of the playhead counts as "already there", so repeated Next/Previous always progress.
    static let stopTolerance = 0.05

    // MARK: zoom and axis

    static func clampedPixelsPerSecond(_ value: CGFloat) -> CGFloat {
        guard value.isFinite else { return minPixelsPerSecond }
        return min(max(value, minPixelsPerSecond), maxPixelsPerSecond)
    }

    /// Zoom that fits `defaultVisibleSeconds` into the viewport (clamped).
    static func defaultPixelsPerSecond(viewportWidth: CGFloat) -> CGFloat {
        guard viewportWidth.isFinite, viewportWidth > 0 else { return clampedPixelsPerSecond(20) }
        return clampedPixelsPerSecond(viewportWidth / defaultVisibleSeconds)
    }

    /// A time kept inside the song; anything that is not a number is the start.
    static func clamp(_ time: Double, duration: Double) -> Double {
        guard time.isFinite else { return 0 }
        return min(max(time, 0), max(0, duration.isFinite ? duration : 0))
    }

    static func x(forTime time: Double, now: Double, playheadX: CGFloat, pixelsPerSecond: CGFloat) -> CGFloat {
        playheadX + CGFloat(time - now) * pixelsPerSecond
    }

    /// Seconds between labelled ruler ticks so that labels stay roughly 50 pt apart or more.
    static func tickStep(pixelsPerSecond: CGFloat) -> Double {
        let wanted = 50 / Double(max(pixelsPerSecond, 1))
        return [1.0, 2, 5, 10, 15, 30, 60].first { $0 >= wanted } ?? 60
    }

    /// `m:ss.s`, e.g. 0:04.5. Rounded to tenths first, so 59.96 reads 1:00.0 and never 0:60.0.
    static func timecode(_ seconds: Double) -> String {
        let tenths = Int(((seconds.isFinite ? max(0, seconds) : 0) * 10).rounded())
        return String(format: "%d:%02d.%d", tenths / 600, (tenths % 600) / 10, tenths % 10)
    }

    // MARK: navigation

    /// A place the playhead can jump to: the start of a block or of an empty spot.
    struct Stop: Equatable {
        enum Kind: Equatable { case block(String), gap(Int) }
        let kind: Kind
        let time: Double
    }

    /// Block and empty-spot starts in song order. Where several share a start (a hidden take under a visible one)
    /// the visible block wins, then a block over a spot, so one time never has two stops.
    static func stops(segments: [SongTimelineState.Segment], gaps: [SongTimelineState.Gap]) -> [Stop] {
        var candidates: [(stop: Stop, rank: Int)] = segments.map { (Stop(kind: .block($0.mediaID), time: $0.start), $0.hidden ? 1 : 0) }
        candidates += gaps.map { (Stop(kind: .gap($0.index), time: $0.start), 2) }
        candidates.sort { ($0.stop.time, $0.rank) < ($1.stop.time, $1.rank) }
        var result: [Stop] = []
        for candidate in candidates where result.last.map({ abs($0.time - candidate.stop.time) > 1e-6 }) ?? true {
            result.append(candidate.stop)
        }
        return result
    }

    static func nextStop(after time: Double, in stops: [Stop]) -> Stop? {
        stops.first { $0.time > time + stopTolerance }
    }

    static func previousStop(before time: Double, in stops: [Stop]) -> Stop? {
        stops.last { $0.time < time - stopTolerance }
    }

    /// Where the playhead starts: the first visible block, else the first block, else the top of the song.
    static func initialTime(segments: [SongTimelineState.Segment]) -> Double {
        (segments.first { !$0.hidden } ?? segments.first)?.start ?? 0
    }

    // MARK: lifting a block

    /// The start a lifted block has now: where it was picked up, plus how far the finger travelled, plus how far the
    /// timeline scrolled underneath it (edge auto-scroll), so the block stays under the finger while the song moves.
    static func liftedDelta(startDelta: Double, fingerTravel: CGFloat, pixelsPerSecond: CGFloat, clockShift: Double) -> Double {
        guard pixelsPerSecond > 0, fingerTravel.isFinite else { return startDelta + (clockShift.isFinite ? clockShift : 0) }
        return startDelta + Double(fingerTravel / pixelsPerSecond) + (clockShift.isFinite ? clockShift : 0)
    }
}
