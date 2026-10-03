import Foundation
import Network

/// KRI-294: what an uploaded Visual is doing before Kria can use it, in words a
/// creator understands.
///
/// After upload the server analyzes each Visual (`queued` → `analyzing` →
/// `ready`) on a small shared worker queue. That wait used to read as a bare
/// "Queued" with nothing moving, so a batch of photos looked stalled and the
/// creator had no idea whether to wait, retry or leave. It can genuinely run
/// to minutes: the queue is shared with footage analysis, and the worker
/// stops taking work for about four minutes on every API deploy. The Add-media
/// sheet and the editor's Visuals library therefore show what each Visual is
/// doing, a batch progress bar while any are pending, and, once the wait runs
/// long, what the creator can do in the meantime.
enum VisualPreparationStage: Equatable, Sendable {
    /// Stored on the server; analysis hasn't started (`uploaded`, `queued`, `pending`).
    case waiting
    /// The server is analyzing it (`analyzing`, `processing`).
    case analyzing
}

extension CreationVisual {
    /// Nil once the Visual is ready or failed, and for statuses this build doesn't know.
    var preparationStage: VisualPreparationStage? {
        switch status {
        case "uploaded", "queued", "pending": .waiting
        case "analyzing", "processing": .analyzing
        default: nil
        }
    }

    /// The status line for a Visual that is still preparing: the full form
    /// under a filename in the Add-media sheet, the short form on the
    /// editor's narrow library tile.
    func preparationCaption(short: Bool = false) -> String? {
        switch preparationStage {
        case .waiting: short ? "Waiting…" : "Uploaded, waiting to be analyzed"
        case .analyzing: "Analyzing…"
        case nil: nil
        }
    }
}

/// Remembers when each Visual was first seen still preparing, so a wait that
/// runs long can say so. One per owner (an Add-media sheet, or an editor
/// session), fed with every poll of the Visuals pool.
struct VisualPreparationClock: Equatable, Sendable {
    /// After this long still preparing, the summary switches to its
    /// "taking longer than usual" copy and says what to do meanwhile.
    static let slowAfter: TimeInterval = 60

    private(set) var firstSeen: [String: Date] = [:]
    private(set) var slowIDs: Set<String> = []

    /// Starts the clock for newly preparing Visuals and forgets the ones that
    /// finished, failed or left the pool. A Visual that fails and is analyzed
    /// again starts a fresh wait.
    mutating func observe(_ assets: [CreationVisual], now: Date) {
        var seen: [String: Date] = [:]
        for asset in assets where asset.preparationStage != nil {
            seen[asset.id] = firstSeen[asset.id] ?? now
        }
        firstSeen = seen
        slowIDs = Set(seen.filter { now.timeIntervalSince($0.value) >= Self.slowAfter }.keys)
    }
}

/// The batch line above the Visuals while any are still uploading or
/// preparing: how many are ready, a determinate progress bar, and what to
/// expect. Nil when there is no wait to explain.
struct VisualPreparationSummary: Equatable, Sendable {
    /// Where the summary is shown; only the "meanwhile" advice differs.
    enum Surface: Sendable { case addMediaSheet, editorLibrary }

    let ready: Int
    let total: Int
    let isSlow: Bool
    /// Files are still uploading and the phone has no connection: they wait,
    /// and the background upload resumes by itself once it's back.
    let waitingForConnection: Bool
    let noun: String
    let surface: Surface

    /// - Parameters:
    ///   - assets: the pool as last polled. Failed Visuals are left out: each
    ///     explains itself on its own row, and counting them would keep the bar
    ///     from ever filling.
    ///   - uploading: chosen files still uploading, which the pool doesn't list yet.
    init?(assets: [CreationVisual], uploading: Int = 0, online: Bool = true, slowIDs: Set<String>, surface: Surface) {
        let counted = assets.filter { $0.status == "ready" || $0.preparationStage != nil }
        let ready = counted.filter { $0.status == "ready" }.count
        let total = counted.count + max(0, uploading)
        guard total > ready else { return nil }
        self.ready = ready
        self.total = total
        self.isSlow = counted.contains { $0.preparationStage != nil && slowIDs.contains($0.id) }
        self.waitingForConnection = !online && uploading > 0
        let kinds = Set(counted.map(\.kind))
        self.noun = kinds == ["image"] ? "photos" : kinds == ["video"] ? "videos" : "visuals"
        self.surface = surface
    }

    var fraction: Double { total > 0 ? Double(ready) / Double(total) : 0 }

    var title: String { isSlow ? "Still getting your \(noun) ready" : "Getting your \(noun) ready" }

    var count: String { "\(ready) of \(total) ready" }

    /// A Visual still preparing can't be placed in the editor, and a phone
    /// render approved while one is preparing is refused (`visuals_processing`),
    /// so the slow copy never suggests creating the video before they're ready.
    var detail: String {
        if waitingForConnection {
            return "Waiting for an internet connection. Uploads pick up again on their own once you’re back online."
        }
        guard isSlow else {
            return "Kria looks at each one before it can use it. This usually takes under a minute."
        }
        switch surface {
        case .addMediaSheet:
            return "This is taking longer than usual. Uploaded files are safe, so you can close this and keep chatting. Wait until they’re ready before you create the video."
        case .editorLibrary:
            return "This is taking longer than usual. Uploaded files are safe and unlock here once they’re ready. You can keep editing meanwhile."
        }
    }

    var accessibilityLabel: String { title + ", " + count + ". " + detail }
}

/// The line under an upload's progress bar. Background uploads wait silently
/// while the phone is offline, so without it a bar stuck at zero looked like
/// a broken upload rather than a missing connection.
enum UploadProgressCaption {
    static func text(progress: Double?, completed: Bool, online: Bool) -> String {
        let fraction = min(max(progress ?? 0, 0), 1)
        if completed || fraction >= 1 { return "Finishing up…" }
        if !online { return "Waiting for an internet connection…" }
        if fraction <= 0 { return "Waiting to upload…" }
        return "Uploading… \(Int((fraction * 100).rounded(.down)))%"
    }
}

/// Whether the phone has a usable network path, for upload captions only.
/// Starts optimistic, so a slow first callback never flashes "offline".
@MainActor final class NetworkReachability: ObservableObject {
    static let shared = NetworkReachability()

    @Published private(set) var isOnline = true
    private let monitor = NWPathMonitor()

    private init() {
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            Task { @MainActor [weak self] in
                guard let self, self.isOnline != online else { return }
                self.isOnline = online
            }
        }
        monitor.start(queue: DispatchQueue(label: "kria.network-reachability"))
    }
}
