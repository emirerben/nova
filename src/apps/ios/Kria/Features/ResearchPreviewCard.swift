import SafariServices
import SwiftUI

/// KRI-881: the sourced wording and its sources on the approval card, before the creator approves.
/// Soft fill, no strokes, Inter only (Kria design rules); each source is a capsule that opens in Safari in-app.
struct ResearchPreviewCard: View {
    let preview: ResearchPreview
    let strings: ResearchPreviewStrings
    @State private var opened: ResearchPreview.Source?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(strings.eyebrow)
                    .font(KriaFont.body(11).weight(.bold))
                    .tracking(1.3)
                    .foregroundStyle(KriaColor.ink)
                Text(strings.intro)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
            }
            ForEach(preview.explanations) { explanation in
                VStack(alignment: .leading, spacing: 8) {
                    if let window = ResearchPreview.window(explanation) {
                        Text(strings.window(window))
                            .font(KriaFont.body(11).weight(.medium))
                            .foregroundStyle(KriaColor.zinc)
                    }
                    Text("“\(explanation.text)”")
                        .font(KriaFont.body(15))
                        .foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    ForEach(explanation.sources) { source in
                        sourceButton(source)
                    }
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.fill)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .sheet(item: $opened) { source in
            ResearchSourceSafariView(url: source.url).ignoresSafeArea()
        }
    }

    private func sourceButton(_ source: ResearchPreview.Source) -> some View {
        Button { opened = source } label: {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(source.title)
                        .font(KriaFont.body(13).weight(.medium))
                        .foregroundStyle(KriaColor.ink)
                        .lineLimit(1)
                    if source.title != source.domain {
                        Text(source.domain)
                            .font(KriaFont.body(11))
                            .foregroundStyle(KriaColor.zinc)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "arrow.up.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(KriaColor.ink)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(KriaColor.paper)
            .clipShape(Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(strings.openLabel(source))
    }
}

/// An in-app Safari view, so checking a source never leaves Kria.
struct ResearchSourceSafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let configuration = SFSafariViewController.Configuration()
        configuration.entersReaderIfAvailable = false
        return SFSafariViewController(url: url, configuration: configuration)
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
