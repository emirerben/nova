import Foundation

// MARK: - Editor identity and capabilities

/// The editor deliberately uses string identities. Some server objects are
/// UUIDs, while slots and generated effects use stable opaque identifiers.
enum EditorSelectionKind: String, Codable, CaseIterable, Hashable, Sendable {
    case clip, text, captionCue = "caption_cue", visualBlock = "visual_block"
    case motionScene = "motion_scene", cameraEffect = "camera_effect"
    case soundEffect = "sound_effect", mediaOverlay = "media_overlay"
    case music, carousel
}

struct EditorSelection: Codable, Equatable, Hashable, Sendable {
    let kind: EditorSelectionKind
    let id: String
}

enum EditorSection: String, Codable, CaseIterable, Hashable, Sendable {
    case timeline, text, captions, captionMeta = "caption_meta", mix, music, backgroundMusic = "background_music"
    case lyrics, orientation, soundEffects = "sound_effects"
    case mediaOverlays = "media_overlays", visualBlocks = "visual_blocks"
    case motionScenes = "motion_scenes", cameraEffects = "camera_effects"
    case carouselMoment = "carousel_moment", title
}

enum NativeEditorWireContract {
    static let lookPresets = ["none", "stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"]
    static let transitions = ["cut", "crossfade", "dip_to_black", "flash"]
    static let textAnimations = ["none", "fade-in", "pop-in", "slide-in"]
    static let musicAlignments = ["preserve_cuts", "resync_beats"]
    static let captionFonts = ["Inter", "Fraunces", "Space Grotesk"]
}

struct EditorCapability: Codable, Equatable, Sendable {
    var editable: Bool
    var reason: String?

    init(editable: Bool, reason: String? = nil) {
        self.editable = editable
        self.reason = reason
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let bool = try? value.decode(Bool.self) {
            editable = bool; reason = nil; return
        }
        let object = try value.decode([String: JSONValue].self)
        editable = object["editable"]?.boolValue ?? false
        reason = object["reason"]?.stringValue
    }

    func encode(to encoder: Encoder) throws {
        var object: [String: JSONValue] = ["editable": .bool(editable)]
        if let reason { object["reason"] = .string(reason) }
        try object.encode(to: encoder)
    }
}

struct EditorRevision: Codable, Equatable, Sendable {
    var baseGeneration: String
    var number: Int?
    var hash: String?
    var snapshotHash: String?

    init(baseGeneration: String = "", number: Int? = nil, hash: String? = nil, snapshotHash: String? = nil) {
        self.baseGeneration = baseGeneration; self.number = number; self.hash = hash; self.snapshotHash = snapshotHash
    }
}

// MARK: - Lossless lane records

struct EditorTimelineSlot: Codable, Equatable, Sendable {
    var id: String?
    var parentSegmentID: String?
    var clipIndex: Int
    var inS: Double
    var durationBeats: Int?
    var durationS: Double?
    var removed: Bool
    var transitionAfter: String
    var transitionDurationS: Double?
    var lookPreset: String?
    var lookAdjustments: [String: JSONValue]?
    var raw: [String: JSONValue]

    init(id: String? = nil, clipIndex: Int, inS: Double, durationS: Double? = nil, durationBeats: Int? = nil, removed: Bool = false, parentSegmentID: String? = nil, transitionAfter: String = "cut", transitionDurationS: Double? = nil, lookPreset: String? = nil, lookAdjustments: [String: JSONValue]? = nil, raw: [String: JSONValue] = [:]) {
        self.id = id; self.parentSegmentID = parentSegmentID; self.clipIndex = clipIndex; self.inS = inS; self.durationBeats = durationBeats; self.durationS = durationS; self.removed = removed; self.transitionAfter = transitionAfter; self.transitionDurationS = transitionDurationS; self.lookPreset = lookPreset; self.lookAdjustments = lookAdjustments; self.raw = raw
    }
}

struct EditorTextElement: Codable, Equatable, Sendable {
    var id: String
    var text: String
    var startS: Double
    var endS: Double
    var role: String?
    var raw: [String: JSONValue]
    init(id: String, text: String, startS: Double = 0, endS: Double = 0, role: String? = nil, raw: [String: JSONValue] = [:]) {
        self.id = id; self.text = text; self.startS = startS; self.endS = endS; self.role = role; self.raw = raw
    }
}

struct EditorCaptionCue: Codable, Equatable, Sendable {
    var id: String
    var startS: Double
    var endS: Double
    var text: String
    var raw: [String: JSONValue]
    init(id: String, startS: Double, endS: Double, text: String, raw: [String: JSONValue] = [:]) {
        self.id = id; self.startS = startS; self.endS = endS; self.text = text; self.raw = raw
    }
}

struct EditorMusic: Codable, Equatable, Sendable {
    var trackID: String
    var startS: Double
    var alignment: String?
    var raw: [String: JSONValue]
    init(trackID: String, startS: Double = 0, alignment: String? = nil, raw: [String: JSONValue] = [:]) {
        self.trackID = trackID; self.startS = startS; self.alignment = alignment; self.raw = raw
    }
}

struct EditorBackgroundMusic: Codable, Equatable, Sendable {
    var trackID: String?
    var enabled: Bool
    var startS: Double?
    var endS: Double?
    var gainDB: Double?
    var muted: Bool
    var raw: [String: JSONValue]
    init(trackID: String? = nil, enabled: Bool = true, startS: Double? = nil, endS: Double? = nil, gainDB: Double? = nil, muted: Bool = false, raw: [String: JSONValue] = [:]) {
        self.trackID = trackID; self.enabled = enabled; self.startS = startS; self.endS = endS; self.gainDB = gainDB; self.muted = muted; self.raw = raw
    }
}

