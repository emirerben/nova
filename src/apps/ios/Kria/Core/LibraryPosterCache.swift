import Foundation

extension SlidePostImageCache {
    /// Gallery posters, keyed by poster identity: a freshly signed URL for a poster already seen never downloads it
    /// again. Lives in Caches (the system may reclaim it) and is removed on sign-out.
    static let library = SlidePostImageCache(
        directory: FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appending(path: "LibraryPosters", directoryHint: .isDirectory),
        fetch: { url in
            #if DEBUG
            if let data = try await PerfFixture.posterData(for: url) { return data }
            #endif
            return try await SlidePostImageCache.defaultFetch(url)
        }
    )
}

extension ProjectSummary {
    /// What identifies this poster's picture. The server's poster identity when it sends one (it changes when the
    /// selected render or frame does); otherwise the object path of the signed URL, which stays the same across
    /// re-signs. Nil without a poster.
    var posterCacheKey: String? {
        guard let posterURL else { return nil }
        return "\(id.uuidString)|\(posterIdentity ?? posterURL.path)"
    }
}
