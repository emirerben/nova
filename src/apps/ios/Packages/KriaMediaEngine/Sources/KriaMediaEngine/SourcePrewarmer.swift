import Foundation

/// Hashes a recorded file and builds its analysis proxy while the creator is still deciding whether to use it, so the
/// upload that follows finds both ready instead of starting them.
///
/// Work is keyed by the file's identity (`FileIdentity`), never by its path. The upload stages the take under a new name
/// (a hardlink, so the same inode), and a take that was replaced or edited since it was prewarmed has a different
/// identity and simply misses. A miss costs nothing but the work the upload would have done anyway.
///
/// `claim` hands the running work to exactly one caller and forgets it, so the upload waits for what is already in
/// flight rather than starting a second copy, and a retry after a failure starts clean.
public final class SourcePrewarmer: @unchecked Sendable {
    public typealias ProxyMaker = @Sendable (_ source: URL, _ destination: URL) async throws -> URL

    fileprivate final class Job: @unchecked Sendable {
        let source: URL
        let identity: FileIdentity
        let proxyURL: URL
        var hash: Task<KnownFingerprint?, Never>!
        var proxy: Task<URL?, Never>!
        private let lock = NSLock()
        private var delivered = false

        init(source: URL, identity: FileIdentity, proxyURL: URL) {
            self.source = source; self.identity = identity; self.proxyURL = proxyURL
        }

        func markDelivered() { lock.lock(); delivered = true; lock.unlock() }

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
            let url = await withTaskCancellationHandler { await job.proxy.value } onCancel: { job.cancel() }
            if url != nil { job.markDelivered() }
            return url
        }

        /// Gives up whatever was not taken. Safe to call after `takeProxy`.
        public func discard() { job.cancel() }
    }

    private let directory: URL
    private let makeProxy: ProxyMaker
    private let lock = NSLock()
    private var jobs: [UUID: Job] = [:]

    /// `directory` is wiped here. Nothing in it can belong to a live job (this instance has none yet) and a previous
    /// process's proxies are unreachable after a relaunch, so keeping them would only leak disk.
    public init(directory: URL, makeProxy: @escaping ProxyMaker) {
        self.directory = directory
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
                      proxyURL: directory.appendingPathComponent("\(UUID().uuidString).mp4"))
        let proxyMaker = makeProxy
        job.hash = Task.detached(priority: .utility) {
            guard !Task.isCancelled else { return nil }
            let fingerprint = try? EngineSignpost.measure("talk.prewarm.hash") { try SHA256Fingerprinter().fingerprint(file: source) }
            // The bytes hashed are the bytes at `identity` only if the file did not move under the read.
            guard let fingerprint, !Task.isCancelled, fingerprint.byteCount == identity.size,
                  (try? FileIdentity(of: source)) == identity else { return nil }
            return KnownFingerprint(identity: identity, fingerprint: fingerprint)
        }
        let proxyURL = job.proxyURL
        job.proxy = Task.detached(priority: .utility) {
            guard !Task.isCancelled else { return nil }
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

#if canImport(AVFoundation)
public extension SourcePrewarmer {
    /// The app's instance: proxies at the analysis preset, in the caches directory (the system may reclaim it).
    static let shared = SourcePrewarmer(
        directory: FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("KriaTakePrewarm", isDirectory: true),
        makeProxy: { source, destination in
            try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: source, destination: destination)
        })
}
#endif
