import Foundation

/// Where a Cinematic bake writes its working file before it replaces the take. The bake removes the file itself when it
/// ends, but killing the app mid-bake leaves it behind (measured on a 13 Pro: 22 to 44 MB after 1 to 4 s of baking, up
/// to roughly 120 MB for a late kill on a 60 s take). Working files live in their own folder so `sweepAbandoned` can
/// clear what an earlier process left without touching anything else, and it never touches a bake that is running now.
final class CinematicStaging: @unchecked Sendable {
    static let shared = CinematicStaging(base: FileManager.default.temporaryDirectory)

    private let directory: URL
    private let base: URL
    private let lock = NSLock()
    private var inUse = Set<String>()

    init(base: URL) {
        self.base = base
        directory = base.appendingPathComponent("CinematicBake", isDirectory: true)
    }

    /// A fresh path for one bake. It is protected from `sweepAbandoned` until `release`.
    func reserve() -> URL {
        let url = directory.appendingPathComponent("\(UUID().uuidString).mov")
        lock.lock(); defer { lock.unlock() }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        inUse.insert(url.lastPathComponent)
        return url
    }

    /// Ends a bake's claim on its working file and removes the file.
    func release(_ url: URL) {
        lock.lock(); defer { lock.unlock() }
        inUse.remove(url.lastPathComponent)
        try? FileManager.default.removeItem(at: url)
    }

    /// Removes working files no running bake owns. The count is how many were removed. The whole sweep holds the lock, so
    /// a bake reserving a path at the same moment waits instead of racing the listing.
    @discardableResult
    func sweepAbandoned() -> Int {
        lock.lock(); defer { lock.unlock() }
        let manager = FileManager.default
        func files(in folder: URL) -> [URL] {
            (try? manager.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        }
        // The earlier layout wrote `cinematic-<uuid>.mov` straight into the temporary folder.
        let legacy = files(in: base).filter { $0.lastPathComponent.hasPrefix("cinematic-") && $0.pathExtension == "mov" }
        let current = files(in: directory).filter { !inUse.contains($0.lastPathComponent) }
        return (legacy + current).reduce(0) { removed, url in removed + ((try? manager.removeItem(at: url)) == nil ? 0 : 1) }
    }
}