/// SFX and media overlays intentionally share a tolerant envelope. The API
/// accepts evolving dictionaries, so known timing fields are typed while all
/// effect-specific fields remain lossless in `raw`.
struct EditorTimedEffect: Codable, Equatable, Sendable {
    var id: String
    var startS: Double
    var endS: Double
    /// Sound effects are point placements (`at_s`), while overlays and camera
    /// effects are windows. Keep the point form typed so a lane edit cannot
    /// leave a stale `at_s` behind in the raw envelope.
    var pointS: Double?
    var kind: String?
    var raw: [String: JSONValue]
    init(id: String, startS: Double, endS: Double, pointS: Double? = nil, kind: String? = nil, raw: [String: JSONValue] = [:]) {
        self.id = id; self.startS = startS; self.endS = endS; self.pointS = pointS; self.kind = kind; self.raw = raw
    }
}

struct EditorVisualBlock: Codable, Equatable, Sendable {
    var id: String
    var kind: String
    var startS: Double
    var endS: Double
    var raw: [String: JSONValue]
    init(id: String, kind: String, startS: Double, endS: Double, raw: [String: JSONValue] = [:]) {
        self.id = id; self.kind = kind; self.startS = startS; self.endS = endS; self.raw = raw
    }
}

struct EditorMotionScene: Codable, Equatable, Sendable {
    var id: String
    var startS: Double
    var endS: Double
    var preset: String?
    var runtimeHash: String?
    var raw: [String: JSONValue]
    init(id: String, startS: Double, endS: Double, preset: String? = nil, runtimeHash: String? = nil, raw: [String: JSONValue] = [:]) {
        self.id = id; self.startS = startS; self.endS = endS; self.preset = preset; self.runtimeHash = runtimeHash; self.raw = raw
    }
}

struct EditorCameraEffect: Codable, Equatable, Sendable {
    var id: String
    var startS: Double
    var endS: Double
    var effect: String?
    var raw: [String: JSONValue]
    init(id: String, startS: Double, endS: Double, effect: String? = nil, raw: [String: JSONValue] = [:]) {
        self.id = id; self.startS = startS; self.endS = endS; self.effect = effect; self.raw = raw
    }
}

struct EditorOpaqueRecord: Codable, Equatable, Sendable {
    let raw: [String: JSONValue]
    let index: Int
    init(raw: [String: JSONValue], index: Int) { self.raw = raw; self.index = index }
}

// MARK: - Canonical editor document

struct EditorDocument: Equatable, Sendable {
    var schemaVersion: Int
    var kind: String
    var editFormat: String
    var clips: [EditorTimelineSlot]
    var tombstones: [EditorTimelineSlot]
    var textElements: [EditorTextElement]
    var captionMeta: [String: JSONValue]
    var captionCues: [EditorCaptionCue]
    var music: EditorMusic?
    var backgroundMusic: EditorBackgroundMusic?
    var mix: [String: JSONValue]
    var soundEffects: [EditorTimedEffect]
    var mediaOverlays: [EditorTimedEffect]
    var visualBlocks: [EditorVisualBlock]
    var motionScenes: [EditorMotionScene]
    /// Runtime compatibility token required alongside the full motion lane
    /// replacement. It is a section-level value, not a scene property.
    var motionRuntimeHash: String?
    var cameraEffects: [EditorCameraEffect]
    var carouselMoment: [String: JSONValue]?
    var lyrics: [String: JSONValue]?
    var title: String?
    var orientation: String?
    var capabilities: [String: EditorCapability]
    var revision: EditorRevision
    var opaqueRecords: [EditorSection: [EditorOpaqueRecord]]

    /// The original root is retained as an AST-like JSON envelope. This is
    /// what lets a no-op save preserve server additions this client cannot yet
    /// interpret.
    private var rawRoot: [String: JSONValue]
    private var rawSections: [String: JSONValue]
    private var sectionPresence: [EditorSection: EditorPresence]
    private var loadedState: LoadedState?

    enum EditorPresence: Equatable, Sendable { case absent, null, value }
    private struct LoadedState: Equatable {
        let schemaVersion: Int; let kind: String; let editFormat: String
        let clips: [EditorTimelineSlot]; let tombstones: [EditorTimelineSlot]; let textElements: [EditorTextElement]
        let captionMeta: [String: JSONValue]; let captionCues: [EditorCaptionCue]; let music: EditorMusic?; let backgroundMusic: EditorBackgroundMusic?
        let mix: [String: JSONValue]; let soundEffects: [EditorTimedEffect]; let mediaOverlays: [EditorTimedEffect]; let visualBlocks: [EditorVisualBlock]
        let motionScenes: [EditorMotionScene]; let motionRuntimeHash: String?; let cameraEffects: [EditorCameraEffect]; let carouselMoment: [String: JSONValue]?; let lyrics: [String: JSONValue]?
        let title: String?; let orientation: String?; let capabilities: [String: EditorCapability]; let revision: EditorRevision
    }

