import SwiftUI

/// The shared sheet chrome (Paper S4.04): a 56pt header, a scrolling body with 24pt side padding and a
/// pinned bar.
///
/// The header shows a back button only when a step can go back, a centred title only when the body has no
/// `heading`, and Close always on the right. Close is never disabled; a flow that loses work on close should
/// confirm before calling `onClose`. Multi-step flows present at `.large`, single-choice sheets at `.medium`.
struct KriaSheetScaffold<Content: View, Bar: View>: View {
    var headerTitle: String?
    /// The one screen title, in `KriaType.title`.
    var heading: String?
    var message: String?
    var onBack: (() -> Void)?
    var closeIdentifier = "kria-sheet-close"
    let onClose: () -> Void
    @ViewBuilder var content: () -> Content
    @ViewBuilder var pinnedBar: () -> Bar

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let heading {
                        Text(heading).kriaType(.title).foregroundStyle(KriaColor.ink)
                            .accessibilityAddTraits(.isHeader)
                    }
                    if let message {
                        Text(message).kriaType(.body).foregroundStyle(KriaColor.mutedInk)
                    }
                    content()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 24).padding(.top, 8).padding(.bottom, 16)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .background(KriaColor.paper)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if Bar.self != EmptyView.self { PinnedBar(content: pinnedBar()) }
        }
    }

    private var header: some View {
        ZStack {
            if let headerTitle, heading == nil {
                Text(headerTitle).kriaType(.button).foregroundStyle(KriaColor.ink)
                    .lineLimit(1).padding(.horizontal, 60)
                    .accessibilityAddTraits(.isHeader)
            }
            HStack {
                if let onBack {
                    Button(action: onBack) { Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)) }
                        .buttonStyle(.kriaIcon)
                        .accessibilityLabel("Back")
                }
                Spacer(minLength: 0)
                Button(action: onClose) { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                    .buttonStyle(.kriaIcon)
                    .accessibilityLabel("Close")
                    .accessibilityIdentifier(closeIdentifier)
            }
            .padding(.horizontal, 12)
        }
        .frame(height: 56)
    }
}

extension KriaSheetScaffold where Bar == EmptyView {
    init(headerTitle: String? = nil, heading: String? = nil, message: String? = nil, onBack: (() -> Void)? = nil,
         closeIdentifier: String = "kria-sheet-close", onClose: @escaping () -> Void,
         @ViewBuilder content: @escaping () -> Content) {
        self.init(headerTitle: headerTitle, heading: heading, message: message, onBack: onBack,
                  closeIdentifier: closeIdentifier, onClose: onClose, content: content, pinnedBar: { EmptyView() })
    }
}

/// 12pt top, 24pt sides and the larger of 16pt and the safe area at the bottom. No top border: a short
/// paper-to-clear gradient above it shows only while content scrolls beneath.
private struct PinnedBar<Bar: View>: View {
    let content: Bar

    var body: some View {
        VStack(spacing: 6) {
            content
        }
        .padding(.horizontal, 24).padding(.top, 12).padding(.bottom, 16)
        .frame(maxWidth: .infinity)
        .background(KriaColor.paper)
        .overlay(alignment: .top) {
            LinearGradient(colors: [KriaColor.paper.opacity(0), KriaColor.paper], startPoint: .top, endPoint: .bottom)
                .frame(height: 12).offset(y: -12).allowsHitTesting(false)
        }
    }
}
