import SwiftUI

struct KriaNoticeAction {
    let title: String
    let handler: () -> Void
}

/// A notice (Paper S4.06): four tones that differ by fill and by the icon disc, never by colour alone.
///
/// The one action rule. A card has at most one button plus one tertiary link.
/// - Failure: the button is ink with a white label, because butter never sits on or beside red.
/// - Info and warning: the button is a secondary on paper.
/// - Success: no button, and `action`/`link` are ignored.
/// - Banner: a single line with one tertiary action; `link` is ignored.
///
/// Warning is slate, not yellow. Failure is announced as an alert; the other tones as a polite status.
struct KriaNoticeCard: View {
    enum Tone { case info, warning, failure, success }
    enum Style { case card, banner }

    let tone: Tone
    let title: String
    var message: String?
    var action: KriaNoticeAction?
    var link: KriaNoticeAction?
    var style: Style = .card
    /// Turn off in a list that rebuilds rows on scroll, so recycling does not re-announce.
    var announces = true
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            switch style {
            case .card: card
            case .banner: banner
            }
        }
        .accessibilityElement(children: .contain)
        .onAppear {
            guard announces else { return }
            var text = AttributedString([title, message].compactMap { $0 }.joined(separator: ". "))
            if tone == .failure { text.accessibilitySpeechAnnouncementPriority = .high }
            AccessibilityNotification.Announcement(text).post()
        }
    }

    private var card: some View {
        HStack(alignment: .top, spacing: 12) {
            KriaNoticeIcon(tone: tone, size: 28)
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).kriaType(.button).foregroundStyle(KriaColor.ink)
                    if let message {
                        Text(message).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                .accessibilityElement(children: .combine)
                if tone != .success, action != nil || link != nil {
                    // Two buttons share a row; at accessibility sizes they stack so neither wraps mid-word.
                    let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 8))
                    layout {
                        if let action {
                            Button(action.title, action: action.handler)
                                .buttonStyle(KriaButtonStyle(role: tone == .failure ? .ink : .secondary, size: .medium, onFill: tone != .failure))
                        }
                        if let link {
                            Button(link.title, action: link.handler)
                                .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium))
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(14)
        .background(fill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var banner: some View {
        HStack(spacing: 12) {
            KriaNoticeIcon(tone: tone, size: 20)
            Text(title)
                .kriaType(.label).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
            if tone != .success, let action {
                Button(action.title, action: action.handler)
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium))
            }
        }
        .padding(.leading, 14).padding(.trailing, 4).padding(.vertical, 4)
        .frame(minHeight: 52)
        .background(fill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var fill: Color {
        switch tone {
        case .info: KriaColor.selectionSoft
        case .warning: KriaColor.warningSoft
        case .failure: KriaColor.failureSoft
        case .success: KriaColor.successSoft
        }
    }
}

/// The round tone glyph of a notice: sky "i", slate triangle, red "!", green check.
struct KriaNoticeIcon: View {
    let tone: KriaNoticeCard.Tone
    var size: CGFloat = 28

    var body: some View {
        ZStack {
            Circle().fill(disc)
            Image(systemName: symbol)
                .font(.system(size: size * 0.46, weight: .bold))
                .foregroundStyle(glyph)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private var disc: Color {
        switch tone {
        case .info: KriaColor.sky
        case .warning: KriaColor.warning
        case .failure: KriaColor.failureText
        case .success: KriaColor.success
        }
    }

    private var glyph: Color { tone == .info ? KriaColor.ink : KriaColor.paper }

    private var symbol: String {
        switch tone {
        case .info: "info"
        case .warning: "exclamationmark.triangle"
        case .failure: "exclamationmark"
        case .success: "checkmark"
        }
    }
}
