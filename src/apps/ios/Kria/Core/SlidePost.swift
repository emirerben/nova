import Foundation
import Combine

struct SlidePostText: Codable, Equatable, Sendable {
    var content: String
    var position: String
}

/// One styled text on a slide. Mirrors the server's `SlideTextElement`; any key this
/// client does not know yet is kept in `extra` and written back untouched, so a newer
/// server field survives an edit made on an older app.
struct SlidePostTextElement: Codable, Equatable, Identifiable, Sendable {
    static let defaultFont = "Inter-Bold"
    static let maxLength = 120
    static let sizeRange = 24...200
    var id: String
    var text: String
    var role = "text"
    var labelSource: String? = nil
    var edited = false
    var fontFamily = SlidePostTextElement.defaultFont
    var color = "#FFFFFF"
    var sizePx = 86
    var alignment = "center"
    var position = "bottom"
    var xFrac: Double? = nil
    var yFrac: Double? = nil
    var maxWidthFrac: Double? = nil
    var strokeWidth = 0
    var shadowEnabled = true
    var background = "none"
    var extra: [String: JSONValue] = [:]

    init(id: String = UUID().uuidString, text: String) { self.id = id; self.text = text }

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ value: String) { stringValue = value }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }
    private static let knownKeys: Set<String> = [
        "id", "text", "role", "label_source", "edited", "font_family", "color", "size_px", "alignment", "position",
        "x_frac", "y_frac", "max_width_frac", "stroke_width", "shadow_enabled", "background",
    ]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        id = try c.decode(String.self, forKey: Key("id"))
        text = try c.decode(String.self, forKey: Key("text"))
        role = try c.decodeIfPresent(String.self, forKey: Key("role")) ?? role
        labelSource = try c.decodeIfPresent(String.self, forKey: Key("label_source"))
        edited = try c.decodeIfPresent(Bool.self, forKey: Key("edited")) ?? edited
        fontFamily = try c.decodeIfPresent(String.self, forKey: Key("font_family")) ?? fontFamily
        color = try c.decodeIfPresent(String.self, forKey: Key("color")) ?? color
        sizePx = try c.decodeIfPresent(Int.self, forKey: Key("size_px")) ?? sizePx
        alignment = try c.decodeIfPresent(String.self, forKey: Key("alignment")) ?? alignment
        position = try c.decodeIfPresent(String.self, forKey: Key("position")) ?? position
        xFrac = try c.decodeIfPresent(Double.self, forKey: Key("x_frac"))
        yFrac = try c.decodeIfPresent(Double.self, forKey: Key("y_frac"))
        maxWidthFrac = try c.decodeIfPresent(Double.self, forKey: Key("max_width_frac"))
        strokeWidth = try c.decodeIfPresent(Int.self, forKey: Key("stroke_width")) ?? strokeWidth
        shadowEnabled = try c.decodeIfPresent(Bool.self, forKey: Key("shadow_enabled")) ?? shadowEnabled
        background = try c.decodeIfPresent(String.self, forKey: Key("background")) ?? background
        for key in c.allKeys where !Self.knownKeys.contains(key.stringValue) {
            extra[key.stringValue] = try c.decode(JSONValue.self, forKey: key)
        }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        for (key, value) in extra where !Self.knownKeys.contains(key) { try c.encode(value, forKey: Key(key)) }
        try c.encode(id, forKey: Key("id"))
        try c.encode(text, forKey: Key("text"))
        try c.encode(role, forKey: Key("role"))
        try c.encodeIfPresent(labelSource, forKey: Key("label_source"))
        try c.encode(edited, forKey: Key("edited"))
        try c.encode(fontFamily, forKey: Key("font_family"))
        try c.encode(color, forKey: Key("color"))
        try c.encode(sizePx, forKey: Key("size_px"))
        try c.encode(alignment, forKey: Key("alignment"))
        try c.encode(position, forKey: Key("position"))
        try c.encodeIfPresent(xFrac, forKey: Key("x_frac"))
        try c.encodeIfPresent(yFrac, forKey: Key("y_frac"))
        try c.encodeIfPresent(maxWidthFrac, forKey: Key("max_width_frac"))
        try c.encode(strokeWidth, forKey: Key("stroke_width"))
        try c.encode(shadowEnabled, forKey: Key("shadow_enabled"))
        try c.encode(background, forKey: Key("background"))
    }

    /// The server's top / center / bottom bucket for this element (its legacy-mirror rule).
    var legacyPositionBucket: String {
        guard position == "custom" else { return position }
        let y = yFrac ?? 0.5
        return y < 0.33 ? "top" : (y > 0.66 ? "bottom" : "center")
    }
    /// Copies everything about how the text looks, including where it sits. Never the words,
    /// identity or label provenance.
    mutating func copyStyle(from other: Self) {
        fontFamily = other.fontFamily; color = other.color; sizePx = other.sizePx; alignment = other.alignment
        position = other.position; xFrac = other.xFrac; yFrac = other.yFrac; maxWidthFrac = other.maxWidthFrac
        strokeWidth = other.strokeWidth; shadowEnabled = other.shadowEnabled; background = other.background
        for (key, value) in other.extra { extra[key] = value }
    }
    var isInvalid: Bool {
        // The server counts code points, not grapheme clusters, so count unicode scalars.
        text.isEmpty || text.unicodeScalars.count > Self.maxLength || fontFamily.isEmpty || !Self.sizeRange.contains(sizePx)
            || (maxWidthFrac.map { !(0.2...1).contains($0) } ?? false)
            || !["left", "center", "right"].contains(alignment) || !["top", "center", "bottom", "custom"].contains(position)
            || !["none", "box"].contains(background) || !Self.isHex(color) || !(0...12).contains(strokeWidth)
            || [xFrac, yFrac].contains { $0.map { !(0...1).contains($0) } ?? false }
    }
    /// Font chips for the Style tab: the default face first, then every live registry font, with
    /// faces that share a font file collapsed (the registry's "Inter" is the default Inter-Bold).
    /// The server accepts every registry name, so no chip can be rejected on save.
    static func fontChoices(catalog: NativeFontCatalog = .shared) -> [String] {
        var seen = Set<String>()
        func key(_ name: String) -> String { catalog.fontURL(for: name)?.lastPathComponent ?? name }
        return ([defaultFont] + catalog.pickerFonts).filter { seen.insert(key($0)).inserted }
    }
    static func isHex(_ value: String) -> Bool {
        value.count == 7 && value.hasPrefix("#") && value.dropFirst().allSatisfy(\.isHexDigit)
    }
}

