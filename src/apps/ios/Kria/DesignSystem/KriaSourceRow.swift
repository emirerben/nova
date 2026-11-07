import SwiftUI

/// One way to bring footage in (Paper S3.01). The pair that matters is Record | Upload: they always sit side by
/// side with the same size, fill and type, and use the same two SF Symbols everywhere.
struct KriaSourceOption: Identifiable {
    let id: String
    let symbol: String
    let title: String
    var caption: String?
    var identifier: String?
    var isEnabled = true
    /// Shows a spinner in the icon disc while an upload is being prepared; the tile ignores taps.
    var isPreparing = false
    let action: () -> Void

    static let recordSymbol = "video.fill"
    static let uploadSymbol = "photo.on.rectangle"

    /// Pass `symbol: "mic.fill"` for an audio recording; video and every other surface keep `video.fill`.
    static func record(symbol: String = recordSymbol, caption: String? = "Film with Kria", identifier: String? = nil,
                       isEnabled: Bool = true, action: @escaping () -> Void) -> KriaSourceOption {
        KriaSourceOption(id: "record", symbol: symbol, title: "Record", caption: caption, identifier: identifier,
                         isEnabled: isEnabled, action: action)
    }

    static func upload(caption: String? = "From your Photos", identifier: String? = nil, isEnabled: Bool = true,
                       isPreparing: Bool = false, action: @escaping () -> Void) -> KriaSourceOption {
        KriaSourceOption(id: "upload", symbol: uploadSymbol, title: "Upload", caption: caption, identifier: identifier,
                         isEnabled: isEnabled, isPreparing: isPreparing, action: action)
    }
}

/// The Record | Upload (| third) row.
///
/// - `.tiles`: 120pt-minimum tiles. Two options get 16pt padding; three get 12pt and a tighter gap.
/// - `.compact`: a 44pt capsule pair with 15pt icons, for "Add more" once media exists.
///
/// `leadsWithFirst` is the Talk to camera lead: the first tile (Record) takes the butter fill and the rest keep
/// the same size. A disabled tile dims to 40%; pass `disabledReason` to say why under the row.
/// Put the row on a `fill` card with `onFill: true` and the tiles become paper.
struct KriaSourceRow: View {
    enum Layout { case tiles, compact }

    let options: [KriaSourceOption]
    var layout: Layout = .tiles
    var leadsWithFirst = false
    var onFill = false
    var disabledReason: String?

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(spacing: 8) {
            // At accessibility sizes two tiles can't hold "Record" side by side, so they stack and each takes the full width.
            if typeSize.isAccessibilitySize {
                VStack(spacing: 12) { buttons }
            } else {
                HStack(spacing: layout == .tiles && options.count > 2 ? 8 : 12) { buttons }
                    // Equal tile heights: the tallest caption sets the row, and every tile stretches to it.
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let disabledReason {
                Text(disabledReason).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder private var buttons: some View {
        ForEach(Array(options.enumerated()), id: \.element.id) { index, option in
            let leads = leadsWithFirst && index == 0
            Button(action: option.action) {
                switch layout {
                case .tiles: tile(option, leads: leads)
                case .compact: capsule(option)
                }
            }
            .buttonStyle(KriaSourceStyle(layout: layout, onFill: onFill, leads: leads, tight: options.count > 2))
            .disabled(!option.isEnabled || option.isPreparing)
            .accessibilityLabel(option.caption.map { "\(option.title), \($0)" } ?? option.title)
            .accessibilityIdentifier(option.identifier ?? "source-\(option.id)")
        }
    }

    private func tile(_ option: KriaSourceOption, leads: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack {
                Circle().fill(onFill && !leads ? KriaColor.fill : KriaColor.paper)
                if option.isPreparing {
                    KriaSpinner(size: 20)
                } else {
                    Image(systemName: option.symbol).font(.system(size: 20, weight: .regular)).foregroundStyle(KriaColor.ink)
                }
            }
            .frame(width: 40, height: 40)
            Spacer(minLength: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(option.title).kriaType(.button).foregroundStyle(KriaColor.ink)
                if let caption = option.caption {
                    Text(caption).kriaType(.caption).foregroundStyle(leads ? KriaColor.ink : KriaColor.mutedInk)
                }
            }
            .multilineTextAlignment(.leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func capsule(_ option: KriaSourceOption) -> some View {
        HStack(spacing: 8) {
            if option.isPreparing {
                KriaSpinner(size: 15)
            } else {
                Image(systemName: option.symbol).font(.system(size: 15, weight: .regular))
            }
            Text(option.title).kriaType(.button)
        }
        .foregroundStyle(KriaColor.ink)
        .frame(maxWidth: .infinity)
    }
}

private struct KriaSourceStyle: ButtonStyle {
    let layout: KriaSourceRow.Layout
    let onFill: Bool
    let leads: Bool
    let tight: Bool
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        let shape = RoundedRectangle(cornerRadius: layout == .tiles ? (onFill ? 16 : 20) : 22, style: .continuous)
        return configuration.label
            .padding(layout == .tiles ? (tight ? 12 : 16) : 0)
            .frame(minHeight: layout == .tiles ? 120 : 44)
            .background(fill(pressed: configuration.isPressed), in: shape)
            .contentShape(shape)
            .opacity(isEnabled ? 1 : 0.4)
    }

    private func fill(pressed: Bool) -> Color {
        if leads { return pressed ? KriaColor.butterPressed : KriaColor.butter }
        if onFill { return pressed ? KriaColor.fillPressed : KriaColor.paper }
        return pressed ? KriaColor.fillPressed : KriaColor.fill
    }
}
