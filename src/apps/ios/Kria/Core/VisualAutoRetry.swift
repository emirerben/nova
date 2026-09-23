import Foundation

/// Bounded automatic reanalysis for Visuals whose server-side analysis failed
/// transiently (`analysis_temporarily_unavailable`: a one-off Gemini 503, a
/// broker publish that didn't go through).
///
/// The Add-media sheet feeds every poll of `GET /plan-items/{id}/assets` into
/// `observe`, sends `POST …/reanalyze` for the ids it returns and reports each
/// outcome through `recordAttempt`. Nothing here re-uploads: a retry only asks
/// the server to analyze the object it already holds. Per asset and per sheet
/// session the scheduler makes at most `maximumAttempts` attempts, `delays`
/// apart, and never touches a failure the server marked non-retryable or
/// attributed to any other code. Those keep the manual Retry button (when
/// retryable) or ask the creator to choose the file again.
///
/// The manual Retry button goes through `beginManualRetry` too, so the sheet
/// never has two reanalyze requests on the wire for one asset: a tap during an
/// automatic attempt is refused, and no automatic attempt fires during a tap.
struct VisualAutoRetryScheduler: Equatable, Sendable {
    /// The only failure the sheet retries on its own: the server's transient
    /// provider/dispatch code. Other retryable codes stay a manual decision.
    static let retryableErrorCode = "analysis_temporarily_unavailable"
    static let maximumAttempts = 3
    /// Wait before attempt 1, 2 and 3, counted from the poll that saw the failure.
    static let delays: [TimeInterval] = [10, 20, 40]

    /// Who sent the reanalyze request that is still awaiting its response.
    enum Request: Equatable, Sendable { case automatic, manual }

    struct Entry: Equatable, Sendable {
        /// Automatic reanalyze requests already sent (or in flight) this session.
        /// Manual taps don't spend this budget.
        var attempts = 0
        /// When the next automatic attempt may fire; nil while none is scheduled.
        var dueAt: Date?
        /// Set from `observe` or `beginManualRetry` until `recordAttempt`.
        var inFlight: Request?
    }

    private(set) var entries: [String: Entry] = [:]

    static func qualifies(_ asset: CreationVisual) -> Bool {
        asset.status == "failed" && asset.retryable != false && asset.errorCode == retryableErrorCode
    }

    /// Whether the row should say an automatic retry is on its way.
    func isRetryPending(_ assetID: String) -> Bool {
        guard let entry = entries[assetID] else { return false }
        return entry.inFlight == .automatic || entry.dueAt != nil
    }

    /// False while a reanalyze request for this asset is awaiting its response.
    func canRetryManually(_ assetID: String) -> Bool { entries[assetID]?.inFlight == nil }

    func attempts(for assetID: String) -> Int { entries[assetID]?.attempts ?? 0 }

    /// Reconciles the scheduler with the latest pool snapshot and returns the
    /// assets whose automatic reanalyze is due now, marking them in flight so
    /// an overlapping poll can't fire them twice. Assets that left the pool
    /// forget their state; assets that recovered or fail for another reason
    /// lose any pending attempt but keep their session budget.
    mutating func observe(_ assets: [CreationVisual], now: Date) -> [String] {
        let present = Set(assets.map(\.id))
        entries = entries.filter { present.contains($0.key) }
        var due: [String] = []
        for asset in assets {
            var entry = entries[asset.id] ?? Entry()
            if entry.inFlight != nil { continue }
            guard Self.qualifies(asset) else {
                entry.dueAt = nil
                if entries[asset.id] != nil { entries[asset.id] = entry }
                continue
            }
            if let dueAt = entry.dueAt {
                if now >= dueAt {
                    entry.dueAt = nil
                    entry.inFlight = .automatic
                    entry.attempts += 1
                    due.append(asset.id)
                }
            } else if let delay = Self.delay(beforeAttempt: entry.attempts + 1) {
                entry.dueAt = now.addingTimeInterval(delay)
            }
            entries[asset.id] = entry
        }
        return due
    }

    /// Claims the asset for a manual Retry tap. Returns false, and changes
    /// nothing, while another reanalyze for it is in flight; otherwise any
    /// pending automatic attempt is dropped. Report the outcome through
    /// `recordAttempt`.
    mutating func beginManualRetry(_ assetID: String) -> Bool {
        var entry = entries[assetID] ?? Entry()
        guard entry.inFlight == nil else { return false }
        entry.inFlight = .manual
        entry.dueAt = nil
        entries[assetID] = entry
        return true
    }

    /// Records the outcome of an attempt `observe` returned or
    /// `beginManualRetry` allowed: the server's response, or nil when the
    /// request itself failed. A failed automatic request still spends budget,
    /// so a dead network can't turn into a loop. When the server answered with
    /// the same transient failure (its dispatch path fails synchronously), the
    /// next automatic attempt is scheduled right away if budget remains.
    mutating func recordAttempt(assetID: String, result: CreationVisual?, now: Date) {
        guard var entry = entries[assetID] else { return }
        entry.inFlight = nil
        entry.dueAt = nil
        if let result, Self.qualifies(result), let delay = Self.delay(beforeAttempt: entry.attempts + 1) {
            entry.dueAt = now.addingTimeInterval(delay)
        }
        entries[assetID] = entry
    }

    private static func delay(beforeAttempt attempt: Int) -> TimeInterval? {
        guard attempt >= 1, attempt <= maximumAttempts else { return nil }
        return delays[min(attempt, delays.count) - 1]
    }
}

extension CreationVisual {
    /// The line under the filename in the Add-media sheet. A failed row shows
    /// the server's explanation instead of a bare "Failed"; a failure the
    /// server won't retry asks the creator to choose the file again, and a
    /// pending automatic retry says so in place of the server's "Try again."
    func statusCaption(retryingAutomatically: Bool = false) -> String {
        guard status == "failed" else { return status.capitalized }
        var detail = errorDetail?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if retryable == false {
            return Self.sentences(detail.isEmpty ? "Failed." : detail, "Choose it again.")
        }
        guard retryingAutomatically else { return detail.isEmpty ? "Failed" : detail }
        if detail.hasSuffix("Try again.") {
            detail = String(detail.dropLast("Try again.".count)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return Self.sentences(detail.isEmpty ? "Failed." : detail, "Retrying automatically…")
    }

    private static func sentences(_ first: String, _ second: String) -> String {
        let terminal: Set<Character> = [".", "!", "?", "…"]
        let closed = first.last.map { terminal.contains($0) } == true ? first : first + "."
        return closed + " " + second
    }
}