struct SlidePostEdits: Codable, Equatable, Sendable {
    static let maxTexts = 4
    var text: SlidePostText? = nil
    var lookPreset: String = "none"
    /// The rich model. nil = an older draft that only has the single legacy `text`.
    var texts: [SlidePostTextElement]? = nil
    enum CodingKeys: String, CodingKey { case text, texts; case lookPreset = "look_preset" }
    init(text: SlidePostText? = nil, lookPreset: String = "none", texts: [SlidePostTextElement]? = nil) {
        self.text = text; self.lookPreset = lookPreset; self.texts = texts
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decodeIfPresent(SlidePostText.self, forKey: .text)
        lookPreset = try c.decodeIfPresent(String.self, forKey: .lookPreset) ?? "none"
        texts = try c.decodeIfPresent([SlidePostTextElement].self, forKey: .texts)
    }
    /// Writes both shapes: `texts`, and the legacy `text` mirroring `texts[0]` (the server's own rule).
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if let texts {
            try c.encode(texts, forKey: .texts)
            try c.encodeIfPresent(texts.first.map { SlidePostText(content: $0.text, position: $0.legacyPositionBucket) }, forKey: .text)
        } else {
            try c.encodeIfPresent(text, forKey: .text)
        }
        try c.encode(lookPreset, forKey: .lookPreset)
    }
    /// What the editor shows: `texts`, or the legacy text lifted into one boxed element.
    var effectiveTexts: [SlidePostTextElement] {
        if let texts { return texts }
        guard let text else { return [] }
        var element = SlidePostTextElement(id: "legacy", text: text.content)
        element.position = text.position; element.background = "box"; element.shadowEnabled = false
        return [element]
    }
    /// Replaces the texts and re-derives the legacy mirror so the value always equals its own round trip.
    mutating func setTexts(_ new: [SlidePostTextElement]) {
        texts = new
        text = new.first.map { SlidePostText(content: $0.text, position: $0.legacyPositionBucket) }
    }
}
struct SlidePostSlide: Codable, Equatable, Identifiable, Sendable {
    var id: String
    var assetID: String
    var kind: String
    var alt: String? = nil
    var edits: SlidePostEdits? = nil
    enum CodingKeys: String, CodingKey { case id, kind, alt, edits; case assetID = "asset_id" }
}
struct SlidePostDraft: Codable, Equatable, Sendable {
    var schemaVersion: Int = 1
    var version: Int
    var platformProfile: String
    var slides: [SlidePostSlide]
    var coverIndex: Int = 0
    var caption: String = ""
    var renderedVersion: Int? = nil
    var userEdited: Bool = false
    enum CodingKeys: String, CodingKey {
        case version, slides, caption
        case schemaVersion = "schema_version", platformProfile = "platform_profile", coverIndex = "cover_index"
        case renderedVersion = "rendered_version", userEdited = "user_edited"
    }
    /// Rendering stamps and server metadata do not turn a clean editor dirty.
    func hasSameContent(as other: Self) -> Bool {
        platformProfile == other.platformProfile && slides == other.slides && coverIndex == other.coverIndex && caption == other.caption
    }
    var validationMessage: String? {
        guard ["tiktok_photo", "instagram_carousel"].contains(platformProfile) else { return "Choose a supported post format." }
        let minCount = platformProfile == "instagram_carousel" ? 2 : 1
        let maxCount = platformProfile == "instagram_carousel" ? 20 : 35
        guard (minCount...maxCount).contains(slides.count) else { return "Choose \(minCount)–\(maxCount) slides for this format." }
        guard slides.indices.contains(coverIndex), Set(slides.map(\.id)).count == slides.count,
              Set(slides.map(\.assetID)).count == slides.count else { return "Review the slide order and cover before saving." }
        guard caption.count <= 2200 else { return "Keep the caption under 2,200 characters." }
        guard !slides.contains(where: { !["image", "video"].contains($0.kind) }) else { return "This post contains an unsupported media type." }
        if platformProfile == "tiktok_photo" && slides.contains(where: { $0.kind == "video" }) { return "Choose Instagram carousel to include videos." }
        for slide in slides {
            if let texts = slide.edits?.texts {
                if texts.count > SlidePostEdits.maxTexts || Set(texts.map(\.id)).count != texts.count || texts.contains(where: \.isInvalid) {
                    return "Slide text needs 1–120 characters, and a slide holds up to 4 texts."
                }
            } else if let text = slide.edits?.text, text.content.isEmpty || text.content.count > 120 || !["top", "center", "bottom"].contains(text.position) {
                return "Slide text needs 1–120 characters and a top, center, or bottom position."
            }
            if let look = slide.edits?.lookPreset, !Self.lookPresets.contains(look) { return "Choose a supported look." }
        }
        return nil
    }
    static let lookPresets = ["none", "stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"]
}
struct SlidePostAsset: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let kind: String
    let status: String
    var sourceFilename: String? = nil
    var displayURL: URL? = nil
    var previewURL: URL? = nil
    var sourceURL: URL? = nil
    var durationS: Double? = nil
    var mediaStatus: String? = nil
    enum CodingKeys: String, CodingKey {
        case id, kind, status
        case sourceFilename = "source_filename", displayURL = "display_url", previewURL = "preview_url", sourceURL = "source_url"
        case durationS = "duration_s", mediaStatus = "media_status"
    }
}
struct SlidePostRenderedSlide: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let assetID: String
    let kind: String
    var url: URL? = nil
    var previewURL: URL? = nil
    enum CodingKeys: String, CodingKey { case id, kind, url; case assetID = "asset_id", previewURL = "preview_url" }
}
struct SlidePostValidationError: Codable, Equatable, Sendable {
    let code: String
    let message: String
    var slideID: String? = nil
    enum CodingKeys: String, CodingKey { case code, message; case slideID = "slide_id" }
}
struct SlidePostState: Codable, Equatable, Sendable {
    var schemaVersion: Int = 1
    let itemID: String
    let title: String
    var jobID: UUID? = nil
    var draft: SlidePostDraft? = nil
    var assets: [SlidePostAsset] = []
    var renderStatus: String = "not_rendered"
    var renderedVersion: Int? = nil
    var slides: [SlidePostRenderedSlide] = []
    var validationErrors: [SlidePostValidationError] = []
    var bundleURL: URL? = nil
    enum CodingKeys: String, CodingKey {
        case title, draft, assets, slides
        case schemaVersion = "schema_version", itemID = "item_id", jobID = "job_id", renderStatus = "render_status"
        case renderedVersion = "rendered_version", validationErrors = "validation_errors", bundleURL = "bundle_url"
    }
    var canExport: Bool {
        guard schemaVersion == 1, renderStatus == "ready", validationErrors.isEmpty,
              let draft, draft.validationMessage == nil, draft.version == renderedVersion,
              draft.renderedVersion == draft.version, slides.count == draft.slides.count else { return false }
        // Preserve exact output order and stable identity, not just an asset set.
        return zip(draft.slides, slides).allSatisfy { ref, output in
            ref.id == output.id && ref.assetID == output.assetID && ref.kind == output.kind && output.url?.scheme == "https"
        }
    }
}
struct SlidePostProposal: Codable, Equatable, Sendable {
    var draft: SlidePostDraft
    let baseVersion: Int
    let fallbackUsed: Bool
    let summary: String
    enum CodingKeys: String, CodingKey { case draft, summary; case baseVersion = "base_version", fallbackUsed = "fallback_used" }
}
struct SlidePostProposalRequest: Encodable, Sendable {
    let expectedVersion: Int
    let platformProfile: String
    let assetIDs: [String]?
    let instruction: String
    enum CodingKeys: String, CodingKey {
        case instruction
        case expectedVersion = "expected_version", platformProfile = "platform_profile", assetIDs = "asset_ids"
    }
}
struct SlidePostSaveRequest: Encodable, Sendable {
    let expectedVersion: Int
    let platformProfile: String
    let slides: [SlidePostSlide]
    let coverIndex: Int
    let caption: String
    init(draft: SlidePostDraft, expectedVersion: Int) {
        self.expectedVersion = expectedVersion; platformProfile = draft.platformProfile; slides = draft.slides
        coverIndex = draft.coverIndex; caption = draft.caption
    }
    enum CodingKeys: String, CodingKey {
        case slides, caption
        case expectedVersion = "expected_version", platformProfile = "platform_profile", coverIndex = "cover_index"
    }
}

