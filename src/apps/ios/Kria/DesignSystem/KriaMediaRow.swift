import SwiftUI

struct KriaMenuAction: Identifiable {
    let id = UUID()
    let title: String
    var systemImage: String?
    var isDestructive = false
    let handler: () -> Void
}

/// One attached item (Paper S4.08): a 52x64 thumbnail, a name, a status line and one trailing 44pt control.
///
/// States: ready and preparing show a label; uploading shows a `KriaProgressTrack` and the percentage; failed
/// shows the failure in failureText and, when `onRetry` is set, a compact "Retry". The name wraps to two lines
/// and truncates in the middle, so a long file name keeps its extension.
struct KriaMediaRow<Thumbnail: View>: View {
    enum Status {
        case ready(String)
        case preparing
        case uploading(Double)
        case failed(String)
    }

    enum Trailing {
        case none
        /// A finished item that needs no action: the green check disc.
        case check
        case remove(label: String = String(localized: "Remove"), () -> Void)
        case menu(label: String = String(localized: "More actions"), [KriaMenuAction])
    }

    let name: String
    let status: Status
    var trailing: Trailing = .none
    var onRetry: (() -> Void)?
    /// The row is its own fill card (r20) instead of a paper row that needs a fill wrapper around it.
    var onFill = false
    @ViewBuilder var thumbnail: () -> Thumbnail

    var body: some View {
        HStack(spacing: 12) {
            thumbnail()
                .frame(width: 52, height: 64)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(name).kriaType(.button).foregroundStyle(KriaColor.ink)
                    .lineLimit(2).truncationMode(.middle)
                statusLine
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            if case .uploading(let progress) = status {
                Text("\(Int((min(1, max(0, progress)) * 100).rounded()))%")
                    .kriaType(.label).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
            }
            if case .failed = status, let onRetry {
                Button("Retry", action: onRetry).buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
            }
            trailingControl
        }
        .padding(8)
        .frame(maxWidth: .infinity)
        .background(onFill ? KriaColor.fill : KriaColor.paper, in: RoundedRectangle(cornerRadius: onFill ? 20 : 16, style: .continuous))
    }

    @ViewBuilder private var statusLine: some View {
        switch status {
        case .ready(let text):
            Text(text).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
        case .preparing:
            HStack(spacing: 6) {
                KriaSpinner(size: 13)
                Text("Preparing…").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            }
        case .uploading(let progress):
            KriaProgressTrack(progress: progress)
                .accessibilityLabel("Uploading")
                .accessibilityValue("\(Int((min(1, max(0, progress)) * 100).rounded())) percent")
        case .failed(let text):
            Text(text).kriaType(.label).foregroundStyle(KriaColor.failureText)
        }
    }

    @ViewBuilder private var trailingControl: some View {
        switch trailing {
        case .none:
            EmptyView()
        case .check:
            KriaCheckDisc(size: 28).accessibilityHidden(true)
        case .remove(let label, let action):
            Button(action: action) { Image(systemName: "trash").font(.system(size: 17)) }
                .buttonStyle(.kriaIcon).accessibilityLabel(label)
        case .menu(let label, let actions):
            Menu {
                ForEach(actions) { item in
                    Button(role: item.isDestructive ? .destructive : nil, action: item.handler) {
                        if let image = item.systemImage { Label(item.title, systemImage: image) } else { Text(item.title) }
                    }
                }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 17, weight: .bold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(width: 44, height: 44)
                    .background(KriaColor.fill, in: Circle())
                    .contentShape(Circle())
            }
            .accessibilityLabel(label)
        }
    }
}

/// The fill placeholder with a 17pt glyph for a thumbnail that has not loaded.
struct KriaThumbnailPlaceholder: View {
    var systemImage = "photo"
    var failed = false

    var body: some View {
        ZStack {
            (failed ? KriaColor.failureSoft : KriaColor.fill)
            Image(systemName: systemImage).font(.system(size: 17))
                .foregroundStyle(failed ? KriaColor.failureText : KriaColor.mutedInk)
        }
    }
}

/// Which steps a format uses, with the current one marked (Paper S4.09). One step means no rail.
/// Current is an ink bar and ink label; done is a check disc and a mutedInk label; upcoming is a fill bar and
/// a zinc label. There are no step digits.
struct KriaStepRail: View {
    let steps: [String]
    let current: Int
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        if steps.count > 1 {
            if typeSize.isAccessibilitySize {
                // Three labels can't share a row at this size without clipping: keep the bars and name only the current step.
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        ForEach(steps.indices, id: \.self) { index in
                            RoundedRectangle(cornerRadius: 2).fill(barColor(index)).frame(height: 4)
                        }
                    }
                    .accessibilityHidden(true)
                    Text("\(steps[min(current, steps.count - 1)]) · step \(current + 1) of \(steps.count)")
                        .kriaType(.label).fontWeight(.semibold).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(steps[min(current, steps.count - 1)]), current step, \(current + 1) of \(steps.count)")
            } else {
                HStack(spacing: 8) {
                    ForEach(steps.indices, id: \.self) { index in
                        VStack(alignment: .leading, spacing: 8) {
                            RoundedRectangle(cornerRadius: 2).fill(barColor(index)).frame(height: 4)
                            HStack(spacing: 6) {
                                if index < current { KriaCheckDisc(size: 16) }
                                Text(steps[index]).kriaType(.label)
                                    .fontWeight(index == current ? .semibold : .medium)
                                    .foregroundStyle(labelColor(index))
                                    .lineLimit(1)
                            }
                            .frame(minHeight: 20)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel(index < current ? String(localized: "\(steps[index]), done") : index == current ? String(localized: "\(steps[index]), current step") : String(localized: "\(steps[index]), not started"))
                    }
                }
            }
        }
    }

    private func barColor(_ index: Int) -> Color {
        index < current ? KriaColor.line : index == current ? KriaColor.ink : KriaColor.fill
    }

    private func labelColor(_ index: Int) -> Color {
        index < current ? KriaColor.mutedInk : index == current ? KriaColor.ink : KriaColor.zinc
    }
}
