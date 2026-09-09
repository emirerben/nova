import Foundation

public enum TimelineMath {
    public static func clamp(_ value: TimeInterval, to range: ClosedRange<TimeInterval>) -> TimeInterval { min(max(value, range.lowerBound), range.upperBound) }

    /// Converts a horizontal timeline coordinate into the corresponding time.
    /// Invalid/empty geometry is deliberately treated as the start of the
    /// timeline so gesture code cannot manufacture NaN values.
    public static func time(forPixelX x: Double, contentWidth: Double, duration: TimeInterval) -> TimeInterval {
        guard x.isFinite, contentWidth.isFinite, contentWidth > 0, duration.isFinite, duration > 0 else { return 0 }
        return clamp((x / contentWidth) * duration, to: 0...duration)
    }

    /// Converts a time into a horizontal coordinate in a timeline lane.
    public static func pixelX(forTime time: TimeInterval, contentWidth: Double, duration: TimeInterval) -> Double {
        guard time.isFinite, contentWidth.isFinite, contentWidth > 0, duration.isFinite, duration > 0 else { return 0 }
        return (clamp(time, to: 0...duration) / duration) * contentWidth
    }

    /// Returns the clip containing a time. At an exact shared boundary the
    /// later clip wins, which matches the hit target visible to the user.
    public static func clip(atTimelineTime time: TimeInterval, in clips: [TimelineClip]) -> TimelineClip? {
        guard time.isFinite else { return nil }
        return clips.reversed().first { time >= $0.timelineStart && time <= $0.timelineStart + $0.duration }
    }

    /// Keeps a trim edge inside its clip while preserving a usable minimum.
    public static func clampedTrimTime(_ time: TimeInterval, edge: TrimEdge,
                                       clipStart: TimeInterval, clipEnd: TimeInterval,
                                       minimumDuration: TimeInterval = 0.1) -> TimeInterval {
        let minimum = min(max(0, minimumDuration.isFinite ? minimumDuration : 0), max(0, clipEnd - clipStart))
        guard clipEnd >= clipStart else { return edge == .leading ? clipStart : clipEnd }
        if edge == .leading { return clamp(time, to: clipStart...(clipEnd - minimum)) }
        return clamp(time, to: (clipStart + minimum)...clipEnd)
    }

    public enum TrimEdge: Sendable { case leading, trailing }
    public static func trim(_ clip: TimelineClip, sourceStart: TimeInterval? = nil, sourceDuration: TimeInterval? = nil) -> TimelineClip {
        var result = clip
        let start = max(0, sourceStart ?? clip.sourceStart)
        let duration = max(0, sourceDuration ?? clip.sourceDuration)
        result.sourceStart = start; result.sourceDuration = duration
        return result
    }
    public static func split(_ clip: TimelineClip, atTimelineTime time: TimeInterval) -> (TimelineClip, TimelineClip)? {
        guard time > clip.timelineStart && time < clip.timelineStart + clip.duration else { return nil }
        let elapsedSource = (time - clip.timelineStart) * clip.rate
        var left = clip; left.id = clip.id + "-a"; left.sourceDuration = elapsedSource
        var right = clip; right.id = clip.id + "-b"; right.sourceStart += elapsedSource; right.sourceDuration -= elapsedSource; right.timelineStart = time
        left.transition = nil; right.transition = clip.transition
        return (left, right)
    }
    public static func move(_ clip: TimelineClip, toTimelineStart start: TimeInterval) -> TimelineClip { var c = clip; c.timelineStart = max(0, start); return c }
    public static func timelineTime(forSourceTime source: TimeInterval, in clip: TimelineClip) -> TimeInterval { clip.timelineStart + (source - clip.sourceStart) / clip.rate }
    public static func sourceTime(forTimelineTime timeline: TimeInterval, in clip: TimelineClip) -> TimeInterval { clip.sourceStart + (timeline - clip.timelineStart) * clip.rate }
    public static func totalDuration(of recipe: EditRecipe) -> TimeInterval { recipe.tracks.flatMap(\.clips).map { $0.timelineStart + $0.duration }.max() ?? 0 }
    public static func crossfadeOverlap(_ first: TimelineClip, _ second: TimelineClip) -> TimeInterval { guard let transition = second.transition, transition.kind == .crossfade else { return 0 }; return min(transition.duration, min(first.duration, second.duration)) }
}