extension KriaAPI {
    func slidePost(itemID: String) async throws -> SlidePostState {
        try await request(path: "plan-items/\(itemID)/slide-post", method: "GET", bodyData: nil, decode: SlidePostState.self)
    }
    func proposeSlidePost(itemID: String, request body: SlidePostProposalRequest) async throws -> SlidePostProposal {
        try await request(path: "plan-items/\(itemID)/slide-post/propose", method: "POST", bodyData: JSONEncoder().encode(body), decode: SlidePostProposal.self)
    }
    func saveSlidePost(itemID: String, request body: SlidePostSaveRequest) async throws -> SlidePostDraft {
        let result = try await request(path: "plan-items/\(itemID)/slide-post", method: "PUT", bodyData: JSONEncoder().encode(body), decode: SlidePostItemResponse.self)
        guard let draft = result.draft else { throw APIError.invalidResponse }
        return draft
    }
    func generateSlidePost(itemID: String, expectedVersion: Int) async throws {
        let _: SlidePostItemResponse = try await request(path: "plan-items/\(itemID)/slide-post/generate", method: "POST", bodyData: JSONEncoder().encode(["expected_version": expectedVersion]), decode: SlidePostItemResponse.self)
    }
}
private struct SlidePostItemResponse: Decodable {
    let draft: SlidePostDraft?
    enum CodingKeys: String, CodingKey { case draft = "slide_post" }
}