    init(schemaVersion: Int = 2, kind: String = "editor", editFormat: String = "montage", clips: [EditorTimelineSlot] = [], tombstones: [EditorTimelineSlot] = [], textElements: [EditorTextElement] = [], captionMeta: [String: JSONValue] = [:], captionCues: [EditorCaptionCue] = [], music: EditorMusic? = nil, backgroundMusic: EditorBackgroundMusic? = nil, mix: [String: JSONValue] = [:], soundEffects: [EditorTimedEffect] = [], mediaOverlays: [EditorTimedEffect] = [], visualBlocks: [EditorVisualBlock] = [], motionScenes: [EditorMotionScene] = [], motionRuntimeHash: String? = nil, cameraEffects: [EditorCameraEffect] = [], carouselMoment: [String: JSONValue]? = nil, lyrics: [String: JSONValue]? = nil, title: String? = nil, orientation: String? = nil, capabilities: [String: EditorCapability] = [:], revision: EditorRevision = EditorRevision(), opaqueRecords: [EditorSection: [EditorOpaqueRecord]] = [:]) {
        self.schemaVersion = schemaVersion; self.kind = kind; self.editFormat = editFormat; self.clips = clips; self.tombstones = tombstones; self.textElements = textElements; self.captionMeta = captionMeta; self.captionCues = captionCues; self.music = music; self.backgroundMusic = backgroundMusic; self.mix = mix; self.soundEffects = soundEffects; self.mediaOverlays = mediaOverlays; self.visualBlocks = visualBlocks; self.motionScenes = motionScenes; self.motionRuntimeHash = motionRuntimeHash; self.cameraEffects = cameraEffects; self.carouselMoment = carouselMoment; self.lyrics = lyrics; self.title = title; self.orientation = orientation; self.capabilities = capabilities; self.revision = revision; self.opaqueRecords = opaqueRecords; self.rawRoot = [:]; self.rawSections = [:]; self.sectionPresence = [:]; self.loadedState = nil
    }

    init(snapshot: [String: JSONValue]) { self = Self.decode(snapshot: snapshot) }

    static func decode(snapshot: [String: JSONValue]) -> EditorDocument {
        let rootPayload = object(snapshot["editor_payload"])
        let sections = object(rootPayload?["sections"]) ?? rootPayload ?? snapshot
        var document = EditorDocument(schemaVersion: integer(snapshot["schema_version"]) ?? 2, kind: snapshot["kind"]?.stringValue ?? "editor", editFormat: snapshot["edit_format"]?.stringValue ?? "montage")
        document.rawRoot = snapshot; document.rawSections = sections
        document.revision = EditorRevision(baseGeneration: string(rootPayload?["base_generation"]) ?? string(snapshot["base_generation"]) ?? "", number: integer(sections["revision_number"]), hash: string(sections["revision_hash"]), snapshotHash: string(snapshot["snapshot_hash"]))
        document.title = string(sections["title"] ?? snapshot["title"]); document.orientation = string(sections["orientation"] ?? snapshot["orientation"])
        // Caption archetypes historically exposed their appearance as variant-
        // level fields while newer snapshots may carry the editor-shaped
        // `caption_meta` object. Normalize both into the commit DTO's field
        // names, but only fill a missing nested key from its sibling alias.
        // JSONValue's `.null` is deliberately retained: a null override means
        // "use the renderer default", and is different from an omitted key.
        document.captionMeta = normalizedCaptionMeta(sections: sections, snapshot: snapshot)
        document.mix = object(sections["mix"] ?? sections["audio_mix"] ?? snapshot["mix"]) ?? [:]
        let hasCanonicalMusic = sections["music_track_id"] != nil || sections["music_window"] != nil
        document.music = hasCanonicalMusic
            ? decodeMusic(nil, fallbackTrack: string(sections["music_track_id"]), fallbackWindow: object(sections["music_window"]))
            : decodeMusic(object(sections["music"] ?? snapshot["music"]), fallbackTrack: nil, fallbackWindow: nil)
        document.backgroundMusic = decodeBackground(object(sections["background_music"] ?? snapshot["background_music"]))
        document.carouselMoment = object(sections["carousel_moment"] ?? snapshot["carousel_moment"])
        document.lyrics = object(sections["lyrics"] ?? snapshot["lyrics"])
        document.capabilities = decodeCapabilities(
            object(snapshot["editor_capabilities"])
                ?? object(sections["editor_capabilities"])
                ?? object(snapshot["capabilities"])
                ?? object(sections["capabilities"])
        )
        document.clips = decodeSlots(array(sections["timeline_slots"] ?? object(snapshot["user_timeline"])?["slots"]) ?? []) .filter { !$0.removed }
        document.tombstones = decodeSlots(array(sections["timeline_slots"] ?? object(snapshot["user_timeline"])?["slots"]) ?? []) .filter(\.removed)
        document.textElements = decodeText(array(sections["text_elements"] ?? snapshot["text_elements"]) ?? [])
        document.captionCues = decodeCaptions(array(sections["caption_cues"] ?? snapshot["caption_cues"]) ?? [])
        document.soundEffects = decodeEffects(array(sections["sound_effects"] ?? snapshot["sound_effects"]) ?? [])
        document.mediaOverlays = decodeEffects(array(sections["media_overlays"] ?? snapshot["media_overlays"]) ?? [])
        document.visualBlocks = decodeVisual(array(sections["visual_blocks"] ?? snapshot["visual_blocks"]) ?? [])
        document.motionScenes = decodeMotion(array(sections["motion_scenes"] ?? snapshot["motion_scenes"]) ?? [])
        document.motionRuntimeHash = string(sections["motion_runtime_hash"] ?? snapshot["motion_runtime_hash"])
        document.cameraEffects = decodeCamera(array(sections["camera_effects"] ?? snapshot["camera_effects"]) ?? [])
        for section in EditorSection.allCases { document.sectionPresence[section] = document.presence(for: section, in: sections) }
        document.opaqueRecords = document.makeOpaqueRecords(sections)
        document.loadedState = document.currentState()
        return document
    }

