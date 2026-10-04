import SwiftUI

/// KRI-294: the batch line above the Visuals while any are uploading or
/// preparing. The bar fills as each one becomes ready; the spinner says the
/// wait is live even while the count hasn't moved.
struct VisualPreparationSummaryView: View {
    let summary: VisualPreparationSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(summary.title).font(KriaFont.body(14).weight(.semibold)).lineLimit(2)
                Spacer(minLength: 8)
                Text(summary.count).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).monospacedDigit()
            }
            ProgressView(value: summary.fraction).tint(KriaColor.ink)
            Text(summary.detail).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(summary.accessibilityLabel)
        .accessibilityIdentifier("visual-preparation-summary")
    }
}

/// A Visual's status line, with a spinner while it is still preparing so a
/// row that hasn't changed text in a while still reads as in progress.
struct VisualStatusLine: View {
    let text: String
    let preparing: Bool
    var color: Color = KriaColor.zinc

    var body: some View {
        HStack(spacing: 5) {
            if preparing { ProgressView().controlSize(.mini).accessibilityHidden(true) }
            Text(text).foregroundStyle(color)
        }
    }
}