/// A versioned, server-authoritative editor. Unsent text, proposals and local
/// edits survive navigation; local storage never contains signed media URLs.
@MainActor final class SlidePostSession: ObservableObject {
    @Published private(set) var state: SlidePostState?
    @Published var draft: SlidePostDraft? { didSet { persist() } }
    @Published var proposal: SlidePostProposal? { didSet { persist() } }
    @Published var selectedID: String? { didSet { persist() } }
    @Published var instruction = "" { didSet { persist() } }
    @Published private(set) var isBusy = false
    @Published var error: String?
    @Published private(set) var operationMessage: String?
    /// Text being edited on the canvas (not persisted; the text panel owns it).
    @Published var selectedTextID: String?
    /// The local draft always wins (KRI-298): a newer server version is rebased onto silently,
    /// so there is no conflict state. Kept as a constant for older call sites.
    var hasConflict: Bool { false }
    private var baseVersion = 0
    private var undoStack: [SlidePostDraft] = []
    private var redoStack: [SlidePostDraft] = []
    private var lastCoalesceKey: String?
    private static let maxHistory = 100
    private static let maxRebases = 2
    private var baselineDraft: SlidePostDraft?
    private var undoDraft: SlidePostDraft?
    private var itemID: String?
    private var restoring = false
    private var mutationGeneration = 0
    private var refreshSequence = 0
    private var appliedRefreshSequence = 0
    private let defaults: UserDefaults
    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    var readyAssets: [SlidePostAsset] { state?.assets.filter { $0.status == "ready" && $0.mediaStatus != "missing" } ?? [] }
    var selectedSlide: SlidePostSlide? { (draft ?? proposal?.draft)?.slides.first { $0.id == selectedID } ?? (draft ?? proposal?.draft)?.slides.first }
    var selectedAsset: SlidePostAsset? { state?.assets.first { $0.id == selectedSlide?.assetID } }
    var hasUnsavedChanges: Bool {
        guard let draft else { return false }
        guard let saved = state?.draft else { return true }
        return !draft.hasSameContent(as: saved)
    }
    var canExport: Bool { state?.canExport == true && !hasUnsavedChanges && !isBusy }
    /// Undo of the last SAVED change (the server's previous version).
    var canUndo: Bool { undoDraft != nil && !isBusy }
    /// Undo/redo of unsaved edits in this editor.
    var canUndoEdit: Bool { !undoStack.isEmpty && !isBusy }
    var canRedoEdit: Bool { !redoStack.isEmpty && !isBusy }
    var isRendering: Bool { ["pending", "queued", "generating", "rendering", "processing"].contains(state?.renderStatus ?? "") }