    func encodeSnapshot() -> [String: JSONValue] {
        if let loadedState, loadedState == currentState() { return rawRoot }
        var root = rawRoot
        let isNew = root.isEmpty
        let canonical = isNew || root["editor_payload"] != nil
        if isNew { root = ["schema_version": .number(Double(schemaVersion)), "kind": .string(kind), "edit_format": .string(editFormat)] }
        var payload = Self.object(root["editor_payload"]) ?? [:]
        var sections = canonical ? (Self.object(payload["sections"]) ?? rawSections) : rawSections
        writeSection(&sections, .timeline, values: clips + tombstones, original: Self.array(rawSections[EditorSection.timeline.wireKey]), encode: encodeSlot, id: { Self.string($0["slot_id"]) }, nonEmpty: !(clips + tombstones).isEmpty)
        writeSection(&sections, .text, values: textElements, original: Self.array(rawSections[EditorSection.text.wireKey]), encode: encodeText, id: { Self.string($0["id"]) }, nonEmpty: !textElements.isEmpty)
        writeSection(&sections, .captions, values: captionCues, original: Self.array(rawSections[EditorSection.captions.wireKey]), encode: encodeCaption, id: { Self.string($0["id"]) }, nonEmpty: !captionCues.isEmpty)
        if sectionChanged(.captionMeta) { writeDictionary(&sections, .captionMeta, value: captionMeta, nonEmpty: !captionMeta.isEmpty) }
        writeDictionary(&sections, .mix, value: mix, nonEmpty: !mix.isEmpty)
        writeMusic(&sections)
        if sectionChanged(.backgroundMusic) {
            if let backgroundMusic { writeDictionary(&sections, .backgroundMusic, value: encodeBackground(backgroundMusic), nonEmpty: true) }
            else if sectionPresence[.backgroundMusic] != .absent { sections[EditorSection.backgroundMusic.wireKey] = .null }
        }
        writeSection(&sections, .soundEffects, values: soundEffects, original: Self.array(rawSections[EditorSection.soundEffects.wireKey]), encode: encodeEffect, id: { Self.string($0["id"]) }, nonEmpty: !soundEffects.isEmpty)
        writeSection(&sections, .mediaOverlays, values: mediaOverlays, original: Self.array(rawSections[EditorSection.mediaOverlays.wireKey]), encode: encodeEffect, id: { Self.string($0["id"]) }, nonEmpty: !mediaOverlays.isEmpty)
        writeSection(&sections, .visualBlocks, values: visualBlocks, original: Self.array(rawSections[EditorSection.visualBlocks.wireKey]), encode: encodeVisual, id: { Self.string($0["id"]) }, nonEmpty: !visualBlocks.isEmpty)
        writeSection(&sections, .motionScenes, values: motionScenes, original: Self.array(rawSections[EditorSection.motionScenes.wireKey]), encode: encodeMotion, id: { Self.string($0["id"]) }, nonEmpty: !motionScenes.isEmpty)
        if sectionChanged(.motionScenes) {
            if let motionRuntimeHash { sections["motion_runtime_hash"] = .string(motionRuntimeHash) }
            else if sections["motion_runtime_hash"] != nil { sections["motion_runtime_hash"] = .null }
        }
        writeSection(&sections, .cameraEffects, values: cameraEffects, original: Self.array(rawSections[EditorSection.cameraEffects.wireKey]), encode: encodeCamera, id: { Self.string($0["id"]) }, nonEmpty: !cameraEffects.isEmpty)
        if sectionChanged(.carouselMoment) {
            if let carouselMoment { sections[EditorSection.carouselMoment.wireKey] = .object(carouselMoment) }
            else { sections[EditorSection.carouselMoment.wireKey] = .null }
        }
        if sectionChanged(.lyrics) {
            if let lyrics { sections[EditorSection.lyrics.wireKey] = .object(lyrics) }
            else if sectionPresence[.lyrics] != .absent { sections[EditorSection.lyrics.wireKey] = .null }
        }
        if sectionChanged(.title) {
            if let title { sections[EditorSection.title.wireKey] = .string(title) }
            else if sectionPresence[.title] != .absent { sections[EditorSection.title.wireKey] = .null }
        }
        if sectionChanged(.orientation) {
            if let orientation { sections[EditorSection.orientation.wireKey] = .string(orientation) }
            else if sectionPresence[.orientation] != .absent { sections[EditorSection.orientation.wireKey] = .null }
        }
        if let base = revision.baseGeneration.nilIfEmpty { payload["base_generation"] = .string(base) }
        if canonical {
            payload["sections"] = .object(sections)
            root["editor_payload"] = .object(payload)
        } else {
            // Legacy snapshots keep their lanes at the root. `sections` is a
            // working copy in this branch, so publish edits back explicitly.
            root = sections
        }
        if isNew || root["schema_version"] != nil { root["schema_version"] = .number(Double(schemaVersion)) }
        if isNew || root["kind"] != nil { root["kind"] = .string(kind) }
        if isNew || root["edit_format"] != nil { root["edit_format"] = .string(editFormat) }
        return root
    }

    func serializedSnapshot() -> [String: JSONValue] { encodeSnapshot() }

