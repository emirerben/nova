import SwiftUI

/// `AsyncImage` that remembers pictures by identity (`SlidePostImageCache.library`). A poster's signed URL changes on
/// every fetch of the library, so `AsyncImage` (keyed by URL) downloaded every poster again each time the gallery
/// opened; here the same picture is found in memory, then on disk, and only then fetched.
struct CachedPosterImage<Content: View>: View {
    let url: URL
    /// Stable identity of the picture (`ProjectSummary.posterCacheKey`), never the URL.
    let key: String
    let content: (AsyncImagePhase) -> Content
    @State private var phase: AsyncImagePhase
    @State private var shownKey: String?

    init(url: URL, key: String, @ViewBuilder content: @escaping (AsyncImagePhase) -> Content) {
        self.url = url
        self.key = key
        self.content = content
        // A picture already decoded in memory is on the first frame, with no placeholder in between.
        if let hit = SlidePostImageCache.library.cachedImage(assetID: key, variant: .poster) {
            _phase = State(initialValue: .success(Image(uiImage: hit)))
            _shownKey = State(initialValue: key)
        } else {
            _phase = State(initialValue: .empty)
            _shownKey = State(initialValue: nil)
        }
    }

    var body: some View {
        content(phase).task(id: key) { await load() }
    }

    private func load() async {
        guard shownKey != key else { return }
        phase = .empty
        do {
            let image = try await SlidePostImageCache.library.image(assetID: key, variant: .poster, url: url)
            phase = .success(Image(uiImage: image))
            shownKey = key
        } catch {
            phase = .failure(error)
        }
    }
}
