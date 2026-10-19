import SwiftUI

/// Selects existing owned catalogue identities; this picker neither uploads nor attaches media.
struct CameraRollSavedPicker: View {
    let api: any KriaAPIClient
    let capacity: Int
    let onSelect: ([SavedMediaAsset]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var assets: [SavedMediaAsset] = []
    @State private var selected = Set<UUID>()
    @State private var cursor: String?
    @State private var query = ""
    @State private var loading = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            List {
                Text("Choose up to \(capacity) saved uploads for this collection.").font(.footnote).foregroundStyle(.secondary)
                ForEach(assets) { asset in
                    Button {
                        if selected.contains(asset.id) { selected.remove(asset.id) }
                        else if selected.count < capacity { selected.insert(asset.id) }
                    } label: {
                        HStack {
                            SavedUploadThumbnail(asset: asset, owner: api.mediaLibraryOwner, api: api) { }
                            Image(systemName: selected.contains(asset.id) ? "checkmark.circle.fill" : "circle")
                            VStack(alignment: .leading) {
                                Text(asset.displayName)
                                Text(asset.kind == "image" ? "Photo" : "Video").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }.accessibilityIdentifier("collection-saved-\(asset.id)")
                        .disabled(asset.source == nil || (!selected.contains(asset.id) && selected.count >= capacity))
                }
                if loading { ProgressView("Loading…") }
                if let error { Text(error).foregroundStyle(KriaColor.failureText); Button("Try again") { Task { await load(more: false) } } }
                if cursor != nil && !loading { Button("Load more") { Task { await load(more: true) } } }
            }
            .navigationTitle("Choose saved uploads").navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "Search filenames")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Add \(selected.count)") { onSelect(assets.filter { selected.contains($0.id) }); dismiss() }.disabled(selected.isEmpty).accessibilityIdentifier("collection-saved-add") }
            }
            .task(id: query) { do { try await Task.sleep(for: .milliseconds(200)); await load(more: false) } catch {} }
        }
    }
    @MainActor private func load(more: Bool) async {
        loading = true; error = nil
        defer { loading = false }
        do {
            let response = try await api.savedMedia(query: query, kind: nil, cursor: more ? cursor : nil)
            try Task.checkCancellation()
            let incoming = response.assets.filter { ["image", "video"].contains($0.kind) }
            // Selected assets stay available when the search changes.
            let retained = assets.filter { selected.contains($0.id) }
            let previous = more ? assets : retained
            assets = previous + incoming.filter { value in !previous.contains { $0.id == value.id } }
            cursor = response.nextCursor
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}
