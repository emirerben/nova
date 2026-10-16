import Foundation
import OSLog

/// Performance intervals for launch, screen-open, network, scroll and editor work.
///
/// Everything here is `os_signpost`, so it costs close to nothing when nobody is recording and
/// is compiled into every configuration. `KriaPerfUITests` reads the intervals through
/// `XCTOSSignpostMetric`; `scripts/ios/bench-device.sh` reads them from an `xctrace` recording on
/// a real phone. Subsystem `com.kria.app`, category `perf`.
///
/// Interval names are the contract with those readers. Rename one and `bench_report.py` loses it.
enum Perf {
    static let subsystem = "com.kria.app"
    static let category = "perf"
    private static let signposter = OSSignposter(subsystem: subsystem, category: category)
    private static let registry = Registry()

    /// Opens a named interval. `id` separates concurrent intervals of the same name (one per poster, per request).
    /// A second `begin` for a name+id that is still open is ignored, so a view's `.task` re-running cannot
    /// restart a launch or open timer.
    static func begin(_ name: StaticString, id: String = "", _ detail: String = "") {
        let key = Registry.key(name, id)
        guard registry.reserve(key) else { return }
        let state = signposter.beginInterval(name, id: signposter.makeSignpostID(), "\(detail, privacy: .public)")
        registry.store(key, state)
    }

    /// Closes the interval opened by `begin`. A no-op when none is open, which makes "first time X happens"
    /// call sites trivial: call `end` wherever X can happen, and only the first call after `begin` records.
    static func end(_ name: StaticString, id: String = "", _ detail: String = "") {
        guard let state = registry.take(Registry.key(name, id)) else { return }
        signposter.endInterval(name, state, "\(detail, privacy: .public)")
    }

    /// Closes `name` once it has been ticked `needed` times ("the first 6 posters are drawn").
    static func tick(_ name: StaticString, id: String = "", needed: Int, _ detail: String = "") {
        let key = Registry.key(name, id)
        guard registry.tick(key, needed: needed) else { return }
        end(name, id: id, detail)
    }

    /// Milestone for the launch timeline: records milliseconds since `KriaApp.init` the FIRST time `name` is reached,
    /// readable in the Debug `perf-counters` element as `t <name>`. Release builds compile it to nothing.
    static func mark(_ name: String) {
        #if DEBUG
        PerfCounters.shared.setOnce("t \(name)", Int((ProcessInfo.processInfo.systemUptime - origin) * 1000))
        #endif
    }
    #if DEBUG
    private static let origin = ProcessInfo.processInfo.systemUptime
    #endif

    /// A point-in-time marker (a rebuild, a cache hit).
    static func event(_ name: StaticString, _ detail: String = "") {
        signposter.emitEvent(name, "\(detail, privacy: .public)")
        #if DEBUG
        PerfCounters.shared.increment(String(describing: name))
        #endif
    }

    /// Times a synchronous block.
    static func measure<T>(_ name: StaticString, _ detail: String = "", _ body: () throws -> T) rethrows -> T {
        let state = signposter.beginInterval(name, id: signposter.makeSignpostID(), "\(detail, privacy: .public)")
        defer { signposter.endInterval(name, state) }
        return try body()
    }

    /// Times an async block.
    static func measureAsync<T>(_ name: StaticString, _ detail: String = "", _ body: () async throws -> T) async rethrows -> T {
        let state = signposter.beginInterval(name, id: signposter.makeSignpostID(), "\(detail, privacy: .public)")
        defer { signposter.endInterval(name, state) }
        return try await body()
    }

    /// `GET /creation-threads/<uuid>/delta` -> `GET /creation-threads/:id/delta`, so intervals group by route.
    static func routeTemplate(method: String?, path: String) -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true).map { part -> String in
            UUID(uuidString: String(part)) != nil ? ":id" : String(part)
        }
        return "\(method ?? "GET") /" + parts.joined(separator: "/")
    }

    private final class Registry: @unchecked Sendable {
        private let lock = NSLock()
        private var open: [String: OSSignpostIntervalState] = [:]
        private var reserved: Set<String> = []

        static func key(_ name: StaticString, _ id: String) -> String { "\(name)#\(id)" }

        private var ticks: [String: Int] = [:]

        /// True on the tick that reaches `needed`; false before it, and false when the interval is not open.
        func tick(_ key: String, needed: Int) -> Bool {
            lock.lock(); defer { lock.unlock() }
            guard open[key] != nil else { return false }
            ticks[key, default: 0] += 1
            guard ticks[key, default: 0] >= needed else { return false }
            ticks[key] = nil
            return true
        }

        func reserve(_ key: String) -> Bool {
            lock.lock(); defer { lock.unlock() }
            return reserved.insert(key).inserted
        }
        func store(_ key: String, _ state: OSSignpostIntervalState) {
            lock.lock(); defer { lock.unlock() }
            open[key] = state
        }
        func take(_ key: String) -> OSSignpostIntervalState? {
            lock.lock(); defer { lock.unlock() }
            reserved.remove(key)
            return open.removeValue(forKey: key)
        }
    }
}

#if DEBUG
/// Counts of what the app did, for UI tests to assert on ("the poster went to the network 0 times on reopen",
/// "the timeline was rebuilt once per data change"). Debug only: the counts are read through the fixture host's
/// invisible `perf-counters` element, which does not exist in a Release build.
final class PerfCounters: @unchecked Sendable {
    static let shared = PerfCounters()
    private let lock = NSLock()
    private var counts: [String: Int] = [:]

    func increment(_ key: String, by amount: Int = 1) {
        lock.lock(); defer { lock.unlock() }
        counts[key, default: 0] += amount
    }

    func setOnce(_ key: String, _ value: Int) {
        lock.lock(); defer { lock.unlock() }
        if counts[key] == nil { counts[key] = value }
    }

    func snapshot() -> [String: Int] {
        lock.lock(); defer { lock.unlock() }
        return counts
    }

    func reset() {
        lock.lock(); defer { lock.unlock() }
        counts.removeAll()
    }

    /// `key=count` lines sorted by key: the label of the `perf-counters` element.
    var summary: String {
        snapshot().sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: "\n")
    }
}
#endif
