import Foundation

/// Hashes a recorded file and builds its analysis proxy while the creator is still deciding whether to use it, so the
/// upload that follows finds both ready instead of starting them.
///
/// Work is keyed by the file's identity (`FileIdentity`), never by its path. The upload stages the take under a new name
/// (a hardlink, so the same inode), and a take that was replaced or edited since it was prewarmed has a different
/// identity and simply misses. The upload then does the work itself, and `cancel(source:)` stops the orphaned job.
///
/// The proxy export waits for the same admission as the upload's own export (`Admission`): more than one
/// `AVAssetExportSession` at a time invites `AVError.operationInterrupted` on older devices, and a take's prewarm must
/// not run next to a Photos clip's proxy or another take's.
///
/// `claim` hands the running work to exactly one caller and forgets it, so the upload waits for what is already in
/// flight rather than starting a second copy, and a retry after a failure starts clean.
public final class SourcePrewarmer: @unchecked Sendable {
    public typealias ProxyMaker = @Sendable (_ source: URL, _ destination: URL) async throws -> URL

    /// One slot of the app's export limit. `acquire` throws when the waiting task is cancelled; `release` must follow
    /// every `acquire` that returned.
    public struct Admission: Sendable {
        public let acquire: @Sendable () async throws -> Void
        public let release: @Sendable () -> Void
        public init(acquire: @escaping @Sendable () async throws -> Void, release: @escaping @Sendable () -> Void) {
            self.acquire = acquire; self.release = release
        }
    }

    fileprivate final class Job: @unchecked Sendable {
        let source: URL
        let identity: FileIdentity
        let proxyURL: URL
        var hash: Task<KnownFingerprint?, Never>!
        var proxy: Task<URL?, Never>!
        private let lock = NSLock()
        private var delivered = false
        private var admitted: Bool

        init(source: URL, identity: FileIdentity, proxyURL: URL, gated: Bool) {
            self.source = source; self.identity = identity; self.proxyURL = proxyURL; admitted = !gated
        }

        func markDelivered() { lock.lock(); delivered = true; lock.unlock() }
        func markAdmitted() { lock.lock(); admitted = true; lock.unlock() }
        var isAdmitted: Bool { lock.lock(); defer { lock.unlock() }; return admitted }

        /// Stops both tasks and deletes a proxy nobody took.
        func cancel() {
            hash.cancel()
            proxy.cancel()
            lock.lock(); let keep = delivered; lock.unlock()
            if !keep { try? FileManager.default.removeItem(at: proxyURL) }
        }
    }

    /// What a caller holds after `claim`. The proxy file belongs to the caller once `takeProxy` returns it.
    public final class Claim: @unchecked Sendable {
        public let identity: FileIdentity
        fileprivate let job: Job
        fileprivate init(job: Job) { self.job = job; identity = job.identity }

        /// The digest of the bytes the claim was made for; nil when the hash failed or the file changed while it ran.
        public func fingerprint() async -> KnownFingerprint? {
            await withTaskCancellationHandler { await job.hash.value } onCancel: { job.cancel() }
        }

        /// The finished proxy, or nil when the transcode failed, was cancelled, or the file changed while it ran.
        public func takeProxy() async -> URL? {
            // A claimant holds the export slot a still-queued prewarm waits for, so waiting on it would never end;
            // the claimant transcodes in its own slot instead.
            if !job.isAdmitted { job.proxy.cancel() }
            let url = await withTaskCancellationHandler { await job.proxy.value } onCancel: { job.cancel() }
            if url != nil { job.markDelivered() }
            return url
        }

        /// Gives up whatever was not taken. Safe to call after `takeProxy`.
        public func discard() { job.cancel() }
    }

    private let directory: URL
    private let makeProxy: ProxyMaker
    private let admission: Admission?
    private let lock = NSLock()
    private var jobs: [UUID: Job] = [:]

