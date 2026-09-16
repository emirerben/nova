import Foundation
#if canImport(os)
import os
#endif

/// Lightweight, opt-in timing/counter accumulator for isolating render
/// bottlenecks on-device. Disabled by default (`enabled == false`): every
/// `measure` call then costs one bool check and nothing else.
///
/// Used to settle, with numbers instead of a guess, which stage of
/// `NativeSkiaShadowPainter` dominates giant-title handwriting's export time
/// (see `docs/reviews/kri-29/giant-title.md`). `DeviceEffectsView` surfaces a
/// `snapshot()` per case in its report JSON under the `profile` key when
/// launched with `-render-profile`.
public enum RenderProfiler {
    // A plain debug/opt-in toggle, set once near app launch and read on every
    // `measure`/`count` call; unsynchronized by design (see the type doc) —
    // Swift 6 strict concurrency's escape hatch for exactly this shape.
    nonisolated(unsafe) public static var enabled = false

    private final class Bucket {
        var seconds: Double = 0
        var count: Int = 0
        var units: Int = 0
    }
    private static let lock = NSLock()
    nonisolated(unsafe) private static var buckets: [String: Bucket] = [:]
    #if canImport(os)
    private static let signposter = OSSignposter(subsystem: "com.kria.mediaengine", category: "render")
    #endif

    /// Wraps a unit of work, recording elapsed wall time, an invocation
    /// count, and an optional caller-supplied `units` count (e.g. pixels
    /// processed) against `name`. A no-op pass-through when disabled.
    @inline(__always)
    public static func measure<T>(_ name: StaticString, units: Int = 0, _ body: () throws -> T) rethrows -> T {
        guard enabled else { return try body() }
        #if canImport(os)
        let state = signposter.beginInterval(name)
        defer { signposter.endInterval(name, state) }
        #endif
        let start = CFAbsoluteTimeGetCurrent()
        defer { record(name, seconds: CFAbsoluteTimeGetCurrent() - start, units: units) }
        return try body()
    }

    /// Records a zero-duration occurrence — used for pure counters (e.g.
    /// cache hit/miss) where there is no work to time.
    public static func count(_ name: StaticString, units: Int = 0) {
        guard enabled else { return }
        record(name, seconds: 0, units: units)
    }

    private static func record(_ name: StaticString, seconds: Double, units: Int) {
        lock.lock(); defer { lock.unlock() }
        let key = "\(name)"
        let bucket = buckets[key] ?? Bucket()
        bucket.seconds += seconds
        bucket.count += 1
        bucket.units += units
        buckets[key] = bucket
    }

    public static func reset() {
        lock.lock(); defer { lock.unlock() }
        buckets.removeAll()
    }

    /// JSON-serializable snapshot — every value is a `Double` so this drops
    /// straight into `JSONSerialization` alongside the rest of a report dict.
    public static func snapshot() -> [String: [String: Double]] {
        lock.lock(); defer { lock.unlock() }
        var out: [String: [String: Double]] = [:]
        for (key, bucket) in buckets {
            out[key] = ["seconds": bucket.seconds, "count": Double(bucket.count), "units": Double(bucket.units)]
        }
        return out
    }
}