    func snapshot(for section: EditorSection) -> JSONValue? {
        let encoded = encodeSnapshot(); let payload = Self.object(encoded["editor_payload"]); let sections = Self.object(payload?["sections"])
        if section == .music {
            if sections?["music_track_id"] != nil || sections?["music_window"] != nil {
                var value: [String: JSONValue] = [:]
                if let track = sections?["music_track_id"] { value["music_track_id"] = track }
                if let window = sections?["music_window"] { value["music_window"] = window }
                return .object(value)
            }
            return sections?["music"]
        }
        return sections?[section.wireKey]
    }

    private func currentState() -> LoadedState {
        LoadedState(schemaVersion: schemaVersion, kind: kind, editFormat: editFormat, clips: clips, tombstones: tombstones, textElements: textElements, captionMeta: captionMeta, captionCues: captionCues, music: music, backgroundMusic: backgroundMusic, mix: mix, soundEffects: soundEffects, mediaOverlays: mediaOverlays, visualBlocks: visualBlocks, motionScenes: motionScenes, motionRuntimeHash: motionRuntimeHash, cameraEffects: cameraEffects, carouselMoment: carouselMoment, lyrics: lyrics, title: title, orientation: orientation, capabilities: capabilities, revision: revision)
    }

    private mutating func makeOpaqueRecords(_ sections: [String: JSONValue]) -> [EditorSection: [EditorOpaqueRecord]] {
        var result: [EditorSection: [EditorOpaqueRecord]] = [:]
        for section in EditorSection.allCases {
            guard let rows = Self.array(sections[section.wireKey]) else { continue }
            let opaque = rows.enumerated().compactMap { index, row -> EditorOpaqueRecord? in
                guard let value = Self.object(row), !isTypedRecord(section, value) else { return nil }
                return EditorOpaqueRecord(raw: value, index: index)
            }
            if !opaque.isEmpty { result[section] = opaque }
        }
        return result
    }
}

private enum NativeEditorMotionCodec {
    static let fps = 30.0
}

private extension EditorSection {
    var wireKey: String {
        switch self { case .timeline: "timeline_slots"; case .text: "text_elements"; case .captions: "caption_cues"; case .captionMeta: "caption_meta"; case .mix: "mix"; case .music: "music"; case .backgroundMusic: "background_music"; case .lyrics: "lyrics"; case .orientation: "orientation"; case .soundEffects: "sound_effects"; case .mediaOverlays: "media_overlays"; case .visualBlocks: "visual_blocks"; case .motionScenes: "motion_scenes"; case .cameraEffects: "camera_effects"; case .carouselMoment: "carousel_moment"; case .title: "title" }
    }
}

private extension EditorDocument {
    typealias RawRecordEncoder<T> = (T) -> JSONValue

    func sectionChanged(_ section: EditorSection) -> Bool {
        guard let loadedState else { return true }
        switch section {
        case .timeline: return loadedState.clips != clips || loadedState.tombstones != tombstones
        case .text: return loadedState.textElements != textElements
        case .captions: return loadedState.captionCues != captionCues
        case .captionMeta: return loadedState.captionMeta != captionMeta
        case .mix: return loadedState.mix != mix
        case .music: return loadedState.music != music
        case .backgroundMusic: return loadedState.backgroundMusic != backgroundMusic
        case .soundEffects: return loadedState.soundEffects != soundEffects
        case .mediaOverlays: return loadedState.mediaOverlays != mediaOverlays
        case .visualBlocks: return loadedState.visualBlocks != visualBlocks
        case .motionScenes: return loadedState.motionScenes != motionScenes || loadedState.motionRuntimeHash != motionRuntimeHash
        case .cameraEffects: return loadedState.cameraEffects != cameraEffects
        case .carouselMoment: return loadedState.carouselMoment != carouselMoment
        case .lyrics: return loadedState.lyrics != lyrics
        case .title: return loadedState.title != title
        case .orientation: return loadedState.orientation != orientation
        }
    }

    func writeSection<T>(_ sections: inout [String: JSONValue], _ section: EditorSection, values: [T], original: [JSONValue]?, encode: RawRecordEncoder<T>, id: ( [String: JSONValue]) -> String?, nonEmpty: Bool) {
        let key = section.wireKey
        guard sectionChanged(section) else { return }
        guard nonEmpty || sectionPresence[section] != .absent else { return }
        let encoded = values.map { encode($0) }
        let opaqueIndices = Set(opaqueRecords[section, default: []].map(\.index))
        var result: [JSONValue] = []
        var nextEncodedIndex = encoded.startIndex
        for (index, row) in (original ?? []).enumerated() {
            guard !opaqueIndices.contains(index), let originalObject = Self.object(row), id(originalObject) != nil else {
                result.append(row); continue
            }
            if nextEncodedIndex < encoded.endIndex {
                result.append(encoded[nextEncodedIndex])
                encoded.formIndex(after: &nextEncodedIndex)
            }
        }
        result.append(contentsOf: encoded[nextEncodedIndex...])
        sections[key] = .array(result)
    }

    func writeDictionary(_ sections: inout [String: JSONValue], _ section: EditorSection, value: [String: JSONValue], nonEmpty: Bool) {
        guard sectionChanged(section) else { return }
        guard nonEmpty || sectionPresence[section] != .absent else { return }
        sections[section.wireKey] = .object(value)
    }

    func writeMusic(_ sections: inout [String: JSONValue]) {
        guard sectionChanged(.music) else { return }
        guard music != nil || sectionPresence[.music] != .absent else { return }
        // The editor-commit API's canonical snapshot uses these two sibling
        // fields. A nested `music` object is accepted only as a legacy input.
        sections.removeValue(forKey: EditorSection.music.wireKey)
        guard let music else {
            sections["music_track_id"] = .null
            sections["music_window"] = .null
            return
        }
        sections["music_track_id"] = .string(music.trackID)
        var window = Self.object(sections["music_window"]) ?? [:]
        window["start_s"] = .number(music.startS)
        window["alignment"] = .string(music.alignment ?? "preserve_cuts")
        sections["music_window"] = .object(window)
    }

