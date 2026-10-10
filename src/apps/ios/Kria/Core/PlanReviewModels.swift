import Foundation

/// Live plan & review wire contract v2 (KRI-439). Mirror of app/kria/plan_contract.py.
/// RULE (decode-with-fallback): nothing here may ever make a block, a feed or a snapshot fail to decode.
/// A payload that is missing, malformed or for an unknown shape decodes to `nil` and the UI falls back to
/// `summary`/`detail`. Unknown sections are dropped. Unknown enum strings become `nil`. Unknown keys are ignored.

enum PlanTransition: String, Sendable { case cut, dissolve, whip, fade }

enum PlanClipKind: String, Sendable { case video, image }

/// Tolerant leaf decoding: a bad value yields nil instead of throwing.
private extension KeyedDecodingContainer {
    func lenient<T: Decodable>(_ type: T.Type, _ key: Key) -> T? { try? decodeIfPresent(type, forKey: key) }
}

struct PlanTitlePayload: Decodable, Equatable, Sendable {
    let text: String
    let highlightWord: String?
    let barID: String?
    enum CodingKeys: String, CodingKey { case text; case highlightWord = "highlight_word"; case barID = "bar_id" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)          // required
        highlightWord = c.lenient(String.self, .highlightWord)
        barID = c.lenient(String.self, .barID)
    }
}

struct PlanClipItem: Decodable, Equatable, Sendable, Identifiable {
    let index: Int
    let mediaID: String?
    let kind: PlanClipKind
    let role: String?
    let label: String?
    let startS: Double
    let endS: Double
    let sourceStartS: Double?
    let sourceEndS: Double?
    /// Transition leaving this clip into the next. nil = last clip or unknown value.
    let transition: PlanTransition?
    let transitionDurationS: Double?
    /// Only from GET /plan; never present in events.
    let thumbnailURL: URL?
    var id: Int { index }
    enum CodingKeys: String, CodingKey {
        case index, kind, role, label, transition
        case mediaID = "media_id", startS = "start_s", endS = "end_s", sourceStartS = "source_start_s"
        case sourceEndS = "source_end_s", transitionDurationS = "transition_duration_s", thumbnailURL = "thumbnail_url"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = try c.decode(Int.self, forKey: .index)
        startS = try c.decode(Double.self, forKey: .startS)
        endS = try c.decode(Double.self, forKey: .endS)
        mediaID = c.lenient(String.self, .mediaID)
        kind = c.lenient(String.self, .kind).flatMap(PlanClipKind.init(rawValue:)) ?? .video
        role = c.lenient(String.self, .role)
        label = c.lenient(String.self, .label)
        sourceStartS = c.lenient(Double.self, .sourceStartS)
        sourceEndS = c.lenient(Double.self, .sourceEndS)
        transition = c.lenient(String.self, .transition).flatMap(PlanTransition.init(rawValue:))
        transitionDurationS = c.lenient(Double.self, .transitionDurationS)
        thumbnailURL = c.lenient(String.self, .thumbnailURL).flatMap(URL.init(string:))
    }
}

struct PlanClipsPayload: Decodable, Equatable, Sendable {
    let totalDurationS: Double
    let clips: [PlanClipItem]
    enum CodingKeys: String, CodingKey { case totalDurationS = "total_duration_s", clips }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        totalDurationS = c.lenient(Double.self, .totalDurationS) ?? 0
        // Drop individual bad rows instead of failing the whole payload.
        clips = (try? c.decode([Lossy<PlanClipItem>].self, forKey: .clips))?.compactMap(\.value) ?? []
    }
}

struct PlanCaptionLine: Decodable, Equatable, Sendable, Identifiable {
    enum Kind: String, Sendable { case cue, bar }
    let id: String          // manual-edit target
    let kind: Kind
    let text: String
    let startS: Double
    let endS: Double
    enum CodingKeys: String, CodingKey { case id, kind, text; case startS = "start_s"; case endS = "end_s" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        text = try c.decode(String.self, forKey: .text)
        startS = try c.decode(Double.self, forKey: .startS)
        endS = try c.decode(Double.self, forKey: .endS)
        kind = c.lenient(String.self, .kind).flatMap(Kind.init(rawValue:)) ?? .cue
    }
}

