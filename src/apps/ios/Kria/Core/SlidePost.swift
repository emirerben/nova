import Foundation
import Combine

struct SlidePostText: Codable, Equatable, Sendable {
    var content: String
    var position: String
}
struct SlidePostEdits: Codable, Equatable, Sendable {
    var text: SlidePostText? = nil
    var lookPreset: String = "none"
    enum CodingKeys: String, CodingKey { case text; case lookPreset = "look_preset" }
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
            if let text = slide.edits?.text, text.content.isEmpty || text.content.count > 120 || !["top", "center", "bottom"].contains(text.position) {
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
    @Published private(set) var hasConflict = false
    private var baseVersion = 0
    private var baselineDraft: SlidePostDraft?
    private var undoDraft: SlidePostDraft?
    private var itemID: String?
    private var restoring = false
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
    var canExport: Bool { state?.canExport == true && !hasUnsavedChanges && !hasConflict && !isBusy }
    var canUndo: Bool { undoDraft != nil && !hasConflict && !isBusy }
    var isRendering: Bool { ["pending", "queued", "generating", "rendering", "processing"].contains(state?.renderStatus ?? "") }

    func refresh(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy else { return }
        do {
            let result = try await api.slidePost(itemID: itemID)
            guard result.schemaVersion == 1, result.itemID == itemID else { throw APIError.invalidResponse }
            if self.itemID != itemID {
                restoring = true
                self.itemID = itemID
                restore()
                restoring = false
            }
            adopt(result)
        } catch is CancellationError { } catch { self.error = error.localizedDescription }
    }

    private func adopt(_ result: SlidePostState) {
        let remoteVersion = result.draft?.version ?? 0
        let wasDirty = draft.map { local in baselineDraft.map { !local.hasSameContent(as: $0) } ?? true } ?? false
        state = result
        baselineDraft = result.draft
        if (wasDirty || proposal != nil) && baseVersion != remoteVersion {
            hasConflict = true
            error = "This post changed on another device. Your edits are kept here. Reload the saved post before editing again."
        } else if !wasDirty {
            draft = result.draft
            baseVersion = remoteVersion
        }
        if let selectedID, (draft ?? proposal?.draft)?.slides.contains(where: { $0.id == selectedID }) != true {
            self.selectedID = (draft ?? proposal?.draft)?.slides.first?.id
        } else if selectedID == nil { selectedID = (draft ?? proposal?.draft)?.slides.first?.id }
        persist()
    }

    func discardLocalChanges() {
        guard let state else { return }
        hasConflict = false; error = nil; proposal = nil; undoDraft = nil
        baseVersion = state.draft?.version ?? 0; draft = state.draft
        selectedID = draft?.slides.first?.id
    }

    func propose(api: any KriaAPIClient, itemID: String, instruction: String, platformProfile: String? = nil) async {
        guard !isBusy, !hasConflict else { return }
        guard !hasUnsavedChanges else { error = "Save your slide edits before asking Kria for another direction."; return }
        let prompt = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, prompt.count <= 2000 else { error = "Tell Kria your direction in 2,000 characters or fewer."; return }
        guard !readyAssets.isEmpty else { error = "Add photos or videos and wait for them to finish preparing."; return }
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
        guard !isBusy, !hasConflict, let proposal else { return }
        guard proposal.baseVersion == baseVersion else { handle(APIError.conflict); return }
        guard let message = proposal.draft.validationMessage else {
            await persistDraft(proposal.draft, api: api, itemID: itemID)
            return
        }
        error = message
    }

    func save(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, !hasConflict, let draft else { return }
        if let message = draft.validationMessage { error = message; return }
        await persistDraft(draft, api: api, itemID: itemID)
    }

    private func persistDraft(_ value: SlidePostDraft, api: any KriaAPIClient, itemID: String) async {
        isBusy = true; error = nil; operationMessage = "Saving your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            let previous = state?.draft
            let saved = try await api.saveSlidePost(itemID: itemID, request: .init(draft: value, expectedVersion: baseVersion))
            baseVersion = saved.version; baselineDraft = saved; draft = saved; proposal = nil; undoDraft = previous
            // Clear old output immediately, even if the follow-up GET fails.
            state?.draft = saved; state?.slides = []; state?.bundleURL = nil
            state?.renderedVersion = saved.renderedVersion
            if state?.jobID != nil { state?.renderStatus = "rendering" }
            let result = try await api.slidePost(itemID: itemID)
            adopt(result)
        } catch { handle(error) }
    }

    func create(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, !hasConflict else { return }
        error = nil
        if proposal != nil { await applyProposal(api: api, itemID: itemID) }
        else if hasUnsavedChanges { await save(api: api, itemID: itemID) }
        guard error == nil, let draft, draft.validationMessage == nil else { return }
        if isRendering { return }
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

    func updateSlide(_ slide: SlidePostSlide) {
        guard !isBusy, !hasConflict, let index = draft?.slides.firstIndex(where: { $0.id == slide.id }) else { return }
        draft?.slides[index] = slide
    }
    func setCover(id: String) {
        guard !isBusy, !hasConflict, let index = draft?.slides.firstIndex(where: { $0.id == id }) else { return }
        draft?.coverIndex = index
    }
    func moveSlide(id: String, offset: Int) {
        guard !isBusy, !hasConflict, var value = draft, let from = value.slides.firstIndex(where: { $0.id == id }) else { return }
        let to = from + offset
        guard value.slides.indices.contains(to) else { return }
        let coverID = value.slides[value.coverIndex].id
        let slide = value.slides.remove(at: from); value.slides.insert(slide, at: to)
        value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        draft = value
    }
    func removeSlide(id: String) {
        guard !isBusy, !hasConflict, var value = draft, value.slides.count > 1 else { return }
        let coverID = value.slides[value.coverIndex].id
        value.slides.removeAll { $0.id == id }; value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        draft = value
        if selectedID == id { selectedID = value.slides.first?.id }
    }
    func addAsset(id: String) {
        guard !isBusy, !hasConflict, var value = draft,
              let asset = readyAssets.first(where: { $0.id == id }), !value.slides.contains(where: { $0.assetID == id }) else { return }
        value.slides.append(.init(id: UUID().uuidString, assetID: id, kind: asset.kind))
        draft = value
    }
    private func handle(_ error: Error) {
        if let apiError = error as? APIError, case .conflict = apiError {
            hasConflict = true
            self.error = "This post changed on another device. Your edits are kept here. Reload the saved post before editing again."
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
