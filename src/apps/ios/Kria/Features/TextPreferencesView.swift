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
    enum CodingKeys: String, CodingKey {
        case featureAvailable = "feature_available", enabled, revision, active, suggestions
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
    func save(role: String, rule: TextPreferenceRule) async {
        guard let memory, memory.textStylingAvailable else { return }
        let key = "text_style_" + role
        do {
            let value = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(rule))
            let item = memory.active.first { $0.normalizedKey == key }
            try await mutate(path: "me/memory/items" + (item.map { "/" + $0.id } ?? ""),
                             method: item == nil ? "POST" : "PATCH", extra: [
                                "instruction": .string("My saved \(role.replacingOccurrences(of: "_", with: " ")) text preferences"),
                                "category": .string("video_style"), "enforcement": .string("default"),
                                "normalized_key": .string(key), "structured_value": .object([key: value])])
            saved = true
        } catch { show(error) }
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
        isWorking = true; saved = false
        defer { isWorking = false }
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
    private let roles = ["all_text", "title", "label", "pinned", "caption", "lyric"]
    init(api: any KriaAPIClient) { _model = StateObject(wrappedValue: TextPreferencesModel(api: api)) }

    var body: some View {
        Form {
            if let memory = model.memory, memory.textStylingAvailable {
                Section {
                    Toggle("Use saved preferences", isOn: Binding(get: { memory.enabled }, set: { enabled in Task { await model.setEnabled(enabled) } }))
                        .accessibilityIdentifier("text-preferences-enabled")
                    if !memory.enabled { Text("Paused. Turn this on to use your choices in new videos.").foregroundStyle(KriaColor.mutedInk) }
                    Picker("Apply to", selection: $role) {
                        ForEach(roles, id: \.self) { Text(label($0)).tag($0) }
                    }.accessibilityIdentifier("text-preference-role")
                } footer: {
                    Text("For future videos. Instructions for one video take priority.")
                }
                fontSection
                Section {
                    TextPreferencePreview(rule: rule)
                        .listRowBackground(KriaColor.softZinc)
                }
                placementSection
                effectsSection
                Section {
                    Button("Reset \(label(role).lowercased()) preferences") {
                        let resetRole = role
                        Task {
                            await model.reset(role: resetRole)
                            if model.error == nil { drafts.removeValue(forKey: resetRole); reloadRule() }
                        }
                    }
                    if let undo = memory.recentUndo {
                        Button("Undo last preference change") {
                            Task { await model.undo(undo.id); if model.error == nil { drafts.removeValue(forKey: role); reloadRule() } }
                        }.accessibilityIdentifier("text-preferences-undo")
                    }
                }
                let other = memory.active.filter { $0.categoryIsText && !($0.normalizedKey ?? "").hasPrefix("text_style_") }
                if !other.isEmpty {
                    Section("Other saved text instructions") {
                        ForEach(other) { item in
                            Text(item.instruction)
                            Button("Forget this instruction") { Task { await model.forget(item) } }
                        }
                    }
                }
                if !memory.suggestions.isEmpty {
                    Section("Suggestions") {
                        ForEach(memory.suggestions.filter(\.categoryIsText)) { item in
                            TextPreferenceSuggestionRow(item: item) { accept in
                                await model.decide(item, accept: accept)
                                if model.error == nil { drafts.removeValue(forKey: role); reloadRule() }
                            }
                        }
                    }
                }
            } else if model.memory != nil { Text("Text preferences are not available yet.") }
            if let learning = model.memory?.editLearning, learning.available || learning.enabled {
                Section("Learn from finished edits") {
                    Toggle("Learn from finished edits", isOn: Binding(get: { learning.enabled }, set: { enabled in Task { await model.setLearningEnabled(enabled) } }))
                        .accessibilityIdentifier("text-learning-enabled")
                    Text("After the same change in three finished videos, Kria can suggest a preference. You choose whether to save it.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    Text("Turning this off clears learning history and pending suggestions. Approved preferences stay saved.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    if learning.enabled {
                        Button("Reset learning history") { Task { await model.resetLearningHistory() } }
                            .accessibilityIdentifier("text-learning-reset")
                    }
                    if model.learningHistoryReset { Text("Learning history reset.") }
                }
            }
            if model.isWorking && model.memory == nil { ProgressView("Loading preferences") }
        }
        .disabled(model.isWorking)
        .tint(KriaColor.ink)
        .scrollContentBackground(.hidden).background(KriaColor.paper)
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
    }

    private var fontSection: some View {
        Section("Fonts") {
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
            Picker("Font", selection: Binding(get: { rule.fontFamily ?? (rule.automaticFont == true ? "automatic" : "defaults") }, set: {
                rule.fontFamily = ["automatic", "defaults"].contains($0) ? nil : $0
                rule.automaticFont = $0 == "defaults" ? nil : $0 == "automatic"
                if let font = rule.fontFamily { rule.excludedFonts.removeAll { $0 == font } }
            })) {
                Text("Use defaults").tag("defaults")
                Text("Automatic").tag("automatic")
                ForEach(NativeFontCatalog.shared.pickerFonts, id: \.self) { Text($0).tag($0) }
            }.accessibilityIdentifier("text-preference-font")
        }
    }

    private var placementSection: some View {
        Section("Placement and color") {
            Picker("Placement", selection: Binding(get: { rule.position ?? "" }, set: { rule.position = $0.isEmpty ? nil : $0 })) {
                Text("Use defaults").tag("")
                ForEach(["top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"], id: \.self) { Text(label($0)).tag($0) }
            }.accessibilityIdentifier("text-preference-placement")
            HStack {
                Text("Color")
                Spacer()
                ForEach(["#FFFFFF", "#000000", "#FFFF00", "#FF0000", "#0000FF"], id: \.self) { hex in
                    Button { rule.color = rule.color == hex ? nil : hex } label: {
                        Circle().fill(nativeEditorColor(hex))
                            .overlay(Circle().strokeBorder(KriaColor.line, lineWidth: 1))
                            .overlay { if rule.color == hex { Image(systemName: "checkmark").font(.caption.bold()).foregroundStyle(hex == "#000000" || hex == "#0000FF" ? Color.white : Color.black) } }
                            .frame(width: 28, height: 28).frame(minWidth: 44, minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(colorName(hex))
                    .accessibilityAddTraits(rule.color == hex ? .isSelected : [])
                }
            }
            .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 8))
            TextField("Custom color, e.g. #FFFFFF", text: Binding(get: { rule.color ?? "" }, set: { rule.color = $0.isEmpty ? nil : $0 }))
                .textInputAutocapitalization(.characters).autocorrectionDisabled()
                .accessibilityLabel("Text color hex")
        }
    }

    private var effectsSection: some View {
        Section {
            Picker("Outline", selection: Binding(get: { rule.strokeWidth.map { $0 == 0 ? "off" : "on" } ?? "default" }, set: { rule.strokeWidth = $0 == "default" ? nil : ($0 == "off" ? 0 : 2) })) {
                Text("Use defaults").tag("default"); Text("Off").tag("off"); Text("On").tag("on")
            }.accessibilityIdentifier("text-preference-outline")
            Picker("Shadow", selection: Binding(get: { rule.shadowEnabled.map { $0 ? "on" : "off" } ?? "default" }, set: { rule.shadowEnabled = $0 == "default" ? nil : $0 == "on" })) {
                Text("Use defaults").tag("default"); Text("Off").tag("off"); Text("On").tag("on")
            }.accessibilityIdentifier("text-preference-shadow")
        } header: { Text("Effects") } footer: { Text("Outlines and shadows are off by default.") }
    }

    private var saveBar: some View {
        VStack(spacing: 8) {
            if model.saved { Label("Saved for future videos.", systemImage: "checkmark.circle.fill").foregroundStyle(KriaColor.success).accessibilityIdentifier("text-preferences-saved") }
            if let memory = model.memory, !model.saved, model.error == nil, rule != memory.rule(role: role) {
                Text("Unsaved changes").foregroundStyle(KriaColor.mutedInk)
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
                    let savedRole = role, savedRule = rule
                    Task { await model.save(role: savedRole, rule: savedRule) }
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
        .padding(.horizontal, 20).padding(.vertical, 12)
        .frame(maxWidth: .infinity).background(KriaColor.paper)
    }

    private func reloadRule() { rule = drafts[role] ?? model.memory?.rule(role: role) ?? TextPreferenceRule() }
    private func label(_ value: String) -> String { value == "pinned" ? "Corner text" : value.replacingOccurrences(of: "_", with: " ").capitalized }
    private func fontSummary(_ fonts: [String]) -> String {
        if fonts.isEmpty { return "Choose fonts" }
        let names = fonts.prefix(2).joined(separator: ", ")
        return fonts.count > 2 ? names + " and \(fonts.count - 2) more" : names
    }
    private func fontRow(_ title: String, symbol: String, fonts: [String]) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 24).foregroundStyle(KriaColor.mutedInk)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                Text(fontSummary(fonts)).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
        }.padding(.vertical, 4)
    }
    private func colorName(_ hex: String) -> String {
        ["#FFFFFF": "White", "#000000": "Black", "#FFFF00": "Yellow", "#FF0000": "Red", "#0000FF": "Blue"][hex] ?? hex
    }
}