struct PlanCaptionsPayload: Decodable, Equatable, Sendable {
    let count: Int
    let lines: [PlanCaptionLine]
    let truncated: Bool
    enum CodingKeys: String, CodingKey { case count, lines, truncated }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        lines = (try? c.decode([Lossy<PlanCaptionLine>].self, forKey: .lines))?.compactMap(\.value) ?? []
        count = c.lenient(Int.self, .count) ?? lines.count
        truncated = c.lenient(Bool.self, .truncated) ?? false
    }
}

struct PlanMixLevels: Decodable, Equatable, Sendable {
    let musicLevel: Double?
    let originalLevel: Double?
    let musicGainDB: Double?
    enum CodingKeys: String, CodingKey { case musicLevel = "music_level", originalLevel = "original_level", musicGainDB = "music_gain_db" }
}

struct PlanMusicPayload: Decodable, Equatable, Sendable {
    enum Source: String, Sendable { case catalog, userSong = "user_song", voiceover }
    enum Mode: String, Sendable { case background, lipsync }
    let source: Source
    let mode: Mode?
    let trackID: String?
    let title: String?
    let artist: String?
    let bpm: Double?
    let startS: Double?
    let artURL: URL?        // GET /plan only
    let mix: PlanMixLevels?
    enum CodingKeys: String, CodingKey {
        case source, mode, title, artist, bpm, mix
        case trackID = "track_id", startS = "start_s", artURL = "art_url"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        source = c.lenient(String.self, .source).flatMap(Source.init(rawValue:)) ?? .catalog
        mode = c.lenient(String.self, .mode).flatMap(Mode.init(rawValue:))
        trackID = c.lenient(String.self, .trackID)
        title = c.lenient(String.self, .title)
        artist = c.lenient(String.self, .artist)
        bpm = c.lenient(Double.self, .bpm)
        startS = c.lenient(Double.self, .startS)
        artURL = c.lenient(String.self, .artURL).flatMap(URL.init(string:))
        mix = c.lenient(PlanMixLevels.self, .mix)
    }
}

struct PlanSfxItem: Decodable, Equatable, Sendable, Identifiable {
    let id: String
    let label: String?
    let atS: Double
    let gain: Double?
    enum CodingKeys: String, CodingKey { case id, label, gain; case atS = "at_s" }
}

struct PlanSfxPayload: Decodable, Equatable, Sendable {
    let count: Int
    let items: [PlanSfxItem]
    enum CodingKeys: String, CodingKey { case count, items }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = (try? c.decode([Lossy<PlanSfxItem>].self, forKey: .items))?.compactMap(\.value) ?? []
        count = c.lenient(Int.self, .count) ?? items.count
    }
}

struct PlanOverlayItem: Decodable, Equatable, Sendable, Identifiable {
    enum Kind: String, Sendable { case image, video, textCard = "text_card", motion, visual }
    let id: String
    let kind: Kind
    let label: String?
    let startS: Double
    let endS: Double
    let displayMode: String?   // "pip" | "fullscreen" | nil
    enum CodingKeys: String, CodingKey { case id, kind, label; case startS = "start_s", endS = "end_s", displayMode = "display_mode" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        startS = try c.decode(Double.self, forKey: .startS)
        endS = try c.decode(Double.self, forKey: .endS)
        kind = c.lenient(String.self, .kind).flatMap(Kind.init(rawValue:)) ?? .image
        label = c.lenient(String.self, .label)
        displayMode = c.lenient(String.self, .displayMode)
    }
}

struct PlanOverlaysPayload: Decodable, Equatable, Sendable {
    let count: Int
    let items: [PlanOverlayItem]
    enum CodingKeys: String, CodingKey { case count, items }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = (try? c.decode([Lossy<PlanOverlayItem>].self, forKey: .items))?.compactMap(\.value) ?? []
        count = c.lenient(Int.self, .count) ?? items.count
    }
}

struct PlanLookPayload: Decodable, Equatable, Sendable {
    let chips: [String]
    let styleID: String?
    let lookPreset: String?
    enum CodingKeys: String, CodingKey { case chips; case styleID = "style_id"; case lookPreset = "look_preset" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        chips = c.lenient([String].self, .chips) ?? []
        styleID = c.lenient(String.self, .styleID)
        lookPreset = c.lenient(String.self, .lookPreset)
    }
}