    func isTypedRecord(_ section: EditorSection, _ value: [String: JSONValue]) -> Bool {
        switch section {
        case .timeline: return Self.string(value["slot_id"]) != nil && Self.integer(value["clip_index"]) != nil && Self.number(value["in_s"]) != nil
        case .text: return Self.string(value["id"]) != nil && Self.string(value["text"]) != nil && Self.number(value["start_s"]) != nil && Self.number(value["end_s"]) != nil
        case .captions: return Self.string(value["id"]) != nil && Self.number(value["start_s"]) != nil && Self.number(value["end_s"]) != nil
        case .soundEffects, .mediaOverlays: return Self.string(value["id"]) != nil && (Self.number(value["start_s"] ?? value["start"] ?? value["at_s"]) != nil) && (Self.number(value["end_s"] ?? value["end"] ?? value["at_s"]) != nil)
        case .visualBlocks: return Self.string(value["id"]) != nil && Self.string(value["kind"]) != nil && Self.number(value["start_s"]) != nil && Self.number(value["end_s"]) != nil
        case .motionScenes:
            let hasSeconds = Self.number(value["start_s"]) != nil && Self.number(value["end_s"]) != nil
            let hasFrames = Self.number(value["start_frame"]) != nil && Self.number(value["end_frame_exclusive"]) != nil
            return Self.string(value["id"]) != nil && (hasSeconds || hasFrames)
        case .cameraEffects: return Self.string(value["id"]) != nil && Self.number(value["start_s"]) != nil && Self.number(value["end_s"]) != nil
        default: return true
        }
    }

    /// Converts the authoritative variant-level caption fields into the
    /// `EditorCommitCaptionMeta` shape consumed by the editor Save route.
    /// Existing nested values win per key, including explicit nulls. Unknown
    /// nested fields remain untouched so a round-trip cannot discard a newer
    /// server-side caption option.
    static func normalizedCaptionMeta(
        sections: [String: JSONValue],
        snapshot: [String: JSONValue]
    ) -> [String: JSONValue] {
        let nestedValue = sections[EditorSection.captionMeta.wireKey] ?? snapshot[EditorSection.captionMeta.wireKey]
        let nested = object(nestedValue)
        var result = nested ?? [:]

        // An explicit null caption_meta section is intentionally not replaced
        // by sibling fields. It remains null in the raw AST until the user
        // makes an actual caption-meta edit, preserving omitted/null shape.
        guard nested != nil || nestedValue == nil else { return result }

        func value(for canonicalKey: String, aliases: [String]) -> JSONValue? {
            if let value = nested?[canonicalKey] { return value }
            for key in aliases {
                if let value = nested?[key] { return value }
            }
            if let value = sections[canonicalKey] ?? snapshot[canonicalKey] { return value }
            for key in aliases {
                if let value = sections[key] ?? snapshot[key] { return value }
            }
            return nil
        }

        let aliases: [(String, [String])] = [
            ("enabled", ["captions_enabled"]),
            ("style", ["voiceover_caption_style", "caption_style"]),
            ("font", ["voiceover_caption_font", "caption_font"]),
            ("font_set", ["caption_font_user_edited"]),
            ("size_px", ["caption_size_px"]),
            ("color", ["caption_text_color", "caption_color"]),
            ("highlight_color", ["caption_highlight_color"]),
            ("stroke_width", ["caption_stroke_width"]),
            ("shadow_enabled", ["caption_shadow_enabled"]),
        ]
        for (canonicalKey, keys) in aliases {
            if let value = value(for: canonicalKey, aliases: keys) { result[canonicalKey] = value }
        }

        if nested?["y_frac"] == nil {
            if let value = nested?["caption_y_frac"] {
                result["y_frac"] = value
            } else if let margin = nested?["caption_margin_v"] {
                switch margin {
                case .number(let margin): result["y_frac"] = .number(1 - margin / 1920)
                case .null: result["y_frac"] = .null
                default: break
                }
            } else if let value = sections["caption_y_frac"] ?? snapshot["caption_y_frac"] {
                result["y_frac"] = value
            } else if let margin = sections["caption_margin_v"] ?? snapshot["caption_margin_v"] {
                switch margin {
                case .number(let margin): result["y_frac"] = .number(1 - margin / 1920)
                case .null: result["y_frac"] = .null
                default: break
                }
            }
        }
        return result
    }

    func presence(for section: EditorSection, in sections: [String: JSONValue]) -> EditorPresence {
        if section == .music && (sections["music_track_id"] != nil || sections["music_window"] != nil) {
            if case .null = sections["music_track_id"] ?? sections["music_window"]! { return .null }
            return .value
        }
        return Self.presence(sections, key: section.wireKey)
    }

