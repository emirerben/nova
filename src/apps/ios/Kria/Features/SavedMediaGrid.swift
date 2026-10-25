import SwiftUI

/// What a saved-upload tile can do right now. The host decides (it knows the role, destination and what is
/// already attached); the grid only draws it and routes taps.
enum SavedTileState: Equatable {
    /// Can be picked: tap or slide toggles it into the numbered selection.
    case selectable
    /// Already part of this post. Shown ticked, never counted against the limit, never toggled.
    case inPost
    /// Being added right now (Done is committing the selection).
    case adding
    /// Can't be picked as is (needs its original, needs a restore, wrong media type). Dimmed; a tap goes to
    /// the host, which explains why or starts the repair.
    case blocked
}

/// The Photos gallery's look for saved uploads: square 4-column tiles, pick-order numbers, video length,
/// slide-to-select and a count footer. Selection is the same `LibraryGallerySelection` the Photos grid uses,
/// keyed by the asset id; nothing is attached or uploaded here, the host acts on `selected` when the creator
/// taps Done.
struct SavedMediaGrid<MenuContent: View>: View {
    let assets: [SavedMediaAsset]
    @Binding var selected: [UUID]
    let limit: Int
    let owner: String?
    let api: any KriaAPIClient
    let state: (SavedMediaAsset) -> SavedTileState
    /// Availability text read aloud with the tile and exposed to UI tests as its accessibility value.
    let detail: (SavedMediaAsset) -> String
    let tileID: (SavedMediaAsset) -> String
    /// Replaces the footer's count while set (committing, errors, paging).
    let message: String?
    let onBlockedTap: (SavedMediaAsset) -> Void
    let onNearEnd: () -> Void
    @ViewBuilder let menu: (SavedMediaAsset) -> MenuContent

    @State private var dragBase: LibraryGallerySelection?
    @State private var limitMessage: String?
    @State private var tapToken = 0
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 4)
    /// Ask for the next page this many tiles before the end, so scrolling never waits at the bottom.
    private static var prefetchDistance: Int { 8 }

    private var current: LibraryGallerySelection {
        LibraryGallerySelection(ids: selected.map(\.uuidString), limit: limit)
    }

    var body: some View {
        VStack(spacing: 0) {
            DragSelectScrollView(
                isEnabled: true,
                isSelected: { index in
                    guard index < assets.count else { return false }
                    return current.contains(assets[index].id.uuidString)
                },
                begin: { dragBase = current },
                apply: { indices, isOn in
                    guard let base = dragBase else { return }
                    var next = base
                    let touched = indices.filter { $0 < assets.count && state(assets[$0]) == .selectable }
                    next.apply(touched.map { assets[$0].id.uuidString }, selected: isOn)
                    write(next)
                },
                end: { dragBase = nil }
            ) {
                LazyVGrid(columns: columns, spacing: 2) {
                    ForEach(Array(assets.enumerated()), id: \.element.id) { index, asset in
                        tile(asset, index: index).dragSelectTile(index: index)
                    }
                }
            }
            .accessibilityIdentifier("saved-uploads-grid")
            GalleryFooter(
                text: message ?? limitMessage ?? "\(selected.count) of \(limit) selected · slide across to select",
                identifier: "saved-uploads-count"
            )
        }
        .sensoryFeedback(.selection, trigger: selected.count)
        .sensoryFeedback(.warning, trigger: tapToken)
    }

    private func write(_ next: LibraryGallerySelection) {
        let ids = next.ids.compactMap(UUID.init(uuidString:))
        guard ids != selected else { return }
        selected = ids
        limitMessage = nil
    }

    private func tap(_ asset: SavedMediaAsset, _ tileState: SavedTileState) {
        switch tileState {
        case .selectable:
            var next = current
            if next.toggle(asset.id.uuidString) == .refusedAtLimit {
                limitMessage = "You can choose up to \(limit) here."
                tapToken += 1
                return
            }
            write(next)
        case .inPost:
            limitMessage = "Already in this post."
            tapToken += 1
        case .adding:
            break
        case .blocked:
            limitMessage = nil
            onBlockedTap(asset)
        }
    }

    private func tile(_ asset: SavedMediaAsset, index: Int) -> some View {
        let tileState = state(asset)
        let number = current.number(of: asset.id.uuidString)
        return Button { tap(asset, tileState) } label: {
            Color.clear
                .aspectRatio(1, contentMode: .fit)
                .overlay { SavedUploadThumbnail(asset: asset, owner: owner, api: api, size: nil) }
                .overlay(alignment: .bottomLeading) {
                    if asset.kind == "audio" {
                        Text(asset.displayName)
                            .font(KriaFont.body(11).weight(.medium)).foregroundStyle(KriaColor.ink).lineLimit(2)
                            .padding(6)
                    }
                }
                .clipped()
                .overlay { if tileState == .blocked { Color.white.opacity(0.55) } }
                .overlay(alignment: .bottomLeading) {
                    if tileState == .blocked {
                        Image(systemName: "exclamationmark.circle.fill")
                            .font(.system(size: 14)).foregroundStyle(KriaColor.failureText).padding(5)
                    }
                }
                .overlay { if tileState == .adding { ProgressView().padding(8).background(.regularMaterial, in: Circle()) } }
                .modifier(GalleryTileChrome(number: number, duration: asset.durationSeconds, isLocked: tileState == .inPost))
        }
        .buttonStyle(.plain)
        .contextMenu { menu(asset) }
        .onAppear { if index >= assets.count - Self.prefetchDistance { onNearEnd() } }
        .accessibilityLabel("\(Self.kindLabel(asset)), \(asset.displayName)")
        .accessibilityValue("\(Self.selectionLabel(number: number, state: tileState)). \(detail(asset))")
        .accessibilityAddTraits(number != nil || tileState == .inPost ? .isSelected : [])
        .accessibilityIdentifier(tileID(asset))
    }

    private static func kindLabel(_ asset: SavedMediaAsset) -> String {
        switch asset.kind {
        case "video": asset.durationSeconds.map { "Video, \(GalleryTileChrome.duration($0))" } ?? "Video"
        case "audio": "Audio"
        default: "Photo"
        }
    }

    private static func selectionLabel(number: Int?, state: SavedTileState) -> String {
        if state == .inPost { return "In this post" }
        return number.map { "Selected, \($0)" } ?? "Not selected"
    }
}
