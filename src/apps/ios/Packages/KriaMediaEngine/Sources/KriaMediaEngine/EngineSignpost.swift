import Foundation
import OSLog

/// The engine's share of the app's `Perf` intervals: same subsystem and category, so one `xctrace` recording or `log`
/// stream holds both. The engine cannot import the app, so `observer` is how the app's bench recorder hears the same
/// intervals. Names are a contract with `scripts/ios/talk-bench.sh`.
public enum EngineSignpost {
    private static let signposter = OSSignposter(subsystem: "com.kria.app", category: "perf")

    /// `(name, milliseconds, detail)`; milliseconds is nil for a counter tick. Set once at launch by a debug bench
    /// recorder, nil in every shipped build.
    nonisolated(unsafe) public static var observer: (@Sendable (String, Double?, String) -> Void)?

    /// `detail` is read after `body` ran, so it can carry what the work found out (bytes hashed, frames held).
    static func measure<T>(_ name: StaticString, detail: @autoclosure () -> String = "", _ body: () throws -> T) rethrows -> T {
        let state = signposter.beginInterval(name, id: signposter.makeSignpostID())
        let start = observer == nil ? 0 : DispatchTime.now().uptimeNanoseconds
        defer {
            let text = detail()
            signposter.endInterval(name, state, "\(text, privacy: .public)")
            if let observer {
                observer(String(describing: name), Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000, text)
            }
        }
        return try body()
    }

    static func measureAsync<T>(_ name: StaticString, isolation: isolated (any Actor)? = #isolation, _ body: () async throws -> T) async rethrows -> T {
        let interval = begin(name)
        defer { interval.end() }
        return try await body()
    }

    /// An interval that spans `await`s or several scopes. Call `end()` exactly once.
    struct Interval {
        fileprivate let name: StaticString
        fileprivate let state: OSSignpostIntervalState
        fileprivate let start: UInt64

        func end(_ detail: String = "") {
            EngineSignpost.signposter.endInterval(name, state, "\(detail, privacy: .public)")
            EngineSignpost.observer?(String(describing: name), Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000, detail)
        }
    }

    static func begin(_ name: StaticString) -> Interval {
        Interval(name: name, state: signposter.beginInterval(name, id: signposter.makeSignpostID()),
                 start: DispatchTime.now().uptimeNanoseconds)
    }

    /// Wall time of one stage of an export frame (`talk.stage.*`), for the Save benchmark. Costs a nil check unless `observer` is set.
    @inline(__always)
    static func stage(_ name: String, since start: UInt64, to end: UInt64? = nil) {
        guard let observer else { return }
        observer(name, Double((end ?? DispatchTime.now().uptimeNanoseconds) &- start) / 1_000_000, "")
    }

    static func count(_ name: StaticString, _ detail: String = "") {
        signposter.emitEvent(name, "\(detail, privacy: .public)")
        observer?(String(describing: name), nil, detail)
    }
}
