import SwiftUI

struct TextPreferenceRule: Codable, Equatable, Sendable {
    var fontFamily: String?
    var automaticFont: Bool?
    var favoriteFonts: [String] = []
    var excludedFonts: [String] = []
    var position: String?
    var color: String?
    var strokeWidth: Double?
    var shadowEnabled: Bool?
    enum CodingKeys: String, CodingKey {
        case automaticFont = "automatic_font", fontFamily = "font_family", favoriteFonts = "favorite_fonts", excludedFonts = "excluded_fonts"
        case position, color, strokeWidth = "stroke_width", shadowEnabled = "shadow_enabled"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fontFamily = try c.decodeIfPresent(String.self, forKey: .fontFamily)
        automaticFont = try c.decodeIfPresent(Bool.self, forKey: .automaticFont)
        favoriteFonts = try c.decodeIfPresent([String].self, forKey: .favoriteFonts) ?? []
        excludedFonts = try c.decodeIfPresent([String].self, forKey: .excludedFonts) ?? []
        position = try c.decodeIfPresent(String.self, forKey: .position)
        color = try c.decodeIfPresent(String.self, forKey: .color)
        strokeWidth = try c.decodeIfPresent(Double.self, forKey: .strokeWidth)
        shadowEnabled = try c.decodeIfPresent(Bool.self, forKey: .shadowEnabled)
    }
}

/// The settings a text type can override. A type's rule holds a value only for what the creator changed on it;
/// everything else follows All text, so "changed" is a property of the rule, never a separate flag to keep in sync.
enum TextSetting: String, Sendable {
    case font, placement, color, outline, shadow

    var title: String {
        switch self {
        case .font: "Font"
        case .placement: "Placement"
        case .color: "Color"
        case .outline: "Outline"
        case .shadow: "Shadow"
        }
    }
}

extension TextPreferenceRule {
    func overrides(_ setting: TextSetting) -> Bool {
        switch setting {
        case .font: fontFamily != nil || automaticFont == true
        case .placement: position != nil
        case .color: color != nil
        case .outline: strokeWidth != nil
        case .shadow: shadowEnabled != nil
        }
    }

    mutating func clear(_ setting: TextSetting) {
        switch setting {
        case .font: fontFamily = nil; automaticFont = nil
        case .placement: position = nil
        case .color: color = nil
        case .outline: strokeWidth = nil
        case .shadow: shadowEnabled = nil
        }
    }
}

struct TextPreferenceMemory: Decodable, Sendable {
    struct EditLearning: Decodable, Sendable { let available: Bool; let enabled: Bool }
    struct Item: Decodable, Identifiable, Sendable {
        struct Evidence: Decodable, Sendable {
            let supportingVideoCount: Int
            enum CodingKeys: String, CodingKey { case supportingVideoCount = "supporting_video_count" }
        }
        let sourceKind: String?
        let evidence: Evidence?
        let id: String
        let normalizedKey: String?
        let instruction: String
        let structuredValue: [String: JSONValue]?
        enum CodingKeys: String, CodingKey {
            case id, instruction, normalizedKey = "normalized_key", structuredValue = "structured_value"
            case sourceKind = "source_kind", evidence
        }
    }
    let editLearning: EditLearning?
    let featureAvailable: Bool
    let textPreferencesAvailable: Bool?
    let enabled: Bool
    let revision: Int
    let active: [Item]
    let suggestions: [Item]
    let recentUndo: TextPreferenceOperation?
    private let lenientDefaults: LenientDecodable<TextStyleDefaults>?
    var textStyleDefaults: TextStyleDefaults? { lenientDefaults?.value }
    enum CodingKeys: String, CodingKey {
        case featureAvailable = "feature_available", enabled, revision, active, suggestions
        case lenientDefaults = "text_style_defaults"
        case textPreferencesAvailable = "text_preferences_available"
        case recentUndo = "recent_undo", editLearning = "edit_learning"
    }
    var textStylingAvailable: Bool { featureAvailable && textPreferencesAvailable == true }
    func rule(role: String) -> TextPreferenceRule {
        guard let value = active.first(where: { $0.normalizedKey == "text_style_" + role })?.structuredValue?["text_style_" + role],
              let data = try? JSONEncoder().encode(value),
              let rule = try? JSONDecoder().decode(TextPreferenceRule.self, from: data) else { return TextPreferenceRule() }
        return rule
    }
}

struct TextPreferenceOperation: Decodable, Sendable {
    let id: String
    enum CodingKeys: String, CodingKey { case id = "operation_id" }
}