    func merged(_ raw: [String: JSONValue], _ known: [String: JSONValue]) -> [String: JSONValue] { raw.merging(known) { _, newer in newer } }
    func encodeSlot(_ item: EditorTimelineSlot) -> JSONValue {
        var known: [String: JSONValue] = ["slot_id": item.id.map(JSONValue.string) ?? .null, "clip_index": .number(Double(item.clipIndex)), "in_s": .number(item.inS), "duration_beats": item.durationBeats.map { .number(Double($0)) } ?? .null, "duration_s": item.durationS.map(JSONValue.number) ?? .null, "removed": .bool(item.removed), "transition_after": .string(item.transitionAfter), "transition_duration_s": item.transitionDurationS.map(JSONValue.number) ?? .null, "parent_segment_id": item.parentSegmentID.map(JSONValue.string) ?? .null, "look_adjustments": item.lookAdjustments.map(JSONValue.object) ?? .null]
        if let lookPreset = item.lookPreset { known["look_preset"] = .string(lookPreset) }
        return .object(merged(item.raw, known))
    }
    func encodeText(_ item: EditorTextElement) -> JSONValue { .object(merged(item.raw, ["id": .string(item.id), "text": .string(item.text), "start_s": .number(item.startS), "end_s": .number(item.endS), "role": item.role.map(JSONValue.string) ?? .null])) }
    func encodeCaption(_ item: EditorCaptionCue) -> JSONValue { .object(merged(item.raw, ["id": .string(item.id), "start_s": .number(item.startS), "end_s": .number(item.endS), "text": .string(item.text)])) }
    func encodeMusic(_ item: EditorMusic) -> [String: JSONValue] { merged(item.raw, ["track_id": .string(item.trackID), "start_s": .number(item.startS), "alignment": item.alignment.map(JSONValue.string) ?? .null]) }
    func encodeBackground(_ item: EditorBackgroundMusic) -> [String: JSONValue] { merged(item.raw, ["track_id": item.trackID.map(JSONValue.string) ?? .null, "enabled": .bool(item.enabled), "start_s": item.startS.map(JSONValue.number) ?? .null, "end_s": item.endS.map(JSONValue.number) ?? .null, "gain_db": item.gainDB.map(JSONValue.number) ?? .null, "muted": .bool(item.muted)]) }
    func encodeEffect(_ item: EditorTimedEffect) -> JSONValue {
        var known: [String: JSONValue] = ["id": .string(item.id), "kind": item.kind.map(JSONValue.string) ?? .null]
        if let pointS = item.pointS {
            known["at_s"] = .number(pointS)
        } else {
            known["start_s"] = .number(item.startS)
            known["end_s"] = .number(item.endS)
        }
        return .object(merged(item.raw, known))
    }
    func encodeVisual(_ item: EditorVisualBlock) -> JSONValue { .object(merged(item.raw, ["id": .string(item.id), "kind": .string(item.kind), "start_s": .number(item.startS), "end_s": .number(item.endS)])) }
    func encodeMotion(_ item: EditorMotionScene) -> JSONValue {
        var value = merged(item.raw, [
            "id": .string(item.id),
            "start_frame": .number((item.startS * NativeEditorMotionCodec.fps).rounded()),
            "end_frame_exclusive": .number((item.endS * NativeEditorMotionCodec.fps).rounded()),
            "preset_id": item.preset.map(JSONValue.string) ?? .null,
        ])
        value.removeValue(forKey: "start_s"); value.removeValue(forKey: "end_s"); value.removeValue(forKey: "preset")
        return .object(value)
    }
    func encodeCamera(_ item: EditorCameraEffect) -> JSONValue { .object(merged(item.raw, ["id": .string(item.id), "start_s": .number(item.startS), "end_s": .number(item.endS), "effect": item.effect.map(JSONValue.string) ?? .null])) }
}