    func refresh(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy else { return }
        let generation = mutationGeneration
        refreshSequence += 1
        let sequence = refreshSequence
        do {
            let result = try await api.slidePost(itemID: itemID)
            // An upload refresh must not roll a completed save back, or let an
            // older poll replace a more recent render/status projection.
            guard generation == mutationGeneration, !isBusy, sequence >= appliedRefreshSequence else { return }
            guard result.schemaVersion == 1, result.itemID == itemID else { throw APIError.invalidResponse }
            if self.itemID != itemID {
                restoring = true
                self.itemID = itemID
                restore()
                restoring = false
            }
            appliedRefreshSequence = sequence
            adopt(result)
        } catch is CancellationError { } catch { self.error = error.localizedDescription }
    }

    /// Export requires an authoritative check; a cached ready projection is
    /// insufficient when another device can save a newer version.
    func revalidateForExport(api: any KriaAPIClient, itemID: String) async throws {
        guard !isBusy, !hasUnsavedChanges else { throw APIError.conflict }
        let generation = mutationGeneration
        let result = try await api.slidePost(itemID: itemID)
        guard result.schemaVersion == 1, result.itemID == itemID else { throw APIError.invalidResponse }
        guard generation == mutationGeneration, !isBusy, !hasUnsavedChanges,
              (result.draft?.version ?? 0) >= baseVersion else { throw APIError.conflict }
        // Invalidate background GETs that began before this authoritative read.
        mutationGeneration += 1
        adopt(result)
        guard canExport else { throw APIError.conflict }
    }

    private func adopt(_ result: SlidePostState) {
        let remoteVersion = result.draft?.version ?? 0
        guard remoteVersion >= baseVersion else { return }
        let wasDirty = draft.map { local in baselineDraft.map { !local.hasSameContent(as: $0) } ?? true } ?? false
        state = result
        baselineDraft = result.draft
        // A clean copy follows the server; unsaved edits stay and simply rebase onto the new version.
        if !wasDirty {
            // Undo must never restore a snapshot taken before a different remote draft was adopted.
            let changed: Bool
            if let local = draft, let remote = result.draft { changed = !local.hasSameContent(as: remote) } else { changed = (draft == nil) != (result.draft == nil) }
            if changed { undoStack = []; redoStack = []; lastCoalesceKey = nil }
            draft = result.draft
        }
        baseVersion = remoteVersion
        if let selectedID, (draft ?? proposal?.draft)?.slides.contains(where: { $0.id == selectedID }) != true {
            self.selectedID = (draft ?? proposal?.draft)?.slides.first?.id
        } else if selectedID == nil { selectedID = (draft ?? proposal?.draft)?.slides.first?.id }
        persist()
    }

    func discardLocalChanges() {
        guard let state else { return }
        error = nil; proposal = nil; undoDraft = nil; undoStack = []; redoStack = []; lastCoalesceKey = nil
        baseVersion = state.draft?.version ?? 0; draft = state.draft
        selectedID = draft?.slides.first?.id
    }

