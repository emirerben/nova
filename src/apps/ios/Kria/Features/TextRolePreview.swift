import SwiftUI

extension TextPreferenceRule {
    /// What this type ends up with: its own choices, else the ones saved for All text. Choosing "Kria picks"
    /// for the font (`automaticFont == true`) is an own choice that overrides an inherited font.
    func inheriting(from base: TextPreferenceRule) -> TextPreferenceRule {
        var merged = self
        if fontFamily == nil && automaticFont != true {
            merged.fontFamily = base.fontFamily
            merged.automaticFont = base.automaticFont
        }
        if favoriteFonts.isEmpty { merged.favoriteFonts = base.favoriteFonts }
        if excludedFonts.isEmpty { merged.excludedFonts = base.excludedFonts }
        merged.position = position ?? base.position
        merged.color = color ?? base.color
        merged.strokeWidth = strokeWidth ?? base.strokeWidth
        merged.shadowEnabled = shadowEnabled ?? base.shadowEnabled
        return merged
    }
}

/// A 9:16 video frame with all five text types drawn at their real size and spot. The selected type shows the
/// creator's current rule at full strength; the others are ghosted so the creator sees where each one sits.
/// Text goes through `SlidePostTextLayerView`, the layer the on-device exporter uses for slide text.
struct TextRolePreview: View {
    static let ghostOpacity = 0.42
    static let frameName = "featured-bosphorus"

    let selected: String
    /// The rule each type ends up with, inheritance already applied.
    let rules: [String: TextPreferenceRule]
    let allTextPosition: String?
    let server: TextStyleDefaults?
    var width: CGFloat = 168
    var identifier = "text-preferences-preview"

    private var size: CGSize { CGSize(width: width, height: width * 16 / 9) }

    var body: some View {
        ZStack(alignment: .topLeading) {
            BundledPosterImage(name: Self.frameName).scaledToFill()
                .frame(width: size.width, height: size.height).clipped()
            LinearGradient(colors: [.black.opacity(0.1), .black.opacity(0.38)], startPoint: .top, endPoint: .bottom)
            ForEach(TextRoleSpec.drawn) { spec in
                let active = selected == "all_text" || selected == spec.role
                SlidePostTextLayerView(texts: [element(for: spec)], size: size)
                    .opacity(active ? 1 : Self.ghostOpacity)
                    .accessibilityHidden(true)
            }
        }
        .frame(width: size.width, height: size.height)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .animation(.easeOut(duration: 0.15), value: selected)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Preview of your text on a video frame")
        .accessibilityValue(TextRoleSpec.spec(selected).label)
        .accessibilityIdentifier(identifier)
    }

    private func element(for spec: TextRoleSpec) -> SlidePostTextElement {
        let rule = rules[spec.role] ?? TextPreferenceRule()
        var anchor = TextRoleSpec.anchor(role: spec.role, position: rule.position, server: server)
        if selected == "all_text", let placed = allTextPosition, rule.position == placed {
            anchor = Self.staggered(anchor, index: TextRoleSpec.drawn.firstIndex { $0.role == spec.role } ?? 0)
        }
        var element = SlidePostTextElement(id: spec.role, text: spec.sample)
        element.fontFamily = font(for: spec.role, rule: rule)
        element.color = rule.color.flatMap { SlidePostTextElement.isHex($0.uppercased()) ? $0.uppercased() : nil } ?? "#FFFFFF"
        element.sizePx = spec.sizePx
        element.alignment = anchor.alignment
        element.position = "custom"
        element.xFrac = anchor.x
        element.yFrac = anchor.y
        element.maxWidthFrac = spec.maxWidthFrac
        element.strokeWidth = Int((rule.strokeWidth ?? 0).rounded())
        element.shadowEnabled = rule.shadowEnabled ?? false
        return element
    }

    /// One spot for every type would pile them up, so the types stack in a column from that spot: downward
    /// from a top spot, upward from a bottom spot, around the middle otherwise. The step clears a two-line title.
    static func staggered(_ anchor: TextAnchor, index: Int) -> TextAnchor {
        let step = 0.10, last = TextRoleSpec.drawn.count - 1
        let offset: Double = anchor.y < 0.35 ? Double(index) : anchor.y > 0.65 ? Double(index - last) : Double(index) - Double(last) / 2
        return TextAnchor(x: anchor.x, y: min(0.94, max(0.06, anchor.y + offset * step)), alignment: anchor.alignment)
    }

    private func font(for role: String, rule: TextPreferenceRule) -> String {
        if let chosen = rule.fontFamily { return chosen }
        let catalog = NativeFontCatalog.shared
        let candidates = rule.favoriteFonts + [TextRoleSpec.defaults(for: role, server: server).fontHint].compactMap { $0 }
        return candidates.first { !rule.excludedFonts.contains($0) && catalog.fontURL(for: $0) != nil } ?? SlidePostTextElement.defaultFont
    }
}