@MainActor final class TextPreferencesModel: ObservableObject {
    @Published var memory: TextPreferenceMemory?
    @Published var isWorking = false
    @Published var error: String?
    @Published var saved = false
    @Published var learningHistoryReset = false
    @Published var needsReload = false
    private let api: any KriaAPIClient
    init(api: any KriaAPIClient) { self.api = api }
    func load() async {
        isWorking = true
        defer { isWorking = false }
        do { memory = try await api.textPreferenceMemory(); error = nil; needsReload = false }
        catch { self.error = error.localizedDescription }
    }
    /// Writes one row per type, in order. Each write reloads memory, so the next one carries a valid revision.
    /// The first failure stops the batch; types already written stay saved and the rest stay unsaved.
    func save(_ writes: [(role: String, rule: TextPreferenceRule)]) async {
        guard memory?.textStylingAvailable == true else { return }
        isWorking = true
        defer { isWorking = false }
        for entry in writes {
            do { try await writeRow(role: entry.role, rule: entry.rule) }
            catch { show(error); return }
        }
        saved = true
    }
    private func writeRow(role: String, rule: TextPreferenceRule) async throws {
        guard let memory else { return }
        let key = "text_style_" + role
        let value = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(rule))
        let item = memory.active.first { $0.normalizedKey == key }
        try await mutate(path: "me/memory/items" + (item.map { "/" + $0.id } ?? ""),
                         method: item == nil ? "POST" : "PATCH", extra: [
                            "instruction": .string("My saved \(role.replacingOccurrences(of: "_", with: " ")) text preferences"),
                            "category": .string("video_style"), "enforcement": .string("default"),
                            "normalized_key": .string(key), "structured_value": .object([key: value])])
    }
    func reset(role: String) async {
        guard let item = memory?.active.first(where: { $0.normalizedKey == "text_style_" + role }) else { return }
        await forget(item)
    }
    func forget(_ item: TextPreferenceMemory.Item) async {
        do { try await mutate(path: "me/memory/items/" + item.id, method: "DELETE") }
        catch { show(error) }
    }
    func undo(_ id: String) async {
        do { try await mutate(path: "me/memory/operations/\(id)/undo", method: "POST") }
        catch { show(error) }
    }
    func setEnabled(_ enabled: Bool) async {
        do { try await mutate(path: "me/memory", method: "PATCH", extra: ["enabled": .bool(enabled)]) }
        catch { show(error) }
    }
    func decide(_ item: TextPreferenceMemory.Item, accept: Bool) async {
        do { try await mutate(path: "me/memory/items/\(item.id)/" + (accept ? "accept" : "suggestion"), method: accept ? "POST" : "DELETE") }
        catch { show(error) }
    }
    func setLearningEnabled(_ enabled: Bool) async {
        do { try await mutate(path: "me/memory/edit-learning", method: "PATCH", extra: ["enabled": .bool(enabled)]) }
        catch { show(error) }
    }
    func resetLearningHistory() async {
        do { try await mutate(path: "me/memory/edit-learning/reset", method: "POST"); learningHistoryReset = true }
        catch { show(error) }
    }
    private func mutate(path: String, method: String, extra: [String: JSONValue] = [:]) async throws {
        guard let memory else { return }
        let wasWorking = isWorking
        isWorking = true; saved = false
        defer { isWorking = wasWorking }
        var body = extra
        body["expected_revision"] = .number(Double(memory.revision))
        body["idempotency_key"] = .string(UUID().uuidString)
        _ = try await api.mutateTextPreferenceMemory(path: path, method: method, body: body)
        self.memory = try await api.textPreferenceMemory()
        error = nil; needsReload = false
    }
    private func show(_ error: Error) {
        needsReload = (error as? APIError)?.isConflict == true
        self.error = needsReload ? "Your saved preferences changed. Reload before saving again." : error.localizedDescription
    }
}