struct PlanPostCaptionPayload: Decodable, Equatable, Sendable {
    let text: String
    let hashtags: [String]      // no leading "#"
    enum CodingKeys: String, CodingKey { case text, hashtags }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        hashtags = c.lenient([String].self, .hashtags) ?? []
    }
}

/// One row that may fail alone (used by the list payloads above).
struct Lossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

enum PlanBlockPayload: Equatable, Sendable {
    case title(PlanTitlePayload), clips(PlanClipsPayload), captions(PlanCaptionsPayload), music(PlanMusicPayload)
    case sfx(PlanSfxPayload), overlays(PlanOverlaysPayload), look(PlanLookPayload), postCaption(PlanPostCaptionPayload)

    /// NEVER throws. nil = missing/malformed: render from `summary`.
    static func decode(section: PlanSectionID, json: JSONValue?) -> PlanBlockPayload? {
        guard case .object = json, let json,
              let data = try? JSONEncoder().encode(json) else { return nil }
        let d = JSONDecoder()
        switch section {
        case .title: return (try? d.decode(PlanTitlePayload.self, from: data)).map(Self.title)
        case .clips: return (try? d.decode(PlanClipsPayload.self, from: data)).map(Self.clips)
        case .captions: return (try? d.decode(PlanCaptionsPayload.self, from: data)).map(Self.captions)
        case .music: return (try? d.decode(PlanMusicPayload.self, from: data)).map(Self.music)
        case .sfx: return (try? d.decode(PlanSfxPayload.self, from: data)).map(Self.sfx)
        case .overlays: return (try? d.decode(PlanOverlaysPayload.self, from: data)).map(Self.overlays)
        case .look: return (try? d.decode(PlanLookPayload.self, from: data)).map(Self.look)
        case .postCaption: return (try? d.decode(PlanPostCaptionPayload.self, from: data)).map(Self.postCaption)
        }
    }
}

struct PlanPreviousValue: Equatable, Sendable {
    let revision: Int
    let jobID: String
    let summary: String?
    let payload: PlanBlockPayload?
    let skipped: Bool
}

// PlanBlock (Core/PlanBlocks.swift) gains these stored properties, all defaulted so existing call sites compile:
//   var revision: Int = 0
//   var changed: Bool = false
//   var payload: PlanBlockPayload? = nil
//   var previous: PlanPreviousValue? = nil
//   var editable: Bool = false          // GET /plan only
// Reducer rule (PlanBlockFeedState.merge): same section, both `decided`: if incoming.revision > current.revision
// REPLACE the whole block (summary, detail, payload, previous, changed, skipped); if equal, keep the existing
// "fill in, never blank" merge and take incoming.payload when non-nil. Lower revision is ignored.

// MARK: GET /creation-threads/{id}/plan

struct PlanSnapshot: Decodable, Sendable {
    enum Status: String, Sendable { case empty, planning, ready, updating, cancelled }
    struct DraftHead: Decodable, Equatable, Sendable {
        let draftID: String, draftRevision: Int, etag: String, canUndo: Bool
        enum CodingKeys: String, CodingKey { case draftID = "draft_id", draftRevision = "draft_revision", etag, canUndo = "can_undo" }
    }
    struct UpdateSummary: Decodable, Equatable, Sendable {
        let turnID: String, jobID: String, text: String, changedSections: [PlanSectionID]
        enum CodingKeys: String, CodingKey { case turnID = "turn_id", jobID = "job_id", text, changedSections = "changed_sections" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            turnID = try c.decode(String.self, forKey: .turnID)
            jobID = try c.decode(String.self, forKey: .jobID)
            text = try c.decode(String.self, forKey: .text)
            changedSections = (try? c.decode([Lossy<String>].self, forKey: .changedSections))?.compactMap { $0.value.flatMap(PlanSectionID.init(rawValue:)) } ?? []
        }
    }
    let threadID: String
    let threadRevision: Int
    let jobID: String?
    let turnID: String?
    let previousJobID: String?
    let status: Status          // unknown value decodes as .empty (feature hidden)
    let scope: [PlanSectionID]?
    let decidedCount: Int
    let totalCount: Int
    let blocks: [PlanBlock]     // unknown sections dropped; sorted by PlanSectionID.order
    let draft: DraftHead?
    let updateSummary: UpdateSummary?
    let nextAfterSequence: Int

