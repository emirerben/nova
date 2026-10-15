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
    @State private var color = ""
    private let roles = ["all_text", "title", "label", "pinned", "caption", "lyric"]
    init(api: any KriaAPIClient) { _model = StateObject(wrappedValue: TextPreferencesModel(api: api)) }
    var body: some View {
        Form {
            Section {
                Text("These choices apply to future videos. Instructions for a particular video take priority.")
                Text("Outlines and shadows are off by default.").foregroundStyle(KriaColor.mutedInk)
            }
            if let learning = model.memory?.editLearning, learning.available || learning.enabled {
                Section("Learn from finished edits") {
                    Toggle("Learn from finished edits", isOn: Binding(get: { learning.enabled }, set: { enabled in Task { await model.setLearningEnabled(enabled) } }))
                        .accessibilityIdentifier("text-learning-enabled")
                    Text("Off by default. After the same change appears in three finished videos, Kria can suggest a preference. Nothing changes until you accept.")
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
            if let memory = model.memory, memory.textStylingAvailable {
                Toggle("Use saved preferences", isOn: Binding(get: { memory.enabled }, set: { enabled in Task { await model.setEnabled(enabled) } }))
                    .disabled(model.isWorking)
                if !memory.enabled { Text("Personalization is paused. Enable it to apply saved choices.") }
                Section("Apply to") {
                    Picker("Text role", selection: $role) {
                        ForEach(roles, id: \.self) { Text(label($0)).tag($0) }
                    }.accessibilityIdentifier("text-preference-role")
                }
                Section("Fonts") {
                    Picker("Required font", selection: Binding(get: { rule.fontFamily ?? (rule.automaticFont == true ? "automatic" : "defaults") }, set: {
                        rule.fontFamily = ["automatic", "defaults"].contains($0) ? nil : $0
                        rule.automaticFont = $0 == "defaults" ? nil : $0 == "automatic"
                    })) {
                        Text("Use defaults").tag("defaults")
                        Text("Automatic").tag("automatic")
                        ForEach(NativeFontCatalog.shared.pickerFonts, id: \.self) { Text($0).tag($0) }
                    }.accessibilityIdentifier("text-preference-font")
                    NavigationLink("Favorite fonts (\(rule.favoriteFonts.count))") {
                        fontChoices(title: "Favorite fonts", selected: $rule.favoriteFonts, other: $rule.excludedFonts)
                    }
                    NavigationLink("Excluded fonts (\(rule.excludedFonts.count))") {
                        fontChoices(title: "Excluded fonts", selected: $rule.excludedFonts, other: $rule.favoriteFonts)
                    }
                }
                Section("Placement and color") {
                    Picker("Placement", selection: Binding(get: { rule.position ?? "" }, set: { rule.position = $0.isEmpty ? nil : $0 })) {
                        Text("Use defaults").tag("")
                        ForEach(["top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"], id: \.self) { Text(label($0)).tag($0) }
                    }.accessibilityIdentifier("text-preference-placement")
                    TextField("Color hex, or blank to use defaults", text: $color)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        .accessibilityLabel("Text color hex")
                }
                Section("Effects") {
                    Picker("Outline", selection: Binding(get: { rule.strokeWidth.map { $0 == 0 ? "off" : "on" } ?? "default" }, set: { rule.strokeWidth = $0 == "default" ? nil : ($0 == "off" ? 0 : 2) })) {
                        Text("Use defaults").tag("default"); Text("Off").tag("off"); Text("On").tag("on")
                    }.accessibilityIdentifier("text-preference-outline")
                    Picker("Shadow", selection: Binding(get: { rule.shadowEnabled.map { $0 ? "on" : "off" } ?? "default" }, set: { rule.shadowEnabled = $0 == "default" ? nil : $0 == "on" })) {
                        Text("Use defaults").tag("default"); Text("Off").tag("off"); Text("On").tag("on")
                    }.accessibilityIdentifier("text-preference-shadow")
                }
                Section {
                    status
                    Button("Save preferences") {
                        rule.color = color.isEmpty ? nil : color
                        let savedRole = role, savedRule = rule
                        Task { await model.save(role: savedRole, rule: savedRule) }
                    }.accessibilityIdentifier("text-preferences-save")
                    Button("Reset \(label(role).lowercased()) preferences") {
                        let resetRole = role
                        Task { await model.reset(role: resetRole); reloadRule() }
                    }
                    if let undo = memory.recentUndo { Button("Undo last preference change") { Task { await model.undo(undo.id); reloadRule() } } }
                }.disabled(model.isWorking)
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
                                await model.decide(item, accept: accept); reloadRule()
                            }
                        }
                    }
                }
            } else if model.memory != nil { Text("Text preferences are not available yet.") }
            if model.memory?.textStylingAvailable != true { status }
            if model.isWorking { ProgressView("Loading preferences") }
        }
        .disabled(model.isWorking)
        .scrollContentBackground(.hidden).background(KriaColor.paper)
        .navigationTitle("Text preferences").navigationBarTitleDisplayMode(.inline)
        .task { await model.load(); reloadRule() }
        .onChange(of: role) { _, _ in reloadRule() }
    }
    @ViewBuilder private var status: some View {
        if model.saved { Text("Saved for future videos.").accessibilityIdentifier("text-preferences-saved") }
        if let error = model.error {
            Text(error).foregroundStyle(KriaColor.failureText)
            Button(model.needsReload ? "Reload preferences" : "Retry") { Task { await model.load(); reloadRule() } }
                .accessibilityIdentifier("text-preferences-reload")
        }
    }
    private func reloadRule() { rule = model.memory?.rule(role: role) ?? TextPreferenceRule(); color = rule.color ?? "" }
    private func label(_ value: String) -> String { value.replacingOccurrences(of: "_", with: " ").capitalized }
    private func fontChoices(title: String, selected: Binding<[String]>, other: Binding<[String]>) -> some View {
        List(NativeFontCatalog.shared.pickerFonts, id: \.self) { font in
            Button {
                if selected.wrappedValue.contains(font) { selected.wrappedValue.removeAll { $0 == font } }
                else { selected.wrappedValue.append(font); other.wrappedValue.removeAll { $0 == font } }
            } label: {
                HStack {
                    Text(font).font(NativeFontCatalog.shared.previewFont(font, size: 16))
                    Spacer()
                    if selected.wrappedValue.contains(font) { Image(systemName: "checkmark") }
                }.frame(minHeight: 44).contentShape(Rectangle())
            }.accessibilityAddTraits(selected.wrappedValue.contains(font) ? .isSelected : [])
        }.navigationTitle(title)
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
                Button("Accept") { Task { await decide(true) } }
                    .accessibilityIdentifier("text-learning-accept-" + item.id)
                Button("Dismiss") { Task { await decide(false) } }
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
    init(api: any KriaAPIClient, refreshToken: String) {
        _model = StateObject(wrappedValue: TextPreferencesModel(api: api))
        self.refreshToken = refreshToken
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
    }
}