struct TextPreferencesView: View {
    @StateObject private var model: TextPreferencesModel
    @State private var role = "all_text"
    @State private var rule = TextPreferenceRule()
    @State private var drafts: [String: TextPreferenceRule] = [:]
    @State private var enlarged = false
    @State private var confirmingLeave = false
    @State private var headerCollapsed = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dismiss) private var dismiss
    init(api: any KriaAPIClient) { _model = StateObject(wrappedValue: TextPreferencesModel(api: api)) }

    private var isAllText: Bool { role == "all_text" }
    private var server: TextStyleDefaults? { model.memory?.textStyleDefaults }
    private var defaults: TextRoleDefaults { TextRoleSpec.defaults(for: role, server: server) }
    /// What a type inherits: the All text rule as the creator is editing it, else as saved.
    private var allTextRule: TextPreferenceRule {
        isAllText ? rule : drafts["all_text"] ?? model.memory?.rule(role: "all_text") ?? TextPreferenceRule()
    }

    /// What the controls show: the type's own choices, else what All text gives it.
    private var effective: TextPreferenceRule { isAllText ? rule : rule.inheriting(from: allTextRule) }

    /// The creator's working copy of a type: the live rule for the visible one, the stashed draft for the rest.
    private func draft(_ role: String) -> TextPreferenceRule? { role == self.role ? rule : drafts[role] }

    /// Types whose working copy differs from what is saved. Switching types stashes edits, so more than one can be pending.
    private var dirtyRoles: [String] {
        guard let memory = model.memory else { return [] }
        return TextRoleSpec.roles.filter { role in draft(role).map { $0 != memory.rule(role: role) } ?? false }
    }

    var body: some View {
        VStack(spacing: 0) {
            if model.memory?.textStylingAvailable == true { header }
            settings
        }
        .background(KriaColor.paper)
        .tint(KriaColor.ink)
        .navigationTitle("Text preferences").navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) { saveBar }
        .task {
            guard model.memory == nil else { return }
            await model.load(); reloadRule()
        }
        .onChange(of: role) { oldRole, _ in
            drafts[oldRole] = rule
            reloadRule()
            model.saved = false
        }
        .onChange(of: rule) { _, _ in model.saved = false }
        .navigationBarBackButtonHidden(!dirtyRoles.isEmpty)
        .toolbar {
            if !dirtyRoles.isEmpty {
                ToolbarItem(placement: .topBarLeading) {
                    Button { confirmingLeave = true } label: {
                        Label("Back", systemImage: "chevron.left").labelStyle(.titleAndIcon)
                    }.accessibilityIdentifier("text-preferences-back")
                }
            }
        }
        .confirmationDialog("Save your changes?", isPresented: $confirmingLeave, titleVisibility: .visible) {
            Button("Save and leave") {
                let writes = dirtyRoles.map { (role: $0, rule: draft($0) ?? TextPreferenceRule()) }
                Task { await model.save(writes); if model.error == nil { dismiss() } }
            }
            Button("Discard changes", role: .destructive) { dismiss() }
            Button("Keep editing", role: .cancel) {}
        } message: {
            Text("You changed \(dirtyRoles.count == 1 ? "a text type" : "\(dirtyRoles.count) text types") that is not saved yet.")
        }
    }

    private var settings: some View {
        List {
            if let memory = model.memory, memory.textStylingAvailable {
                fontSection
                placementSection
                effectsSection
                savedSection(memory)
                let other = memory.active.filter { $0.categoryIsText && !($0.normalizedKey ?? "").hasPrefix("text_style_") }
                if !other.isEmpty {
                    Group {
                        groupLabel("Other saved text instructions")
                        ForEach(other) { item in
                            Text(item.instruction)
                            Button("Forget this instruction") { Task { await model.forget(item) } }
                        }
                    }.plainRow()
                }
                if !memory.suggestions.isEmpty {
                    Group {
                        groupLabel("Suggestions")
                        ForEach(memory.suggestions.filter(\.categoryIsText)) { item in
                            TextPreferenceSuggestionRow(item: item) { accept in
                                await model.decide(item, accept: accept)
                                if model.error == nil { drafts.removeValue(forKey: role); reloadRule() }
                            }
                        }
                    }.plainRow()
                }
            } else if model.memory != nil { Text("Text preferences are not available yet.").plainRow() }
            if let learning = model.memory?.editLearning, learning.available || learning.enabled {
                Group {
                    groupLabel("Learn from finished edits")
                    Toggle("Learn from finished edits", isOn: Binding(get: { learning.enabled }, set: { enabled in Task { await model.setLearningEnabled(enabled) } }))
                        .accessibilityIdentifier("text-learning-enabled")
                    note("After the same change in three finished videos, Kria can suggest a preference. You choose whether to save it.")
                    if learning.enabled {
                        note("Turning this off clears learning history and pending suggestions. Approved preferences stay saved.")
                        Button("Reset learning history") { Task { await model.resetLearningHistory() } }
                            .accessibilityIdentifier("text-learning-reset")
                    }
                    if model.learningHistoryReset { Text("Learning history reset.") }
                }.plainRow()
            }
            if model.isWorking && model.memory == nil { ProgressView("Loading preferences").plainRow() }
        }
        .listStyle(.plain)
        .listRowSpacing(0)
        .environment(\.defaultMinListRowHeight, 44)
        .scrollContentBackground(.hidden)
        .font(KriaFont.body(15))
        .disabled(model.isWorking)
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentOffset.y + $0.contentInsets.top } action: { _, distance in
            // Two thresholds so a small drag near the top cannot flip the header back and forth.
            if distance > 16 { headerCollapsed = true } else if distance < 4 { headerCollapsed = false }
        }
    }

    private func groupLabel(_ title: String, first: Bool = false) -> some View {
        KriaSectionLabel(title: title).padding(.top, first ? 8 : 18).padding(.bottom, 2)
    }

    private func note(_ text: String) -> some View {
        Text(text).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk).fixedSize(horizontal: false, vertical: true)
    }

    private func savedSection(_ memory: TextPreferenceMemory) -> some View {
        Group {
            groupLabel("Saved preferences")
            Toggle("Use saved preferences", isOn: Binding(get: { memory.enabled }, set: { enabled in Task { await model.setEnabled(enabled) } }))
                .accessibilityIdentifier("text-preferences-enabled")
            note(memory.enabled ? "Used for new videos. A request in chat wins." : "Paused. Turn this on to use your choices in new videos.")
            Button("Reset \(TextRoleSpec.spec(role).label.lowercased()) preferences") {
                let resetRole = role
                Task {
                    await model.reset(role: resetRole)
                    if model.error == nil { drafts.removeValue(forKey: resetRole); reloadRule() }
                }
            }.frame(minHeight: 44, alignment: .leading)
            if let undo = memory.recentUndo {
                Button("Undo last preference change") {
                    Task { await model.undo(undo.id); if model.error == nil { drafts.removeValue(forKey: role); reloadRule() } }
                }.frame(minHeight: 44, alignment: .leading).accessibilityIdentifier("text-preferences-undo")
            }
        }.plainRow()
    }

    /// Pinned above the scrolling settings so the frame stays in view while a setting changes. Once the settings
    /// scroll, the frame shrinks and the caption fades so the settings get the screen.
    private var header: some View {
        let spec = TextRoleSpec.spec(role)
        let collapsed = headerCollapsed
        return VStack(spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(TextRoleSpec.all) { chip($0) }
                }
                .padding(.horizontal, 24)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("text-preference-role")
            }
            preview(width: collapsed ? 88 : 120)
                .overlay {
                    Button { enlarged = true } label: { Color.clear.contentShape(Rectangle()) }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Enlarge preview")
                        .accessibilityIdentifier("text-preferences-preview-enlarge")
                }
                .overlay(alignment: .bottomTrailing) {
                    // Sits just outside the frame so it never covers the sample text drawn on it.
                    Button { enlarged = true } label: {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                            .font(.system(size: 14, weight: .semibold)).foregroundStyle(KriaColor.ink)
                            .frame(width: 40, height: 40).background(KriaColor.fill, in: Circle())
                            .frame(width: 44, height: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .offset(x: 48)
                    .accessibilityHidden(true)
                }
            Text(spec.blurb).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .multilineTextAlignment(.center).lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 24)
                // Stays in the tree for VoiceOver; only its pixels go.
                .frame(height: collapsed ? 0 : nil)
                .opacity(collapsed ? 0 : 1)
                .accessibilityIdentifier("text-preference-role-description")
        }
        .padding(.top, 4).padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        // A huge text setting would let the pinned part eat the whole screen.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
        .animation(KriaMotion.isReduced(reduceMotion) ? nil : .easeOut(duration: 0.2), value: collapsed)
        .background(KriaColor.paper)
        .shadow(color: .black.opacity(collapsed ? 0.06 : 0), radius: 6, y: 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("text-preferences-header")
        .sheet(isPresented: $enlarged) { enlargedPreview }
    }

    private func preview(width: CGFloat, identifier: String = "text-preferences-preview") -> TextRolePreview {
        TextRolePreview(selected: role, rules: previewRules, allTextPosition: allTextRule.position, server: server,
                        width: width, identifier: identifier)
    }

    private var enlargedPreview: some View {
        let spec = TextRoleSpec.spec(role)
        return ScrollView {
            VStack(spacing: 14) {
                Text(spec.label).font(KriaFont.body(20).weight(.semibold)).foregroundStyle(KriaColor.ink)
                Text(spec.blurb).font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
                    .accessibilityIdentifier("text-preference-sheet-description")
                preview(width: 300, identifier: "text-preferences-preview-large")
                Button { enlarged = false } label: { Text("Done").frame(maxWidth: .infinity) }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .frame(width: 300)
                    .accessibilityIdentifier("text-preferences-preview-done")
            }
            .padding(20).frame(maxWidth: .infinity)
        }
        .background(KriaColor.paper)
        .presentationDetents([.large])
    }

    private func chip(_ spec: TextRoleSpec) -> some View {
        let selected = role == spec.role
        let unsaved = dirtyRoles.contains(spec.role)
        return Button { role = spec.role } label: {
            HStack(spacing: 6) {
                Text(spec.label)
                if unsaved { Circle().fill(KriaColor.ink).frame(width: 6, height: 6) }
            }
                .font(KriaFont.body(14).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .padding(.horizontal, 16).frame(minHeight: 44)
                .background(selected ? KriaColor.butter : KriaColor.fill, in: Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("text-preference-role-" + spec.role)
        .accessibilityValue(unsaved ? "unsaved changes" : "")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var previewRules: [String: TextPreferenceRule] {
        var rules: [String: TextPreferenceRule] = [:]
        for spec in TextRoleSpec.drawn {
            let own = spec.role == role ? rule : drafts[spec.role] ?? model.memory?.rule(role: spec.role) ?? TextPreferenceRule()
            rules[spec.role] = own.inheriting(from: allTextRule)
        }
        return rules
    }

    private var fontSection: some View {
        Group {
            groupLabel("Fonts", first: true)
            pickerRow(.font) {
                Picker("Font", selection: Binding(get: {
                    effective.fontFamily ?? "auto"
                }, set: {
                    if $0 == "auto" {
                        rule.fontFamily = nil; rule.automaticFont = isAllText ? nil : true
                    } else {
                        rule.fontFamily = $0; rule.automaticFont = false
                        rule.excludedFonts.removeAll { $0 == rule.fontFamily }
                    }
                })) {
                    Text(defaults.font).tag("auto")
                    ForEach(NativeFontCatalog.shared.pickerFonts, id: \.self) { Text($0).tag($0) }
                }.accessibilityIdentifier("text-preference-font")
            }
            if let hint = defaults.fontHint, effective.fontFamily == nil {
                note("Kria starts with \(hint) for \(TextRoleSpec.spec(role).label.lowercased()).")
            }
            NavigationLink {
                TextPreferenceFontPicker(rule: $rule, selection: .favorite)
            } label: {
                fontRow("Favorite fonts", symbol: "heart", fonts: rule.favoriteFonts)
            }.accessibilityIdentifier("text-preference-favorite-fonts")
            NavigationLink {
                TextPreferenceFontPicker(rule: $rule, selection: .excluded)
            } label: {
                fontRow("Never use", symbol: "nosign", fonts: rule.excludedFonts)
            }
            .accessibilityLabel("Excluded fonts, " + fontSummary(rule.excludedFonts))
            .accessibilityIdentifier("text-preference-excluded-fonts")
        }.plainRow()
    }

    /// Whether this type can still show "Kria picks the clearest spot" or Auto color: those are the absence of a
    /// value, so a type can only show them while All text has none to inherit.
    private var inheritsNoPosition: Bool { isAllText || allTextRule.position == nil }
    private var inheritsNoColor: Bool { isAllText || allTextRule.color == nil }

    private var placementSection: some View {
        Group {
            groupLabel("Placement and color")
            pickerRow(.placement) {
                Picker("Placement", selection: Binding(get: { effective.position ?? "" }, set: { rule.position = $0.isEmpty ? nil : $0 })) {
                    if inheritsNoPosition { Text(defaults.placement).tag("") }
                    ForEach(TextRoleSpec.positions, id: \.self) { Text(TextRoleSpec.positionName($0)).tag($0) }
                }.accessibilityIdentifier("text-preference-placement")
            }
            VStack(alignment: .leading, spacing: 4) {
                settingHeader(.color)
                colorSwatches
                if effective.color == nil { note(defaults.colorHint) }
            }.padding(.vertical, 4)
        }.plainRow()
    }

    private static let presetColors = ["#FFFFFF", "#000000", "#FFFF00", "#FF0000", "#0000FF"]

    private var colorSwatches: some View {
        let current = effective.color?.uppercased()
        let customHex = current.flatMap { SlidePostTextElement.isHex($0) && !Self.presetColors.contains($0) ? $0 : nil }
        return HStack(spacing: 0) {
            if inheritsNoColor {
                Button { rule.color = nil } label: {
                    Text("Auto")
                        .font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .dynamicTypeSize(...DynamicTypeSize.large)
                        .frame(width: 40, height: 40)
                        .background(current == nil ? KriaColor.butter : KriaColor.fill, in: Circle())
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Auto")
                .accessibilityAddTraits(current == nil ? .isSelected : [])
                .accessibilityIdentifier("text-preference-color-auto")
            }
            ForEach(Self.presetColors, id: \.self) { hex in
                Button { rule.color = hex } label: {
                    ZStack {
                        Circle().fill(KriaColor.fill).frame(width: 40, height: 40)
                        Circle().fill(nativeEditorColor(hex)).frame(width: 28, height: 28)
                        if current == hex {
                            Image(systemName: "checkmark").font(.caption.bold())
                                .foregroundStyle(hex == "#000000" || hex == "#0000FF" ? Color.white : Color.black)
                        }
                    }.frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(colorName(hex))
                .accessibilityAddTraits(current == hex ? .isSelected : [])
            }
            customSwatch(customHex)
        }
    }

    /// The system picker sits invisibly under our own circle so the swatch matches the presets; the circle never takes the tap.
    private func customSwatch(_ customHex: String?) -> some View {
        ZStack {
            ColorPicker("Custom color", selection: Binding(
                get: { nativeEditorColor(effective.color ?? "#FFFFFF") },
                set: { rule.color = nativeEditorHex($0) }
            ), supportsOpacity: false)
                .labelsHidden().opacity(0.02)
                .frame(width: 44, height: 44)
                .accessibilityValue(customHex ?? "")
                .accessibilityAddTraits(customHex != nil ? .isSelected : [])
                .accessibilityIdentifier("text-preference-color-custom")
            ZStack {
                Circle().fill(KriaColor.fill).frame(width: 40, height: 40)
                if let customHex {
                    Circle().fill(nativeEditorColor(customHex)).frame(width: 28, height: 28)
                    Image(systemName: "checkmark").font(.caption.bold())
                        .foregroundStyle(Self.isLight(customHex) ? Color.black : Color.white)
                } else {
                    Image(systemName: "paintpalette").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink)
                }
            }
            .allowsHitTesting(false).accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, minHeight: 44)
    }

    private static func isLight(_ hex: String) -> Bool {
        let value = UInt32(hex.dropFirst(), radix: 16) ?? 0
        let luma = 0.299 * Double((value >> 16) & 255) + 0.587 * Double((value >> 8) & 255) + 0.114 * Double(value & 255)
        return luma > 150
    }

    private var effectsSection: some View {
        Group {
            groupLabel("Effects")
            effectRow(.outline, id: "text-preference-outline",
                      value: Binding(get: { isOn(effective.strokeWidth.map { $0 != 0 }, fallback: defaults.outline) ? "on" : "off" },
                                     set: { rule.strokeWidth = $0 == "on" ? 2 : 0 }))
            effectRow(.shadow, id: "text-preference-shadow",
                      value: Binding(get: { isOn(effective.shadowEnabled, fallback: defaults.shadow) ? "on" : "off" },
                                     set: { rule.shadowEnabled = $0 == "on" }))
        }.plainRow()
    }

    private func isOn(_ explicit: Bool?, fallback: String) -> Bool { explicit ?? (fallback.lowercased() == "on") }

    /// Label and its source tag left, choices right on one row; a text size too big for that stacks them.
    private func effectRow(_ setting: TextSetting, id: String, value: Binding<String>) -> some View {
        let control = KriaSegmentedControl(options: [(label: "Off", value: "off"), (label: "On", value: "on")], selection: value)
            .accessibilityIdentifier(id)
        return ViewThatFits(in: .horizontal) {
            HStack { settingHeader(setting); Spacer(minLength: 12); control }
            VStack(alignment: .leading, spacing: 4) { settingHeader(setting); control }
        }.padding(.vertical, 2)
    }

    private func pickerRow<Content: View>(_ setting: TextSetting, @ViewBuilder picker: () -> Content) -> some View {
        let picker = picker().labelsHidden().fixedSize().frame(minHeight: 44)
        return ViewThatFits(in: .horizontal) {
            HStack { settingHeader(setting); Spacer(minLength: 12); picker }
            // The menu label carries its own leading inset; pull it back so the value lines up under the title.
            VStack(alignment: .leading, spacing: 0) { settingHeader(setting); picker.padding(.leading, -12) }
        }
    }

    private func settingHeader(_ setting: TextSetting) -> some View {
        HStack(spacing: 8) {
            Text(setting.title)
            sourceTag(setting)
        }
    }

    /// A type either follows All text or has its own value. The tag says which, and "Changed" is also the way back.
    @ViewBuilder private func sourceTag(_ setting: TextSetting) -> some View {
        let id = "text-preference-\(setting.rawValue)-source"
        if !isAllText {
            if rule.overrides(setting) {
                Button { rule.clear(setting) } label: {
                    Text("Changed")
                        .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 10).padding(.vertical, 3)
                        .background(KriaColor.butter, in: Capsule())
                        .frame(minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(setting.title) changed on this type")
                .accessibilityHint("Double tap to follow All text again")
                .accessibilityIdentifier(id)
            } else {
                Text("From All text")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityLabel("\(setting.title) from All text")
                    .accessibilityIdentifier(id)
            }
        }
    }

    private var saveBar: some View {
        VStack(spacing: 8) {
            if model.saved { Label("Saved for future videos.", systemImage: "checkmark.circle.fill").foregroundStyle(KriaColor.success).accessibilityIdentifier("text-preferences-saved") }
            if !model.saved, model.error == nil, !dirtyRoles.isEmpty {
                Text(dirtyRoles.count == 1 ? "Unsaved changes" : "Unsaved changes in \(dirtyRoles.count) types")
                    .foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("text-preferences-unsaved")
            }
            if let error = model.error {
                Text(error).foregroundStyle(KriaColor.failureText)
                Button(model.needsReload ? "Reload preferences" : "Retry") {
                    let discardDraft = model.needsReload
                    Task {
                        await model.load()
                        if discardDraft && model.error == nil { drafts.removeValue(forKey: role); reloadRule() }
                    }
                }.accessibilityIdentifier("text-preferences-reload")
            }
            if model.memory?.textStylingAvailable == true {
                Button {
                    // With nothing pending the visible type is still written, as before.
                    let writes = (dirtyRoles.isEmpty ? [role] : dirtyRoles).map { (role: $0, rule: draft($0) ?? rule) }
                    Task { await model.save(writes) }
                } label: {
                    HStack {
                        if model.isWorking { ProgressView() }
                        Text(model.isWorking ? "Saving…" : "Save preferences").frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(KriaPrimaryButtonStyle())
                .accessibilityIdentifier("text-preferences-save")
                .disabled(model.isWorking || model.needsReload)
            }
        }
        .font(KriaFont.body(14))
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .padding(.horizontal, 20).padding(.vertical, 12)
        .frame(maxWidth: .infinity).background(KriaColor.paper)
    }

    private func reloadRule() { rule = drafts[role] ?? model.memory?.rule(role: role) ?? TextPreferenceRule() }
    private func fontSummary(_ fonts: [String]) -> String {
        if fonts.isEmpty { return "Choose fonts" }
        let names = fonts.prefix(2).joined(separator: ", ")
        return fonts.count > 2 ? names + " and \(fonts.count - 2) more" : names
    }
    private func fontRow(_ title: String, symbol: String, fonts: [String]) -> some View {
        let icon = Image(systemName: symbol).frame(width: 24).foregroundStyle(KriaColor.mutedInk)
        let summary = Text(fontSummary(fonts)).foregroundStyle(KriaColor.mutedInk)
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) { icon; Text(title).lineLimit(1); Spacer(minLength: 8); summary.lineLimit(1) }
            HStack(alignment: .top, spacing: 12) { icon; VStack(alignment: .leading, spacing: 2) { Text(title); summary } }
                .padding(.vertical, 6)
        }.frame(minHeight: 44)
    }
    private func colorName(_ hex: String) -> String {
        ["#FFFFFF": "White", "#000000": "Black", "#FFFF00": "Yellow", "#FF0000": "Red", "#0000FF": "Blue"][hex.uppercased()] ?? hex
    }
}

private struct TextPreferenceFontPicker: View {
    enum Selection { case favorite, excluded }
    @Binding var rule: TextPreferenceRule
    let selection: Selection
    @State private var search = ""
    private var fonts: [String] {
        NativeFontCatalog.shared.pickerFonts.filter { search.isEmpty || $0.localizedCaseInsensitiveContains(search) }
    }
    private func isSelected(_ font: String) -> Bool {
        (selection == .favorite ? rule.favoriteFonts : rule.excludedFonts).contains(font)
    }
    var body: some View {
        List {
            Section {
                ForEach(fonts, id: \.self) { font in
                    Button { toggle(font) } label: {
                        HStack(spacing: 14) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(font).font(NativeFontCatalog.shared.previewFont(font, size: 20))
                                Text("A little moment.").font(NativeFontCatalog.shared.previewFont(font, size: 14)).foregroundStyle(KriaColor.mutedInk)
                            }
                            Spacer()
                            Image(systemName: isSelected(font) ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(isSelected(font) ? KriaColor.ink : KriaColor.line)
                        }.padding(.vertical, 6).frame(minHeight: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(font)
                    .accessibilityIdentifier("text-preference-font-" + font)
                    .accessibilityAddTraits(isSelected(font) ? .isSelected : [])
                }
            } footer: {
                Text(selection == .favorite ? "Kria will favor these fonts when choosing a style." : "Kria will avoid these fonts in future videos. Return to save your choices.")
            }
        }
        .searchable(text: $search, prompt: "Find a font")
        .tint(KriaColor.ink)
        .navigationTitle(selection == .favorite ? "Favorite fonts" : "Never use")
        .navigationBarTitleDisplayMode(.inline)
    }
    private func toggle(_ font: String) {
        if selection == .favorite {
            if isSelected(font) { rule.favoriteFonts.removeAll { $0 == font } }
            else { rule.favoriteFonts.append(font); rule.excludedFonts.removeAll { $0 == font } }
        } else {
            if isSelected(font) { rule.excludedFonts.removeAll { $0 == font } }
            else {
                rule.excludedFonts.append(font); rule.favoriteFonts.removeAll { $0 == font }
                if rule.fontFamily == font { rule.fontFamily = nil; rule.automaticFont = true }
            }
        }
    }
}

private extension View {
    /// Rows sit on paper and are separated by whitespace, as on the account screen: no hairlines, no row fill.
    func plainRow() -> some View {
        listRowInsets(EdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24))
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
    }
}

private extension TextPreferenceMemory.Item {
    var categoryIsText: Bool { (normalizedKey ?? "").hasPrefix("text_style_") || ["font_family", "text_color", "text_position", "stroke_width", "shadow_enabled"].contains(normalizedKey ?? "") }
}

struct SavedTextPreferenceUndo: View {
    let operationID: String
    let api: any KriaAPIClient
    @State private var working = false
    @State private var undone = false
    @State private var error: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if undone { Text("Preference undone.") }
            else {
                Button("Undo saved preference") {
                    working = true
                    Task {
                        defer { working = false }
                        do {
                            let memory = try await api.textPreferenceMemory()
                            _ = try await api.mutateTextPreferenceMemory(path: "me/memory/operations/\(operationID)/undo", method: "POST", body: [
                                "expected_revision": .number(Double(memory.revision)),
                                "idempotency_key": .string(UUID().uuidString)])
                            undone = true; error = nil
                        } catch { self.error = error.localizedDescription }
                    }
                }.disabled(working).accessibilityIdentifier("chat-preference-undo")
            }
            if let error { Text(error).foregroundStyle(KriaColor.failureText) }
        }.font(KriaFont.body(13)).frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct TextPreferenceSuggestionRow: View {
    let item: TextPreferenceMemory.Item
    let decide: (Bool) async -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.instruction)
            if let count = item.evidence?.supportingVideoCount {
                Text("Based on \(count) finished videos").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
            HStack {
                Button { Task { await decide(true) } } label: {
                    Text("Accept").frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
                    .accessibilityIdentifier("text-learning-accept-" + item.id)
                Button { Task { await decide(false) } } label: {
                    Text("Dismiss").frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
                    .accessibilityIdentifier("text-learning-dismiss-" + item.id)
            }
            .buttonStyle(.borderless)
        }
        .accessibilityElement(children: .contain)
    }
}

struct FinishedEditSuggestionsView: View {
    @StateObject private var model: TextPreferencesModel
    let refreshToken: String
    let contentChanged: () -> Void
    init(api: any KriaAPIClient, refreshToken: String, contentChanged: @escaping () -> Void) {
        _model = StateObject(wrappedValue: TextPreferencesModel(api: api))
        self.refreshToken = refreshToken
        self.contentChanged = contentChanged
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let memory = model.memory, memory.editLearning?.enabled == true {
                ForEach(memory.suggestions.filter { $0.sourceKind == "finished_edits" }) { item in
                    TextPreferenceSuggestionRow(item: item) { accept in await model.decide(item, accept: accept) }
                }
                if let error = model.error {
                    Text(error).foregroundStyle(KriaColor.failureText)
                    Button("Reload suggestions") { Task { await model.load() } }
                }
            }
        }
        .disabled(model.isWorking)
        .task(id: refreshToken) { await model.load() }
        .onChange(of: model.memory?.revision) { _, _ in contentChanged() }
    }
}
