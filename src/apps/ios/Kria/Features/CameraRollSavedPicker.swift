import SwiftUI

/// Selects existing owned catalogue identities; this picker neither uploads nor attaches media.
/// Same grid as the post's Saved uploads sheet, so both pickers look and behave like the Photos gallery.
struct CameraRollSavedPicker: View {
    let api: any KriaAPIClient
    let capacity: Int
    let onSelect: ([SavedMediaAsset]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var assets: [SavedMediaAsset] = []
    /// Picks stay available when a search no longer lists them.
    @State private var pool: [UUID: SavedMediaAsset] = [:]
    @State private var selected: [UUID] = []
    @State private var cursor: String?
    @State private var query = ""
    @State private var loading = false
    @State private var hasLoadedOnce = false
    @State private var error: String?
    @State private var notice: String?
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if let error {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                        Spacer(minLength: 0)
                        Button("Try again") { Task { await load(more: !assets.isEmpty) } }
                            .font(KriaFont.body(13).weight(.semibold))
                    }
                    .padding(.horizontal, 16).padding(.vertical, 8)
                }
                if assets.isEmpty {
                    if loading { ProgressView("Loading…").frame(maxHeight: .infinity) }
                    else if error == nil {
                        ContentUnavailableView(query.isEmpty ? "No saved uploads yet" : "No matching uploads", systemImage: "square.stack")
                            .frame(maxHeight: .infinity)
                    } else { Spacer(minLength: 0) }
                } else {
                    SavedMediaGrid(
                        assets: assets, selected: $selected, limit: capacity, owner: api.mediaLibraryOwner, api: api,
                        state: { $0.source == nil ? .blocked : .selectable },
                        detail: { $0.source == nil ? "Cloud copy unavailable" : "Available to reuse" },
                        tileID: { "collection-saved-\($0.id)" },
                        message: notice ?? (loading ? "Loading more…" : nil),
                        onBlockedTap: { _ in notice = "This upload’s cloud copy is unavailable." },
                        onNearEnd: { if cursor != nil, !loading, error == nil { Task { await load(more: true) } } }
                    ) { _ in EmptyView() }
                }
            }
            .navigationTitle("Choose saved uploads").navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "Search filenames")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add \(selected.count)") { onSelect(selected.compactMap { pool[$0] }); dismiss() }
                        .disabled(selected.isEmpty).accessibilityIdentifier("collection-saved-add")
                }
            }
            .task(id: query) {
                if hasLoadedOnce { do { try await Task.sleep(for: .milliseconds(200)) } catch { return } }
                await load(more: false)
                hasLoadedOnce = true
            }
        }
    }
    @MainActor private func load(more: Bool) async {
        loading = true; error = nil; notice = nil
        defer { loading = false }
        do {
            let response = try await api.savedMedia(query: query, kind: nil, cursor: more ? cursor : nil)
            try Task.checkCancellation()
            let incoming = response.assets.filter { ["image", "video"].contains($0.kind) }
            for asset in incoming { pool[asset.id] = asset }
            // Selected assets stay listed when the search changes.
            let retained = assets.filter { selected.contains($0.id) }
            let previous = more ? assets : retained
            assets = previous + incoming.filter { value in !previous.contains { $0.id == value.id } }
            cursor = response.nextCursor
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}