    enum CodingKeys: String, CodingKey {
        case status, scope, blocks, draft
        case threadID = "thread_id", threadRevision = "thread_revision", jobID = "job_id", turnID = "turn_id"
        case previousJobID = "previous_job_id", decidedCount = "decided_count", totalCount = "total_count"
        case updateSummary = "update_summary", nextAfterSequence = "next_after_sequence"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        threadID = try c.decode(String.self, forKey: .threadID)
        threadRevision = try c.decode(Int.self, forKey: .threadRevision)
        nextAfterSequence = try c.decode(Int.self, forKey: .nextAfterSequence)
        jobID = c.lenient(String.self, .jobID)
        turnID = c.lenient(String.self, .turnID)
        previousJobID = c.lenient(String.self, .previousJobID)
        status = c.lenient(String.self, .status).flatMap(Status.init(rawValue:)) ?? .empty
        scope = (try? c.decode([Lossy<String>].self, forKey: .scope))?.compactMap { $0.value.flatMap(PlanSectionID.init(rawValue:)) }
        // Each block goes through the SAME constructor the feed reducer uses, so event and snapshot blocks parse alike.
        let rows = (try? c.decode([Lossy<[String: JSONValue]>].self, forKey: .blocks))?.compactMap(\.value) ?? []
        blocks = rows.compactMap { PlanBlock(json: $0) }.sorted { $0.section.order < $1.section.order }
        decidedCount = c.lenient(Int.self, .decidedCount) ?? blocks.filter { $0.state == .decided }.count
        totalCount = c.lenient(Int.self, .totalCount) ?? blocks.count
        draft = c.lenient(DraftHead.self, .draft)
        updateSummary = c.lenient(UpdateSummary.self, .updateSummary)
    }
}

// MARK: requests

/// Body fragment appended to the turn body when the Review view sends "Update video".
struct ScopedTurnFields: Encodable, Sendable {
    let scope: [String]                   // PlanSectionID.rawValue, 1...7, never "post_caption"
    let manualEdits: [ManualPlanEdit]?    // nil/empty = omitted
    enum CodingKeys: String, CodingKey { case scope; case manualEdits = "manual_edits" }
}

struct ManualPlanEdit: Encodable, Equatable, Sendable {
    let kind: String                      // "rewrite_text" | "set_mix"
    let targetID: String?
    let text: String?
    let musicLevel: Double?
    let originalLevel: Double?
    let musicGainDB: Double?
    enum CodingKeys: String, CodingKey {
        case kind, text
        case targetID = "target_id", musicLevel = "music_level", originalLevel = "original_level", musicGainDB = "music_gain_db"
    }
    func encode(to encoder: Encoder) throws {       // omit nil keys (server body is extra="forbid" but accepts omission)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(kind, forKey: .kind)
        try c.encodeIfPresent(targetID, forKey: .targetID)
        try c.encodeIfPresent(text, forKey: .text)
        try c.encodeIfPresent(musicLevel, forKey: .musicLevel)
        try c.encodeIfPresent(originalLevel, forKey: .originalLevel)
        try c.encodeIfPresent(musicGainDB, forKey: .musicGainDB)
    }
}

struct PlanSectionUndoRequest: Encodable, Sendable {
    let expectedThreadRevision: Int, expectedBlockRevision: Int, expectedDraftRevision: Int
    enum CodingKeys: String, CodingKey {
        case expectedThreadRevision = "expected_thread_revision", expectedBlockRevision = "expected_block_revision"
        case expectedDraftRevision = "expected_draft_revision"
    }
}

struct PlanSectionUndoResult: Decodable, Equatable, Sendable {
    let sectionID: String, threadRevision: Int, draftRevision: Int, turnID: String?
    enum CodingKeys: String, CodingKey { case sectionID = "section_id", threadRevision = "thread_revision", draftRevision = "draft_revision", turnID = "turn_id" }
}