    func propose(api: any KriaAPIClient, itemID: String, instruction: String, platformProfile: String? = nil) async {
        guard !isBusy else { return }
        guard !hasUnsavedChanges else { error = "Save your slide edits before asking Kria for another direction."; return }
        let prompt = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, prompt.count <= 2000 else { error = "Tell Kria your direction in 2,000 characters or fewer."; return }
        guard !readyAssets.isEmpty else { error = "Add photos or videos and wait for them to finish preparing."; return }
        mutationGeneration += 1
        self.instruction = instruction; isBusy = true; error = nil; operationMessage = "Kria is arranging your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            let profile = platformProfile ?? draft?.platformProfile ?? proposal?.draft.platformProfile ?? (readyAssets.contains(where: { $0.kind == "video" }) ? "instagram_carousel" : "tiktok_photo")
            let ids = draft?.slides.map(\.assetID) ?? proposal?.draft.slides.map(\.assetID) ?? readyAssets.map(\.id)
            let result = try await api.proposeSlidePost(itemID: itemID, request: .init(expectedVersion: baseVersion, platformProfile: profile, assetIDs: ids, instruction: prompt))
            guard result.baseVersion == baseVersion else { throw APIError.conflict }
            proposal = result
            if selectedID == nil { selectedID = result.draft.slides.first?.id }
        } catch { handle(error) }
    }

    func applyProposal(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, let proposal else { return }
        guard let message = proposal.draft.validationMessage else {
            await persistDraft(proposal.draft, api: api, itemID: itemID)
            return
        }
        error = message
    }

    func save(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, let draft else { return }
        if let message = draft.validationMessage { error = message; return }
        await persistDraft(draft, api: api, itemID: itemID)
    }

    private func persistDraft(_ value: SlidePostDraft, api: any KriaAPIClient, itemID: String) async {
        mutationGeneration += 1
        isBusy = true; error = nil; operationMessage = "Saving your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            let previous = state?.draft
            var rebases = 0
            var saved: SlidePostDraft
            while true {
                do {
                    saved = try await api.saveSlidePost(itemID: itemID, request: .init(draft: value, expectedVersion: baseVersion))
                    break
                } catch let failure as APIError where failure == .conflict && rebases < Self.maxRebases {
                    // The newest thing the user saw in the editor wins: learn the server's current
                    // version and write the local draft over it. No banner, no choice.
                    rebases += 1
                    let latest = try await api.slidePost(itemID: itemID)
                    guard latest.schemaVersion == 1, latest.itemID == itemID else { throw APIError.invalidResponse }
                    baseVersion = latest.draft?.version ?? baseVersion
                }
            }
            baseVersion = saved.version; baselineDraft = saved; proposal = nil; undoDraft = previous
            // Saved content is what the user was looking at; keep any later undo history but
            // swap in the server's stamps (version, rendered_version).
            draft = saved
            // Clear old output immediately, even if the follow-up GET fails.
            state?.draft = saved; state?.slides = []; state?.bundleURL = nil
            state?.renderedVersion = saved.renderedVersion
            if state?.jobID != nil { state?.renderStatus = "rendering" }
            let result = try await api.slidePost(itemID: itemID)
            adopt(result)
        } catch { handle(error) }
    }

    func create(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy else { return }
        error = nil
        if proposal != nil { await applyProposal(api: api, itemID: itemID) }
        else if hasUnsavedChanges { await save(api: api, itemID: itemID) }
        guard error == nil, let draft, draft.validationMessage == nil else { return }
        if isRendering { return }
        mutationGeneration += 1
        isBusy = true; error = nil; operationMessage = "Creating your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            try await api.generateSlidePost(itemID: itemID, expectedVersion: draft.version)
            state?.renderStatus = "rendering"; state?.slides = []; state?.bundleURL = nil
            let result = try await api.slidePost(itemID: itemID)
            adopt(result)
        } catch { handle(error) }
    }

    func undo(api: any KriaAPIClient, itemID: String) async {
        guard canUndo, let previous = undoDraft else { return }
        await persistDraft(previous, api: api, itemID: itemID)
    }

    // MARK: Unsaved-edit history

    /// The single way an edit reaches `draft`: pushes the previous draft onto the undo stack and
    /// clears redo. `coalescing` merges a run of the same gesture (typing, a slider drag) into one
    /// undo step. Also the entry point for a staged AI result (Lane E): stage it, show it, and the
    /// user can undo it before saving.
    func stageDraft(_ new: SlidePostDraft, coalescing key: String? = nil) {
        // Dropped while a save/propose is in flight: the draft is being rebased onto the server's
        // answer, and the controls that call this are disabled/veiled in that window anyway.
        guard !isBusy, let current = draft, !new.hasSameContent(as: current) else { return }
        if key == nil || key != lastCoalesceKey {
            undoStack.append(current)
            if undoStack.count > Self.maxHistory { undoStack.removeFirst() }
        }
        lastCoalesceKey = key
        redoStack = []
        draft = new
    }
    func undoEdit() {
        guard !isBusy, let current = draft, let previous = undoStack.popLast() else { return }
        redoStack.append(current)
        restoreHistory(previous, over: current)
    }
    func redoEdit() {
        guard !isBusy, let current = draft, let next = redoStack.popLast() else { return }
        undoStack.append(current)
        restoreHistory(next, over: current)
    }
    private func restoreHistory(_ entry: SlidePostDraft, over current: SlidePostDraft) {
        var value = entry
        value.version = current.version; value.renderedVersion = current.renderedVersion; value.userEdited = current.userEdited
        lastCoalesceKey = nil
        draft = value
        if let selectedID, !value.slides.contains(where: { $0.id == selectedID }) { self.selectedID = value.slides.first?.id }
        if let selectedTextID, selectedSlide?.edits?.effectiveTexts.contains(where: { $0.id == selectedTextID }) != true { self.selectedTextID = nil }
    }

    // MARK: Slide edits

    func updateSlide(_ slide: SlidePostSlide) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slide.id }) else { return }
        value.slides[index] = slide
        stageDraft(value)
    }
    func setCover(id: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == id }) else { return }
        value.coverIndex = index
        stageDraft(value)
    }
    func setCaption(_ caption: String) {
        guard var value = draft else { return }
        value.caption = String(caption.prefix(2200))
        stageDraft(value, coalescing: "caption")
    }
    func moveSlide(id: String, offset: Int) {
        guard let from = draft?.slides.firstIndex(where: { $0.id == id }) else { return }
        moveSlide(id: id, toIndex: from + offset)
    }
    /// Reorders keeping the cover on the same slide (identity, not position).
    func moveSlide(id: String, toIndex to: Int) {
        guard var value = draft, let from = value.slides.firstIndex(where: { $0.id == id }),
              value.slides.indices.contains(to), to != from else { return }
        let coverID = value.slides[value.coverIndex].id
        let slide = value.slides.remove(at: from); value.slides.insert(slide, at: to)
        value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        stageDraft(value)
    }
    func removeSlide(id: String) {
        guard var value = draft, value.slides.count > 1 else { return }
        let coverID = value.slides[value.coverIndex].id
        value.slides.removeAll { $0.id == id }; value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        stageDraft(value)
        if selectedID == id { selectedID = value.slides.first?.id }
    }
    func addAsset(id: String) {
        guard var value = draft,
              let asset = readyAssets.first(where: { $0.id == id }), !value.slides.contains(where: { $0.assetID == id }) else { return }
        value.slides.append(.init(id: UUID().uuidString, assetID: id, kind: asset.kind))
        stageDraft(value)
    }

    // MARK: Slide text

    /// Adds a text to the slide (up to four) and selects it. The first rich edit of a legacy slide
    /// carries its old text over as `texts[0]` so nothing is lost.
    @discardableResult
    func addText(slideID: String, text: String = "Your text") -> String? {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard texts.count < SlidePostEdits.maxTexts else { return nil }
        let element = SlidePostTextElement(text: text)
        texts.append(element)
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        selectedTextID = element.id
        return element.id
    }
    /// Edits one text in place. `coalescing` collapses a continuous gesture into one undo step.
    func updateText(slideID: String, textID: String, coalescing key: String? = nil, _ mutate: (inout SlidePostTextElement) -> Void) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard let at = texts.firstIndex(where: { $0.id == textID }) else { return }
        let before = texts[at]
        mutate(&texts[at])
        if texts[at].role == "label", texts[at] != before { texts[at].edited = true }
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value, coalescing: key.map { "\($0)-\(textID)" })
    }
    func removeText(slideID: String, textID: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        texts.removeAll { $0.id == textID }
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        if selectedTextID == textID { selectedTextID = texts.first?.id }
    }
    /// Copies a text onto the same slide (the slide itself cannot be duplicated: one asset, one slide).
    @discardableResult
    func duplicateText(slideID: String, textID: String) -> String? {
        guard let source = draft?.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.first(where: { $0.id == textID }),
              var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard texts.count < SlidePostEdits.maxTexts else { return nil }
        var copy = source
        copy.id = UUID().uuidString
        if copy.position == "custom", let y = copy.yFrac { copy.yFrac = min(1, y + 0.08) }
        texts.append(copy)
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        selectedTextID = copy.id
        return copy.id
    }
    /// One undo step: every other slide's texts take this text's whole look, position included.
    /// Words, ids and label provenance are never copied.
    func applyStyleToAllSlides(slideID: String, textID: String) {
        guard var value = draft,
              let source = value.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.first(where: { $0.id == textID }) else { return }
        for index in value.slides.indices where value.slides[index].id != slideID {
            guard var edits = value.slides[index].edits, !edits.effectiveTexts.isEmpty else { continue }
            var texts = edits.effectiveTexts
            // Only the matching text takes the look (and position); siblings keep their own place.
            let sourceIndex = value.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.firstIndex(where: { $0.id == textID }) ?? 0
            texts[sourceIndex < texts.count ? sourceIndex : 0].copyStyle(from: source)
            edits.setTexts(texts)
            value.slides[index].edits = edits
        }
        stageDraft(value)
    }
    func setLook(slideID: String, preset: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        edits.lookPreset = preset
        value.slides[index].edits = edits
        stageDraft(value)
    }
    /// Drops texts the user left empty (the server rejects them) when text editing ends.
    func removeEmptyTexts(slideID: String) {
        guard let texts = draft?.slides.first(where: { $0.id == slideID })?.edits?.texts, texts.contains(where: { $0.text.isEmpty }),
              var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        edits.setTexts(texts.filter { !$0.text.isEmpty })
        value.slides[index].edits = edits
        stageDraft(value)
    }

    private func handle(_ error: Error) {
        if let apiError = error as? APIError, case .conflict = apiError {
            self.error = "This post kept changing while we saved. Your edits are still here. Try again."
        } else { self.error = error.localizedDescription }
    }
    private struct LocalState: Codable {
        let draft: SlidePostDraft?
        let proposal: SlidePostProposal?
        let selectedID: String?
        let instruction: String
        let baseVersion: Int
        let baselineDraft: SlidePostDraft?
    }
    private func persist() {
        guard !restoring, let itemID else { return }
        let local = LocalState(draft: draft, proposal: proposal, selectedID: selectedID, instruction: instruction, baseVersion: baseVersion, baselineDraft: baselineDraft)
        guard let data = try? JSONEncoder().encode(local) else { return }
        defaults.set(data, forKey: "kria.slide-post.\(itemID)")
    }
    private func restore() {
        guard let itemID, let data = defaults.data(forKey: "kria.slide-post.\(itemID)"), let local = try? JSONDecoder().decode(LocalState.self, from: data) else {
            draft = nil; proposal = nil; selectedID = nil; instruction = ""; baseVersion = 0; baselineDraft = nil; return
        }
        draft = local.draft; proposal = local.proposal; selectedID = local.selectedID; instruction = local.instruction; baseVersion = local.baseVersion; baselineDraft = local.baselineDraft
    }
}

