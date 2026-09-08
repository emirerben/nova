import Foundation

public enum TimelineMath {
    public static func clamp(_ value: TimeInterval, to range: ClosedRange<TimeInterval>) -> TimeInterval { min(max(value, range.lowerBound), range.upperBound) }
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