private extension EditorDocument {
    static func decodeSlots(_ rows: [JSONValue]) -> [EditorTimelineSlot] { rows.compactMap { value in guard let o = object(value), let id = string(o["slot_id"]), let index = integer(o["clip_index"]), let inS = number(o["in_s"]) else { return nil }; return EditorTimelineSlot(id: id, clipIndex: index, inS: inS, durationS: number(o["duration_s"]), durationBeats: integer(o["duration_beats"]), removed: o["removed"]?.boolValue ?? false, parentSegmentID: string(o["parent_segment_id"]), transitionAfter: string(o["transition_after"]) ?? "cut", transitionDurationS: number(o["transition_duration_s"]), lookPreset: string(o["look_preset"]), lookAdjustments: object(o["look_adjustments"]), raw: o) } }
    static func decodeText(_ rows: [JSONValue]) -> [EditorTextElement] { rows.compactMap { value in guard let o = object(value), let id = string(o["id"]), let text = string(o["text"]), let start = number(o["start_s"]), let end = number(o["end_s"]) else { return nil }; return EditorTextElement(id: id, text: text, startS: start, endS: end, role: string(o["role"]), raw: o) } }
    static func decodeCaptions(_ rows: [JSONValue]) -> [EditorCaptionCue] { rows.compactMap { value in guard let o = object(value), let id = string(o["id"]), let start = number(o["start_s"]), let end = number(o["end_s"]) else { return nil }; return EditorCaptionCue(id: id, startS: start, endS: end, text: string(o["text"]) ?? "", raw: o) } }
    static func decodeEffects(_ rows: [JSONValue]) -> [EditorTimedEffect] {
        rows.compactMap { value in
            guard let o = object(value), let id = string(o["id"]) else { return nil }
            let pointS = number(o["at_s"])
            if let pointS {
                // SFX placements are anchored at `at_s`; trim metadata describes
                // the source slice, not a second timeline window. Give the
                // timeline a visible (but conservative) display span while
                // retaining the canonical point and trim fields for encoding.
                let trimStart = number(o["trim_start_s"]) ?? 0
                let trimEnd = number(o["trim_end_s"])
                let duration = number(o["duration_s"])
                let sourceSpan = (trimEnd ?? duration ?? 0.1) - trimStart
                let displayEnd = pointS + max(0.1, sourceSpan)
                return EditorTimedEffect(id: id, startS: pointS, endS: displayEnd, pointS: pointS, kind: string(o["kind"] ?? o["type"]), raw: o)
            }
            guard let start = number(o["start_s"] ?? o["start"] ?? o["at_s"]),
                  let end = number(o["end_s"] ?? o["end"] ?? o["at_s"]) else { return nil }
            return EditorTimedEffect(id: id, startS: start, endS: end, pointS: pointS, kind: string(o["kind"] ?? o["type"]), raw: o)
        }
    }
    static func decodeVisual(_ rows: [JSONValue]) -> [EditorVisualBlock] { rows.compactMap { value in guard let o = object(value), let id = string(o["id"]), let kind = string(o["kind"]), let start = number(o["start_s"]), let end = number(o["end_s"]) else { return nil }; return EditorVisualBlock(id: id, kind: kind, startS: start, endS: end, raw: o) } }
    static func decodeMotion(_ rows: [JSONValue]) -> [EditorMotionScene] {
        rows.compactMap { value in
            guard let o = object(value), let id = string(o["id"]) else { return nil }
            let start = number(o["start_s"]) ?? number(o["start_frame"]).map { $0 / NativeEditorMotionCodec.fps }
            let end = number(o["end_s"]) ?? number(o["end_frame_exclusive"]).map { $0 / NativeEditorMotionCodec.fps }
            guard let start, let end else { return nil }
            return EditorMotionScene(id: id, startS: start, endS: end, preset: string(o["preset_id"] ?? o["preset"]), runtimeHash: string(o["runtime_hash"] ?? o["runtime_compatibility_hash"]), raw: o)
        }
    }
    static func decodeCamera(_ rows: [JSONValue]) -> [EditorCameraEffect] { rows.compactMap { value in guard let o = object(value), let id = string(o["id"]), let start = number(o["start_s"]), let end = number(o["end_s"]) else { return nil }; return EditorCameraEffect(id: id, startS: start, endS: end, effect: string(o["effect"] ?? o["type"]), raw: o) } }
    static func decodeMusic(_ value: [String: JSONValue]?, fallbackTrack: String?, fallbackWindow: [String: JSONValue]?) -> EditorMusic? {
        var value = value
        if value == nil, let fallbackTrack {
            var canonical = fallbackWindow ?? [:]
            canonical["track_id"] = .string(fallbackTrack)
            value = canonical
        }
        guard let value, let track = string(value["track_id"]) else { return nil }
        return EditorMusic(trackID: track, startS: number(value["start_s"]) ?? 0, alignment: string(value["alignment"]), raw: value)
    }
    static func decodeBackground(_ value: [String: JSONValue]?) -> EditorBackgroundMusic? { guard let value else { return nil }; return EditorBackgroundMusic(trackID: string(value["track_id"]), enabled: value["enabled"]?.boolValue ?? true, startS: number(value["start_s"]), endS: number(value["end_s"]), gainDB: number(value["gain_db"]), muted: value["muted"]?.boolValue ?? false, raw: value) }
    static func decodeCapabilities(_ value: [String: JSONValue]?) -> [String: EditorCapability] {
        guard let value else { return [:] }
        var result: [String: EditorCapability] = [:]

        func visit(_ objectValue: [String: JSONValue], prefix: String) {
            // Operation capabilities are nested (`clips.trim`,
            // `music_operations.window`, `lanes.text`). A capability object
            // with `editable`/`reason` is a leaf; preserve its reason instead
            // of collapsing the whole parent to an unusable false value.
            let isLeaf = objectValue["editable"] != nil
                || (objectValue["reason"] != nil
                    && objectValue.keys.allSatisfy { $0 == "editable" || $0 == "reason" })
            if isLeaf {
                result[prefix] = EditorCapability(
                    editable: objectValue["editable"]?.boolValue ?? false,
                    reason: objectValue["reason"]?.stringValue
                )
                return
            }
            for (key, child) in objectValue {
                let childKey = prefix.isEmpty ? key : "\(prefix).\(key)"
                if let childObject = object(child) {
                    visit(childObject, prefix: childKey)
                } else if let bool = child.boolValue {
                    result[childKey] = EditorCapability(editable: bool)
                }
            }
        }

        visit(value, prefix: "")
        // Legacy/current responses put reason text beside the boolean (for
        // example `sfx` + `sfx_reason`). Merge it into the same leaf entry.
        for (key, child) in value {
            guard key.hasSuffix("_reason"), let reason = child.stringValue else { continue }
            let capabilityKey = String(key.dropLast("_reason".count))
            if let existing = result[capabilityKey] {
                result[capabilityKey] = EditorCapability(editable: existing.editable, reason: reason)
            } else {
                result[capabilityKey] = EditorCapability(editable: false, reason: reason)
            }
        }
        return result
    }
    static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { return value }; return nil }
    static func array(_ value: JSONValue?) -> [JSONValue]? { if case let .array(value) = value { return value }; return nil }
    static func string(_ value: JSONValue?) -> String? { value?.stringValue }
    static func number(_ value: JSONValue?) -> Double? { if case let .number(value) = value { return value }; return nil }
    static func integer(_ value: JSONValue?) -> Int? { guard let value = number(value), value.rounded() == value else { return nil }; return Int(value) }
    static func presence(_ object: [String: JSONValue], key: String) -> EditorPresence { guard let value = object[key] else { return .absent }; if case .null = value { return .null }; return .value }
}

private extension JSONValue {
    var boolValue: Bool? { if case let .bool(value) = self { return value }; return nil }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