/// Pure preview geometry, mirroring the server's `render_text_element_png`: `x_frac` is the line's
/// left edge for left alignment, its right edge for right alignment and its centre for centre;
/// `y_frac` (or a vertical preset) is the block's vertical centre.
enum SlidePostTextLayout {
    static let xDefaults: [String: Double] = ["left": 0.08, "center": 0.5, "right": 0.92]
    static let yPresets: [String: Double] = ["top": 0.12, "center": 0.5, "bottom": 0.82]

    static func anchor(for element: SlidePostTextElement) -> (x: Double, y: Double) {
        let x = element.xFrac ?? xDefaults[element.alignment] ?? 0.5
        let y = element.position == "custom" ? (element.yFrac ?? 0.5) : (yPresets[element.position] ?? 0.82)
        return (x, y)
    }
    /// The anchor after a drag of `translation` points, as canvas fractions (clamped to 0...1).
    /// Starting from the edge anchor means dragging a left/right text writes an edge x, not a centre.
    static func dragged(from element: SlidePostTextElement, translation: CGSize, canvas: CGSize) -> (x: Double, y: Double) {
        guard canvas.width > 0, canvas.height > 0 else { return anchor(for: element) }
        let start = anchor(for: element)
        return (min(max(start.x + Double(translation.width / canvas.width), 0), 1),
                min(max(start.y + Double(translation.height / canvas.height), 0), 1))
    }
}
