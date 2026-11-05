import SwiftUI

/// The one type scale (Paper S4.02). Inter only, every size scales with Dynamic Type, and there is no
/// all-caps tracked eyebrow style: a group heading is `.label` in sentence case.
enum KriaType: CaseIterable {
    /// One screen heading per screen.
    case title
    /// Card and section headings.
    case heading
    /// Prose, field text, assistant messages.
    case body
    /// Every button, chip and tappable row label.
    case button
    /// Status, metadata, receipts, detail lines.
    case label
    /// Constraints, disabled reasons, helper lines.
    case caption
    /// The camera countdown only.
    case display

    var size: CGFloat {
        switch self {
        case .title: 28
        case .heading: 20
        case .body, .button: 15
        case .label: 13
        case .caption: 12
        case .display: 48
        }
    }

    var weight: Font.Weight {
        switch self {
        case .title, .display: .bold
        case .heading, .button: .semibold
        case .label: .medium
        case .body, .caption: .regular
        }
    }

    /// Points of letter spacing (the board gives title -0.4 and heading -0.2).
    var tracking: CGFloat {
        switch self {
        case .title: -0.4
        case .heading: -0.2
        default: 0
        }
    }

    var lineHeight: CGFloat {
        switch self {
        case .title: 34
        case .heading: 26
        case .body: 21
        case .button: 20
        case .label: 18
        case .caption: 16
        case .display: 58
        }
    }

    private var textStyle: Font.TextStyle {
        switch self {
        case .title: .title
        case .heading: .title3
        case .body, .button: .body
        case .label: .footnote
        case .caption: .caption
        case .display: .largeTitle
        }
    }

    var font: Font {
        let base = Font.custom("Inter", size: size, relativeTo: textStyle).weight(weight)
        return self == .display ? base.monospacedDigit() : base
    }

    /// Inter's natural line is about 1.21 of its size; the board's line height adds the rest.
    var lineSpacing: CGFloat { max(0, lineHeight - size * 1.21) }
}

extension View {
    /// Applies a `KriaType` token: font, tracking and line height.
    func kriaType(_ type: KriaType) -> some View {
        font(type.font).tracking(type.tracking).lineSpacing(type.lineSpacing)
    }
}
