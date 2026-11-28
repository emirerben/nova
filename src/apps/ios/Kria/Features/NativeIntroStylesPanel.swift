import SwiftUI

/// KRI-1633: "Try intro styles". A strip of fitted looks for the video's title, each drawn on this iPhone over her own
/// clip (`DesignPreviewRenderer`, the A1-B1 picker's renderer), plus "No intro style". Tapping a tile swaps the title
/// rows in the editor and plays them; Save keeps them. Rows, not previews, are what gets applied.
struct NativeIntroStylesPanel: View {
    @ObservedObject var session: NativeEditorSession
    let projectID: UUID?
    let onDone: () -> Void
    @Environment(\.nativeEditorConnectedPanel) private var connected

    static let tile = CGSize(width: 108, height: 192)

    var body: some View {
        VStack(spacing: 6) {
            HStack {
                Text("Intro styles").font(KriaFont.body(connected ? 18 : 15).weight(.semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button {
                    onDone()
                } label: {
                    Text("Done").frame(minWidth: 64, minHeight: 44)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                }
                .accessibilityIdentifier("native-editor-intro-styles-done")
            }
            .nativeEditorPanelResizeSurface()
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .padding(.horizontal, connected ? 24 : 16)
        .padding(.bottom, 8)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(connected ? Color.clear : KriaColor.paper)
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        // `.contain` keeps the children's own identifiers (a bare identifier here would overwrite theirs).
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-intro-styles-panel")
        .task(id: session.introStylesContext()) { await session.loadIntroStyles() }
    }

    @ViewBuilder private var content: some View {
        if let offer = session.introStylesCurrentOffer {
            strip(offer)
        } else {
            switch session.introStylesState {
            case .idle where !session.canOfferIntroStyles:
                unavailableLine(nil)
            case .idle, .loading, .ready:
                skeleton
            case .unavailable(let reason):
                unavailableLine(reason)
            case .failed:
                Button {
                    Task { await session.loadIntroStyles(force: true) }
                } label: {
                    Text(IntroStyleLabels.failureLine)
                        .kriaType(.caption).foregroundStyle(KriaColor.failureText)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("intro-styles-failed")
            }
        }
    }

    private func unavailableLine(_ reason: String?) -> some View {
        Text(IntroStyleLabels.unavailableLine(reason: reason))
            .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 8)
            .accessibilityIdentifier("intro-styles-unavailable")
    }

    /// The wait is a few seconds at most: skeleton tiles, no number and no fill (DESIGN.md loading rules).
    private var skeleton: some View {
        HStack(spacing: 10) {
            ForEach(0..<3, id: \.self) { _ in
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(KriaColor.ink.opacity(0.08))
                    .frame(width: Self.tile.width, height: Self.tile.height)
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 8)
        .kriaSoftPulse()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(String(localized: "Loading intro styles", comment: "VoiceOver: intro style tiles are loading"))
        .accessibilityIdentifier("intro-styles-loading")
    }

    private func strip(_ offer: IntroStylesOffer) -> some View {
        let selected = session.introStyleSelectedKey(in: offer)
        let enabled = session.canEdit(.text)
        // A card look only where she can move and delete its cards (`applyIntroStyle` refuses it otherwise).
        let options = offer.options.filter { !$0.isCardLook || session.canEdit(.mediaOverlays) }
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: 10) {
                ForEach(Array(options.enumerated()), id: \.element.key) { index, option in
                    IntroStyleTile(option: option, name: IntroStyleLabels.name(forKey: option.key, position: index + 1),
                                   selected: selected == option.key, projectID: projectID) {
                        session.applyIntroStyle(option)
                    }
                    .disabled(!enabled)
                }
            }
            .padding(.top, 8)
        }
        .accessibilityIdentifier("intro-styles-strip")
    }
}

/// One look: a 9:16 still of her clip in that look (or a plain label tile when no still was drawn), its name under
/// it. A card look's still has her photos in it. The selected tile is marked with the ink fill, never a border.
private struct IntroStyleTile: View {
    let option: IntroStylesOffer.Option
    let name: String
    let selected: Bool
    let projectID: UUID?
    let select: () -> Void
    @State private var rendered: DesignPreviewRenderer.Rendered?
    @State private var finished = false

    var body: some View {
        Button(action: select) {
            VStack(alignment: .leading, spacing: 8) {
                ZStack(alignment: .bottomLeading) {
                    still
                        .frame(width: NativeIntroStylesPanel.tile.width, height: NativeIntroStylesPanel.tile.height)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    if option.recommended {
                        Text("Recommended").kriaType(.caption).fontWeight(.semibold).fixedSize()
                            .foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 8).padding(.vertical, 2)
                            .background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .padding(6)
                    }
                }
                Text(name)
                    .kriaType(.label)
                    .foregroundStyle(selected ? KriaColor.paper : KriaColor.ink)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .frame(width: NativeIntroStylesPanel.tile.width, height: 44, alignment: .topLeading)
                    .background(selected ? KriaColor.ink : Color.clear, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
            .frame(width: NativeIntroStylesPanel.tile.width, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel([name, option.recommended ? String(localized: "recommended") : nil,
                             option.isStickerLook ? IntroStyleLabels.stickerLook : option.isCardLook ? IntroStyleLabels.photoCardLook : nil]
                                .compactMap { $0 }.joined(separator: ", "))
        .accessibilityValue(value)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("intro-style-option-\(option.key)")
        .task(id: option.key) {
            guard let preview = option.preview else { finished = true; return }
            rendered = await DesignPreviewRenderer.render(preview, projectID: projectID, cards: option.previewCards)
            finished = true
        }
    }

    @ViewBuilder private var still: some View {
        if let rendered {
            Image(decorative: rendered.image, scale: 1).resizable().scaledToFill()
        } else {
            // No still (or not drawn yet): a label tile, so no look loses its name.
            ZStack {
                KriaColor.fill
                Text(name).kriaType(.label).foregroundStyle(KriaColor.ink).multilineTextAlignment(.center).padding(8)
            }
        }
    }

    private var value: String {
        switch rendered?.backdrop {
        case .footage: String(localized: "Preview drawn over your clip")
        case .poster: String(localized: "Preview drawn over your clip's poster")
        case .plain: String(localized: "Preview drawn without your clip")
        case nil: finished ? String(localized: "No preview") : String(localized: "Drawing preview")
        }
    }
}

/// "Try intro styles": the row that opens the panel, at the top of the Text tool and in a title's Style tab.
struct NativeIntroStylesEntryRow: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: "sparkles").font(.system(size: 14, weight: .semibold)).accessibilityHidden(true)
                Text("Try intro styles", comment: "Editor: opens fitted looks for the title")
                    .kriaType(.button)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityHidden(true)
            }
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 12)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("native-editor-intro-styles-entry")
    }
}