private struct TextPreferencePreview: View {
    let rule: TextPreferenceRule
    private var font: String? {
        rule.fontFamily ?? rule.favoriteFonts.first ?? ["Inter", "Hanken Grotesk", "Poppins Bold"].first { !rule.excludedFonts.contains($0) }
    }
    private var backgroundColor: Color {
        let rgb = UInt32((rule.color ?? "#30352C").replacingOccurrences(of: "#", with: ""), radix: 16) ?? 0x30352C
        let brightness = Double((rgb >> 16) & 255) * 0.299 + Double((rgb >> 8) & 255) * 0.587 + Double(rgb & 255) * 0.114
        return brightness > 128 ? KriaColor.ink : KriaColor.sage
    }
    private var alignment: Alignment {
        switch rule.position {
        case "top_left": .topLeading
        case "top": .top
        case "top_right": .topTrailing
        case "bottom_left": .bottomLeading
        case "bottom": .bottom
        case "bottom_right": .bottomTrailing
        default: .center
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Font preview").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            Text("A little moment.")
                .font(NativeFontCatalog.shared.previewFont(font, size: 27))
                .foregroundStyle(rule.color.map { nativeEditorColor($0) } ?? KriaColor.ink)
                .shadow(color: rule.shadowEnabled == true ? .black.opacity(0.4) : .clear, radius: 2, y: 2)
                .padding(18).frame(maxWidth: .infinity, minHeight: 100, alignment: alignment)
                .background(backgroundColor).clipShape(RoundedRectangle(cornerRadius: 16))
                .accessibilityIdentifier("text-preferences-preview")
        }.padding(.vertical, 6)
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