    /// `directory` is wiped here. Nothing in it can belong to a live job (this instance has none yet) and a previous
    /// process's proxies are unreachable after a relaunch, so keeping them would only leak disk.
    public init(directory: URL, admission: Admission? = nil, makeProxy: @escaping ProxyMaker) {
        self.directory = directory
        self.admission = admission
        self.makeProxy = makeProxy
        try? FileManager.default.removeItem(at: directory)
    }

    /// Starts hashing and transcoding `source` for `key` (the take's id). A second call for the same `key` and the same
    /// file does nothing; for a different file it replaces the first job, cancelling it.
    public func start(key: UUID, source: URL) {
        guard let identity = try? FileIdentity(of: source), identity.size > 0 else { return }
        lock.lock(); defer { lock.unlock() }
        if let existing = jobs[key] {
            if existing.identity == identity { return }
            existing.cancel()
            jobs[key] = nil
        }
        let job = Job(source: source, identity: identity,
                      proxyURL: directory.appendingPathComponent("\(UUID().uuidString).mp4"), gated: admission != nil)
        let proxyMaker = makeProxy
        let admission = admission
        job.hash = Task.detached(priority: .utility) {
            guard !Task.isCancelled else { return nil }
            let fingerprint = try? EngineSignpost.measure("talk.prewarm.hash") { try SHA256Fingerprinter().fingerprint(file: source) }
            // The bytes hashed are the bytes at `identity` only if the file did not move under the read.
            guard let fingerprint, !Task.isCancelled, fingerprint.byteCount == identity.size,
                  (try? FileIdentity(of: source)) == identity else { return nil }
            return KnownFingerprint(identity: identity, fingerprint: fingerprint)
        }
        let proxyURL = job.proxyURL
        job.proxy = Task.detached(priority: .utility) { [job] in
            guard !Task.isCancelled else { return nil }
            if let admission {
                // Cancelled while queued (Retake, or a claimant that transcodes itself): leaves the queue, holds nothing.
                do { try await admission.acquire() } catch { return nil }
            }
            defer { admission?.release() }
            job.markAdmitted()
            let interval = EngineSignpost.begin("talk.prewarm.proxy")
            do {
                let output = try await proxyMaker(source, proxyURL)
                guard !Task.isCancelled, (try? FileIdentity(of: source)) == identity else {
                    try? FileManager.default.removeItem(at: proxyURL)
                    interval.end("discarded")
                    return nil
                }
                interval.end("ready")
                return output
            } catch {
                try? FileManager.default.removeItem(at: proxyURL)
                interval.end("failed")
                return nil
            }
        }
        jobs[key] = job
    }

    /// Drops the job for `key` (Retake, Discard). Running work is cancelled and its proxy deleted.
    public func cancel(key: UUID) {
        lock.lock(); let job = jobs.removeValue(forKey: key); lock.unlock()
        job?.cancel()
    }

    /// Drops the job prewarming the file at `source`, for an upload that staged a copy and so can never claim it.
    public func cancel(source: URL) {
        let path = source.standardizedFileURL.path
        lock.lock()
        let doomed = jobs.filter { $0.value.source.standardizedFileURL.path == path }
        doomed.keys.forEach { jobs[$0] = nil }
        lock.unlock()
        doomed.values.forEach { $0.cancel() }
    }

    public func cancelAll() {
        lock.lock(); let all = Array(jobs.values); jobs.removeAll(); lock.unlock()
        all.forEach { $0.cancel() }
    }

    /// Takes over the job whose file has `source`'s identity, if any. Nil for a file that was never prewarmed, was
    /// replaced or edited since, or was already claimed.
    public func claim(source: URL) -> Claim? {
        guard let identity = try? FileIdentity(of: source) else { return nil }
        lock.lock(); defer { lock.unlock() }
        guard let entry = jobs.first(where: { $0.value.identity == identity }) else { return nil }
        jobs[entry.key] = nil
        return Claim(job: entry.value)
    }

    /// For tests: how many jobs are held.
    public var heldCount: Int { lock.lock(); defer { lock.unlock() }; return jobs.count }
}
